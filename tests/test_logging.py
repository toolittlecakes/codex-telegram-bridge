from __future__ import annotations

import logging
from pathlib import Path

from codex_telegram_bridge.config import BridgeConfig, load_config
from codex_telegram_bridge.logging_setup import configure_logging


def test_bridge_config_derives_log_paths_from_state_path(tmp_path: Path) -> None:
    config = BridgeConfig(state_path=tmp_path / "runtime" / "state.json")

    assert config.log_path == tmp_path / "runtime" / "logs" / "bridge.log"
    assert config.protocol_log_path == tmp_path / "runtime" / "logs" / "protocol.log"


def test_load_config_infers_log_defaults_from_state_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token = "token"

[bridge]
state_path = "{tmp_path / 'custom-state' / 'state.json'}"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.bridge.log_path == (tmp_path / "custom-state" / "logs" / "bridge.log").resolve()
    assert config.bridge.protocol_log_path == (tmp_path / "custom-state" / "logs" / "protocol.log").resolve()
    assert config.bridge.console_log is False


def test_configure_logging_routes_operational_and_protocol_logs_to_separate_files(tmp_path: Path) -> None:
    config = BridgeConfig(
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "logs" / "bridge.log",
        protocol_log_path=tmp_path / "logs" / "protocol.log",
        console_log=False,
    )
    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    configure_logging(config)

    bridge_logger = logging.getLogger("codex_telegram_bridge.bridge")
    protocol_logger = logging.getLogger("codex_telegram_bridge.codex_rpc")

    bridge_logger.info("bridge started")
    protocol_logger.debug("rpc request")
    protocol_logger.warning("rpc warning")

    for handler in logging.getLogger().handlers:
        handler.flush()
    for handler in protocol_logger.handlers:
        handler.flush()

    bridge_log = config.log_path.read_text(encoding="utf-8")
    protocol_log = config.protocol_log_path.read_text(encoding="utf-8")

    assert "bridge started" in bridge_log
    assert "rpc warning" in bridge_log
    assert "rpc request" not in bridge_log

    assert "rpc request" in protocol_log
    assert "rpc warning" in protocol_log
