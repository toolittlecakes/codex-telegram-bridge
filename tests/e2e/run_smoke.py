# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.28,<1.0",
#   "telegramify-markdown>=1.1.2,<2.0",
#   "websockets>=15.0,<16.0",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_telegram_bridge.config import ensure_parent_dirs, load_config
from codex_telegram_bridge.diagnostics import collect_desktop_snapshot, collect_doctor_report, dump_json
from codex_telegram_bridge.state import BridgeState

from assertions import assert_approval, assert_attach, assert_detach, assert_new_thread, assert_queue, assert_reply, assert_startup
from scenarios import render_scenario_plan

DEFAULT_ARTIFACTS_DIR = ROOT / ".e2e" / "artifacts" / "latest"
RUN_MANIFEST_PATH = "run.json"
STATE_COPY_PATH = "state.json"
DOCTOR_PATH = "doctor.json"
SNAPSHOT_PATH = "desktop_snapshot.json"
PLAN_PATH = "scenario_plan.json"
DESKTOP_SCREENSHOT_PATH = "desktop.png"
ASSERTION_PATH = "last_assertion.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_smoke.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start the bridge in the background and wait until ready")
    _add_common_args(start_parser)
    start_parser.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for bridge readiness")

    stop_parser = subparsers.add_parser("stop", help="Stop the background bridge process")
    stop_parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    stop_parser.add_argument("--timeout", type=float, default=10.0, help="Seconds to wait before SIGKILL")

    snapshot_parser = subparsers.add_parser("snapshot", help="Capture doctor, desktop snapshot, and state artifacts")
    _add_common_args(snapshot_parser)

    plan_parser = subparsers.add_parser("plan", help="Write the deterministic scenario plan for this run")
    _add_common_args(plan_parser)
    plan_parser.add_argument("--run-id", help="Optional run id override for scenario prompts")

    assert_parser = subparsers.add_parser("assert", help="Run a live assertion against the current bridge/Codex state")
    _add_common_args(assert_parser)
    assert_parser.add_argument(
        "scenario",
        choices=("startup", "new-thread", "reply", "queue", "attach", "detach", "approval"),
    )
    assert_parser.add_argument("--thread-id", help="Thread id required by attach/detach/reply/queue/approval assertions")
    assert_parser.add_argument("--text", help="Expected user text for new-thread/reply/queue assertions")

    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=ROOT / "config.e2e.example.toml")
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "start":
        raise SystemExit(asyncio.run(cmd_start(args)))
    if args.command == "stop":
        raise SystemExit(cmd_stop(args))
    if args.command == "snapshot":
        raise SystemExit(asyncio.run(cmd_snapshot(args)))
    if args.command == "plan":
        raise SystemExit(asyncio.run(cmd_plan(args)))
    if args.command == "assert":
        raise SystemExit(asyncio.run(cmd_assert(args)))
    parser.error(f"Unknown command: {args.command}")


