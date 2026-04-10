from __future__ import annotations

import contextlib
from typing import Any

from codex_telegram_bridge.config import AppConfig
from codex_telegram_bridge.desktop_client import DesktopConversation
from codex_telegram_bridge.diagnostics import build_desktop_client, collect_doctor_report
from codex_telegram_bridge.state import BridgeState, ThreadState


async def assert_startup(config: AppConfig, state: BridgeState) -> dict[str, Any]:
    report = await collect_doctor_report(config, state)
    if not report.get("ok"):
        raise AssertionError(f"startup doctor failed: {report}")
    desktop = report.get("desktop") or {}
    if int(desktop.get("project_count") or 0) <= 0:
        raise AssertionError("startup doctor found no visible Codex projects")
    return {
        "doctor": report,
    }


async def assert_new_thread(config: AppConfig, state: BridgeState, *, text: str) -> dict[str, Any]:
    conversation = await _find_thread_with_user_text(config, text)
    if conversation is None:
        raise AssertionError(f"no visible Codex thread contains user text {text!r}")
    thread_state = state.threads.get(conversation.thread_id)
    if thread_state is None:
        raise AssertionError(f"thread {conversation.thread_id} exists in Codex but is not attached in bridge state")
    return {
        "thread_id": conversation.thread_id,
        "preview": conversation.preview,
    }


async def assert_reply(
    config: AppConfig,
    state: BridgeState,
    *,
    thread_id: str,
    text: str,
) -> dict[str, Any]:
    thread_state = _require_thread_state(state, thread_id)
    conversation = await _read_thread(config, thread_id)
    if conversation is None:
        raise AssertionError(f"thread {thread_id} is not visible in Codex Desktop")
    if not _conversation_has_user_text(conversation, text):
        raise AssertionError(f"thread {thread_id} does not contain user text {text!r}")
    return {
        "thread_id": thread_id,
        "pending_message_ids": list(thread_state.pending_message_ids),
        "current_turn_id": thread_state.current_turn_id,
    }


async def assert_queue(
    config: AppConfig,
    state: BridgeState,
    *,
    thread_id: str,
    text: str,
) -> dict[str, Any]:
    thread_state = _require_thread_state(state, thread_id)
    if any(item.text == text for item in thread_state.queued_inputs):
        return {
            "thread_id": thread_id,
            "queue_state": "queued",
            "queued_inputs": [item.text for item in thread_state.queued_inputs],
        }

    conversation = await _read_thread(config, thread_id)
    if conversation is None:
        raise AssertionError(f"thread {thread_id} is not visible in Codex Desktop")
    if not _conversation_has_user_text(conversation, text):
        raise AssertionError(
            f"thread {thread_id} neither keeps queued input {text!r} nor shows it in Codex conversation history"
        )
    return {
        "thread_id": thread_id,
        "queue_state": "replayed",
        "current_turn_id": thread_state.current_turn_id,
    }


def assert_attach(state: BridgeState, *, thread_id: str) -> dict[str, Any]:
    thread_state = _require_thread_state(state, thread_id)
    if thread_state.primary_chat_id is None:
        raise AssertionError(f"thread {thread_id} exists in state but has no bound Telegram chat")
    return {
        "thread_id": thread_id,
        "primary_chat_id": thread_state.primary_chat_id,
        "last_chain_message_id": thread_state.last_chain_message_id,
    }


def assert_detach(state: BridgeState, *, thread_id: str) -> dict[str, Any]:
    if thread_id in state.threads:
        raise AssertionError(f"thread {thread_id} is still attached in bridge state")
    return {
        "thread_id": thread_id,
        "detached": True,
    }


async def assert_approval(config: AppConfig, *, thread_id: str) -> dict[str, Any]:
    conversation = await _read_thread(config, thread_id)
    if conversation is None:
        raise AssertionError(f"thread {thread_id} is not visible in Codex Desktop")
    if not conversation.requests:
        raise AssertionError(f"thread {thread_id} has no active desktop approval requests")
    return {
        "thread_id": thread_id,
        "request_ids": [request.request_id for request in conversation.requests],
        "request_kinds": [request.kind for request in conversation.requests],
    }


async def _find_thread_with_user_text(config: AppConfig, text: str) -> DesktopConversation | None:
    desktop = build_desktop_client(config)
    try:
        await desktop.start()
        await desktop.wait_until_task_index_ready()
        for summary in await desktop.list_threads():
            conversation = await desktop.read_thread(summary.thread_id)
            if conversation is None:
                continue
            if _conversation_has_user_text(conversation, text):
                return conversation
    finally:
        with contextlib.suppress(Exception):
            await desktop.close()
    return None


async def _read_thread(config: AppConfig, thread_id: str) -> DesktopConversation | None:
    desktop = build_desktop_client(config)
    try:
        await desktop.start()
        await desktop.wait_until_task_index_ready()
        return await desktop.read_thread(thread_id)
    finally:
        with contextlib.suppress(Exception):
            await desktop.close()


def _require_thread_state(state: BridgeState, thread_id: str) -> ThreadState:
    thread_state = state.threads.get(thread_id)
    if thread_state is None:
        raise AssertionError(f"thread {thread_id} is not attached in bridge state")
    return thread_state


def _conversation_has_user_text(conversation: DesktopConversation, expected_text: str) -> bool:
    normalized_expected = expected_text.strip()
    if not normalized_expected:
        return False
    for turn in conversation.turns:
        for item in turn.items:
            if item.get("type") != "userMessage":
                continue
            content = item.get("content") or []
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip() == normalized_expected:
                    return True
    return False


__all__ = [
    "assert_approval",
    "assert_attach",
    "assert_detach",
    "assert_new_thread",
    "assert_queue",
    "assert_reply",
    "assert_startup",
]
