from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "codex-telegram-bridge" / "config.toml"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "codex-telegram-bridge" / "state.json"
DEFAULT_DESKTOP_APP_PATH = Path("/Applications/Codex.app")
DEFAULT_DESKTOP_USER_DATA_DIR = Path.home() / "Library" / "Application Support" / "com.openai.chat"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 7


def default_log_dir(state_path: Path) -> Path:
    return state_path.parent / "logs"


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    primary_chat_id: int | None = None
    allowed_chat_ids: list[int] = field(default_factory=list)
    api_base_url: str = "https://api.telegram.org"
    processing_reaction: str = "👀"
    done_reaction: str = "👌"
    delete_approval_messages: bool = True


@dataclass(slots=True)
class DesktopConfig:
    app_path: Path = DEFAULT_DESKTOP_APP_PATH
    user_data_dir: Path = DEFAULT_DESKTOP_USER_DATA_DIR
    remote_debugging_port: int = 9229
    launch_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 1.0


@dataclass(slots=True)
class BridgeConfig:
    state_path: Path = DEFAULT_STATE_PATH
    allow_first_private_chat: bool = True
    max_message_chars: int = 3900
    log_level: str = "INFO"
    protocol_log_level: str = "DEBUG"
    log_path: Path | None = None
    protocol_log_path: Path | None = None
    console_log: bool = False
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    def __post_init__(self) -> None:
        if self.log_path is None:
            self.log_path = default_log_dir(self.state_path) / "bridge.log"
        if self.protocol_log_path is None:
            self.protocol_log_path = default_log_dir(self.state_path) / "protocol.log"


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    desktop: DesktopConfig
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
    if _looks_like_placeholder_token(str(bot_token)):
        raise ValueError("telegram.bot_token still contains the example placeholder; set your real bot token")

    telegram = TelegramConfig(
        bot_token=str(bot_token),
        primary_chat_id=_coerce_int_optional(telegram_raw.get("primary_chat_id")),
        allowed_chat_ids=[int(x) for x in telegram_raw.get("allowed_chat_ids", [])],
        api_base_url=str(telegram_raw.get("api_base_url", "https://api.telegram.org")).rstrip("/"),
        processing_reaction=str(telegram_raw.get("processing_reaction", "👀")),
        done_reaction=str(telegram_raw.get("done_reaction", "👌")),
        delete_approval_messages=bool(telegram_raw.get("delete_approval_messages", True)),
    )

    desktop_raw = dict(raw.get("desktop") or {})
    desktop = DesktopConfig(
        app_path=_expand_path(desktop_raw.get("app_path", DEFAULT_DESKTOP_APP_PATH)),
        user_data_dir=_expand_path(desktop_raw.get("user_data_dir", DEFAULT_DESKTOP_USER_DATA_DIR)),
        remote_debugging_port=int(desktop_raw.get("remote_debugging_port", 9229)),
        launch_timeout_seconds=float(desktop_raw.get("launch_timeout_seconds", 30.0)),
        poll_interval_seconds=float(desktop_raw.get("poll_interval_seconds", 1.0)),
    )

    bridge_raw = dict(raw.get("bridge") or {})
    state_path = _expand_path(bridge_raw.get("state_path", DEFAULT_STATE_PATH))
    log_dir = default_log_dir(state_path)
    bridge = BridgeConfig(
        state_path=state_path,
        allow_first_private_chat=bool(bridge_raw.get("allow_first_private_chat", True)),
        max_message_chars=int(bridge_raw.get("max_message_chars", 3900)),
        log_level=str(bridge_raw.get("log_level", "INFO")).upper(),
        protocol_log_level=str(bridge_raw.get("protocol_log_level", "DEBUG")).upper(),
        log_path=_expand_path(bridge_raw.get("log_path", log_dir / "bridge.log")),
        protocol_log_path=_expand_path(bridge_raw.get("protocol_log_path", log_dir / "protocol.log")),
        console_log=bool(bridge_raw.get("console_log", False)),
        log_max_bytes=int(bridge_raw.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
        log_backup_count=int(bridge_raw.get("log_backup_count", DEFAULT_LOG_BACKUP_COUNT)),
    )

    return AppConfig(telegram=telegram, desktop=desktop, bridge=bridge)


def ensure_parent_dirs(config: AppConfig) -> None:
    config.bridge.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.bridge.log_path.parent.mkdir(parents=True, exist_ok=True)
    config.bridge.protocol_log_path.parent.mkdir(parents=True, exist_ok=True)


EXAMPLE_CONFIG = """# Telegram bot settings.
[telegram]
bot_token = "123456:replace-me"
# Optional: if absent, the first private chat that messages the bot becomes primary.
# primary_chat_id = 123456789
# Optional additional allow-list. If omitted, the bridge uses primary_chat_id / first private chat.
# allowed_chat_ids = [123456789]
processing_reaction = "👀"
done_reaction = "👌"
delete_approval_messages = true

[desktop]
app_path = "/Applications/Codex.app"
user_data_dir = "~/Library/Application Support/com.openai.chat"
remote_debugging_port = 9229
launch_timeout_seconds = 30
poll_interval_seconds = 1

[bridge]
state_path = "~/.local/state/codex-telegram-bridge/state.json"
allow_first_private_chat = true
max_message_chars = 3900
log_level = "INFO"
protocol_log_level = "DEBUG"
log_path = "~/.local/state/codex-telegram-bridge/logs/bridge.log"
protocol_log_path = "~/.local/state/codex-telegram-bridge/logs/protocol.log"
console_log = false
log_max_bytes = 10485760
log_backup_count = 7
"""


def _coerce_int_optional(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _looks_like_placeholder_token(value: str) -> bool:
    token = value.strip()
    return token == "123456:replace-me" or "replace-me" in token.lower()
