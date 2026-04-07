from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from codex_telegram_bridge.bridge import BridgeApp
from codex_telegram_bridge.config import AppConfig, BridgeConfig, CodexConfig, TelegramConfig
from codex_telegram_bridge.state import BridgeState


@dataclass
class FakeCodex:
    responses: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    respond_results: list[tuple[int | str, dict[str, Any]]] = field(default_factory=list)

    async def start(self):  # pragma: no cover - not used in tests
        return None

    async def close(self):  # pragma: no cover - not used in tests
        return None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, params or {}))
        queue = self.responses.setdefault(method, [])
        if queue:
            return queue.pop(0)
        return {}

    async def respond_result(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.respond_results.append((request_id, result))


@dataclass
class FakeTelegram:
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[tuple[int, int, str | None]] = field(default_factory=list)
    deleted_messages: list[tuple[int, int]] = field(default_factory=list)
    callback_answers: list[tuple[str, str | None]] = field(default_factory=list)

    async def close(self):  # pragma: no cover - not used in tests
        return None

    async def get_updates(self, **kwargs):  # pragma: no cover - not used in tests
        return []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
    ):
        message_id = 10_000 + len(self.sent_messages) + 1
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
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
        codex=CodexConfig(thread_start={"approvalPolicy": "unlessTrusted"}),
        bridge=BridgeConfig(state_path=tmp_path / "state.json", poll_external_threads=False),
    )
    state = BridgeState()
    app = BridgeApp(config, state)
    app.codex = FakeCodex()
    app.telegram = FakeTelegram()
    return app


@pytest.mark.asyncio
async def test_new_message_starts_thread_and_turn(app: BridgeApp) -> None:
    app.codex.responses["thread/start"] = [{"thread": {"id": "thr_1", "preview": ""}}]
    app.codex.responses["turn/start"] = [{"turn": {"id": "turn_1", "status": "inProgress", "items": [], "error": None}}]

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
    assert app.state.lookup_thread_for_message(1234, 111) == "thr_1"
    assert app.state.threads["thr_1"].current_turn_id == "turn_1"
    assert app.codex.requests[0][0] == "thread/start"
    assert app.codex.requests[1][0] == "turn/start"
    assert app.telegram.reactions == [(1234, 111, "👀")]


@pytest.mark.asyncio
async def test_reply_routes_back_to_existing_thread_and_steers(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.current_turn_id = "turn_1"
    app.state.bind_message(1234, 200, "thr_1")
    app._loaded_threads.add("thr_1")
    app.codex.responses["turn/steer"] = [{"turnId": "turn_1"}]

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
    assert app.codex.requests[-1] == (
        "turn/steer",
        {
            "threadId": "thr_1",
            "expectedTurnId": "turn_1",
            "input": [{"type": "text", "text": "follow up"}],
        },
    )


@pytest.mark.asyncio
async def test_command_approval_round_trip(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    thread = app.state.get_or_create_thread("thr_1")
    thread.primary_chat_id = 1234
    thread.last_chain_message_id = 300
    thread.pending_message_ids = [301]

    await app._handle_codex_request(
        "item/commandExecution/requestApproval",
        99,
        {
            "threadId": "thr_1",
            "turnId": "turn_1",
            "itemId": "item_1",
            "reason": "Needs shell access",
            "command": "pytest -q",
            "cwd": "/repo",
        },
    )

    assert len(app.telegram.sent_messages) == 1
    prompt = app.telegram.sent_messages[0]
    assert prompt["reply_to_message_id"] == 301
    assert "pytest -q" in prompt["text"]

    approval_message_id = 10001
    await app._handle_callback_query(
        {
            "id": "cbq-1",
            "from": {"id": 1, "is_bot": False},
            "data": "approve:99",
            "message": {"message_id": approval_message_id, "chat": {"id": 1234, "type": "private"}},
        }
    )

    assert app.codex.respond_results == [(99, {"decision": "accept"})]
    assert app.telegram.deleted_messages == [(1234, approval_message_id)]


@pytest.mark.asyncio
async def test_external_poller_creates_new_chain(app: BridgeApp) -> None:
    app.state.primary_chat_id = 1234
    app.codex.responses["thread/list"] = [
        {
            "data": [
                {
                    "id": "thr_ext",
                    "preview": "Fix tests",
                    "updatedAt": 200,
                    "status": {"type": "notLoaded"},
                }
            ]
        }
    ]
    app.codex.responses["thread/read"] = [
        {
            "thread": {
                "id": "thr_ext",
                "turns": [
                    {
                        "id": "turn_ext",
                        "status": "completed",
                        "items": [
                            {"id": "item_ext", "type": "agentMessage", "text": "All done."}
                        ],
                    }
                ],
            }
        }
    ]

    await app._poll_external_threads_once()

    assert len(app.telegram.sent_messages) == 1
    assert "All done." in app.telegram.sent_messages[0]["text"]
    assert app.state.lookup_thread_for_message(1234, 10001) == "thr_ext"
