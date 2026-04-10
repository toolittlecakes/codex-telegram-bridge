from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .bridge import BridgeApp
from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG, ensure_parent_dirs, load_config
from .diagnostics import collect_desktop_snapshot, collect_doctor_report, dump_json
from .desktop_client import DesktopClientError
from .logging_setup import configure_logging
from .state import BridgeState
from .telegram_api import TelegramApiError


class SingleInstanceError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-telegram-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the Telegram bridge")
    run_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Run machine-readable bridge diagnostics")
    doctor_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})",
    )
    doctor_parser.add_argument(
        "--out",
        type=Path,
        help="Optional path to write the JSON report",
    )

    snapshot_parser = subparsers.add_parser("desktop-snapshot", help="Print a machine-readable Codex Desktop snapshot")
    snapshot_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})",
    )
    snapshot_parser.add_argument(
        "--out",
        type=Path,
        help="Optional path to write the JSON snapshot",
    )
    snapshot_parser.add_argument(
        "--screenshot",
        type=Path,
        help="Optional path to save a PNG screenshot from Codex Desktop",
    )

    init_parser = subparsers.add_parser("init-config", help="Print an example config to stdout")
    init_parser.add_argument(
        "--path",
        type=Path,
        help="Optional path to write instead of printing to stdout",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        if args.path:
            args.path.parent.mkdir(parents=True, exist_ok=True)
            args.path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
            print(f"Wrote example config to {args.path}")
        else:
            print(EXAMPLE_CONFIG)
        return

    if args.command == "run":
        try:
            asyncio.run(_run(args.config))
        except (ValueError, TelegramApiError, DesktopClientError, SingleInstanceError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        except FileNotFoundError as exc:
            missing = exc.filename or "required command"
            print(f"Error: failed to start {missing}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    if args.command == "doctor":
        raise SystemExit(asyncio.run(_doctor(args.config, args.out)))

    if args.command == "desktop-snapshot":
        raise SystemExit(asyncio.run(_desktop_snapshot(args.config, args.out, args.screenshot)))

    parser.error(f"Unknown command: {args.command}")


async def _run(config_path: Path) -> None:
    config = load_config(config_path)
    ensure_parent_dirs(config)
    logging_result = configure_logging(config.bridge)
    logger = logging.getLogger(__name__)
    logger.info(
        "Logging configured (bridge_log=%s, protocol_log=%s, console=%s)",
        logging_result.log_path,
        logging_result.protocol_log_path,
        config.bridge.console_log,
    )

    state = BridgeState.load(config.bridge.state_path)
    state.reset_ephemeral_runtime_state()
    app = BridgeApp(config, state)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(app.stop()))

    with _hold_single_instance_lock(config.bridge.state_path):
        await app.run()


async def _doctor(config_path: Path, out_path: Path | None) -> int:
    payload: dict[str, object]
    try:
        config = load_config(config_path)
        ensure_parent_dirs(config)
        state = BridgeState.load(config.bridge.state_path)
        payload = await collect_doctor_report(config, state)
    except Exception as exc:
        payload = {
            "ok": False,
            "stage": "doctor",
            "config_path": str(config_path),
            "error": str(exc),
        }
    _emit_json_payload(payload, out_path)
    return 0 if payload.get("ok") else 1


async def _desktop_snapshot(
    config_path: Path,
    out_path: Path | None,
    screenshot_path: Path | None,
) -> int:
    payload: dict[str, object]
    try:
        config = load_config(config_path)
        payload = await collect_desktop_snapshot(config, screenshot_path=screenshot_path)
    except Exception as exc:
        payload = {
            "ok": False,
            "stage": "desktop-snapshot",
            "config_path": str(config_path),
            "error": str(exc),
        }
    _emit_json_payload(payload, out_path)
    return 0 if payload.get("ok") else 1


def _emit_json_payload(payload: dict[str, object], out_path: Path | None) -> None:
    if out_path is not None:
        dump_json(out_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


@contextlib.contextmanager
def _hold_single_instance_lock(state_path: Path):
    lock_path = state_path.with_suffix(".run.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip()
            detail = f" Another instance is holding {lock_path}."
            if holder:
                detail = f" Another instance is holding {lock_path}: {holder}."
            raise SingleInstanceError(f"Another codex-telegram-bridge instance is already running.{detail}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":  # pragma: no cover
    main()
