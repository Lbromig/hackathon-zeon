"""P0 normal-mode loop: observe -> decide -> execute -> verify -> emit -> repeat.

The engine is the deterministic "spine": it drives the tools, retries verification,
and gates on human checkpoints, while delegating the *what next* decision to a
pluggable :class:`~backend.app.agent.policy.Policy` (the "brain"). It stops when the
goal is satisfied or when the policy has no applicable skill.

Events are emitted in the **same dict shape** as ``uncap_aspirate.run`` so the
existing UI / ``/ws/workflow`` consumers keep working unchanged:

    {"step", "phase", "attempt", ...}   phase in
      started | verifying | passed | failed | retrying
    plus an additive "checkpoint" phase and a terminal "stopped" phase.

``run()`` is a *blocking synchronous generator* on purpose: the websocket layer runs
it off the event loop in a thread and streams the yielded events (see api/agent.py).
"""
from __future__ import annotations

from typing import Any, Callable, Iterator

from .policy import Decision, Observation, Policy, RuleBasedPolicy
from .tools import SKILL_ORDER, Toolbox

GOAL = "Uncap the tube and aspirate the sample, verified at each step."

MAX_ATTEMPTS = 3

# Skills before which the run pauses to chat with the human (per the plan).
DEFAULT_CHECKPOINTS: frozenset[str] = frozenset({"uncap", "aspirate"})

# A checkpoint gate is any callable that blocks until the human says "continue".
CheckpointGate = Callable[[dict[str, Any]], None]


class Engine:
    def __init__(
        self,
        toolbox: Toolbox,
        policy: Policy | None = None,
        *,
        goal: str = GOAL,
        checkpoints: frozenset[str] | set[str] | None = None,
        checkpoint_gate: CheckpointGate | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.toolbox = toolbox
        self.policy = policy or RuleBasedPolicy()
        self.goal = goal
        self.checkpoints = frozenset(DEFAULT_CHECKPOINTS if checkpoints is None else checkpoints)
        self.checkpoint_gate = checkpoint_gate
        self.max_attempts = max_attempts

        self._passed: dict[str, bool] = {}
        self._history: list[dict[str, Any]] = []
        self._last_skill: str | None = None
        self._last_result: dict[str, Any] | None = None

    # --- observation ------------------------------------------------------- #
    def _observe(self) -> Observation:
        skills = self.toolbox.list_skills()
        return Observation(
            goal=self.goal,
            world_model=self.toolbox.get_world_model(),
            skills=skills,
            passed=dict(self._passed),
            last_skill=self._last_skill,
            last_result=self._last_result,
            history=list(self._history),
        )

    def _goal_met(self, skills: list[dict[str, Any]]) -> bool:
        """Goal is met when every canonical skill that exists has passed verification."""
        names = [s["name"] for s in skills if s["name"] in SKILL_ORDER] or [s["name"] for s in skills]
        return bool(names) and all(self._passed.get(n, False) for n in names)

    # --- main loop --------------------------------------------------------- #
    def run(self) -> Iterator[dict[str, Any]]:
        while True:
            obs = self._observe()
            decision: Decision | None = self.policy.choose(obs)

            if decision is None:
                if self._goal_met(obs.skills):
                    yield {"phase": "done", "goal": self.goal}
                else:
                    yield {
                        "phase": "stopped",
                        "reason": "no applicable skill",
                        "goal": self.goal,
                        "passed": dict(self._passed),
                    }
                return

            skill = decision.skill
            if skill not in {s["name"] for s in obs.skills}:
                yield {"phase": "stopped", "reason": f"unknown skill {skill!r}"}
                return

            # Checkpoint: emit + pause for human approval before acting.
            if skill in self.checkpoints:
                cp = self.toolbox.checkpoint(f"About to run '{skill}' — {decision.rationale}")
                cp_event = {"step": skill, "phase": "checkpoint", **cp}
                yield cp_event
                if self.checkpoint_gate is not None:
                    self.checkpoint_gate(cp_event)  # blocks until "continue"

            stop = yield from self._run_skill(skill, decision)
            if stop:
                # Skill failed verification within budget: don't re-pick it forever.
                return

    def _run_skill(self, skill: str, decision: Decision) -> Iterator[dict[str, Any]]:
        """Yield step events; return True if the run should stop (permanent failure)."""
        info = self.toolbox.skills[skill]
        attempt = 0
        while True:
            attempt += 1
            yield {
                "step": skill, "phase": "started", "attempt": attempt,
                "devices": list(info.devices), "capability": info.capability,
            }

            self.toolbox.call_skill(skill, decision.params)

            yield {"step": skill, "phase": "verifying", "attempt": attempt}
            result = self.toolbox.verify(skill)
            self._last_skill = skill
            self._last_result = result
            self._history.append({"skill": skill, "attempt": attempt, "verification": result})

            if result["ok"]:
                self._passed[skill] = True
                yield {"step": skill, "phase": "passed", "attempt": attempt, "verification": result}
                return False
            if attempt >= self.max_attempts:
                yield {"step": skill, "phase": "failed", "attempt": attempt, "verification": result}
                # Verification never passed -> no applicable progress; stop the chain.
                yield {"phase": "stopped", "reason": f"verification failed for {skill!r}"}
                return True
            yield {"step": skill, "phase": "retrying", "attempt": attempt, "verification": result}
