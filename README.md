# codex-telegram-bridge

A minimal **reply-chain Telegram bridge** for **Codex Desktop**.

The bridge launches or connects to a local `Codex.app` instance with Electron remote debugging enabled, drives the composer through the renderer DOM, and reads thread state from the same renderer via CDP.

## Behavior

- every **new Telegram message without a reply** opens a **project picker** in Telegram, then starts a **new Codex Desktop thread** in the selected project
- every **reply** to a known Telegram chain goes back to the **same Desktop thread**
- `attach <thread_id>` binds an existing visible Desktop thread to the current Telegram chat and sends its latest assistant message
- `attach` without an id opens a picker of recent Desktop sessions in Telegram
- `detach <thread_id>` or reply-`detach` stops Telegram sync for that Desktop thread and removes its bindings
- while a turn is running, extra Telegram inputs are **queued** and sent after the current turn finishes
- the bot sets **👀** on accepted user messages and **👌** when the related turn reaches a terminal state
- assistant Markdown is converted with `telegramify-markdown`
- Desktop approval prompts are surfaced in Telegram with **Approve** / **Deny** buttons

## Requirements

- Python **3.11+**
- local **Codex Desktop** installed at `/Applications/Codex.app` or another configured path
- a Telegram bot token
- access to the same local Codex Desktop profile / sessions you want the bridge to control

## Install

### Local editable install

```bash
uv pip install -e .
```

### Install as a tool with `uv`

```bash
uv tool install .
```

That gives you:

```bash
codex-telegram-bridge --help
```

## Quick start

Create a config file:

```bash
codex-telegram-bridge init-config --path ~/.config/codex-telegram-bridge/config.toml
```

Set at least:

- `telegram.bot_token`
- optionally `desktop.app_path`
- optionally `desktop.user_data_dir`

Then run:

```bash
codex-telegram-bridge run --config ~/.config/codex-telegram-bridge/config.toml
```

If `telegram.primary_chat_id` is omitted and `bridge.allow_first_private_chat = true`, the first private chat that messages the bot becomes the primary chat.

## Example config

See [`config.example.toml`](./config.example.toml).

## UX model

- **New message** → choose a Desktop project in Telegram, then create a new Desktop thread there
- **Reply to a known thread message** → same Desktop thread
- **`attach <thread_id>`** → make Telegram the active continuation point for an existing Desktop thread
- **`detach`** → stop syncing a previously attached thread
- **Bot replies to your message** → the whole conversation stays in one Telegram reply chain

## Commands

```bash
codex-telegram-bridge init-config [--path PATH]
codex-telegram-bridge run [--config PATH]
```

Telegram control commands:

```text
attach <thread_id>
attach
detach <thread_id>
detach
```

## State

Persistent state lives in:

```text
~/.local/state/codex-telegram-bridge/state.json
```

It stores:

- Telegram update offset
- primary chat id
- Telegram message → Desktop thread bindings
- last delivered thread items
- queued user inputs

## Limitations

- The bridge is optimized for **one-user private chat** usage.
- It assumes **Codex Desktop is the only real writer** for the controlled threads.
- Do not run a second plain Codex Desktop instance against the same profile while the bridge-owned instance is active.
- Queueing replaces the old app-server `turn/steer` path.
- The bridge only sees threads that are visible through the current Desktop renderer state.
- The DOM / React contract is private Codex Desktop implementation detail, so upstream UI changes can break the bridge.

## Development

Run tests:

```bash
uv run pytest
```

Run locally without installing:

```bash
PYTHONPATH=src uv run python -m codex_telegram_bridge.cli run --config ./config.example.toml
```
