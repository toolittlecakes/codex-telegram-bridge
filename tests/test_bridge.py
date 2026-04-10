from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import codex_telegram_bridge.bridge as bridge_module
from codex_telegram_bridge.bridge import BridgeApp, PendingApproval
from codex_telegram_bridge.cli import SingleInstanceError, _hold_single_instance_lock
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig, load_config
from codex_telegram_bridge.desktop_client import (
    DesktopClientError,
    DesktopDraftConflictError,
    CodexDesktopClient,
    DesktopConversation,
    DesktopConversationSummary,
    DesktopProject,
    DesktopRequest,
    DesktopSessionInfo,
    DesktopTurn,
    _COMPOSER_STATE_JS,
    _CURRENT_THREAD_ID_JS,
    _FOCUS_COMPOSER_JS,
    _THREAD_HEADER_TITLE_JS,
    _click_send_button_js,
    _project_button_center_js,
)
from codex_telegram_bridge.formatting import render_markdown_chunks
from codex_telegram_bridge.state import ApprovalCleanupMessage, BridgeState, QueuedInput
from codex_telegram_bridge.telegram_api import TelegramApiError, TelegramBotApi


def make_turn(
    turn_id: str,
    status: str,
    *,
    items: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> DesktopTurn:
    payload = {
        "turnId": turn_id,
        "status": status,
        "items": items or [],
        "error": error,
    }
    return DesktopTurn(
        turn_id=turn_id,
        status=status,
        items=list(items or []),
        error=error,
        raw=payload,
    )


def make_conversation(
    thread_id: str,
    *,
    title: str | None = None,
    turns: list[DesktopTurn] | None = None,
    requests: list[DesktopRequest] | None = None,
) -> DesktopConversation:
    return DesktopConversation(
        thread_id=thread_id,
        title=title,
        cwd="/repo",
        host_id="local",
        source="desktop",
        turns=list(turns or []),
        requests=list(requests or []),
        runtime_status=None,
        raw={"id": thread_id},
    )


@dataclass
class FakeDesktop:
    threads: dict[str, DesktopConversation] = field(default_factory=dict)
    thread_summaries: list[DesktopConversationSummary] = field(default_factory=list)
    projects: list[DesktopProject] = field(default_factory=lambda: [DesktopProject(label="repo", path="/repo")])
    new_thread_results: dict[tuple[str, str], DesktopConversation] = field(default_factory=dict)
    send_results: dict[tuple[str, str], DesktopConversation] = field(default_factory=dict)
    activated_threads: list[str] = field(default_factory=list)
    started_inputs: list[tuple[str, str]] = field(default_factory=list)
    sent_inputs: list[tuple[str, str]] = field(default_factory=list)
    approval_clicks: list[tuple[str, bool]] = field(default_factory=list)
    approval_click_labels: list[list[str] | None] = field(default_factory=list)
    start_replace_flags: list[bool] = field(default_factory=list)
    fail_activate: set[str] = field(default_factory=set)
    fail_start_messages: set[tuple[str, str]] = field(default_factory=set)
    fail_send: set[tuple[str, str]] = field(default_factory=set)
    fail_approval_threads: set[str] = field(default_factory=set)
    draft_conflicts: dict[tuple[str, str], str] = field(default_factory=dict)
    start_calls: int = 0

    async def start(self) -> DesktopSessionInfo:  # pragma: no cover - not used in tests
        self.start_calls += 1
        return DesktopSessionInfo(
            debugger_url="ws://127.0.0.1:9229/devtools/page/1",
            page_url="app://-/index.html",
            page_title="Codex",
        )

    async def wait_until_task_index_ready(self) -> None:  # pragma: no cover - not used in tests
        return None

    async def close(self) -> None:  # pragma: no cover - not used in tests
        return None

    async def list_threads(self) -> list[DesktopConversationSummary]:
        if self.thread_summaries:
            return copy.deepcopy(self.thread_summaries)
        return [
            DesktopConversationSummary(
                thread_id=thread.thread_id,
                title=thread.title,
                current=False,
                cwd=thread.cwd,
            )
            for thread in self.threads.values()
        ]

    async def list_projects(self) -> list[DesktopProject]:
        return copy.deepcopy(self.projects)

    async def read_thread(self, thread_id: str) -> DesktopConversation | None:
        conversation = self.threads.get(thread_id)
        return copy.deepcopy(conversation) if conversation is not None else None

    async def start_new_thread(
        self,
        project_path: str,
        text: str,
        *,
        replace_existing_draft: bool = False,
    ) -> DesktopConversation:
        key = (project_path, text)
        self.start_replace_flags.append(replace_existing_draft)
        if key in self.draft_conflicts and not replace_existing_draft:
            raise DesktopDraftConflictError(context="a new thread", draft_text=self.draft_conflicts[key])
        if key in self.fail_start_messages:
            raise DesktopClientError("start failed")
        self.started_inputs.append(key)
        conversation = self.new_thread_results.get(key)
        if conversation is None:
            raise DesktopClientError("no desktop thread configured")
        conversation = copy.deepcopy(conversation)
        self.threads[conversation.thread_id] = copy.deepcopy(conversation)
        return conversation

    async def activate_thread(self, thread_id: str) -> DesktopConversation:
        if thread_id in self.fail_activate:
            raise DesktopClientError("missing thread")
        conversation = self.threads.get(thread_id)
        if conversation is None:
            raise DesktopClientError("missing thread")
        self.activated_threads.append(thread_id)
        return copy.deepcopy(conversation)

    async def send_message(self, thread_id: str, text: str) -> DesktopConversation:
        key = (thread_id, text)
        if key in self.fail_send:
            raise DesktopClientError("send failed")
        self.sent_inputs.append(key)
        conversation = self.send_results.get(key) or self.threads.get(thread_id)
        if conversation is None:
            raise DesktopClientError("missing thread")
        cloned = copy.deepcopy(conversation)
        self.threads[thread_id] = copy.deepcopy(cloned)
        return cloned

    async def click_approval_action(
        self,
        thread_id: str,
        *,
        approve: bool,
        labels: list[str] | None = None,
    ) -> None:
        if thread_id in self.fail_approval_threads:
            raise DesktopClientError("approval button missing")
        self.approval_clicks.append((thread_id, approve))
        self.approval_click_labels.append(labels)


@dataclass
class FakeTelegram:
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[tuple[int, int, str | None]] = field(default_factory=list)
    deleted_messages: list[tuple[int, int]] = field(default_factory=list)
    callback_answers: list[tuple[str, str | None]] = field(default_factory=list)
    fail_send_calls: set[int] = field(default_factory=set)
    send_call_count: int = 0

    async def close(self) -> None:  # pragma: no cover - not used in tests
        return None

    async def get_updates(self, **kwargs):  # pragma: no cover - not used in tests
        return []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ):
        self.send_call_count += 1
        if self.send_call_count in self.fail_send_calls:
            raise TelegramApiError("send failed")
        message_id = 10_000 + len(self.sent_messages) + 1
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "entities": entities,
            "inline_keyboard": inline_keyboard,
            "disable_notification": disable_notification,
        }
        self.sent_messages.append(payload)
        return type("Sent", (), {"chat_id": chat_id, "message_id": message_id, "raw": payload})

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        self.deleted_messages.append((chat_id, message_id))
        return True

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        self.callback_answers.append((callback_query_id, text))
        return True

    async def set_message_reaction(self, *, chat_id: int, message_id: int, emoji: str | None) -> bool:
        self.reactions.append((chat_id, message_id, emoji))
        return True


