"""Orchestration: what the runner does with a verdict, and what it feeds the verifier.

Two defects are pinned here, both of them in the runner rather than in the
agents.

**A contradiction is not a failed attempt.** A retry assumes the action did not
take effect, so repeating it is safe. Contradicting channels mean the physical
state is unknown, and repeating the unscrew then makes the worse case worse: if
depth says the cap is on while torque says it came off, the cap may have snapped
or the tube may be getting crushed. The runner used to retry it three times and
report "failed", which reads to an operator as "it did not work" when the honest
statement is "we do not know what happened".

**The torque channel was dead in production.** ``CapRemovedAgent`` finds the
unscrewing peak in ``Evidence.during``, and the runner never populated it, so
``peak`` fell back to the pre-step (unloaded) reading and the channel reported
"wrist never loaded" on every run whatever the arm did. Both tests for that are
here: the trace reaching the verifier, and the same fake arm reading dead when
nothing samples it.

No hardware, no cv2, no fastapi, no threads and no clock. The fake arm reads its
wrist torque off a script, one value per ``status()`` call, which is enough
because the runner's read order is fixed: before, then one read per ``sample()``,
then after.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from backend.app.workflows import uncap_aspirate
from backend.app.workflows.uncap_aspirate import Step
from core.verification.agents import (
    DISAGREEMENT_SPREAD,
    SINGLE_CHANNEL_CAP,
    CapRemovedAgent,
    Evidence,
    VerificationResult,
)

TURNING_ARM = "right"
HOLDING_ARM = "left"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _FakeArm:
    """An arm whose wrist torque reads off a script, one value per status() call.

    Simulated, not measured. The values are picked to sit either side of
    ``CapRemovedAgent.MIN_UNSCREW_TORQUE_NM`` so the channel's own gate is what
    decides the test; they are not a recording of a real unscrew. The last value
    repeats once the script runs out, so a test does not have to count reads it
    does not care about.
    """

    def __init__(self, torque_nm: list[float]) -> None:
        self._script = list(torque_nm) or [0.0]
        self.reads: list[float] = []

    def status(self) -> dict[str, Any]:
        nm = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        self.reads.append(nm)
        # Six slots: _wrist_effort reads [-1], the joint that turns the cap.
        return {"effort": {"joints_torque": [0.0] * 5 + [nm]}}


class _FakeDM:
    """Just enough DeviceManager for the runner: get() and KeyError.

    The cameras are deliberately absent. ``_frames`` swallows the KeyError, so
    the vision channel stays out of the way and the tests below are about the
    channels they name.
    """

    def __init__(self, **devices: Any) -> None:
        self._devices = devices

    def get(self, device_id: str) -> Any:
        if device_id not in self._devices:
            raise KeyError(device_id)
        return self._devices[device_id]


class _RecordingAgent:
    """Returns a fixed verdict and keeps every Evidence it was handed."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        self.seen: list[Evidence] = []

    def verify(self, evidence: Evidence) -> VerificationResult:
        self.seen.append(evidence)
        return self.result


def _uncap_step() -> Step:
    return Step("uncap", "dual_arm_manipulation", [TURNING_ARM], "cap_removed",
                {"turning_arm": TURNING_ARM, "holding_arm": HOLDING_ARM,
                 "overview_camera": "overview_cam"})


def _two_step_plan() -> list[Step]:
    """uncap then transport, so a test can show the chain stopping."""
    return [_uncap_step(),
            Step("transport", "arm_transport", [HOLDING_ARM], "grasp_secure",
                 {"holding_arm": HOLDING_ARM})]


def _no_motion(step: Step, dm: Any, sample: Any) -> None:
    """An executor that runs no motion and collects no trace."""


def _samples(n: int):
    """An executor that takes ``n`` during-samples and moves nothing."""

    def execute(step: Step, dm: Any, sample: Any) -> None:
        for _ in range(n):
            sample()

    return execute


