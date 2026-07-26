"""Tool registry for the P0 agent.

Each tool is a plain Python callable *plus* a JSON-schema description in the shape
the Claude Agent SDK / Messages API expects (``{"name", "description", "input_schema"}``),
so the exact same registry can back a :class:`~backend.app.agent.policy.RuleBasedPolicy`
offline today and be handed to Claude tool-use tomorrow with no changes.

The tools are backed by *real* objects, not toys:

* **skills**   -> the steps in ``uncap_aspirate.PLAN`` wrapped as named callables.
                  Executing a skill re-uses ``uncap_aspirate._execute`` (the same
                  capability -> driver mapping the hardcoded workflow uses).
* **world model** -> ``backend.app.services.twin`` (the shared live digital twin).
* **verify**   -> ``core.verification.agents.AGENTS`` (the "did it work?" layer).

Nothing here decides *what* to do next — that is the policy's job. These tools are
the deterministic "spine" the policy (the "brain") is allowed to pull.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.verification.agents import AGENTS, Evidence, VerificationAgent

from ..services import twin
from ..services.device_manager import DeviceManager
from ..workflows import uncap_aspirate

# --------------------------------------------------------------------------- #
# Skills: the vetted, pre-defined step snippets the agent may pick from.
# Wrap each Step in the canonical PLAN as a named callable. Canonical order is
# preserved (dict insertion order == PLAN order) so a rule-based policy can walk it.
# --------------------------------------------------------------------------- #
@dataclass
class Skill:
    """A single vetted step, executable against a DeviceManager."""

    step: uncap_aspirate.Step

    @property
    def name(self) -> str:
        return self.step.key

    @property
    def capability(self) -> str:
        return self.step.capability

    @property
    def devices(self) -> list[str]:
        return self.step.devices

    @property
    def verifier(self) -> str:
        return self.step.verifier

    def run(self, dm: DeviceManager, params: dict[str, Any] | None = None) -> None:
        """Deterministic execution: map capability -> driver calls (P0 ignores params)."""
        uncap_aspirate._execute(self.step, dm)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "devices": list(self.devices),
            "verifier": self.verifier,
        }


# Canonical, ordered registry of skills built straight from the hero workflow.
SKILLS: dict[str, Skill] = {step.key: Skill(step) for step in uncap_aspirate.PLAN}
# Canonical order the goal is expected to be reached in.
SKILL_ORDER: list[str] = [step.key for step in uncap_aspirate.PLAN]


# --------------------------------------------------------------------------- #
# Toolbox: bound, callable implementations of the P0 tools.
# --------------------------------------------------------------------------- #
class Toolbox:
    """Bound tool implementations for one run.

    Dependencies are injectable so the engine can be driven end-to-end offline
    (stub twin / stub verifiers) without hardware, a network, or an API key.
    """

    def __init__(
        self,
        dm: DeviceManager,
        *,
        world_provider: Callable[[], Any] | None = None,
        skills: dict[str, Skill] | None = None,
        verifiers: dict[str, VerificationAgent] | None = None,
    ) -> None:
        self._dm = dm
        self._world_provider = world_provider or twin.get_world
        self.skills = skills if skills is not None else SKILLS
        self._verifiers = verifiers if verifiers is not None else AGENTS
        # Pre-motion snapshots, keyed by skill name. The policy calls call_skill and
        # verify as two separate tools, so without stashing this the before-state is
        # gone by the time we verify — and the verifiers measure *change*.
        self._pre: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    # --- read tools -------------------------------------------------------- #
    def get_world_model(self) -> dict[str, Any]:
        """Live digital-twin snapshot (entities + poses), or an empty scene."""
        wm = self._world_provider()
        if wm is None:
            return {"entities": [], "present": False}
        return {"entities": wm.snapshot(), "present": True}

    def list_skills(self) -> list[dict[str, Any]]:
        """The vetted skills the agent may pick from, in canonical order."""
        return [self.skills[k].describe() for k in self.skills]

    # --- act tools --------------------------------------------------------- #
    def call_skill(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deterministically execute a named skill. Raises KeyError on unknown skill."""
        if name not in self.skills:
            raise KeyError(f"unknown skill: {name!r} (have {list(self.skills)})")
        skill = self.skills[name]
        # Captured before the motion so verify() can measure the change it caused.
        self._pre[name] = (uncap_aspirate._telemetry(skill.step, self._dm),
                           uncap_aspirate._frames(self._dm))
        skill.run(self._dm, params or {})
        return {
            "skill": name,
            "status": "executed",
            "params": params or {},
            "capability": skill.capability,
            "devices": list(skill.devices),
        }

    def verify(self, step: str) -> dict[str, Any]:
        """Run the verification agent bound to ``step`` and return a plain dict."""
        if step not in self.skills:
            raise KeyError(f"unknown skill: {step!r}")
        skill = self.skills[step]
        agent = self._verifiers[skill.verifier]
        before, before_frames = self._pre.get(step, ({}, {}))
        evidence = Evidence(
            frames=uncap_aspirate._frames(self._dm),
            telemetry=uncap_aspirate._telemetry(skill.step, self._dm),
            before=before,
            before_frames=before_frames,
            expected=dict(skill.step.params),
        )
        result = agent.verify(evidence)
        return {
            "step": step,
            "verifier": skill.verifier,
            "ok": bool(result.ok),
            "confidence": float(result.confidence),
            "detail": result.detail,
            "data": result.data,
        }

    def checkpoint(self, summary: str) -> dict[str, Any]:
        """Package a human-in-the-loop checkpoint. The engine emits + gates on this."""
        return {"summary": summary}


# --------------------------------------------------------------------------- #
# Tool schemas: the description each tool would present to the Claude Agent SDK.
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    """A callable + its Claude-SDK-shaped JSON schema."""

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        """The dict shape the Claude Agent SDK / Messages ``tools=[...]`` expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# Instance-independent schemas (safe to hand to the Messages API as ``tools=``).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_world_model",
        "description": "Return the live digital-twin snapshot: entities, poses and states.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_skills",
        "description": "List the vetted skills the agent may pick from, in canonical order.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "call_skill",
        "description": "Deterministically execute one vetted skill by name (P0: no param tuning).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from list_skills."},
                "params": {"type": "object", "description": "Reserved for P1 bounded tuning."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify",
        "description": "Run the verification agent for a step and report ok/confidence/detail.",
        "input_schema": {
            "type": "object",
            "properties": {"step": {"type": "string", "description": "Skill/step name to verify."}},
            "required": ["step"],
            "additionalProperties": False,
        },
    },
    {
        "name": "checkpoint",
        "description": "Pause for human approval with a short summary before continuing.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]

_SCHEMA_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


def build_tools(toolbox: Toolbox) -> dict[str, Tool]:
    """Bind the tool schemas to a Toolbox's callables -> a name -> Tool registry."""
    bindings: dict[str, Callable[..., Any]] = {
        "get_world_model": toolbox.get_world_model,
        "list_skills": toolbox.list_skills,
        "call_skill": toolbox.call_skill,
        "verify": toolbox.verify,
        "checkpoint": toolbox.checkpoint,
    }
    registry: dict[str, Tool] = {}
    for name, fn in bindings.items():
        schema = _SCHEMA_BY_NAME[name]
        registry[name] = Tool(
            name=name,
            description=schema["description"],
            input_schema=schema["input_schema"],
            fn=fn,
        )
    return registry
