from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from .bridge import BridgeApp
from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG, ensure_parent_dirs, load_config
from .desktop_client import DesktopClientError
from .state import BridgeState
from .telegram_api import TelegramApiError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-telegram-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the Telegram bridge")
    run_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config TOML (default: {DEFAULT_CONFIG_PATH})",
    )

    init_parser = subparsers.add_parser("init-config", help="Print an example config to stdout")
    init_parser.add_argument(
        "--path",
        type=Path,
        help="Optional path to write instead of printing to stdout",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        if args.path:
            args.path.parent.mkdir(parents=True, exist_ok=True)
            args.path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
            print(f"Wrote example config to {args.path}")
        else:
            print(EXAMPLE_CONFIG)
        return

    if args.command == "run":
        try:
            asyncio.run(_run(args.config))
        except (ValueError, TelegramApiError, DesktopClientError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        except FileNotFoundError as exc:
            missing = exc.filename or "required command"
            print(f"Error: failed to start {missing}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    parser.error(f"Unknown command: {args.command}")


async def _run(config_path: Path) -> None:
    config = load_config(config_path)
    ensure_parent_dirs(config)
    logging.basicConfig(
        level=getattr(logging, config.bridge.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    state = BridgeState.load(config.bridge.state_path)
    state.reset_ephemeral_runtime_state()
    app = BridgeApp(config, state)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(app.stop()))

    await app.run()


if __name__ == "__main__":  # pragma: no cover
    main()
