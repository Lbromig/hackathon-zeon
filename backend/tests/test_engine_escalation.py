"""The agent engine must escalate a sensor contradiction instead of retrying it.

``uncap_aspirate.run`` and ``Engine._run_skill`` are two separate orchestration
paths over the same verification agents, and both used to retry any result that
was not ok. Retrying is only the right response when the assumption behind it
holds, namely that the action simply did not take effect. A contradiction between
two independent channels means the physical state is unknown, so there is no
starting state to retry from.

These also pin the ordering: the contradiction is checked before the pass, so a
contradiction that somehow arrives alongside ok=True can never be reported as
success. That pairing is the exact bug the record was added to catch, where a
contradiction fused into a number above PASS_THRESHOLD.

Offline: mock fleet, stubbed verifiers, no hardware and no API key.
"""
from __future__ import annotations

from typing import Any

import drivers.mock  # noqa: F401  -- registers mock_arm / mock_camera / mock_liquid_handler

from backend.app.agent.engine import Engine
from backend.app.agent.policy import RuleBasedPolicy
from backend.app.agent.tools import SKILL_ORDER, Toolbox
from backend.app.services.device_manager import DeviceManager
from core.verification.agents import Evidence, VerificationResult

DISAGREEMENT = {
    "high": "torque", "low": "depth", "spread": 0.9, "would_have_fused_to": 0.6,
}


def _mock_dm() -> DeviceManager:
    dm = DeviceManager()
    dm._drivers = {}
    from drivers import build_driver

    for cfg in (
        {"type": "mock_arm", "id": "left", "name": "Left arm"},
        {"type": "mock_arm", "id": "right", "name": "Right arm"},
        {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
        {"type": "mock_camera", "id": "on_arm", "name": "On-arm cam"},
    ):
        dm._drivers[cfg["id"]] = build_driver(cfg)
    return dm


class _StubAgent:
    """A verifier whose verdict and data payload we control.

    Also records the Evidence it was handed, which is how the during-trace test
    checks that the samples actually arrived rather than trusting the plumbing.
    """

    def __init__(self, name: str, *, ok: bool, data: dict[str, Any] | None = None) -> None:
        self.name = name
        self._ok = ok
        self._data = data or {}
        self.seen: list[Evidence] = []

    def verify(self, evidence: Evidence) -> VerificationResult:
        self.seen.append(evidence)
        return VerificationResult(
            ok=self._ok,
            confidence=1.0 if self._ok else 0.0,
            detail="stub",
            data=dict(self._data),
        )


def _toolbox(agents: dict[str, _StubAgent]) -> Toolbox:
    dm = _mock_dm()
    return Toolbox(dm, world_provider=lambda: None, verifiers=agents)


def _agents_for(**kwargs: Any) -> dict[str, _StubAgent]:
    """One stub per verifier named in the canonical plan."""
    dm = _mock_dm()
    probe = Toolbox(dm, world_provider=lambda: None)
    return {s.verifier: _StubAgent(s.verifier, **kwargs) for s in probe.skills.values()}


def _run(agents: dict[str, _StubAgent], *, max_attempts: int = 3) -> list[dict]:
    engine = Engine(
        _toolbox(agents), RuleBasedPolicy(),
        checkpoints=frozenset(), max_attempts=max_attempts,
    )
    return list(engine.run())


def test_a_contradiction_escalates_on_the_first_attempt():
    events = _run(_agents_for(ok=False, data={"disagreement": DISAGREEMENT}))

    first = SKILL_ORDER[0]
    starts = [e for e in events if e.get("step") == first and e["phase"] == "started"]
    assert len(starts) == 1, "a contradiction must not be repeated even once"
    assert not any(e["phase"] == "retrying" for e in events)
    assert not any(e["phase"] == "failed" for e in events)
    assert any(e["phase"] == "escalated" for e in events)


def test_escalation_is_terminal_and_says_why():
    events = _run(_agents_for(ok=False, data={"disagreement": DISAGREEMENT}))

    assert events[-1]["phase"] == "stopped"
    assert "contradict" in events[-1]["reason"]
    # later skills never run
    assert all(e.get("step") in (SKILL_ORDER[0], None) for e in events)


def test_the_escalated_event_hoists_the_disagreement():
    """A consumer should be able to name the two channels without unpacking data."""
    events = _run(_agents_for(ok=False, data={"disagreement": DISAGREEMENT}))

    escalated = next(e for e in events if e["phase"] == "escalated")
    assert escalated["disagreement"]["high"] == "torque"
    assert escalated["disagreement"]["low"] == "depth"
    assert "needs a human" in escalated["detail"]


def test_a_contradiction_is_never_reported_as_a_pass():
    """The ordering guard: ok=True alongside a recorded contradiction.

    No agent emits this pair today. The check exists so that if one ever does,
    an unknown physical state cannot be reported green.
    """
    events = _run(_agents_for(ok=True, data={"disagreement": DISAGREEMENT}))

    assert not any(e["phase"] == "passed" for e in events)
    assert any(e["phase"] == "escalated" for e in events)


def test_a_plain_failure_still_retries_then_fails():
    """Guard against the escalation path swallowing ordinary retryable failure."""
    events = _run(_agents_for(ok=False))

    first = SKILL_ORDER[0]
    starts = [e for e in events if e.get("step") == first and e["phase"] == "started"]
    assert len(starts) == 3
    assert any(e["phase"] == "failed" for e in events)
    assert not any(e["phase"] == "escalated" for e in events)


def test_a_clean_run_still_passes_every_skill():
    events = _run(_agents_for(ok=True))

    assert [e["step"] for e in events if e.get("phase") == "passed"] == SKILL_ORDER
    assert not any(e["phase"] == "escalated" for e in events)


def test_during_samples_reach_the_verifier_through_the_toolbox():
    """The torque channel needs Evidence.during, and this path used to drop it.

    Skill.run forwarded no sampler, so the trace was empty, the torque peak fell
    back to the unloaded pre-step reading, and the channel reported "wrist never
    loaded" regardless of what the arm did.
    """
    agents = _agents_for(ok=True)
    _run(agents)

    uncap = agents["cap_removed"]
    assert uncap.seen, "the cap_removed verifier was never called"
    assert uncap.seen[0].during, "no during-samples arrived from the motion"
    # each sample is a {device_id: status()} snapshot for the step's devices
    assert all(isinstance(s, dict) and s for s in uncap.seen[0].during)
