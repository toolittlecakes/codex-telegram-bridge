from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    name: str
    description: str
    prompt_template: str | None
    assertion: str
    required_args: tuple[str, ...] = ()


SCENARIOS: tuple[ScenarioDefinition, ...] = (
    ScenarioDefinition(
        name="startup",
        description="Bridge is running, Telegram auth is valid, and Codex Desktop is reachable.",
        prompt_template=None,
        assertion="startup",
    ),
    ScenarioDefinition(
        name="new-thread",
        description="Send a new top-level Telegram message and expect a new Codex thread to contain the same user text.",
        prompt_template="E2E NEW THREAD {run_id}",
        assertion="new-thread",
        required_args=("text",),
    ),
    ScenarioDefinition(
        name="reply",
        description="Reply in Telegram to an existing chain and expect the target Codex thread to receive the same user text.",
        prompt_template="E2E REPLY {run_id}",
        assertion="reply",
        required_args=("thread_id", "text"),
    ),
    ScenarioDefinition(
        name="queue",
        description="Reply while a turn is active and expect the bridge to keep or replay the queued input for the thread.",
        prompt_template="E2E QUEUE {run_id}",
        assertion="queue",
        required_args=("thread_id", "text"),
    ),
    ScenarioDefinition(
        name="attach",
        description="Run `attach <thread_id>` in Telegram and expect the thread to become bound in bridge state.",
        prompt_template=None,
        assertion="attach",
        required_args=("thread_id",),
    ),
    ScenarioDefinition(
        name="detach",
        description="Run `detach <thread_id>` or reply-`detach` and expect the bridge state to drop the thread.",
        prompt_template=None,
        assertion="detach",
        required_args=("thread_id",),
    ),
    ScenarioDefinition(
        name="approval",
        description="Trigger a Codex approval prompt and expect the target thread to expose active desktop requests.",
        prompt_template="E2E APPROVAL {run_id}",
        assertion="approval",
        required_args=("thread_id",),
    ),
)


def render_scenario_plan(*, run_id: str, bot_username: str | None, chat_id: int | None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "telegram": {
            "bot_username": bot_username,
            "primary_chat_id": chat_id,
        },
        "scenarios": [
            {
                "name": scenario.name,
                "description": scenario.description,
                "assertion": scenario.assertion,
                "prompt": scenario.prompt_template.format(run_id=run_id) if scenario.prompt_template is not None else None,
                "required_args": list(scenario.required_args),
            }
            for scenario in SCENARIOS
        ],
    }


__all__ = ["SCENARIOS", "ScenarioDefinition", "render_scenario_plan"]