@pytest.fixture()
def app(tmp_path: Path) -> BridgeApp:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", delete_approval_messages=True),
        desktop=DesktopConfig(),
        bridge=BridgeConfig(state_path=tmp_path / "state.json"),
    )
    state = BridgeState()
    app = BridgeApp(config, state)
    app.desktop = FakeDesktop()
    app.telegram = FakeTelegram()
    return app


@pytest.mark.asyncio
async def test_new_message_prompts_for_project_selection(app: BridgeApp) -> None:
    update = {
        "message": {
            "message_id": 111,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "hello",
        }
    }

    await app._process_telegram_update(update)

    assert app.state.primary_chat_id == 1234
    assert app.state.lookup_thread_for_message(1234, 111) is None
    assert app.desktop.started_inputs == []
    assert app.telegram.reactions == []
    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": "Choose a Codex Desktop project for this new thread.",
            "reply_to_message_id": 111,
            "entities": None,
            "inline_keyboard": [
                [{"text": "repo", "callback_data": "project:1:0"}],
                [{"text": "Cancel", "callback_data": "project-cancel:1"}],
            ],
            "disable_notification": False,
        }
    ]


@pytest.mark.asyncio
async def test_telegram_updates_loop_does_not_advance_offset_when_processing_fails(
    app: BridgeApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = {
        "update_id": 42,
        "message": {
            "message_id": 111,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "hello",
        },
    }

    async def fake_get_updates(*, offset: int, timeout: int = 30, allowed_updates: list[str] | None = None):
        assert offset == 0
        return [update]

    async def fake_process(_: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    async def fake_sleep(_: float) -> None:
        app._shutdown.set()

    app.telegram.get_updates = fake_get_updates  # type: ignore[method-assign]
    app._process_telegram_update = fake_process  # type: ignore[method-assign]
    monkeypatch.setattr(bridge_module.asyncio, "sleep", fake_sleep)

    await app._telegram_updates_loop()

    assert app.state.telegram_update_offset == 0


@pytest.mark.asyncio
async def test_project_selection_starts_thread_and_turn(app: BridgeApp) -> None:
    app.desktop.new_thread_results[("/repo", "hello")] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "inProgress")],
    )

    update = {
        "message": {
            "message_id": 111,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "hello",
        }
    }

    await app._process_telegram_update(update)

    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "project:1:0",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.state.lookup_thread_for_message(1234, 111) == "thr_1"
    assert app.state.threads["thr_1"].current_turn_id == "turn_1"
    assert app.desktop.started_inputs == [("/repo", "hello")]
    assert app.telegram.reactions == [(1234, 111, "👀")]


@pytest.mark.asyncio
async def test_project_selection_prompts_for_replace_when_new_chat_contains_draft(app: BridgeApp) -> None:
    app.desktop.draft_conflicts[("/repo", "hello")] = "current text"

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "hello",
            }
        }
    )

    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "project:1:0",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.started_inputs == []
    assert app.state.lookup_thread_for_message(1234, 111) is None
    assert app.telegram.deleted_messages == [(1234, 10001)]
    assert len(app._pending_new_thread_replacements) == 1
    prompt = app.telegram.sent_messages[-1]
    assert prompt["chat_id"] == 1234
    assert prompt["text"] == (
        "This project's new chat window already contains a draft:\n\n"
        "Current text:\ncurrent text\n\n"
        "Replace it with your new message?"
    )
    assert prompt["reply_to_message_id"] == 111
    assert prompt["disable_notification"] is False
    assert prompt["entities"] is not None
    assert len(prompt["entities"]) == 1
    assert prompt["entities"][0]["type"] == "pre"
    assert prompt["entities"][0]["length"] == 12
    assert prompt["inline_keyboard"] == [
        [
            {"text": "Replace", "callback_data": "new-thread-replace:2"},
            {"text": "Cancel", "callback_data": "new-thread-replace-cancel:2"},
        ]
    ]


