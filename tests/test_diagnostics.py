from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import codex_telegram_bridge.diagnostics as diagnostics
from codex_telegram_bridge.cli import build_parser
from codex_telegram_bridge.config import AppConfig, BridgeConfig, DesktopConfig, TelegramConfig
from codex_telegram_bridge.desktop_client import (
    DesktopConversation,
    DesktopConversationSummary,
    DesktopProject,
    DesktopRequest,
    DesktopSessionInfo,
    DesktopTurn,
)
from codex_telegram_bridge.state import BridgeState


@dataclass
class FakeTelegram:
    async def get_me(self) -> dict[str, Any]:
        return {"id": 777, "username": "bridge_test_bot", "is_bot": True}

    async def close(self) -> None:
        return None


@dataclass
class FakeDesktop:
    screenshot_payload: bytes = b"png"

    async def start(self) -> DesktopSessionInfo:
        return DesktopSessionInfo(
            debugger_url="ws://127.0.0.1:9239/devtools/page/1",
            page_url="app://-/index.html",
            page_title="Codex",
        )

    async def wait_until_task_index_ready(self) -> None:
        return None

    async def snapshot(self) -> dict[str, Any]:
        return {
            "current_thread_id": "thr_1",
            "composer": {"ok": True, "text": "hello"},
            "visible_buttons": ["Approve", "Deny"],
            "projects": [DesktopProject(label="repo", path="/repo")],
            "threads": [
                DesktopConversationSummary(
                    thread_id="thr_1",
                    title="Thread 1",
                    current=True,
                    cwd="/repo",
                    project_label="repo",
                    project_path="/repo",
                    updated_at=datetime(2026, 4, 9, 13, 0),
                )
            ],
            "current_thread": DesktopConversation(
                thread_id="thr_1",
                title="Thread 1",
                cwd="/repo",
                host_id="local",
                source="desktop",
                turns=[
                    DesktopTurn(
                        turn_id="turn_1",
                        status="completed",
                        items=[{"type": "agentMessage", "text": "done"}],
                        raw={"turnId": "turn_1", "status": "completed"},
                    )
                ],
                requests=[
                    DesktopRequest(
                        request_id="req_1",
                        kind="command",
                        raw={"command": "pytest -q"},
                    )
                ],
            ),
        }

    async def capture_screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.screenshot_payload)
        return path

    async def close(self) -> None:
        return None


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(bot_token="token", primary_chat_id=1234, allowed_chat_ids=[1234]),
        desktop=DesktopConfig(),
        bridge=BridgeConfig(state_path=tmp_path / "state.json"),
    )


@pytest.mark.asyncio
async def test_collect_doctor_report_includes_telegram_and_desktop_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    state = BridgeState(primary_chat_id=1234)

    monkeypatch.setattr(diagnostics, "build_telegram_api", lambda _: FakeTelegram())
    monkeypatch.setattr(diagnostics, "build_desktop_client", lambda _: FakeDesktop())

    report = await diagnostics.collect_doctor_report(config, state)

    assert report["ok"] is True
    assert report["telegram"] == {
        "ok": True,
        "bot_id": 777,
        "username": "bridge_test_bot",
        "is_bot": True,
    }
    assert report["desktop"]["ok"] is True
    assert report["desktop"]["project_count"] == 1
    assert report["desktop"]["thread_count"] == 1
    assert report["desktop"]["current_thread_id"] == "thr_1"
    assert report["bridge"]["lock"]["available"] is True


@pytest.mark.asyncio
async def test_collect_desktop_snapshot_serializes_current_thread_and_writes_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    screenshot_path = tmp_path / "artifacts" / "desktop.png"

    monkeypatch.setattr(diagnostics, "build_desktop_client", lambda _: FakeDesktop(screenshot_payload=b"image-bytes"))

    snapshot = await diagnostics.collect_desktop_snapshot(config, screenshot_path=screenshot_path)

    assert snapshot["ok"] is True
    assert snapshot["current_thread_id"] == "thr_1"
    assert snapshot["composer"] == {"ok": True, "text": "hello"}
    assert snapshot["visible_buttons"] == ["Approve", "Deny"]
    assert snapshot["projects"] == [{"label": "repo", "path": "/repo"}]
    assert snapshot["threads"][0]["updated_at"] == "2026-04-09T13:00:00"
    assert snapshot["current_thread"]["requests"] == [
        {
            "request_id": "req_1",
            "kind": "command",
            "raw": {"command": "pytest -q"},
        }
    ]
    assert screenshot_path.read_bytes() == b"image-bytes"


def test_build_parser_supports_doctor_and_desktop_snapshot_commands() -> None:
    parser = build_parser()

    doctor_args = parser.parse_args(["doctor", "--config", "/tmp/config.toml"])
    snapshot_args = parser.parse_args(["desktop-snapshot", "--config", "/tmp/config.toml", "--screenshot", "/tmp/desktop.png"])

    assert doctor_args.command == "doctor"
    assert doctor_args.config == Path("/tmp/config.toml")
    assert snapshot_args.command == "desktop-snapshot"
    assert snapshot_args.screenshot == Path("/tmp/desktop.png")