def _phases(events: list[dict], step: str | None = None) -> list[str]:
    return [e["phase"] for e in events if step is None or e.get("step") == step]


# --------------------------------------------------------------------------- #
# a real contradiction, so the stubs below cannot drift from the agent
# --------------------------------------------------------------------------- #
def _real_contradiction() -> VerificationResult:
    """A genuine ``CapRemovedAgent`` contradiction, produced by the agent itself.

    Torque says the cap came off (a peak well over the gate, collapsing to
    nothing). Depth, the one channel that looks at the tube, sees the mouth at
    the same height it was captured at and says the cap is still on. That is the
    exact pairing the disagreement branch was added for.

    Built here rather than hand-written so the orchestration tests escalate on a
    payload the agent really emits. A hand-written ``{"disagreement": ...}`` would
    keep passing after the agent renamed the key, which is the failure mode this
    whole file is meant to catch.

    Simulated frames: gaussian noise around a fixed standoff, no camera involved.
    """
    from core.verification.depth_height import measure_region

    scale = 1e-4                  # D405 metres per count, not the D400 1e-3
    roi = (10, 10, 60, 60)
    standoff_m = 0.25

    def frame(seed: int):
        rng = np.random.default_rng(seed)
        metres = rng.normal(standoff_m, 0.0025, size=(80, 80))
        return np.clip(metres / scale, 0, 65535).astype("uint16")

    def effort(nm: float) -> dict[str, Any]:
        return {TURNING_ARM: {"effort": {"joints_torque": [0.0] * 5 + [nm]}}}

    result = CapRemovedAgent().verify(Evidence(
        telemetry=effort(0.05),
        during=[effort(1.2)],
        frames={"depth": frame(2)},
        expected={"turning_arm": TURNING_ARM,
                  "depth_scale": scale,
                  "cap_roi": roi,
                  "cap_reference": measure_region(frame(1), scale, roi)},
    ))
    assert "disagreement" in result.data, (
        "CapRemovedAgent no longer reports a contradiction for torque-off vs "
        "depth-cap-on, so the escalation branch below is testing nothing real"
    )
    return result


@pytest.fixture
def contradiction() -> VerificationResult:
    return _real_contradiction()


@pytest.fixture
def agents(monkeypatch):
    """Swap the runner's agent table for stubs keyed by verifier name."""

    def _set(**by_verifier: Any) -> None:
        monkeypatch.setattr(uncap_aspirate, "AGENTS", dict(by_verifier))

    return _set


# --------------------------------------------------------------------------- #
# 1. a contradiction escalates, immediately, and stops the chain
# --------------------------------------------------------------------------- #
def test_disagreement_escalates_without_a_second_attempt(agents, contradiction):
    agents(cap_removed=_RecordingAgent(contradiction),
           grasp_secure=_RecordingAgent(VerificationResult(True, 1.0, "stub")))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0]), HOLDING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, max_attempts=3, plan=_two_step_plan(),
                                     execute=_no_motion))

    assert _phases(events, "uncap") == ["started", "verifying", "escalated"]
    # The motion ran once. This is the point: a contradiction must not be
    # repeated even once, so max_attempts never comes into it.
    assert _phases(events).count("started") == 1
    assert "retrying" not in _phases(events)
    assert "failed" not in _phases(events)
    # And the chain stops: the tube is in an unknown state, so transporting it is
    # not something to do next.
    assert not [e for e in events if e.get("step") == "transport"]


def test_escalated_event_names_the_two_channels(agents, contradiction):
    agents(cap_removed=_RecordingAgent(contradiction))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, plan=[_uncap_step()], execute=_no_motion))
    escalated = events[-1]

    assert escalated["phase"] == "escalated"
    # Hoisted to the top level so a consumer does not have to dig through the
    # agent's data payload to say which sensors disagreed and by how much.
    d = escalated["disagreement"]
    assert set(d) >= {"high", "low", "spread"}
    assert d["high"] != d["low"]
    assert d["spread"] >= DISAGREEMENT_SPREAD
    assert d == escalated["verification"]["data"]["disagreement"]
    # Distinguishable from "we tried and it never passed" in the event itself.
    assert "contradict" in escalated["detail"]
    assert "contradict" in escalated["verification"]["detail"]