@pytest.mark.asyncio
async def test_new_thread_replace_callback_deletes_prompt_and_retries_with_replace(app: BridgeApp) -> None:
    app.desktop.draft_conflicts[("/repo", "hello")] = "current text"
    app.desktop.new_thread_results[("/repo", "hello")] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "inProgress")],
    )

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "hello",
            }
        }
    )
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "project:1:0",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    callback_key = next(iter(app._pending_new_thread_replacements))
    await app._handle_callback_query(
        {
            "id": "cbq-2",
            "from": {"id": 1, "is_bot": False},
            "data": f"new-thread-replace:{callback_key}",
            "message": {"message_id": 10002, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.start_replace_flags == [False, True]
    assert app.desktop.started_inputs == [("/repo", "hello")]
    assert app.telegram.deleted_messages == [(1234, 10001), (1234, 10002)]
    assert app.state.lookup_thread_for_message(1234, 111) == "thr_1"
    assert app.state.threads["thr_1"].current_turn_id == "turn_1"
    assert app.telegram.reactions == [(1234, 111, "👀")]


@pytest.mark.asyncio
async def test_new_thread_replace_cancel_deletes_prompt_without_retry(app: BridgeApp) -> None:
    app.desktop.draft_conflicts[("/repo", "hello")] = "current text"

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "hello",
            }
        }
    )
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "project:1:0",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    callback_key = next(iter(app._pending_new_thread_replacements))
    await app._handle_callback_query(
        {
            "id": "cbq-2",
            "from": {"id": 1, "is_bot": False},
            "data": f"new-thread-replace-cancel:{callback_key}",
            "message": {"message_id": 10002, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.start_replace_flags == [False]
    assert app.desktop.started_inputs == []
    assert app.telegram.deleted_messages == [(1234, 10001), (1234, 10002)]
    assert app.state.lookup_thread_for_message(1234, 111) is None
    assert app._pending_new_thread_replacements == {}


@pytest.mark.asyncio
async def test_reply_routes_back_to_existing_thread_and_queues_while_busy(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.current_turn_id = "turn_1"
    app.state.bind_message(1234, 200, "thr_1")

    update = {
        "message": {
            "message_id": 201,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "follow up",
            "reply_to_message": {"message_id": 200},
        }
    }

    await app._process_telegram_update(update)

    assert app.state.lookup_thread_for_message(1234, 201) == "thr_1"
    assert thread.queued_inputs == [QueuedInput(chat_id=1234, message_id=201, text="follow up")]
    assert app.desktop.sent_inputs == []


@pytest.mark.asyncio
async def test_reply_without_known_binding_reports_error_instead_of_starting_thread(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234

    update = {
        "message": {
            "message_id": 201,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "follow up",
            "reply_to_message": {"message_id": 999},
        }
    }

    await app._process_telegram_update(update)

    assert app.desktop.started_inputs == []
    assert app.state.lookup_thread_for_message(1234, 201) is None
    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": (
                "This reply target is not bound to a known bridge thread. "
                "Send a new top-level message to start a thread, or attach one explicitly."
            ),
            "reply_to_message_id": 201,
            "entities": None,
            "inline_keyboard": None,
            "disable_notification": False,
        }
    ]


@pytest.mark.asyncio
async def test_reply_binds_existing_thread_only_after_successful_send(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 200
    app.state.bind_message(1234, 200, "thr_1")
    app.desktop.threads["thr_1"] = make_conversation("thr_1", title="Thread 1")
    app.desktop.send_results[("thr_1", "follow up")] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_2", "inProgress")],
    )

    update = {
        "message": {
            "message_id": 201,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "follow up",
            "reply_to_message": {"message_id": 200},
        }
    }

    await app._process_telegram_update(update)

    assert app.desktop.sent_inputs == [("thr_1", "follow up")]
    assert app.state.lookup_thread_for_message(1234, 201) == "thr_1"
    assert thread.pending_message_ids == [201]
    assert thread.current_turn_id == "turn_2"


@pytest.mark.asyncio
async def test_reply_send_failure_does_not_bind_message_to_thread(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 200
    app.state.bind_message(1234, 200, "thr_1")
    app.desktop.threads["thr_1"] = make_conversation("thr_1", title="Thread 1")
    app.desktop.fail_send.add(("thr_1", "follow up"))

    update = {
        "message": {
            "message_id": 201,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "follow up",
            "reply_to_message": {"message_id": 200},
        }
    }

    await app._process_telegram_update(update)

    assert app.state.lookup_thread_for_message(1234, 201) is None
    assert thread.pending_message_ids == []
    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": "Failed to send to Codex Desktop: send failed",
            "reply_to_message_id": 201,
            "entities": None,
            "inline_keyboard": None,
            "disable_notification": False,
        }
    ]


@pytest.mark.asyncio
async def test_sync_thread_delivers_completion_and_starts_queued_input(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.current_turn_id = "turn_1"
    thread.pending_message_ids = [111]
    thread.last_chain_message_id = 111
    thread.queued_inputs = [QueuedInput(chat_id=1234, message_id=112, text="next step")]
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "All done."}],
            )
        ],
    )
    app.desktop.send_results[("thr_1", "next step")] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "All done."}],
            ),
            make_turn("turn_2", "inProgress"),
        ],
    )

    await app._sync_thread("thr_1")

    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": "All done.",
            "reply_to_message_id": 111,
            "entities": None,
            "inline_keyboard": None,
            "disable_notification": False,
        }
    ]
    assert app.telegram.reactions == [(1234, 111, "👌")]
    assert app.desktop.sent_inputs == [("thr_1", "next step")]
    assert thread.last_delivered_item_id == "item_1"
    assert thread.last_delivered_turn_id == "turn_1"
    assert thread.current_turn_id == "turn_2"
    assert thread.pending_message_ids == []


@pytest.mark.asyncio
async def test_sync_thread_mirrors_codex_side_user_message_into_telegram(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 111
    thread.last_handled_user_input_key = "turn:turn_1"
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn("turn_1", "completed", items=[make_user_message_item("old")]),
            make_turn("turn_2", "inProgress", items=[make_user_message_item("written in codex")]),
        ],
    )

    await app._sync_thread("thr_1")

    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": "written in codex",
            "reply_to_message_id": 111,
            "entities": None,
            "inline_keyboard": None,
            "disable_notification": False,
        }
    ]
    assert thread.last_handled_user_input_key == "turn:turn_2"
    assert thread.last_chain_message_id == 10001
    assert app.state.lookup_thread_for_message(1234, 10001) == "thr_1"


@pytest.mark.asyncio
async def test_sync_thread_does_not_echo_telegram_originated_user_message_back_into_telegram(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 200
    app.state.bind_message(1234, 200, "thr_1")
    app.desktop.threads["thr_1"] = make_conversation("thr_1", title="Thread 1")
    in_progress = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_2", "inProgress", items=[make_user_message_item("follow up")])],
    )
    app.desktop.send_results[("thr_1", "follow up")] = in_progress
    app.desktop.threads["thr_1"] = in_progress

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 201,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "follow up",
                "reply_to_message": {"message_id": 200},
            }
        }
    )

    await app._sync_thread("thr_1")

    assert app.telegram.sent_messages == []
    assert thread.last_handled_user_input_key == "turn:turn_2"


@pytest.mark.asyncio
async def test_sync_thread_does_not_echo_telegram_message_during_inflight_send(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 200
    app.state.bind_message(1234, 200, "thr_1")

    started = asyncio.Event()
    release = asyncio.Event()
    in_progress = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_2", "inProgress", items=[make_user_message_item("follow up")])],
    )
    app.desktop.threads["thr_1"] = in_progress

    async def delayed_send_message(thread_id: str, text: str) -> DesktopConversation:
        assert thread_id == "thr_1"
        assert text == "follow up"
        started.set()
        await release.wait()
        return in_progress

    app.desktop.send_message = delayed_send_message  # type: ignore[method-assign]

    send_task = asyncio.create_task(
        app._send_thread_input(
            thread,
            text="follow up",
            source_chat_id=1234,
            source_message_id=201,
            bind_on_success=True,
        )
    )
    await started.wait()

    await app._sync_thread("thr_1")
    release.set()
    await send_task

    assert app.telegram.sent_messages == []
    assert thread.last_handled_user_input_key == "turn:turn_2"


@pytest.mark.asyncio
async def test_sync_thread_delivers_completion_after_restart_when_pending_reply_target_is_missing(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.pending_message_ids = [51]
    thread.last_chain_message_id = 116
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "Recovered after restart."}],
            )
        ],
    )

    attempts: list[dict[str, Any]] = []

    async def flaky_send_message(
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ):
        del entities, inline_keyboard, disable_notification
        attempts.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})
        if len(attempts) == 1:
            raise TelegramApiError("Bad Request: message to be replied not found (error_code=400, http_status=400)")
        return type("Sent", (), {"chat_id": chat_id, "message_id": 4242, "raw": {}})

    app.telegram.send_message = flaky_send_message  # type: ignore[method-assign]

    await app._sync_thread("thr_1")

    assert attempts == [
        {"chat_id": 1234, "text": "Recovered after restart.", "reply_to_message_id": 51},
        {"chat_id": 1234, "text": "Recovered after restart.", "reply_to_message_id": 116},
    ]
    assert app.telegram.reactions == [(1234, 51, "👌")]
    assert thread.last_delivered_item_id == "item_1"
    assert thread.last_delivered_turn_id == "turn_1"
    assert thread.pending_message_ids == []
    assert thread.last_chain_message_id == 4242
    assert app.state.lookup_thread_for_message(1234, 4242) == "thr_1"


