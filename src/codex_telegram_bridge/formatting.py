from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

from telegramify_markdown import convert, split_entities


@dataclass(slots=True)
class RenderedTextChunk:
    text: str
    entities: list[dict[str, Any]]


def chunk_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in text.split("\n"):
        piece = paragraph if not current else "\n" + paragraph
        if current_len + len(piece) <= max_chars:
            current.append(piece)
            current_len += len(piece)
            continue

        if current:
            chunks.append("".join(current).lstrip("\n"))
            current = []
            current_len = 0

        if len(paragraph) <= max_chars:
            current = [paragraph]
            current_len = len(paragraph)
            continue

        start = 0
        while start < len(paragraph):
            end = min(start + max_chars, len(paragraph))
            chunks.append(paragraph[start:end])
            start = end

    if current:
        chunks.append("".join(current).lstrip("\n"))
    return [chunk for chunk in chunks if chunk]


def extract_latest_agent_message_from_turn(turn: dict[str, Any]) -> tuple[str | None, str | None]:
    items = turn.get("items") or []
    for item in reversed(items):
        if item.get("type") == "agentMessage" and item.get("text"):
            return _coerce_item_id(item), str(item.get("text"))
    return None, None


def extract_latest_agent_message_from_thread(thread: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    turns = thread.get("turns") or []
    for turn in reversed(turns):
        item_id, text = extract_latest_agent_message_from_turn(turn)
        if item_id is None or text is None:
            continue
        turn_id = turn.get("id")
        return item_id, str(turn_id) if turn_id is not None else None, text
    return None, None, None


def render_markdown_chunks(markdown: str, max_utf16_len: int) -> list[RenderedTextChunk]:
    text, entities = convert(markdown)
    chunks = split_entities(text, entities, max_utf16_len=max_utf16_len)
    return [
        RenderedTextChunk(
            text=chunk_text,
            entities=[entity.to_dict() for entity in chunk_entities],
        )
        for chunk_text, chunk_entities in chunks
        if chunk_text
    ]


def format_approval_prompt(kind: str, params: dict[str, Any], started_item: dict[str, Any] | None) -> str:
    lines: list[str] = []
    reason = params.get("reason")

    if kind == "command":
        lines.append("Approval needed: command execution")
        if reason:
            lines.append(f"Reason: {reason}")
        cwd = params.get("cwd") or (started_item or {}).get("cwd")
        if cwd:
            lines.append(f"CWD: {cwd}")
        command = params.get("command") or (started_item or {}).get("command")
        if command:
            lines.extend(["", "Command:", str(command)])
        actions = params.get("commandActions") or (started_item or {}).get("commandActions")
        rendered = _render_iterable(actions)
        if rendered:
            lines.extend(["", "Actions:", rendered])
        return "\n".join(lines).strip()

    if kind == "file":
        lines.append("Approval needed: file change")
        if reason:
            lines.append(f"Reason: {reason}")
        changes = (started_item or {}).get("changes") or []
        rendered_paths = [str(change.get("path")) for change in changes if change.get("path")]
        if rendered_paths:
            lines.extend(["", "Files:"])
            lines.extend(f"• {path}" for path in rendered_paths[:20])
        return "\n".join(lines).strip()

    if kind == "permissions":
        lines.append("Approval needed: additional permissions")
        if reason:
            lines.append(f"Reason: {reason}")
        permissions = params.get("permissions") or {}
        fs_write = (((permissions.get("fileSystem") or {}).get("write")) or [])
        network_enabled = ((permissions.get("network") or {}).get("enabled"))
        if fs_write:
            lines.extend(["", "Filesystem write:"])
            lines.extend(f"• {path}" for path in fs_write)
        if network_enabled:
            lines.extend(["", "Network:", "• enabled"])
        return "\n".join(lines).strip()

    return "Approval needed"



def format_turn_failure(turn: dict[str, Any]) -> str:
    status = turn.get("status") or "failed"
    error = turn.get("error") or {}
    message = error.get("message")
    if message:
        return f"Codex finished with status {status}: {message}"
    return f"Codex finished with status {status}."



def _render_iterable(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Iterable):
        rendered = [str(item) for item in value if item is not None]
        if rendered:
            return "\n".join(f"• {item}" for item in rendered)
    return None


def _coerce_item_id(item: dict[str, Any]) -> str | None:
    item_id = item.get("id")
    return str(item_id) if item_id is not None else None
