from __future__ import annotations

import logging
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from .config import BridgeConfig


@dataclass(frozen=True, slots=True)
class LoggingSetupResult:
    log_path: str
    protocol_log_path: str


def configure_logging(config: BridgeConfig) -> LoggingSetupResult:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    _reset_logger(root)
    root.setLevel(logging.DEBUG)

    bridge_handler = RotatingFileHandler(
        config.log_path,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    bridge_handler.setLevel(_coerce_level(config.log_level))
    bridge_handler.setFormatter(formatter)
    root.addHandler(bridge_handler)

    if config.console_log:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(_coerce_level(config.log_level))
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    protocol_logger = logging.getLogger("codex_telegram_bridge.codex_rpc")
    _reset_logger(protocol_logger)
    protocol_logger.setLevel(_coerce_level(config.protocol_log_level))
    protocol_logger.propagate = True

    protocol_handler = RotatingFileHandler(
        config.protocol_log_path,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    protocol_handler.setLevel(_coerce_level(config.protocol_log_level))
    protocol_handler.setFormatter(formatter)
    protocol_logger.addHandler(protocol_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return LoggingSetupResult(
        log_path=str(config.log_path),
        protocol_log_path=str(config.protocol_log_path),
    )


def _coerce_level(raw: str) -> int:
    level = getattr(logging, raw.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Unsupported log level: {raw}")
    return level


def _reset_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