@pytest.mark.asyncio
async def test_sync_thread_does_not_advance_when_multi_chunk_delivery_fails(app: BridgeApp) -> None:
    app.config.bridge.max_message_chars = 12
    app.telegram.fail_send_calls = {2}
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.current_turn_id = "turn_1"
    thread.pending_message_ids = [111]
    thread.last_chain_message_id = 111
    thread.queued_inputs = [QueuedInput(chat_id=1234, message_id=112, text="next step")]
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "One two three four five six"}],
            )
        ],
    )

    await app._sync_thread("thr_1")

    assert [message["text"] for message in app.telegram.sent_messages] == ["One two thre"]
    assert app.telegram.deleted_messages == [(1234, 10001)]
    assert app.telegram.reactions == []
    assert app.desktop.sent_inputs == []
    assert thread.last_delivered_item_id is None
    assert thread.last_delivered_turn_id is None
    assert thread.current_turn_id is None
    assert thread.pending_message_ids == [111]
    assert thread.queued_inputs == [QueuedInput(chat_id=1234, message_id=112, text="next step")]


@pytest.mark.asyncio
async def test_command_approval_round_trip(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 300
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        requests=[
            DesktopRequest(
                request_id="req_1",
                kind="command",
                raw={
                    "reason": "Needs shell access",
                    "command": "pytest -q",
                    "cwd": "/repo",
                },
            )
        ],
    )

    await app._sync_thread("thr_1")

    assert len(app.telegram.sent_messages) == 1
    prompt = app.telegram.sent_messages[0]
    assert prompt["reply_to_message_id"] == 300
    assert "pytest -q" in prompt["text"]

    callback_key = next(iter(app._pending_approvals))
    approval_message_id = 10001
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": f"approve:{callback_key}",
            "message": {"message_id": approval_message_id, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.approval_clicks == [("thr_1", True)]
    assert app.desktop.approval_click_labels == [["Approve", "Accept", "Allow"]]
    assert app.telegram.deleted_messages == [(1234, approval_message_id)]
    assert app.state.threads["thr_1"].last_chain_message_id == 300


@pytest.mark.asyncio
async def test_command_approval_uses_desktop_action_labels_from_request(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 300
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        requests=[
            DesktopRequest(
                request_id="req_1",
                kind="command",
                raw={
                    "reason": "Needs shell access",
                    "command": "pytest -q",
                    "cwd": "/repo",
                    "commandActions": ["Run command", "Reject"],
                },
            )
        ],
    )

    await app._sync_thread("thr_1")

    callback_key = next(iter(app._pending_approvals))
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": f"approve:{callback_key}",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.approval_click_labels == [["Approve", "Accept", "Allow", "Run command"]]


@pytest.mark.asyncio
async def test_command_approval_uses_available_decision_labels_for_accept(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 300
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        requests=[
            DesktopRequest(
                request_id="req_1",
                kind="command",
                raw={
                    "params": {
                        "availableDecisions": ["accept", "cancel"],
                    }
                },
            )
        ],
    )

    await app._sync_thread("thr_1")

    callback_key = next(iter(app._pending_approvals))
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": f"approve:{callback_key}",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.approval_click_labels == [["Approve", "Accept", "Allow", "Yes"]]


@pytest.mark.asyncio
async def test_command_approval_uses_available_decision_labels_for_cancel(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 300
    app.desktop.threads["thr_1"] = make_conversation(
        "thr_1",
        requests=[
            DesktopRequest(
                request_id="req_1",
                kind="command",
                raw={
                    "params": {
                        "availableDecisions": ["accept", "cancel"],
                    }
                },
            )
        ],
    )

    await app._sync_thread("thr_1")

    callback_key = next(iter(app._pending_approvals))
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": f"deny:{callback_key}",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.approval_click_labels == [["Deny", "Decline", "Skip", "Cancel", "No"]]


@pytest.mark.asyncio
async def test_stale_callback_query_does_not_abort_processing_when_answer_fails(app: BridgeApp) -> None:
    app.telegram.answer_callback_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=TelegramApiError(
            "Bad Request: query is too old and response timeout expired or query ID is invalid "
            "(error_code=400, http_status=400)"
        )
    )

    await app._handle_callback_query(
        {
            "id": "cbq-stale",
            "from": {"id": 1, "is_bot": False},
            "data": "approve:missing",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.telegram.deleted_messages == [(1234, 10001)]


@pytest.mark.asyncio
async def test_telegram_updates_loop_advances_offset_for_stale_callback_query(app: BridgeApp) -> None:
    update = {
        "update_id": 42,
        "callback_query": {
            "id": "cbq-stale",
            "from": {"id": 1, "is_bot": False},
            "data": "approve:missing",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        },
    }

    async def fake_get_updates(*, offset: int, timeout: int = 30, allowed_updates: list[str] | None = None):
        del timeout, allowed_updates
        assert offset == 0
        app._shutdown.set()
        return [update]

    app.telegram.get_updates = fake_get_updates  # type: ignore[method-assign]
    app.telegram.answer_callback_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=TelegramApiError(
            "Bad Request: query is too old and response timeout expired or query ID is invalid "
            "(error_code=400, http_status=400)"
        )
    )

    await app._telegram_updates_loop()

    assert app.state.telegram_update_offset == 43


@pytest.mark.asyncio
async def test_attach_command_binds_existing_thread_and_sends_latest_message(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.threads["thr_attached"] = make_conversation(
        "thr_attached",
        title="Existing thread",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "**Bold** and `code`"}],
            )
        ],
    )
    update = {
        "message": {
            "message_id": 111,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "attach thr_attached",
        }
    }

    await app._process_telegram_update(update)

    assert app.desktop.activated_threads == ["thr_attached"]
    assert app.state.lookup_thread_for_message(1234, 111) == "thr_attached"
    assert app.state.lookup_thread_for_message(1234, 10001) == "thr_attached"
    assert len(app.telegram.sent_messages) == 2
    assert app.telegram.sent_messages[1]["reply_to_message_id"] == 10001
    assert app.telegram.sent_messages[1]["text"] == "Bold and code"
    assert app.telegram.sent_messages[1]["entities"] == [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "code", "offset": 9, "length": 4},
    ]
    assert app.state.threads["thr_attached"].last_delivered_item_id == "item_1"
    assert app.state.threads["thr_attached"].last_delivered_turn_id == "turn_1"


@pytest.mark.asyncio
async def test_attach_command_skips_in_progress_partial_and_sends_latest_terminal_message(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.threads["thr_attached"] = make_conversation(
        "thr_attached",
        title="Existing thread",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "Stable reply"}],
            ),
            make_turn(
                "turn_2",
                "inProgress",
                items=[{"id": "item_partial", "type": "agentMessage", "text": "Streaming partial"}],
            ),
        ],
    )

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "attach thr_attached",
            }
        }
    )

    assert len(app.telegram.sent_messages) == 2
    assert app.telegram.sent_messages[1]["text"] == "Stable reply"
    assert app.state.threads["thr_attached"].current_turn_id == "turn_2"
    assert app.state.threads["thr_attached"].last_delivered_item_id == "item_1"
    assert app.state.threads["thr_attached"].last_delivered_turn_id == "turn_1"