def test_escalation_beats_the_retry_budget_even_at_max_attempts_one(
        agents, contradiction):
    """The branch order matters: with max_attempts=1 both paths are terminal.

    If the retry budget were checked first, a contradiction would be reported as
    an ordinary failure whenever the budget happened to be exhausted, which is
    the misreport this change exists to prevent.
    """
    agents(cap_removed=_RecordingAgent(contradiction))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, max_attempts=1, plan=[_uncap_step()],
                                     execute=_no_motion))
    assert events[-1]["phase"] == "escalated"


def test_a_recorded_contradiction_is_never_reported_as_a_pass(agents):
    """Branch order against the pass, not just against the retry budget.

    ok=True carrying a disagreement record must still escalate. No agent in
    AGENTS emits that pair today, so this is not a live path; it is pinned
    because it is the exact shape of the bug the disagreement record was added
    for. A contradiction that fuses to a number over PASS_THRESHOLD used to be
    reported green, and a green step on an unknown physical state is the one
    failure this whole layer exists to prevent. Whatever produces the pair
    later, the runner must not be the thing that launders it into success.

    Hand-written payload, deliberately, because what is under test is the
    runner's branch order and not the agent's key. The tests above take the same
    branch on a record the real agent produced, so a rename cannot hide here.
    """
    agents(cap_removed=_RecordingAgent(VerificationResult(
        True, 0.61, "channels contradict",
        {"channels": {"torque": 0.9, "depth": 0.0},
         "disagreement": {"high": "torque", "low": "depth", "spread": 0.9,
                          "would_have_fused_to": 0.45}})),
        grasp_secure=_RecordingAgent(VerificationResult(True, 1.0, "stub")))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0]), HOLDING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, plan=_two_step_plan(), execute=_no_motion))

    assert _phases(events, "uncap") == ["started", "verifying", "escalated"]
    assert "passed" not in _phases(events)
    assert not [e for e in events if e.get("step") == "transport"]


# --------------------------------------------------------------------------- #
# 2. an ordinary failure still retries, and a pass still passes
# --------------------------------------------------------------------------- #
def test_ordinary_failure_still_retries_then_fails(agents):
    """No disagreement recorded, so the old behaviour must be untouched."""
    agents(cap_removed=_RecordingAgent(
        VerificationResult(False, 0.1, "nothing moved", {"channels": {"torque": 0.1}})))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, max_attempts=3, plan=_two_step_plan(),
                                     execute=_no_motion))

    assert _phases(events, "uncap") == [
        "started", "verifying", "retrying",
        "started", "verifying", "retrying",
        "started", "verifying", "failed",
    ]
    assert "escalated" not in _phases(events)
    assert not [e for e in events if e.get("step") == "transport"]


def test_pass_first_time_and_the_chain_continues(agents):
    agents(cap_removed=_RecordingAgent(VerificationResult(True, 0.7, "torque=1.00")),
           grasp_secure=_RecordingAgent(VerificationResult(True, 0.7, "width ok")))
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.0]), HOLDING_ARM: _FakeArm([0.0])})

    events = list(uncap_aspirate.run(dm, plan=_two_step_plan(), execute=_no_motion))

    assert _phases(events, "uncap") == ["started", "verifying", "passed"]
    assert _phases(events, "transport") == ["started", "verifying", "passed"]
    assert "retrying" not in _phases(events)
    assert "escalated" not in _phases(events)


