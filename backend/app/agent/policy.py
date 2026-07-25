"""Policies: the pluggable "brain" that chooses the next skill each step.

A policy sees an :class:`Observation` (goal, world-model snapshot, available skills,
which steps have passed verification, last action + result, recent history) and
returns a :class:`Decision` (next skill + params) or ``None`` when it has nothing
applicable to do.

Two implementations:

* :class:`RuleBasedPolicy` -- deterministic, offline, no network/key required. Picks
  the next not-yet-passed skill in canonical PLAN order. This is the default so the
  loop runs and is tested without Claude.
* :class:`ClaudePolicy` -- wires the Anthropic Agent SDK / Messages tool-use structure.
  It is **gated**: it only constructs / runs when ``ANTHROPIC_API_KEY`` is set. With no
  key, :func:`default_policy` returns a :class:`RuleBasedPolicy` instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .tools import SKILL_ORDER, TOOL_SCHEMAS


@dataclass
class Observation:
    """Everything a policy is given to decide one step."""

    goal: str
    world_model: dict[str, Any]
    skills: list[dict[str, Any]]
    passed: dict[str, bool]                       # skill name -> passed verification?
    last_skill: str | None = None
    last_result: dict[str, Any] | None = None     # last verify() result
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "world_model": self.world_model,
            "skills": self.skills,
            "passed": self.passed,
            "last_skill": self.last_skill,
            "last_result": self.last_result,
            "history": self.history[-10:],
        }


@dataclass
class Decision:
    """A policy's choice for the next step."""

    skill: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@runtime_checkable
class Policy(Protocol):
    def choose(self, observation: Observation) -> Decision | None:
        """Return the next skill to run, or ``None`` when nothing applies."""
        ...


class RuleBasedPolicy:
    """Deterministic offline default: next not-yet-passed skill in canonical order.

    Returns ``None`` once every skill has passed (goal reached) or when no skill in
    canonical order is still outstanding — which is exactly the "no applicable skill"
    stop condition the engine relies on.
    """

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order or list(SKILL_ORDER)

    def choose(self, observation: Observation) -> Decision | None:
        available = {s["name"] for s in observation.skills}
        for name in self.order:
            if name in available and not observation.passed.get(name, False):
                return Decision(
                    skill=name,
                    params={},
                    rationale=f"next unverified skill in canonical order: {name}",
                )
        return None


class ClaudePolicy:
    """Claude-backed policy — GATED behind ANTHROPIC_API_KEY.

    Wires the Messages API tool-use structure (the same ``TOOL_SCHEMAS`` the toolbox
    exposes) so the model can select the next ``call_skill``. Constructing this without
    a key raises, and :func:`default_policy` will never return one without a key — so
    the P0 loop stays fully runnable and testable offline.
    """

    SYSTEM = (
        "You are the NORMAL-MODE selector for a supervised lab-automation loop. "
        "Each step, look at the goal, the world-model snapshot and which steps have "
        "already passed verification, then choose exactly ONE next action by calling "
        "the `call_skill` tool with a skill name from `list_skills`. You may ONLY pick "
        "from the vetted skills; you may not tune parameters, propose new code, or "
        "recover from failures (those are later phases). If the goal is already met, "
        "do not call any tool."
    )

    def __init__(self, *, api_key: str | None = None, model: str = "claude-sonnet-4-5") -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ClaudePolicy requires ANTHROPIC_API_KEY. Use RuleBasedPolicy offline."
            )
        self.model = model
        self._client = None  # lazily created on first choose()

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Imported lazily so the package imports fine without the SDK installed.
            from anthropic import Anthropic  # type: ignore

            self._client = Anthropic(api_key=self.api_key)
        return self._client

    def choose(self, observation: Observation) -> Decision | None:
        client = self._ensure_client()
        message = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self.SYSTEM,
            tools=TOOL_SCHEMAS,
            tool_choice={"type": "auto"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Current observation (JSON):\n"
                        + json.dumps(observation.to_json_dict(), default=str)
                        + "\n\nChoose the next skill by calling `call_skill`, or stop "
                        "if the goal is met."
                    ),
                }
            ],
        )
        for block in getattr(message, "content", []):
            if getattr(block, "type", None) == "tool_use" and block.name == "call_skill":
                args = block.input or {}
                skill = args.get("name")
                if skill:
                    return Decision(
                        skill=skill,
                        params=args.get("params", {}) or {},
                        rationale="selected by ClaudePolicy",
                    )
        return None  # model declined to act -> goal met / nothing to do


def default_policy() -> Policy:
    """ClaudePolicy when a key is present, else the deterministic RuleBasedPolicy."""
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return ClaudePolicy()
        except Exception:
            pass
    return RuleBasedPolicy()