@pytest.mark.asyncio
async def test_attach_without_id_prompts_for_recent_sessions(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.thread_summaries = [
        DesktopConversationSummary(
            thread_id="thr_recent",
            title="Очень длинное название треда, которое нужно обрезать для кнопки",
            current=False,
            cwd="/repo",
            project_label="ai_assistant",
            project_path="/Users/sne/projects/ai_assistant",
            updated_at=datetime(2026, 4, 8, 19, 18),
        )
    ]

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "attach",
            }
        }
    )

    assert len(app.telegram.sent_messages) == 1
    sent = app.telegram.sent_messages[0]
    assert sent["chat_id"] == 1234
    assert sent["text"] == "Choose a recent Codex Desktop session to attach."
    assert sent["reply_to_message_id"] == 111
    assert sent["disable_notification"] is False
    assert sent["inline_keyboard"] == [
        [{"text": sent["inline_keyboard"][0][0]["text"], "callback_data": "attach:1:0"}],
        [{"text": "Cancel", "callback_data": "attach-cancel:1"}],
    ]
    assert sent["inline_keyboard"][0][0]["text"].startswith("ai_assistant: Очень длинное название треда")
    assert sent["inline_keyboard"][0][0]["text"].endswith("...")


@pytest.mark.asyncio
async def test_attach_picker_selection_attaches_existing_thread(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.thread_summaries = [
        DesktopConversationSummary(
            thread_id="thr_attached",
            title="Existing thread",
            current=False,
            cwd="/repo",
            project_label="repo",
            project_path="/repo",
        )
    ]
    app.desktop.threads["thr_attached"] = make_conversation(
        "thr_attached",
        title="Existing thread",
        turns=[
            make_turn(
                "turn_1",
                "completed",
                items=[{"id": "item_1", "type": "agentMessage", "text": "latest"}],
            )
        ],
    )

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "attach",
            }
        }
    )
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "attach:1:0",
            "message": {"message_id": 10001, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.desktop.activated_threads == ["thr_attached"]
    assert app.state.lookup_thread_for_message(1234, 111) == "thr_attached"
    assert app.telegram.deleted_messages == [(1234, 10001)]
    assert len(app.telegram.sent_messages) == 3
    assert app.telegram.sent_messages[1]["text"] == "Attached thread thr_attached. Reply in this chain to continue it from Telegram."
    assert app.telegram.sent_messages[2]["text"] == "latest"


@pytest.mark.asyncio
async def test_attach_picker_uses_untitled_label_when_recent_session_title_is_missing(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.thread_summaries = [
        DesktopConversationSummary(
            thread_id="thr_recent",
            title=None,
            current=False,
            cwd="/repo",
            project_label="repo",
            project_path="/repo",
            updated_at=datetime(2026, 4, 8, 19, 18),
        )
    ]

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 111,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "attach",
            }
        }
    )

    sent = app.telegram.sent_messages[0]
    assert sent["inline_keyboard"][0][0]["text"] == "repo: Untitled 19:18"


@pytest.mark.asyncio
async def test_attach_command_reports_activation_error(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.desktop.fail_activate.add("thr_missing")

    update = {
        "message": {
            "message_id": 111,
            "chat": {"id": 1234, "type": "private"},
            "from": {"id": 1, "is_bot": False},
            "text": "attach thr_missing",
        }
    }

    await app._process_telegram_update(update)

    assert app.telegram.sent_messages == [
        {
            "chat_id": 1234,
            "text": "Failed to attach thread thr_missing. Check that it is visible in Codex Desktop.",
            "reply_to_message_id": 111,
            "entities": None,
            "inline_keyboard": None,
            "disable_notification": False,
        }
    ]


@pytest.mark.asyncio
async def test_detach_by_reply_removes_thread_bindings_and_pending_approvals(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 200
    app.state.bind_message(1234, 200, "thr_1")
    app.state.bind_message(1234, 201, "thr_1")
    app._pending_approvals["1"] = PendingApproval(
        callback_key="1",
        request_id="req_1",
        kind="command",
        thread_id="thr_1",
        chat_id=1234,
        message_id=555,
    )
    app.state.approval_cleanup_messages.append(ApprovalCleanupMessage(chat_id=1234, message_id=555))

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 300,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "detach",
                "reply_to_message": {"message_id": 200},
            }
        }
    )

    assert "thr_1" not in app.state.threads
    assert app.state.lookup_thread_for_message(1234, 200) is None
    assert app.state.lookup_thread_for_message(1234, 201) is None
    assert app._pending_approvals == {}
    assert app.telegram.deleted_messages == [(1234, 555)]
    assert app.telegram.sent_messages[-1]["text"] == "Detached thread thr_1. Telegram will stop receiving updates from it."


@pytest.mark.asyncio
async def test_detach_by_explicit_thread_id_removes_thread(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.state.bind_message(1234, 200, "thr_1")

    await app._process_telegram_update(
        {
            "message": {
                "message_id": 300,
                "chat": {"id": 1234, "type": "private"},
                "from": {"id": 1, "is_bot": False},
                "text": "detach thr_1",
            }
        }
    )

    assert "thr_1" not in app.state.threads
    assert app.state.lookup_thread_for_message(1234, 200) is None
    assert app.telegram.sent_messages[-1]["text"] == "Detached thread thr_1. Telegram will stop receiving updates from it."


def test_load_config_rejects_placeholder_bot_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[telegram]
bot_token = "123456:replace-me"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="example placeholder"):
        load_config(config_path)


def test_hold_single_instance_lock_rejects_second_holder(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    with _hold_single_instance_lock(state_path):
        with pytest.raises(SingleInstanceError, match="already running"):
            with _hold_single_instance_lock(state_path):
                pass


def test_reset_ephemeral_runtime_state_keeps_cleanup_messages() -> None:
    state = BridgeState(
        next_callback_key=7,
        approval_cleanup_messages=[ApprovalCleanupMessage(chat_id=1234, message_id=555)],
    )
    thread = state.get_or_create_thread("thr_1")
    thread.current_turn_id = "turn_1"

    state.reset_ephemeral_runtime_state()

    assert thread.current_turn_id is None
    assert state.approval_cleanup_messages == [ApprovalCleanupMessage(chat_id=1234, message_id=555)]
    assert state.next_callback_key == 7


def test_bridge_state_round_trips_last_handled_user_input_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = BridgeState()
    thread = state.get_or_create_thread("thr_1")
    thread.last_handled_user_input_key = "turn:turn_2"

    state.save(state_path)
    loaded = BridgeState.load(state_path)

    assert loaded.threads["thr_1"].last_handled_user_input_key == "turn:turn_2"


def test_render_markdown_chunks_does_not_emit_text_link_for_local_file_paths() -> None:
    chunks = render_markdown_chunks(
        "See [bridge.py](/Users/sne/projects/codex-telegram-bridge/src/codex_telegram_bridge/bridge.py#L167).",
        max_utf16_len=3900,
    )

    assert len(chunks) == 1
    assert chunks[0].text == (
        "See bridge.py (/Users/sne/projects/codex-telegram-bridge/src/codex_telegram_bridge/bridge.py#L167)."
    )
    assert all(entity.get("type") != "text_link" for entity in chunks[0].entities)


@pytest.mark.asyncio
async def test_telegram_api_surfaces_json_error_description() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "error_code": 401, "description": "Unauthorized"},
        )

    api = TelegramBotApi("token")
    await api._client.aclose()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(TelegramApiError, match=r"Unauthorized \(error_code=401, http_status=401\)"):
        await api.get_me()

    await api.close()


