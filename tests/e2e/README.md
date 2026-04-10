# E2E Harness

This directory contains the local smoke-test harness for the real Telegram + Codex Desktop loop.

## Prerequisites

- Copy [`config.e2e.example.toml`](../../config.e2e.example.toml) to a real local config and fill:
  - `telegram.bot_token`
  - `telegram.primary_chat_id`
  - `telegram.allowed_chat_ids`
  - dedicated `desktop.user_data_dir`
- Log the dedicated Telegram test account into Telegram Web with a persistent browser profile outside git.
- Use a dedicated Codex Desktop profile and a dedicated test project visible in the sidebar.

## Commands

Start the bridge in the background and wait until Telegram + Codex are both reachable:

```bash
uv run tests/e2e/run_smoke.py start --config ./config.e2e.toml
```

Capture the latest machine-readable artifacts:

```bash
uv run tests/e2e/run_smoke.py snapshot --config ./config.e2e.toml
```

Write the deterministic scenario plan for the current run:

```bash
uv run tests/e2e/run_smoke.py plan --config ./config.e2e.toml
```

Run a live assertion after driving Telegram Web externally:

```bash
uv run tests/e2e/run_smoke.py assert new-thread --config ./config.e2e.toml --text "E2E NEW THREAD 20260409T000000Z"
uv run tests/e2e/run_smoke.py assert attach --config ./config.e2e.toml --thread-id thr_123
```

Stop the background bridge process:

```bash
uv run tests/e2e/run_smoke.py stop
```

## Artifacts

The harness writes into `.e2e/artifacts/latest/` by default:

- `bridge.log`
- `doctor.json`
- `desktop_snapshot.json`
- `desktop.png`
- `state.json`
- `scenario_plan.json`
- `last_assertion.json`

`bridge.log` here is the harness-captured console stream. The bridge's rotating operational logs live under `.e2e/state/logs/`.

These files are intentionally machine-readable so an external Telegram Web driver such as `agent-browser` can use them in a closed feedback loop.
