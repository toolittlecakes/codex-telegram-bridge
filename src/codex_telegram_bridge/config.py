from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "codex-telegram-bridge" / "config.toml"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "codex-telegram-bridge" / "state.json"


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    primary_chat_id: int | None = None
    allowed_chat_ids: list[int] = field(default_factory=list)
    api_base_url: str = "https://api.telegram.org"
    processing_reaction: str = "👀"
    done_reaction: str = "✅"
    delete_approval_messages: bool = True


@dataclass(slots=True)
class CodexConfig:
    command: list[str] = field(default_factory=lambda: ["codex", "app-server", "--listen", "stdio://"])
    client_name: str = "codex_telegram_bridge"
    client_title: str = "Codex Telegram Bridge"
    client_version: str = "0.1.0"
    experimental_api: bool = False
    opt_out_notification_methods: list[str] = field(
        default_factory=lambda: [
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
            "item/reasoning/textDelta",
            "item/plan/delta",
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        ]
    )
    thread_start: dict[str, Any] = field(default_factory=dict)
    turn_start_defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BridgeConfig:
    state_path: Path = DEFAULT_STATE_PATH
    poll_external_threads: bool = True
    external_poll_interval_seconds: float = 3.0
    external_poll_limit: int = 100
    external_source_kinds: list[str] = field(
        default_factory=lambda: ["cli", "vscode", "exec", "appServer", "unknown"]
    )
    external_header_template: str = "🧵 External Codex thread\n{preview}\n\n{text}"
    allow_first_private_chat: bool = True
    max_message_chars: int = 3900
    log_level: str = "INFO"


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    codex: CodexConfig = field(default_factory=CodexConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)


def _expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def load_config(path: Path | None) -> AppConfig:
    path = _expand_path(path or DEFAULT_CONFIG_PATH)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    telegram_raw = dict(raw.get("telegram") or {})
    bot_token = telegram_raw.get("bot_token") or os.environ.get("CTB_TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("telegram.bot_token (or CTB_TELEGRAM_BOT_TOKEN) is required")

    telegram = TelegramConfig(
        bot_token=str(bot_token),
        primary_chat_id=_coerce_int_optional(telegram_raw.get("primary_chat_id")),
        allowed_chat_ids=[int(x) for x in telegram_raw.get("allowed_chat_ids", [])],
        api_base_url=str(telegram_raw.get("api_base_url", "https://api.telegram.org")).rstrip("/"),
        processing_reaction=str(telegram_raw.get("processing_reaction", "👀")),
        done_reaction=str(telegram_raw.get("done_reaction", "✅")),
        delete_approval_messages=bool(telegram_raw.get("delete_approval_messages", True)),
    )

    codex_raw = dict(raw.get("codex") or {})
    codex = CodexConfig(
        command=_coerce_command(codex_raw.get("command")),
        client_name=str(codex_raw.get("client_name", "codex_telegram_bridge")),
        client_title=str(codex_raw.get("client_title", "Codex Telegram Bridge")),
        client_version=str(codex_raw.get("client_version", "0.1.0")),
        experimental_api=bool(codex_raw.get("experimental_api", False)),
        opt_out_notification_methods=[str(x) for x in codex_raw.get("opt_out_notification_methods", CodexConfig().opt_out_notification_methods)],
        thread_start=dict(codex_raw.get("thread_start") or {}),
        turn_start_defaults=dict(codex_raw.get("turn_start_defaults") or {}),
    )

    bridge_raw = dict(raw.get("bridge") or {})
    bridge = BridgeConfig(
        state_path=_expand_path(bridge_raw.get("state_path", DEFAULT_STATE_PATH)),
        poll_external_threads=bool(bridge_raw.get("poll_external_threads", True)),
        external_poll_interval_seconds=float(bridge_raw.get("external_poll_interval_seconds", 3.0)),
        external_poll_limit=int(bridge_raw.get("external_poll_limit", 100)),
        external_source_kinds=[str(x) for x in bridge_raw.get("external_source_kinds", BridgeConfig().external_source_kinds)],
        external_header_template=str(
            bridge_raw.get("external_header_template", BridgeConfig().external_header_template)
        ),
        allow_first_private_chat=bool(bridge_raw.get("allow_first_private_chat", True)),
        max_message_chars=int(bridge_raw.get("max_message_chars", 3900)),
        log_level=str(bridge_raw.get("log_level", "INFO")).upper(),
    )

    return AppConfig(telegram=telegram, codex=codex, bridge=bridge)


def ensure_parent_dirs(config: AppConfig) -> None:
    config.bridge.state_path.parent.mkdir(parents=True, exist_ok=True)


EXAMPLE_CONFIG = """# Telegram bot settings.
[telegram]
bot_token = "123456:replace-me"
# Optional: if absent, the first private chat that messages the bot becomes primary.
# primary_chat_id = 123456789
# Optional additional allow-list. If omitted, the bridge uses primary_chat_id / first private chat.
# allowed_chat_ids = [123456789]
processing_reaction = "👀"
done_reaction = "✅"
delete_approval_messages = true

[codex]
command = ["codex", "app-server", "--listen", "stdio://"]
client_name = "codex_telegram_bridge"
client_title = "Codex Telegram Bridge"
client_version = "0.1.0"
experimental_api = false
# You can remove entries here if you want more live noise from Codex.
opt_out_notification_methods = [
  "item/agentMessage/delta",
  "item/reasoning/summaryTextDelta",
  "item/reasoning/summaryPartAdded",
  "item/reasoning/textDelta",
  "item/plan/delta",
  "item/commandExecution/outputDelta",
  "item/fileChange/outputDelta",
]

# Passed to thread/start for brand new threads created by the bot.
[codex.thread_start]
# cwd = "/Users/me/project"
# model = "gpt-5.4"
# approvalPolicy = "unlessTrusted"
# sandbox = "workspaceWrite"
# personality = "friendly"

# Passed to turn/start in addition to input. Usually left empty.
[codex.turn_start_defaults]
# approvalsReviewer = "user"

[bridge]
state_path = "~/.local/state/codex-telegram-bridge/state.json"
poll_external_threads = true
external_poll_interval_seconds = 3.0
external_poll_limit = 100
# Leave this list alone unless your Codex build rejects some source kinds.
external_source_kinds = ["cli", "vscode", "exec", "appServer", "unknown"]
allow_first_private_chat = true
max_message_chars = 3900
log_level = "INFO"
"""


def _coerce_command(value: Any) -> list[str]:
    if value is None:
        return CodexConfig().command
    if not isinstance(value, list) or not value:
        raise ValueError("codex.command must be a non-empty array of strings")
    return [str(x) for x in value]


def _coerce_int_optional(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
