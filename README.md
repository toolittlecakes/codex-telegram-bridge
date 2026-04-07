# codex-telegram-bridge

A minimal **reply-chain Telegram bridge** for **Codex app-server**.

The goal is simple:

- every **new Telegram message without a reply** starts a **new Codex thread**;
- every **reply** to a bot/user message already bound to a Codex thread goes back to the **same thread**;
- while a turn is running, follow-up user messages are sent as **`turn/steer`** when possible;
- the bot sets **👀** on the user message when it is accepted for work and **✅** when the turn finishes;
- approvals are surfaced as **reply messages with inline buttons** (**Approve** / **Deny**) and the prompt is deleted after the user clicks it.

This project intentionally stays small:

- **no database** (`state.json` only)
- **no slash commands in the bot UX**
- **no fork flow**
- **no aiogram / pyrogram** — plain Telegram Bot API over HTTP
- **no direct parsing of Codex session files** as the primary control plane

## Current behavior

### Bot-created / bridge-controlled threads

For threads started from Telegram (or later resumed by the bridge), the bridge uses **live app-server events**.

### External CLI / Desktop threads

An optional poller uses `thread/list` + `thread/read` to detect new terminal turns and forward final answers into Telegram.
This is **best effort**. It works well for passive mirroring and lightweight takeovers, but it does **not** promise perfect live control over an already-running TUI/Desktop turn.

## Requirements

- Python **3.11+**
- `codex` available on your `PATH`
- a Telegram bot token
- access to the same local Codex home / sessions as the Codex instance you want to mirror/control

## Install

### Local editable install

```bash
uv pip install -e .
```

### Install as a tool with `uv`

```bash
uv tool install .
```

That gives you a console command:

```bash
codex-telegram-bridge --help
```

### Install from Git

Once this repository is on GitHub:

```bash
uv tool install git+https://github.com/<you>/codex-telegram-bridge
```

## Quick start

Create a config file:

```bash
codex-telegram-bridge init-config --path ~/.config/codex-telegram-bridge/config.toml
```

Edit it and set at least:

- `telegram.bot_token`
- optionally `telegram.primary_chat_id`
- optionally `codex.thread_start.cwd`
- optionally `codex.thread_start.approvalPolicy`
- optionally `codex.thread_start.sandbox`

Then run:

```bash
codex-telegram-bridge run --config ~/.config/codex-telegram-bridge/config.toml
```

If `telegram.primary_chat_id` is omitted and `bridge.allow_first_private_chat = true`, the first private chat that messages the bot becomes the primary chat.

## Example config

See [`config.example.toml`](./config.example.toml).

## UX model

### Routing

- **New message** → start a new thread
- **Reply to any known thread message** → route back to that thread
- **Bot replies to your message** → keeps the whole thread as a single Telegram reply chain

### Reactions

- **👀** = accepted / in progress
- **✅** = turn reached a terminal state and the bridge finished handling it

### Approvals

When Codex requests approval for:

- command execution
- file changes
- permission grants

…the bridge sends a **reply message** in Telegram with buttons:

- **Approve**
- **Deny**

When the button is pressed:

- the bridge answers the app-server request
- the approval message is deleted

## Commands

```bash
codex-telegram-bridge init-config [--path PATH]
codex-telegram-bridge run [--config PATH]
```

## State model

All persistent state lives in a single JSON file (by default):

```text
~/.local/state/codex-telegram-bridge/state.json
```

It stores:

- Telegram update offset
- primary chat id
- Telegram message → Codex thread bindings
- thread metadata
- last delivered items
- pending / queued user messages

## Limitations

- The bridge is optimized for **one-user private chat** usage.
- External session mirroring is **best effort**.
- `tool/requestUserInput` and MCP elicitation requests are only surfaced as “handle locally” notices for now.
- The bridge does not implement a rich approval matrix; buttons are intentionally **Approve** / **Deny** only.
- It does not try to become a perfect multi-client coordinator for Desktop + CLI + bot at the same time.

## Development

Run tests:

```bash
PYTHONPATH=src pytest
```

Run locally without installing:

```bash
PYTHONPATH=src python -m codex_telegram_bridge run --config ./config.example.toml
```

## Packaging and publishing

Build distributions with `uv`:

```bash
uv build
```

Publish when you are ready:

```bash
uv publish
```

Or install directly from a Git repo as a tool:

```bash
uv tool install git+https://github.com/<you>/codex-telegram-bridge
```

## License

MIT