@pytest.mark.asyncio
async def test_telegram_api_get_updates_uses_timeout_larger_than_long_poll() -> None:
    captured_timeout: httpx.Timeout | float | None = None

    class RecordingClient:
        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: httpx.Timeout | float | None = None,
        ) -> httpx.Response:
            del url, json
            nonlocal captured_timeout
            captured_timeout = timeout
            return httpx.Response(200, json={"ok": True, "result": []})

        async def aclose(self) -> None:
            return None

    api = TelegramBotApi("token")
    await api._client.aclose()
    api._client = RecordingClient()  # type: ignore[assignment]

    updates = await api.get_updates(offset=0, timeout=30)

    assert updates == []
    assert isinstance(captured_timeout, httpx.Timeout)
    assert captured_timeout.connect == 10.0
    assert captured_timeout.read == 35.0
    await api.close()


@pytest.mark.asyncio
async def test_telegram_api_uses_http_error_class_when_message_is_empty() -> None:
    class FailingClient:
        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: httpx.Timeout | float | None = None,
        ) -> httpx.Response:
            del url, json, timeout
            raise httpx.ReadTimeout("")

        async def aclose(self) -> None:
            return None

    api = TelegramBotApi("token")
    await api._client.aclose()
    api._client = FailingClient()  # type: ignore[assignment]

    with pytest.raises(TelegramApiError, match=r"Telegram API request failed for getMe: ReadTimeout"):
        await api.get_me()

    await api.close()


@pytest.mark.asyncio
async def test_safe_send_message_explains_missing_reply_target_without_retrying_original(app: BridgeApp) -> None:
    attempts: list[dict[str, Any]] = []

    async def flaky_send_message(
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ):
        del entities, inline_keyboard, disable_notification
        attempts.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})
        if len(attempts) == 1:
            raise TelegramApiError("Bad Request: message to be replied not found (error_code=400, http_status=400)")
        return type("Sent", (), {"chat_id": chat_id, "message_id": 4242, "raw": {}})

    app.telegram.send_message = flaky_send_message  # type: ignore[method-assign]

    sent = await app._safe_send_message(chat_id=1234, text="hello", reply_to_message_id=999)

    assert sent is None
    assert attempts == [
        {"chat_id": 1234, "text": "hello", "reply_to_message_id": 999},
        {
            "chat_id": 1234,
            "text": (
                "Failed to send a reply because the referenced Telegram message is no longer available. "
                "Reply to a newer bridge message or send a new top-level message."
            ),
            "reply_to_message_id": None,
        },
    ]


@pytest.mark.asyncio
async def test_safe_send_message_retries_missing_reply_target_against_fallback_chain_message(app: BridgeApp) -> None:
    attempts: list[dict[str, Any]] = []

    async def flaky_send_message(
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        entities: list[dict[str, Any]] | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ):
        del entities, inline_keyboard, disable_notification
        attempts.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})
        if len(attempts) == 1:
            raise TelegramApiError("Bad Request: message to be replied not found (error_code=400, http_status=400)")
        return type("Sent", (), {"chat_id": chat_id, "message_id": 4242, "raw": {}})

    app.telegram.send_message = flaky_send_message  # type: ignore[method-assign]

    sent = await app._safe_send_message(
        chat_id=1234,
        text="hello",
        reply_to_message_id=999,
        fallback_reply_to_message_id=1001,
    )

    assert sent is not None
    assert sent.message_id == 4242
    assert attempts == [
        {"chat_id": 1234, "text": "hello", "reply_to_message_id": 999},
        {"chat_id": 1234, "text": "hello", "reply_to_message_id": 1001},
    ]


class FakeWs:
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self._send_error = send_error
        self._responses = list(responses or [])

    async def send(self, payload: str) -> None:
        if self._send_error is not None:
            error = self._send_error
            self._send_error = None
            raise error

    async def recv(self) -> str:
        if not self._responses:
            raise AssertionError("unexpected recv")
        return json.dumps(self._responses.pop(0))


class ActivationProbeClient(CodexDesktopClient):
    def __init__(self) -> None:
        super().__init__(
            app_path=Path("/Applications/Codex.app"),
            remote_debugging_port=9229,
            user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
            launch_timeout_seconds=0.02,
            poll_interval_seconds=0.0,
        )
        self.thread = make_conversation("thr_1", title="Thread 1")
        self.prepare_results: list[dict[str, Any] | None] = []
        self.current_thread_ids: list[str | None] = []
        self.header_titles: list[str | None] = []
        self.prepare_calls: list[str] = []

    async def _prepare_thread_activation(self, thread_id: str) -> dict[str, Any] | None:
        self.prepare_calls.append(thread_id)
        if self.prepare_results:
            return self.prepare_results.pop(0)
        return {"ok": True, "phase": "clicked"}

    async def read_thread(self, thread_id: str) -> DesktopConversation | None:
        if thread_id != self.thread.thread_id:
            return None
        return copy.deepcopy(self.thread)

    async def _current_thread_id(self) -> str | None:
        if self.current_thread_ids:
            return self.current_thread_ids.pop(0)
        return None

    async def _read_thread_header_title(self) -> str | None:
        if self.header_titles:
            return self.header_titles.pop(0)
        return self.thread.title

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        del expression
        if self.header_titles:
            return {"ok": True, "title": self.header_titles.pop(0)}
        return {"ok": True, "title": self.thread.title}


def make_user_message_item(text: str) -> dict[str, Any]:
    return {
        "type": "userMessage",
        "content": [{"type": "input_text", "text": text}],
    }


class SendProbeClient(CodexDesktopClient):
    def __init__(
        self,
        read_sequence: list[DesktopConversation],
        *,
        composer_text: str = "",
        composer_text_sequence: list[str] | None = None,
        eval_results: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            app_path=Path("/Applications/Codex.app"),
            remote_debugging_port=9229,
            user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
            launch_timeout_seconds=0.02,
            poll_interval_seconds=0.0,
        )
        self._read_sequence = [copy.deepcopy(item) for item in read_sequence]
        self.inserted_texts: list[str] = []
        self.composer_text = composer_text
        self.composer_text_sequence = list(composer_text_sequence or [])
        self.eval_results = list(eval_results or [])

    async def read_thread(self, thread_id: str) -> DesktopConversation | None:
        if not self._read_sequence:
            raise AssertionError("unexpected read_thread call")
        current = self._read_sequence.pop(0)
        return copy.deepcopy(current) if current.thread_id == thread_id else None

    async def activate_thread(self, thread_id: str) -> DesktopConversation:
        if not self._read_sequence:
            raise AssertionError("missing activation conversation")
        current = self._read_sequence[0]
        return copy.deepcopy(current)

    async def _focus_composer(self) -> None:
        return None

    async def _read_composer_text(self) -> str:
        if self.composer_text_sequence:
            return self.composer_text_sequence.pop(0)
        return self.composer_text

    async def _insert_text(self, text: str) -> None:
        self.inserted_texts.append(text)
        self.composer_text = text.rstrip("\n")

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        if self.eval_results:
            return self.eval_results.pop(0)
        return {"ok": True}


