"""Verification agents return a well-formed result for empty evidence."""
from core.verification.agents import AGENTS, Evidence, VerificationResult


def test_all_agents_registered():
    assert set(AGENTS) == {"cap_removed", "grasp_secure", "tube_aligned", "aspiration_ok"}


def test_agents_return_verification_results():
    ev = Evidence()
    for name, agent in AGENTS.items():
        result = agent.verify(ev)
        assert isinstance(result, VerificationResult), name
        assert isinstance(result.ok, bool)
        assert 0.0 <= result.confidence <= 1.0