async def cmd_start(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_parent_dirs(config)
    artifacts_dir = args.artifacts_dir.resolve()
    manifest_path = artifacts_dir / RUN_MANIFEST_PATH
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    manifest = _read_manifest(manifest_path)
    if manifest and _is_process_running(int(manifest["pid"])):
        return _print_payload(
            {
                "ok": False,
                "error": f"bridge process {manifest['pid']} is already running for {artifacts_dir}",
                "manifest_path": str(manifest_path),
            }
        )

    log_path = artifacts_dir / "bridge.log"
    run_id = _build_run_id()
    process = _spawn_bridge_process(args.config, log_path)
    manifest = {
        "pid": process.pid,
        "config_path": str(Path(args.config).resolve()),
        "artifacts_dir": str(artifacts_dir),
        "log_path": str(log_path),
        "run_id": run_id,
        "started_at": _utc_now(),
    }
    dump_json(manifest_path, manifest)

    ready_report = await _wait_until_ready(config, timeout_seconds=args.timeout, process=process)
    if not ready_report.get("ok"):
        _terminate_process(process, timeout_seconds=5.0)
        payload = {
            "ok": False,
            "error": "bridge did not become ready",
            "doctor": ready_report,
            "manifest_path": str(manifest_path),
            "log_path": str(log_path),
        }
        dump_json(artifacts_dir / DOCTOR_PATH, ready_report)
        return _print_payload(payload)

    await _capture_artifacts(config, artifacts_dir, run_id=run_id, doctor_report=ready_report)
    return _print_payload(
        {
            "ok": True,
            "pid": process.pid,
            "artifacts_dir": str(artifacts_dir),
            "manifest_path": str(manifest_path),
            "doctor_path": str(artifacts_dir / DOCTOR_PATH),
            "snapshot_path": str(artifacts_dir / SNAPSHOT_PATH),
            "plan_path": str(artifacts_dir / PLAN_PATH),
        }
    )


def cmd_stop(args: argparse.Namespace) -> int:
    artifacts_dir = args.artifacts_dir.resolve()
    manifest_path = artifacts_dir / RUN_MANIFEST_PATH
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        return _print_payload(
            {
                "ok": True,
                "stopped": False,
                "reason": f"no manifest at {manifest_path}",
            }
        )

    pid = int(manifest["pid"])
    if not _is_process_running(pid):
        manifest_path.unlink(missing_ok=True)
        return _print_payload(
            {
                "ok": True,
                "stopped": False,
                "reason": f"process {pid} is not running",
            }
        )

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not _is_process_running(pid):
            manifest_path.unlink(missing_ok=True)
            return _print_payload({"ok": True, "stopped": True, "pid": pid})
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    manifest_path.unlink(missing_ok=True)
    return _print_payload({"ok": True, "stopped": True, "pid": pid, "forced": True})


async def cmd_snapshot(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_parent_dirs(config)
    artifacts_dir = args.artifacts_dir.resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(artifacts_dir / RUN_MANIFEST_PATH) or {}
    run_id = str(manifest.get("run_id") or _build_run_id())
    paths = await _capture_artifacts(config, artifacts_dir, run_id=run_id)
    return _print_payload({"ok": True, **paths})


async def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_parent_dirs(config)
    artifacts_dir = args.artifacts_dir.resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    state = BridgeState.load(config.bridge.state_path)
    doctor = await collect_doctor_report(config, state)
    manifest = _read_manifest(artifacts_dir / RUN_MANIFEST_PATH)
    run_id = args.run_id or (manifest.get("run_id") if manifest is not None else None) or _build_run_id()
    plan = render_scenario_plan(
        run_id=str(run_id),
        bot_username=(doctor.get("telegram") or {}).get("username"),
        chat_id=config.telegram.primary_chat_id or state.primary_chat_id,
    )
    plan_path = artifacts_dir / PLAN_PATH
    dump_json(plan_path, plan)
    return _print_payload({"ok": True, "plan_path": str(plan_path), "plan": plan})


async def cmd_assert(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = BridgeState.load(config.bridge.state_path)
    try:
        if args.scenario == "startup":
            details = await assert_startup(config, state)
        elif args.scenario == "new-thread":
            _require_arg(args.text, "--text")
            details = await assert_new_thread(config, state, text=args.text)
        elif args.scenario == "reply":
            _require_arg(args.thread_id, "--thread-id")
            _require_arg(args.text, "--text")
            details = await assert_reply(config, state, thread_id=args.thread_id, text=args.text)
        elif args.scenario == "queue":
            _require_arg(args.thread_id, "--thread-id")
            _require_arg(args.text, "--text")
            details = await assert_queue(config, state, thread_id=args.thread_id, text=args.text)
        elif args.scenario == "attach":
            _require_arg(args.thread_id, "--thread-id")
            details = assert_attach(state, thread_id=args.thread_id)
        elif args.scenario == "detach":
            _require_arg(args.thread_id, "--thread-id")
            details = assert_detach(state, thread_id=args.thread_id)
        elif args.scenario == "approval":
            _require_arg(args.thread_id, "--thread-id")
            details = await assert_approval(config, thread_id=args.thread_id)
        else:
            raise AssertionError(f"unknown scenario {args.scenario}")
    except Exception as exc:
        payload = {
            "ok": False,
            "scenario": args.scenario,
            "error": str(exc),
        }
        dump_json(args.artifacts_dir.resolve() / ASSERTION_PATH, payload)
        return _print_payload(payload)

    payload = {
        "ok": True,
        "scenario": args.scenario,
        "details": details,
    }
    dump_json(args.artifacts_dir.resolve() / ASSERTION_PATH, payload)
    return _print_payload(payload)


async def _wait_until_ready(
    config,
    *,
    timeout_seconds: float,
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_report: dict[str, Any] = {
        "ok": False,
        "error": "bridge not checked yet",
    }
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {
                "ok": False,
                "error": f"bridge exited with code {process.returncode}",
            }
        state = BridgeState.load(config.bridge.state_path)
        last_report = await collect_doctor_report(config, state)
        if (last_report.get("telegram") or {}).get("ok") and (last_report.get("desktop") or {}).get("ok"):
            last_report["ok"] = True
            return last_report
        await asyncio.sleep(1.0)
    return last_report


async def _capture_artifacts(
    config,
    artifacts_dir: Path,
    *,
    run_id: str,
    doctor_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    state = BridgeState.load(config.bridge.state_path)
    doctor = doctor_report or await collect_doctor_report(config, state)
    doctor_path = dump_json(artifacts_dir / DOCTOR_PATH, doctor)
    snapshot = await collect_desktop_snapshot(config, screenshot_path=artifacts_dir / DESKTOP_SCREENSHOT_PATH)
    snapshot_path = dump_json(artifacts_dir / SNAPSHOT_PATH, snapshot)
    if config.bridge.state_path.exists():
        shutil.copy2(config.bridge.state_path, artifacts_dir / STATE_COPY_PATH)
    plan = render_scenario_plan(
        run_id=run_id,
        bot_username=(doctor.get("telegram") or {}).get("username"),
        chat_id=config.telegram.primary_chat_id or state.primary_chat_id,
    )
    plan_path = dump_json(artifacts_dir / PLAN_PATH, plan)
    return {
        "doctor_path": str(doctor_path),
        "snapshot_path": str(snapshot_path),
        "plan_path": str(plan_path),
        "state_path": str(artifacts_dir / STATE_COPY_PATH),
        "screenshot_path": str(artifacts_dir / DESKTOP_SCREENSHOT_PATH),
    }


def _spawn_bridge_process(config_path: Path, log_path: Path) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    src_path = str(SRC)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    command = [sys.executable, "-m", "codex_telegram_bridge", "run", "--config", str(Path(config_path).resolve())]
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
        )
    return process


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _terminate_process(process: subprocess.Popen[str], *, timeout_seconds: float) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)
    process.kill()


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _require_arg(value: str | None, flag: str) -> None:
    if value:
        return
    raise AssertionError(f"{flag} is required for this scenario")


def _build_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _print_payload(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    main()
