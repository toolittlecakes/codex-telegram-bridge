# User Flows

This file is the canonical checklist for product-level verification of the Telegram ↔ Codex Desktop bridge.

## Coverage Matrix

| Flow | User action | Expected behavior | Automated coverage | Live status on 2026-04-09 | Notes |
| --- | --- | --- | --- | --- | --- |
| Startup | Start bridge with valid config | Telegram auth works, state is readable, Codex Desktop is reachable, projects are visible | `uv run pytest`, `uv run tests/e2e/run_smoke.py assert startup` | Passed | Baseline health check before any live flow |
| New thread | Send a fresh top-level Telegram message and pick a project | Bot shows project picker, selected project starts a new Codex thread, Telegram chain binds to that thread | Unit coverage in `tests/test_bridge.py`, live assert `new-thread` | Passed earlier in this session; dirty new-chat drafts now surface an explicit `Replace` / `Cancel` prompt | Remaining live check: re-run the cold path once against a real stale draft and confirm `Replace` clears it end-to-end |
| Reply to known chain | Reply in Telegram to a bot message already bound to a Desktop thread | Same Desktop thread gets the same user text as a new user turn | Unit coverage in `tests/test_bridge.py`, live assert `reply` | Passed | Re-verified live after fixing approval callback/reply-chain state handling on text `E2E REPLY AFTER APPROVAL FIX 20260409T094700Z` |
| Queue while busy | Reply again while the same Desktop thread is still running a turn | Input is queued in state or replayed into the same thread after the current turn completes | Unit coverage in `tests/test_bridge.py`, live assert `queue` | Passed | Live run verified the replayed path: a busy Desktop turn finished, then the queued Telegram reply was delivered into the same thread |
| Attach by id | Send `attach <thread_id>` | Existing Desktop thread binds to Telegram and latest assistant message is sent into the chain | Unit coverage in `tests/test_bridge.py`, live assert `attach` | Passed | Verified end-to-end on thread `019d7151-d3f2-7bc0-ada2-10796fac8f84` |
| Attach picker | Send `attach` without an id | Bot sends a picker of recent visible Desktop sessions | Unit coverage in `tests/test_bridge.py`, live DOM check in Telegram Web | Passed | Verified that Telegram showed recent sessions and inline buttons |
| Detach by id | Send `detach <thread_id>` | Thread binding is removed and Telegram stops receiving updates for that Desktop thread | Unit coverage in `tests/test_bridge.py`, live assert `detach` | Passed | Verified end-to-end on thread `019d7151-d3f2-7bc0-ada2-10796fac8f84` |
| Detach by reply | Reply `detach` to a bound chain message | Same as explicit detach, but reply target resolves the thread | Unit coverage in `tests/test_bridge.py` | Passed | Live run used Telegram Web reply-mode and successfully detached the bound thread |
| Approval surfacing | Trigger a Desktop approval request | Bot surfaces approval controls in the Telegram chain for the same Desktop thread | Unit coverage in `tests/test_bridge.py`, live assert `approval` | Passed | Live run produced a real approval request and Telegram showed `Approve` / `Deny` buttons |
| Approval action | Click `Approve` or `Deny` in Telegram | Callback resolves the Desktop approval prompt and the Telegram approval message is cleaned up | Unit coverage in `tests/test_bridge.py` | Passed | Re-verified live with `Deny`: Telegram callback cleared the Desktop request and the bridge advanced the Telegram offset instead of replaying the stale callback |
| Restart and recovery | Restart bridge while state already exists | State reloads, thread bindings survive, polling resumes without duplicate sync | Unit coverage around state/load behavior, live restart + existing-chain reply probe | Passed | Re-verified live on text `E2E REPLY AFTER RESTART VALID TARGET 20260409T102000Z`: after restart, replying to the last still-bound bridge message delivered the text into the same recovered Desktop thread |

## Recommended Live Smoke Order

1. `startup`
2. `new-thread`
3. `attach <thread_id>`
4. `reply`
5. `detach <thread_id>`
6. `attach`
7. `queue`
8. `approval`
9. restart/recovery

## Commands

Start the bridge and capture artifacts:

```bash
uv run tests/e2e/run_smoke.py start --config ./.e2e/config.e2e.toml
```

Write the deterministic scenario plan:

```bash
uv run tests/e2e/run_smoke.py plan --config ./.e2e/config.e2e.toml
```

Run startup health verification:

```bash
uv run tests/e2e/run_smoke.py assert startup --config ./.e2e/config.e2e.toml --artifacts-dir ./.e2e/artifacts/latest
```

Run a live reply assertion against an attached thread:

```bash
uv run tests/e2e/run_smoke.py assert reply \
  --config ./.e2e/config.e2e.toml \
  --artifacts-dir ./.e2e/artifacts/latest \
  --thread-id <thread_id> \
  --text "E2E REPLY <run-id>"
```

## Current Risks

- The new dirty-draft path is covered by unit tests, but still needs one live end-to-end rerun with a real stale `New chat` draft in Codex Desktop.
- Manual Telegram reruns must reply to a still-bound bridge message. Replying to later bridge error messages is expected to return `This reply target is not bound to a known bridge thread`.