# --------------------------------------------------------------------------- #
# 3. during-sampling
# --------------------------------------------------------------------------- #
def test_during_samples_reach_the_verifier(agents):
    recorder = _RecordingAgent(VerificationResult(True, 0.7, "stub"))
    agents(cap_removed=recorder)
    arm = _FakeArm([0.02, 0.9, 1.2, 0.7, 0.05])   # before, 3 during, after
    dm = _FakeDM(**{TURNING_ARM: arm})

    list(uncap_aspirate.run(dm, plan=[_uncap_step()], execute=_samples(3)))

    evidence = recorder.seen[0]
    assert len(evidence.during) == 3
    # Each sample is a live telemetry snapshot of the step's devices, taken at
    # the moment sample() was called, not a copy of the before-state.
    peaks = [s[TURNING_ARM]["effort"]["joints_torque"][-1] for s in evidence.during]
    assert peaks == [0.9, 1.2, 0.7]
    assert arm.reads == [0.02, 0.9, 1.2, 0.7, 0.05]


def test_the_default_executor_collects_a_trace(agents):
    """Pins production, not just the injected path.

    Injecting an executor that samples proves the runner plumbs the list through;
    it says nothing about whether the real ``_execute`` ever calls the hook. It
    does, at the place the ratchet loop will go. With no motion behind it that
    reading is the unloaded wrist, which is honest and is why the torque channel
    still refuses to draw a conclusion (see the test below).

    At least one, not exactly one. What is worth pinning is that the hook is
    reached at all. The current count of one is a placeholder standing in for the
    ratchet loop, so asserting it exactly would make the suite fail the day that
    loop lands, which is the change this wiring exists to enable.
    """
    recorder = _RecordingAgent(VerificationResult(True, 0.7, "stub"))
    agents(cap_removed=recorder)
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.02]), HOLDING_ARM: _FakeArm([0.0])})

    list(uncap_aspirate.run(dm, plan=[_uncap_step()]))

    assert len(recorder.seen[0].during) >= 1


def test_during_samples_make_the_real_torque_channel_live():
    """The whole point of the wiring, checked against the real agent.

    A peak of 1.2 Nm collapsing to 0.05 Nm is a 96% drop, past DROP_HI, so the
    torque channel scores 1.0 and the fused confidence is SINGLE_CHANNEL_CAP:
    one channel alone can pass, but not by much. Vision and depth are absent
    here (no cameras on the fake fleet), which is also true of this workflow on
    the bench today.
    """
    arm = _FakeArm([0.02, 1.2, 0.9, 0.05])       # before, 2 during, after
    dm = _FakeDM(**{TURNING_ARM: arm})

    events = list(uncap_aspirate.run(dm, plan=[_uncap_step()], execute=_samples(2)))

    assert events[-1]["phase"] == "passed"
    data = events[-1]["verification"]["data"]
    assert "torque" in data["channels"]
    assert data["torque_peak_nm"] == pytest.approx(1.2)
    assert data["torque_final_nm"] == pytest.approx(0.05)
    # The peak came from a during-sample, not from the pre-step reading, which is
    # the whole regression: 0.02 Nm would have been below the gate.
    assert data["torque_peak_nm"] > 0.02
    # And the cap is asserted, not just described in the docstring: one channel
    # can carry the step, but only to SINGLE_CHANNEL_CAP.
    assert data["channels"]["torque"] == pytest.approx(1.0)
    assert events[-1]["verification"]["confidence"] == pytest.approx(SINGLE_CHANNEL_CAP)


def test_without_during_samples_the_torque_channel_stays_dead():
    """The regression itself, with the same arm and the same readings.

    Nothing samples, so the peak falls back to the pre-step reading of 0.02 Nm,
    which is below MIN_UNSCREW_TORQUE_NM. The agent then has no channel at all
    and fails closed. This is what every run of this workflow did before the
    trace was wired, including the runs where the cap came off.
    """
    dm = _FakeDM(**{TURNING_ARM: _FakeArm([0.02, 1.2, 0.9, 0.05])})

    events = list(uncap_aspirate.run(dm, max_attempts=1, plan=[_uncap_step()],
                                     execute=_no_motion))

    assert events[-1]["phase"] == "failed"
    assert "wrist never loaded" in events[-1]["verification"]["detail"]