class ApprovalProbeClient(CodexDesktopClient):
    def __init__(self, *, eval_results: list[dict[str, Any]]) -> None:
        super().__init__(
            app_path=Path("/Applications/Codex.app"),
            remote_debugging_port=9229,
            user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
            launch_timeout_seconds=0.02,
            poll_interval_seconds=0.0,
        )
        self.eval_results = list(eval_results)
        self.activation_calls: list[str] = []

    async def activate_thread(self, thread_id: str) -> DesktopConversation:
        self.activation_calls.append(thread_id)
        return make_conversation(thread_id)

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        del expression
        if self.eval_results:
            return self.eval_results.pop(0)
        return {"ok": True}


class ComposerFocusProbeClient(CodexDesktopClient):
    def __init__(self, *, eval_results: list[dict[str, Any]]) -> None:
        super().__init__(
            app_path=Path("/Applications/Codex.app"),
            remote_debugging_port=9229,
            user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
            launch_timeout_seconds=0.02,
            poll_interval_seconds=0.0,
        )
        self.eval_results = list(eval_results)

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        del expression
        if self.eval_results:
            return self.eval_results.pop(0)
        return {"ok": True}


class StartThreadProbeClient(CodexDesktopClient):
    def __init__(
        self,
        *,
        list_threads_sequence: list[list[DesktopConversationSummary]],
        conversations: dict[str, DesktopConversation],
        composer_text: str = "",
    ) -> None:
        super().__init__(
            app_path=Path("/Applications/Codex.app"),
            remote_debugging_port=9229,
            user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
            launch_timeout_seconds=0.02,
            poll_interval_seconds=0.0,
        )
        self._list_threads_sequence = [copy.deepcopy(item) for item in list_threads_sequence]
        self._conversations = {thread_id: copy.deepcopy(conversation) for thread_id, conversation in conversations.items()}
        self.clicked_project_paths: list[str] = []
        self.inserted_texts: list[str] = []
        self.clear_calls = 0
        self.composer_text = composer_text

    async def list_threads(self) -> list[DesktopConversationSummary]:
        if not self._list_threads_sequence:
            raise AssertionError("unexpected list_threads call")
        return copy.deepcopy(self._list_threads_sequence.pop(0))

    async def read_thread(self, thread_id: str) -> DesktopConversation | None:
        conversation = self._conversations.get(thread_id)
        return copy.deepcopy(conversation) if conversation is not None else None

    async def _project_for_path(self, project_path: str) -> DesktopProject | None:
        return DesktopProject(label="repo", path=project_path)

    async def _click_project_new_thread_button(self, project_path: str) -> None:
        self.clicked_project_paths.append(project_path)

    async def _focus_composer(self) -> None:
        return None

    async def _clear_visible_composer(self) -> None:
        self.clear_calls += 1
        self.composer_text = ""

    async def _read_composer_text(self) -> str:
        return self.composer_text

    async def _insert_text(self, text: str) -> None:
        self.inserted_texts.append(text)
        self.composer_text = text.rstrip("\n")

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        return {"ok": True}


