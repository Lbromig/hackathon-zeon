"""The unimplemented-verifier gate, and why it is currently a no-op.

The gate (`allow_unimplemented_verifiers`, env `HZ_ALLOW_UNIMPLEMENTED_VERIFIERS`)
exists so the workflow can be demonstrated before every checker is written, on the
condition that a simulated pass is *visibly* simulated: `confidence=0.0`,
`simulated=True`, "SIMULATED" in the detail. Anything weighting results by
confidence then gives it none.

These tests originally pinned that behaviour for `grasp_secure`, `tube_aligned`
and `aspiration_ok`, which were stubs when the gate was written. They are not
stubs any more — `c29a76c` made all four agents real and fail-closed — so the gate
now stands in for nothing, and these tests assert that instead of the old premise.

The property that still matters, more than the escape hatch itself: **the flag
must never force a pass on a real checker.** A flag that could do that would be a
way to fabricate evidence, which is worse than having no flag. That is what
`test_the_flag_cannot_fake_any_verifier` pins, and it is why this file was
rewritten when the gate went idle rather than deleted.
"""
from __future__ import annotations

import pytest

from core.verification.agents import AGENTS, Evidence, VerificationResult


@pytest.fixture
def flag(monkeypatch):
    """Set the gate, since `settings` is read at import time."""

    def _set(on: bool):
        from core import config

        monkeypatch.setattr(config.settings, "allow_unimplemented_verifiers", on)

    return _set


def test_every_agent_fails_closed_on_empty_evidence(flag):
    """No evidence must never read as a pass, flag or no flag."""
    flag(False)
    for name, agent in AGENTS.items():
        result = agent.verify(Evidence())
        assert result.ok is False, name
        assert result.confidence == 0.0, name
        assert result.detail, f"{name} gave no reason for failing"


def test_the_flag_cannot_fake_any_verifier(flag):
    """With the gate ON, nothing may report a simulated pass.

    All four agents are implemented, so there is nothing for the escape hatch to
    stand in for. A failure here means either a verifier regressed to a stub, or
    the gate gained the ability to override a real check — and the second would be
    a way to fabricate evidence.
    """
    flag(True)
    for name, agent in AGENTS.items():
        result = agent.verify(Evidence())
        assert result.ok is False, f"{name} passed on empty evidence with the flag on"
        assert result.confidence == 0.0, name
        data = result.data if isinstance(result.data, dict) else {}
        assert not data.get("simulated"), f"{name} reported a simulated pass"
        assert "SIMULATED" not in result.detail, name


def test_the_gate_is_currently_idle():
    """Pins that the gate covers nothing, so a future stub forces an explicit call.

    An assertion rather than a comment: if someone adds a verifier as a stub, this
    fails and the decision to reopen the escape hatch has to be made deliberately.
    """
    from core import config

    assert config.settings.allow_unimplemented_verifiers is False, (
        "the default must stay fail-closed"
    )
    assert set(AGENTS) == {"cap_removed", "grasp_secure", "tube_aligned", "aspiration_ok"}


def test_results_stay_well_formed_either_way(flag):
    for on in (False, True):
        flag(on)
        for name, agent in AGENTS.items():
            result = agent.verify(Evidence())
            assert isinstance(result, VerificationResult), name
            assert isinstance(result.ok, bool), name
            assert 0.0 <= result.confidence <= 1.0, name
