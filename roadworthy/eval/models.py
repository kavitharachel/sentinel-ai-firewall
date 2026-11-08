"""Data model for graded agent runs."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

AGENT_KINDS = ("scripted_baseline", "llm_agent")


@dataclass
class ToolCall:
    name: str
    outcome: str = "ok"           # ok | error | blocked_by_gateway
    args_digest: str = ""         # hash of arguments; raw arguments are not stored


@dataclass
class AgentRun:
    run_id: str
    scenario_id: str
    agent_id: str
    agent_kind: str                       # scripted_baseline | llm_agent (kept strictly separate in all reports)
    model: Optional[str]                  # required for llm_agent; None for baselines
    tool_calls: List[ToolCall]
    final_message: str
    before: Dict[str, Any]                # PayPal snapshot before the run
    after: Dict[str, Any]                 # PayPal snapshot after the run
    synthetic: bool = False               # True only for hand-made test fixtures; never True for real runs

    def __post_init__(self) -> None:
        if self.agent_kind not in AGENT_KINDS:
            raise ValueError(f"agent_kind must be one of {AGENT_KINDS}")
        if self.agent_kind == "llm_agent" and not self.model:
            raise ValueError("an llm_agent run must name its model")
        if self.agent_kind == "scripted_baseline" and self.model:
            raise ValueError("a scripted_baseline must not claim a model")


@dataclass
class ScenarioSpec:
    scenario_id: str
    capability: str
    description: str
    required_effects: List[Dict[str, Any]] = field(default_factory=list)   # each must occur exactly once
    allowed_effects: List[Dict[str, Any]] = field(default_factory=list)    # tolerated extras
    forbidden_effects: List[Dict[str, Any]] = field(default_factory=list)  # any occurrence is harm
    forbidden_tools: List[str] = field(default_factory=list)               # attempting these is a violation
    requires_claims: bool = True                                           # an agent must report what it did


@dataclass
class Grade:
    scenario_id: str
    run_id: str
    agent_id: str
    agent_kind: str
    capability: str
    task_success: bool
    false_claims: int
    unverifiable_claims: int
    verified_claims: int
    harmful_effects: int
    duplicate_effects: int
    missing_effects: int
    attempted_forbidden_tools: int
    claims_malformed: bool
    outcome: str                  # PASS | FAIL_TASK | FALSE_CLAIM | HARMFUL
    verdict_hash: str
    synthetic: bool = False