@pytest.mark.asyncio
async def test_desktop_client_reconnects_after_closed_websocket() -> None:
    client = CodexDesktopClient(
        app_path=Path("/Applications/Codex.app"),
        remote_debugging_port=9229,
        user_data_dir=Path("/tmp/codex-telegram-bridge-test"),
        launch_timeout_seconds=1,
        poll_interval_seconds=0.1,
    )
    first_ws = FakeWs(send_error=ConnectionClosedError(Close(1011, "keepalive"), None))
    second_ws = FakeWs(responses=[{"id": 2, "result": {"result": {"value": 1}}}])
    client._ws = first_ws  # type: ignore[assignment]
    client._page_ws_url = "ws://first"

    reconnects = 0

    async def fake_ensure_ready() -> None:
        nonlocal reconnects
        reconnects += 1
        client._ws = second_ws  # type: ignore[assignment]
        client._page_ws_url = "ws://second"
        client._cdp_ready = True

    client._ensure_cdp_ready = fake_ensure_ready  # type: ignore[method-assign]

    result = await client._call_cdp("Runtime.evaluate", {"expression": "1"}, _skip_ready=True)

    assert result == {"result": {"value": 1}}
    assert reconnects == 1
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_activate_thread_retries_after_expanding_group() -> None:
    client = ActivationProbeClient()
    client.prepare_results = [
        {"ok": True, "phase": "expanded-group"},
        {"ok": True, "phase": "clicked"},
    ]
    client.current_thread_ids = [None, "thr_1"]

    result = await client.activate_thread("thr_1")

    assert result.thread_id == "thr_1"
    assert client.prepare_calls == ["thr_1", "thr_1"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_activate_thread_waits_for_header_title_to_match_target_conversation() -> None:
    client = ActivationProbeClient()
    client.current_thread_ids = ["thr_1", "thr_1"]
    client.header_titles = ["Wrong thread", "Thread 1"]

    result = await client.activate_thread("thr_1")

    assert result.thread_id == "thr_1"
    assert client.prepare_calls == ["thr_1", "thr_1"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_click_approval_action_retries_until_button_is_visible() -> None:
    client = ApprovalProbeClient(
        eval_results=[
            {"ok": False, "visibleButtons": ["Later"]},
            {"ok": True},
        ]
    )

    await client.click_approval_action("thr_1", approve=True, labels=["Run command"])

    assert client.activation_calls == ["thr_1"]
    assert client.eval_results == []
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_focus_composer_retries_until_visible_composer_is_focused() -> None:
    client = ComposerFocusProbeClient(
        eval_results=[
            {"ok": False, "error": "composer-not-focused"},
            {"ok": True},
        ]
    )

    await client._focus_composer()

    assert client.eval_results == []
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_activate_thread_surfaces_sidebar_store_error() -> None:
    client = ActivationProbeClient()
    client.prepare_results = [{"ok": False, "error": "thread-not-in-sidebar-store"}]

    with pytest.raises(DesktopClientError, match="thread-not-in-sidebar-store"):
        await client.activate_thread("thr_1")

    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_send_message_waits_for_matching_user_turn() -> None:
    before = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "completed", items=[make_user_message_item("old")])],
    )
    hydrated_old = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn("turn_1", "completed", items=[make_user_message_item("old")]),
            make_turn("turn_2", "completed", items=[make_user_message_item("older hydrated later")]),
        ],
    )
    with_probe = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn("turn_1", "completed", items=[make_user_message_item("old")]),
            make_turn("turn_2", "completed", items=[make_user_message_item("older hydrated later")]),
            make_turn("turn_3", "inProgress", items=[make_user_message_item("probe message")]),
        ],
    )
    client = SendProbeClient([before, hydrated_old, with_probe])

    result = await client.send_message("thr_1", "probe message")

    assert result.latest_turn is not None
    assert result.latest_turn.turn_id == "turn_3"
    assert client.inserted_texts == ["probe message\n"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_send_message_fails_when_composer_contains_draft() -> None:
    before = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "completed", items=[make_user_message_item("old")])],
    )
    client = SendProbeClient([before, before], composer_text="unsent desktop draft")

    with pytest.raises(DesktopClientError, match="Desktop composer is not empty"):
        await client.send_message("thr_1", "probe message")

    assert client.inserted_texts == []
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_send_message_waits_for_transient_composer_draft_to_clear() -> None:
    before = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "completed", items=[make_user_message_item("old")])],
    )
    after = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn("turn_1", "completed", items=[make_user_message_item("old")]),
            make_turn("turn_2", "inProgress", items=[make_user_message_item("probe message")]),
        ],
    )
    client = SendProbeClient(
        [before, before, after],
        composer_text_sequence=["transient draft", ""],
    )

    result = await client.send_message("thr_1", "probe message")

    assert result.latest_turn is not None
    assert result.latest_turn.turn_id == "turn_2"
    assert client.inserted_texts == ["probe message\n"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_start_new_thread_waits_for_matching_first_user_message() -> None:
    old_summary = DesktopConversationSummary(thread_id="thr_old", title="Old", current=True, cwd="/repo")
    wrong_summary = DesktopConversationSummary(thread_id="thr_wrong", title="Wrong", current=False, cwd="/repo")
    right_summary = DesktopConversationSummary(thread_id="thr_right", title="Right", current=False, cwd="/repo")
    client = StartThreadProbeClient(
        list_threads_sequence=[
            [old_summary],
            [old_summary, wrong_summary, right_summary],
        ],
        conversations={
            "thr_wrong": make_conversation(
                "thr_wrong",
                title="Wrong",
                turns=[make_turn("turn_wrong", "inProgress", items=[make_user_message_item("someone else")])],
            ),
            "thr_right": make_conversation(
                "thr_right",
                title="Right",
                turns=[make_turn("turn_right", "inProgress", items=[make_user_message_item("hello")])],
            ),
        },
    )

    result = await client.start_new_thread("/repo", "hello")

    assert result.thread_id == "thr_right"
    assert client.clicked_project_paths == ["/repo"]
    assert client.inserted_texts == ["hello\n"]
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_start_new_thread_fails_when_composer_contains_draft() -> None:
    old_summary = DesktopConversationSummary(thread_id="thr_old", title="Old", current=True, cwd="/repo")
    client = StartThreadProbeClient(
        list_threads_sequence=[[old_summary]],
        conversations={},
        composer_text="unsent desktop draft",
    )

    with pytest.raises(DesktopClientError, match="Desktop composer is not empty for a new thread"):
        await client.start_new_thread("/repo", "hello")

    assert client.inserted_texts == []
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_start_new_thread_clears_existing_draft_when_replace_is_requested() -> None:
    old_summary = DesktopConversationSummary(thread_id="thr_old", title="Old", current=True, cwd="/repo")
    right_summary = DesktopConversationSummary(thread_id="thr_right", title="Right", current=False, cwd="/repo")
    client = StartThreadProbeClient(
        list_threads_sequence=[
            [old_summary],
            [old_summary, right_summary],
        ],
        conversations={
            "thr_right": make_conversation(
                "thr_right",
                title="Right",
                turns=[make_turn("turn_right", "inProgress", items=[make_user_message_item("hello")])],
            )
        },
        composer_text="unsent desktop draft",
    )

    result = await client.start_new_thread("/repo", "hello", replace_existing_draft=True)

    assert result.thread_id == "thr_right"
    assert client.clear_calls == 1
    assert client.inserted_texts == ["hello\n"]
    await client._http.aclose()


def test_project_button_center_js_matches_current_start_new_chat_button_contract() -> None:
    expression = _project_button_center_js("/repo")

    assert "aria.startsWith('start new ')" in expression
    assert "aria.endsWith(expectedSuffix)" in expression
    assert "Start new thread in" not in expression


def test_click_send_button_js_matches_current_composer_panel_contract() -> None:
    expression = _click_send_button_js()

    assert 'querySelectorAll(\'.ProseMirror[contenteditable="true"]\')' in expression
    assert 'div[class*="bg-token-input-background"]' in expression
    assert 'querySelector(\'.ProseMirror[contenteditable="true"]\')' not in expression
    assert "div.bg-token-input-background" not in expression


def test_focus_composer_js_prefers_visible_composer_contract() -> None:
    assert 'querySelectorAll(\'.ProseMirror[contenteditable="true"]\')' in _FOCUS_COMPOSER_JS
    assert "no-visible-composer" in _FOCUS_COMPOSER_JS
    assert "composer-not-focused" in _FOCUS_COMPOSER_JS


def test_composer_state_js_prefers_visible_composer_contract() -> None:
    assert 'querySelectorAll(\'.ProseMirror[contenteditable="true"]\')' in _COMPOSER_STATE_JS
    assert "no-visible-composer" in _COMPOSER_STATE_JS


def test_current_thread_id_js_reads_react_current_conversation_id() -> None:
    assert "currentConversationId" in _CURRENT_THREAD_ID_JS
    assert "aria-current" not in _CURRENT_THREAD_ID_JS


def test_thread_header_title_js_reads_visible_codex_header_title() -> None:
    assert "header" in _THREAD_HEADER_TITLE_JS
    assert ".text-token-foreground" in _THREAD_HEADER_TITLE_JS


@pytest.mark.asyncio
async def test_desktop_client_send_message_fails_when_thread_already_has_active_turn() -> None:
    active = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_2", "inProgress", items=[make_user_message_item("still running")])],
    )
    client = SendProbeClient([active])

    with pytest.raises(DesktopClientError, match=r"still running turn turn_2"):
        await client.send_message("thr_1", "probe message")

    assert client.inserted_texts == []
    await client._http.aclose()


@pytest.mark.asyncio
async def test_desktop_client_send_message_reports_active_turn_when_button_disappears_mid_send() -> None:
    before = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[make_turn("turn_1", "completed", items=[make_user_message_item("old")])],
    )
    active = make_conversation(
        "thr_1",
        title="Thread 1",
        turns=[
            make_turn("turn_1", "completed", items=[make_user_message_item("old")]),
            make_turn("turn_2", "inProgress", items=[make_user_message_item("new work")]),
        ],
    )
    client = SendProbeClient(
        [before, before, active],
        eval_results=[{"ok": False, "error": "send-button-not-found"}],
    )

    with pytest.raises(DesktopClientError, match=r"still running turn turn_2"):
        await client.send_message("thr_1", "probe message")

    assert client.inserted_texts == ["probe message\n"]
    await client._http.aclose()
