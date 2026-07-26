"""The unimplemented-verifier gate.

The default must be fail-closed. A checker nobody has written cannot be allowed
to report success, because the whole point of the verification layer is that a
pass means something was actually observed.

The escape hatch exists so the workflow can be demonstrated before every checker
is written, but a simulated pass has to be visibly simulated. These tests pin
both halves of that.
"""
from __future__ import annotations

import pytest

from core.verification.agents import AGENTS, Evidence, VerificationResult

UNIMPLEMENTED = ("grasp_secure", "tube_aligned", "aspiration_ok")


@pytest.fixture
def flag(monkeypatch):
    """Set the gate, since `settings` is read at import time."""

    def _set(on: bool):
        from core import config

        monkeypatch.setattr(config.settings, "allow_unimplemented_verifiers", on)

    return _set


def test_unimplemented_agents_fail_closed_by_default(flag):
    flag(False)
    for name in UNIMPLEMENTED:
        result = AGENTS[name].verify(Evidence())
        assert result.ok is False, name
        assert result.confidence == 0.0, name
        assert result.data["checked"] is False, name


def test_flag_lets_them_pass_but_marks_it_simulated(flag):
    flag(True)
    for name in UNIMPLEMENTED:
        result = AGENTS[name].verify(Evidence())
        assert result.ok is True, name
        # A simulated pass carries no confidence. Anything that weights results
        # by confidence therefore gives it no weight at all.
        assert result.confidence == 0.0, name
        assert result.data["simulated"] is True, name
        assert result.data["checked"] is False, name
        assert "SIMULATED" in result.detail, name


def test_the_real_agent_ignores_the_flag(flag):
    """cap_removed is implemented, so the escape hatch must not touch it.

    If the flag could force a pass on a real checker it would be a way to fake
    evidence, which is worse than having no flag at all.
    """
    flag(True)
    result = AGENTS["cap_removed"].verify(Evidence())
    assert result.ok is False
    assert "missing" in result.detail
    assert not result.data.get("simulated")


def test_failure_detail_names_the_flag(flag):
    """Someone hitting the closed gate should learn how to open it."""
    flag(False)
    result = AGENTS["grasp_secure"].verify(Evidence())
    assert "HZ_ALLOW_UNIMPLEMENTED_VERIFIERS" in result.detail


def test_results_stay_well_formed_either_way(flag):
    for on in (False, True):
        flag(on)
        for name, agent in AGENTS.items():
            result = agent.verify(Evidence())
            assert isinstance(result, VerificationResult), name
            assert isinstance(result.ok, bool), name
            assert 0.0 <= result.confidence <= 1.0, name
