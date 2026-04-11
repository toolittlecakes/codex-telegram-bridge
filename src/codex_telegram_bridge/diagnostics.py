from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .desktop_client import (
    CodexDesktopClient,
    DesktopConversation,
    DesktopConversationSummary,
    DesktopProject,
    DesktopRequest,
    DesktopSessionInfo,
    DesktopTurn,
)
from .state import BridgeState
from .telegram_api import TelegramBotApi


def build_desktop_client(config: AppConfig) -> CodexDesktopClient:
    return CodexDesktopClient(
        app_path=config.desktop.app_path,
        remote_debugging_port=config.desktop.remote_debugging_port,
        user_data_dir=config.desktop.user_data_dir,
        launch_timeout_seconds=config.desktop.launch_timeout_seconds,
        send_ack_timeout_seconds=config.desktop.send_ack_timeout_seconds,
        poll_interval_seconds=config.desktop.poll_interval_seconds,
    )


def build_telegram_api(config: AppConfig) -> TelegramBotApi:
    return TelegramBotApi(
        bot_token=config.telegram.bot_token,
        base_url=config.telegram.api_base_url,
    )


def collect_bridge_runtime_report(config: AppConfig, state: BridgeState) -> dict[str, Any]:
    lock_path = config.bridge.state_path.with_suffix(".run.lock")
    lock_report = _probe_lock(lock_path)
    return {
        "ok": True,
        "state_path": str(config.bridge.state_path),
        "state_exists": config.bridge.state_path.exists(),
        "thread_count": len(state.threads),
        "primary_chat_id": state.primary_chat_id,
        "message_binding_count": len(state.message_bindings),
        "approval_cleanup_message_count": len(state.approval_cleanup_messages),
        "lock": lock_report,
    }


async def collect_doctor_report(config: AppConfig, state: BridgeState) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": True,
        "timestamp": _utc_now(),
        "bridge": collect_bridge_runtime_report(config, state),
        "telegram": {"ok": False},
        "desktop": {"ok": False},
    }

    telegram = build_telegram_api(config)
    try:
        bot = await telegram.get_me()
        report["telegram"] = {
            "ok": True,
            "bot_id": bot.get("id"),
            "username": bot.get("username"),
            "is_bot": bot.get("is_bot"),
        }
    except Exception as exc:
        report["telegram"] = {
            "ok": False,
            "error": str(exc),
        }
        report["ok"] = False
    finally:
        with contextlib.suppress(Exception):
            await telegram.close()

    desktop = build_desktop_client(config)
    try:
        session = await desktop.start()
        await desktop.wait_until_task_index_ready()
        snapshot = await desktop.snapshot()
        report["desktop"] = {
            "ok": True,
            "session": _serialize_session(session),
            "task_index_ready": True,
            "current_thread_id": snapshot["current_thread_id"],
            "project_count": len(snapshot["projects"]),
            "thread_count": len(snapshot["threads"]),
        }
    except Exception as exc:
        report["desktop"] = {
            "ok": False,
            "task_index_ready": False,
            "error": str(exc),
        }
        report["ok"] = False
    finally:
        with contextlib.suppress(Exception):
            await desktop.close()

    return report


async def collect_desktop_snapshot(
    config: AppConfig,
    *,
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    desktop = build_desktop_client(config)
    try:
        session = await desktop.start()
        await desktop.wait_until_task_index_ready()
        snapshot = await desktop.snapshot()
        payload: dict[str, Any] = {
            "ok": True,
            "timestamp": _utc_now(),
            "session": _serialize_session(session),
            "current_thread_id": snapshot["current_thread_id"],
            "composer": dict(snapshot["composer"]),
            "visible_buttons": list(snapshot["visible_buttons"]),
            "projects": [_serialize_project(project) for project in snapshot["projects"]],
            "threads": [_serialize_thread_summary(thread) for thread in snapshot["threads"]],
            "current_thread": _serialize_conversation(snapshot["current_thread"]),
        }
        if screenshot_path is not None:
            saved = await desktop.capture_screenshot(screenshot_path)
            payload["screenshot_path"] = str(saved)
        return payload
    finally:
        with contextlib.suppress(Exception):
            await desktop.close()


def _serialize_session(session: DesktopSessionInfo) -> dict[str, Any]:
    return {
        "debugger_url": session.debugger_url,
        "page_url": session.page_url,
        "page_title": session.page_title,
    }


def _serialize_project(project: DesktopProject) -> dict[str, Any]:
    return {
        "label": project.label,
        "path": project.path,
    }


def _serialize_thread_summary(thread: DesktopConversationSummary) -> dict[str, Any]:
    return {
        "thread_id": thread.thread_id,
        "title": thread.title,
        "current": thread.current,
        "cwd": thread.cwd,
        "project_label": thread.project_label,
        "project_path": thread.project_path,
        "updated_at": thread.updated_at.isoformat() if thread.updated_at is not None else None,
    }


def _serialize_turn(turn: DesktopTurn) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "status": turn.status,
        "items": turn.items,
        "error": turn.error,
    }


def _serialize_request(request: DesktopRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "kind": request.kind,
        "raw": request.raw,
    }


def _serialize_conversation(conversation: DesktopConversation | None) -> dict[str, Any] | None:
    if conversation is None:
        return None
    return {
        "thread_id": conversation.thread_id,
        "title": conversation.title,
        "cwd": conversation.cwd,
        "host_id": conversation.host_id,
        "source": conversation.source,
        "runtime_status": conversation.runtime_status,
        "preview": conversation.preview,
        "turns": [_serialize_turn(turn) for turn in conversation.turns],
        "requests": [_serialize_request(request) for request in conversation.requests],
    }


def _probe_lock(lock_path: Path) -> dict[str, Any]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    holder: str | None = None
    available = False
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            available = True
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or None
        finally:
            if available:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {
        "path": str(lock_path),
        "available": available,
        "holder": holder,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


__all__ = [
    "build_desktop_client",
    "build_telegram_api",
    "collect_bridge_runtime_report",
    "collect_desktop_snapshot",
    "collect_doctor_report",
    "dump_json",
]
