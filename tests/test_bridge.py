from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from codex_telegram_bridge.bridge import BridgeApp
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig, load_config
from codex_telegram_bridge.desktop_client import (
    DesktopClientError,
    CodexDesktopClient,
    DesktopConversation,
    DesktopConversationSummary,
    DesktopProject,
    DesktopRequest,
    DesktopSessionInfo,
    DesktopTurn,
)
from codex_telegram_bridge.state import BridgeState, QueuedInput
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
    fail_activate: set[str] = field(default_factory=set)
    fail_start_messages: set[tuple[str, str]] = field(default_factory=set)
    fail_send: set[tuple[str, str]] = field(default_factory=set)
    fail_approval_threads: set[str] = field(default_factory=set)
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

    async def start_new_thread(self, project_path: str, text: str) -> DesktopConversation:
        key = (project_path, text)
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

    async def click_approval_action(self, thread_id: str, *, approve: bool) -> None:
        if thread_id in self.fail_approval_threads:
            raise DesktopClientError("approval button missing")
        self.approval_clicks.append((thread_id, approve))


@dataclass
class FakeTelegram:
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[tuple[int, int, str | None]] = field(default_factory=list)
    deleted_messages: list[tuple[int, int]] = field(default_factory=list)
    callback_answers: list[tuple[str, str | None]] = field(default_factory=list)

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
    assert app.telegram.deleted_messages == [(1234, approval_message_id)]


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


def make_user_message_item(text: str) -> dict[str, Any]:
    return {
        "type": "userMessage",
        "content": [{"type": "input_text", "text": text}],
    }


class SendProbeClient(CodexDesktopClient):
    def __init__(self, read_sequence: list[DesktopConversation], *, composer_text: str = "") -> None:
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
        return self.composer_text

    async def _insert_text(self, text: str) -> None:
        self.inserted_texts.append(text)

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
