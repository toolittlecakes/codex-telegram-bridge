from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


@dataclass(slots=True)
class QueuedInput:
    chat_id: int
    message_id: int
    text: str


@dataclass(slots=True)
class ThreadState:
    thread_id: str
    primary_chat_id: int | None = None
    last_chain_message_id: int | None = None
    last_delivered_turn_id: str | None = None
    last_delivered_item_id: str | None = None
    last_handled_user_input_key: str | None = None
    current_turn_id: str | None = None
    pending_message_ids: list[int] = field(default_factory=list)
    queued_inputs: list[QueuedInput] = field(default_factory=list)
    preview: str | None = None


@dataclass(slots=True)
class ApprovalCleanupMessage:
    chat_id: int
    message_id: int


@dataclass(slots=True)
class BridgeState:
    version: int = 3
    telegram_update_offset: int = 0
    next_callback_key: int = 0
    primary_chat_id: int | None = None
    message_bindings: dict[str, str] = field(default_factory=dict)
    threads: dict[str, ThreadState] = field(default_factory=dict)
    approval_cleanup_messages: list[ApprovalCleanupMessage] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "BridgeState":
        if not path.exists():
            return BridgeState()
        raw = json.loads(path.read_text(encoding="utf-8"))
        threads: dict[str, ThreadState] = {}
        for thread_id, thread_raw in (raw.get("threads") or {}).items():
            queued = [QueuedInput(**item) for item in thread_raw.get("queued_inputs", [])]
            threads[thread_id] = ThreadState(
                thread_id=thread_id,
                primary_chat_id=thread_raw.get("primary_chat_id"),
                last_chain_message_id=thread_raw.get("last_chain_message_id"),
                last_delivered_turn_id=thread_raw.get("last_delivered_turn_id"),
                last_delivered_item_id=thread_raw.get("last_delivered_item_id"),
                last_handled_user_input_key=thread_raw.get("last_handled_user_input_key"),
                current_turn_id=thread_raw.get("current_turn_id"),
                pending_message_ids=list(thread_raw.get("pending_message_ids", [])),
                queued_inputs=queued,
                preview=thread_raw.get("preview"),
            )
        cleanup = [ApprovalCleanupMessage(**item) for item in raw.get("approval_cleanup_messages", [])]
        return BridgeState(
            version=int(raw.get("version", 1)),
            telegram_update_offset=int(raw.get("telegram_update_offset", 0)),
            next_callback_key=int(raw.get("next_callback_key", 0)),
            primary_chat_id=raw.get("primary_chat_id"),
            message_bindings={str(k): str(v) for k, v in (raw.get("message_bindings") or {}).items()},
            threads=threads,
            approval_cleanup_messages=cleanup,
        )

    def save(self, path: Path) -> None:
        payload = {
            "version": self.version,
            "telegram_update_offset": self.telegram_update_offset,
            "next_callback_key": self.next_callback_key,
            "primary_chat_id": self.primary_chat_id,
            "message_bindings": self.message_bindings,
            "threads": {
                thread_id: {
                    "primary_chat_id": state.primary_chat_id,
                    "last_chain_message_id": state.last_chain_message_id,
                    "last_delivered_turn_id": state.last_delivered_turn_id,
                    "last_delivered_item_id": state.last_delivered_item_id,
                    "last_handled_user_input_key": state.last_handled_user_input_key,
                    "current_turn_id": state.current_turn_id,
                    "pending_message_ids": state.pending_message_ids,
                    "queued_inputs": [asdict(item) for item in state.queued_inputs],
                    "preview": state.preview,
                }
                for thread_id, state in self.threads.items()
            },
            "approval_cleanup_messages": [asdict(item) for item in self.approval_cleanup_messages],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        tmp_path.replace(path)

    def get_or_create_thread(self, thread_id: str) -> ThreadState:
        if thread_id not in self.threads:
            self.threads[thread_id] = ThreadState(thread_id=thread_id)
        return self.threads[thread_id]

    def bind_message(self, chat_id: int, message_id: int, thread_id: str) -> None:
        self.message_bindings[self._message_key(chat_id, message_id)] = thread_id
        thread = self.get_or_create_thread(thread_id)
        thread.primary_chat_id = thread.primary_chat_id or chat_id
        thread.last_chain_message_id = message_id

    def lookup_thread_for_message(self, chat_id: int, message_id: int) -> str | None:
        return self.message_bindings.get(self._message_key(chat_id, message_id))

    def remove_thread(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)
        self.message_bindings = {
            message_key: bound_thread_id
            for message_key, bound_thread_id in self.message_bindings.items()
            if bound_thread_id != thread_id
        }

    @staticmethod
    def _message_key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def reset_ephemeral_runtime_state(self) -> None:
        for thread in self.threads.values():
            thread.current_turn_id = None


__all__ = [
    "ApprovalCleanupMessage",
    "BridgeState",
    "QueuedInput",
    "ThreadState",
]
