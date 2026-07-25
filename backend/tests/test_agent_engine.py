"""End-to-end tests for the P0 agent engine — offline, no hardware, no API key.

Drives the engine with the deterministic RuleBasedPolicy over a mock fleet and a
stubbed twin/verify so it reaches the goal without a network. Also asserts the
engine stops (rather than looping) when no skill applies and when verification
never passes.
"""
from __future__ import annotations

import drivers.mock  # noqa: F401  -- registers mock_arm / mock_camera / mock_liquid_handler

from backend.app.agent.engine import Engine
from backend.app.agent.policy import Decision, Observation, RuleBasedPolicy
from backend.app.agent.tools import SKILL_ORDER, Toolbox, build_tools
from backend.app.services.device_manager import DeviceManager
from core.verification.agents import Evidence, VerificationResult


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mock_dm() -> DeviceManager:
    dm = DeviceManager()
    dm._drivers = {}  # start empty, then build a hardware-free fleet
    from drivers import build_driver

    fleet = [
        {"type": "mock_arm", "id": "left", "name": "Left arm"},
        {"type": "mock_arm", "id": "right", "name": "Right arm"},
        {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
        {"type": "mock_camera", "id": "on_arm", "name": "On-arm cam"},
    ]
    for cfg in fleet:
        dm._drivers[cfg["id"]] = build_driver(cfg)
    return dm


class _StubAgent:
    """A verification agent whose verdict we control."""

    def __init__(self, name: str, ok: bool) -> None:
        self.name = name
        self._ok = ok

    def verify(self, evidence: Evidence) -> VerificationResult:  # noqa: ARG002
        return VerificationResult(ok=self._ok, confidence=1.0 if self._ok else 0.0, detail="stub")


def _passing_toolbox() -> Toolbox:
    dm = _mock_dm()
    # Stub the twin (offline: no calibrated world) and make every verifier pass.
    verifiers = {}
    tb = Toolbox(dm, world_provider=lambda: None)
    for skill in tb.skills.values():
        verifiers[skill.verifier] = _StubAgent(skill.verifier, ok=True)
    return Toolbox(dm, world_provider=lambda: None, verifiers=verifiers)


# --------------------------------------------------------------------------- #
# tool registry
# --------------------------------------------------------------------------- #
def test_toolbox_and_registry_shape():
    tb = _passing_toolbox()
    tools = build_tools(tb)
    assert set(tools) == {"get_world_model", "list_skills", "call_skill", "verify", "checkpoint"}
    for tool in tools.values():
        spec = tool.spec()
        assert set(spec) == {"name", "description", "input_schema"}
        assert spec["input_schema"]["type"] == "object"
    # skills come straight from the canonical PLAN, in order
    assert [s["name"] for s in tb.list_skills()] == SKILL_ORDER
    assert tb.get_world_model() == {"entities": [], "present": False}


# --------------------------------------------------------------------------- #
# rule-based policy
# --------------------------------------------------------------------------- #
def test_rule_based_policy_walks_canonical_order_then_stops():
    policy = RuleBasedPolicy()
    skills = [{"name": n} for n in SKILL_ORDER]

    # nothing passed -> first skill
    obs = Observation(goal="g", world_model={}, skills=skills, passed={})
    assert policy.choose(obs).skill == SKILL_ORDER[0]

    # first passed -> second skill
    obs = Observation(goal="g", world_model={}, skills=skills, passed={SKILL_ORDER[0]: True})
    assert policy.choose(obs).skill == SKILL_ORDER[1]

    # all passed -> None (goal reached, no applicable skill)
    obs = Observation(goal="g", world_model={}, skills=skills, passed={n: True for n in SKILL_ORDER})
    assert policy.choose(obs) is None


# --------------------------------------------------------------------------- #
# engine end-to-end
# --------------------------------------------------------------------------- #
def test_engine_reaches_goal_offline():
    engine = Engine(_passing_toolbox(), RuleBasedPolicy(), checkpoints=frozenset())
    events = list(engine.run())

    # every canonical skill ran and passed, in order
    passed_order = [e["step"] for e in events if e.get("phase") == "passed"]
    assert passed_order == SKILL_ORDER

    # each skill: started -> verifying -> passed
    for skill in SKILL_ORDER:
        phases = [e["phase"] for e in events if e.get("step") == skill]
        assert phases == ["started", "verifying", "passed"]

    # terminal event is a single "done"
    assert events[-1] == {"phase": "done", "goal": engine.goal}
    assert [e["phase"] for e in events].count("done") == 1


def test_engine_emits_and_gates_on_checkpoint():
    released: list[dict] = []
    engine = Engine(
        _passing_toolbox(),
        RuleBasedPolicy(),
        checkpoints=frozenset({"uncap", "aspirate"}),
        checkpoint_gate=lambda cp: released.append(cp),  # auto-continue, record it
    )
    events = list(engine.run())
    cps = [e for e in events if e.get("phase") == "checkpoint"]
    assert [c["step"] for c in cps] == ["uncap", "aspirate"]
    assert all("summary" in c for c in cps)
    assert len(released) == 2  # gate was invoked before each checkpointed skill
    # a checkpoint precedes its skill's "started"
    order = [(e.get("phase"), e.get("step")) for e in events]
    assert order.index(("checkpoint", "uncap")) < order.index(("started", "uncap"))


def test_engine_stops_when_no_skill_applies():
    class _NoOpPolicy:
        def choose(self, observation: Observation) -> Decision | None:  # noqa: ARG002
            return None

    engine = Engine(_passing_toolbox(), _NoOpPolicy(), checkpoints=frozenset())
    events = list(engine.run())
    # policy never acted and goal isn't met -> single "stopped" event, no steps run
    assert events == [{
        "phase": "stopped",
        "reason": "no applicable skill",
        "goal": engine.goal,
        "passed": {},
    }]


def test_engine_stops_on_persistent_verification_failure():
    dm = _mock_dm()
    verifiers = {}
    probe = Toolbox(dm, world_provider=lambda: None)
    for skill in probe.skills.values():
        verifiers[skill.verifier] = _StubAgent(skill.verifier, ok=False)
    tb = Toolbox(dm, world_provider=lambda: None, verifiers=verifiers)

    engine = Engine(tb, RuleBasedPolicy(), checkpoints=frozenset(), max_attempts=3)
    events = list(engine.run())

    first = SKILL_ORDER[0]
    attempts = [e for e in events if e.get("step") == first and e["phase"] == "started"]
    assert len(attempts) == 3  # retried up to max_attempts, then gave up
    assert any(e["phase"] == "failed" for e in events)
    assert events[-1]["phase"] == "stopped"
    # it stops at the first failing skill; later skills never run
    assert all(e.get("step") in (first, None) for e in events)
