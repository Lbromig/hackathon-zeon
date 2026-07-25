"""P0 AI-agent orchestration: normal-mode step-by-step selector loop.

The agent picks the next action each step from *existing, vetted skills* based on
the world-model + verification state, executes it deterministically, verifies, and
supports human checkpoints. No parameter tuning, no recovery mode, no code
generation (those are P1+ in ``docs/AGENT_ORCHESTRATION.md``).

The policy (the "brain") is pluggable: :class:`~backend.app.agent.policy.ClaudePolicy`
wires the Claude Agent SDK / Messages tool-use structure but is gated behind
``ANTHROPIC_API_KEY``; :class:`~backend.app.agent.policy.RuleBasedPolicy` is a
deterministic offline default so the loop runs and is tested without a network or key.
"""
from __future__ import annotations

from .engine import GOAL, Engine
from .policy import (
    ClaudePolicy,
    Decision,
    Observation,
    Policy,
    RuleBasedPolicy,
    default_policy,
)
from .tools import SKILLS, TOOL_SCHEMAS, Skill, Tool, Toolbox, build_tools

__all__ = [
    "Engine", "GOAL",
    "Policy", "Decision", "Observation",
    "RuleBasedPolicy", "ClaudePolicy", "default_policy",
    "Toolbox", "Tool", "Skill", "SKILLS", "TOOL_SCHEMAS", "build_tools",
]
