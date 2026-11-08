"""Trust Engine helpers: build graded evidence and replay how the earned-permission engine reacts to it.
Uses the real engine (roadworthy.permissions.engine); nothing is re-implemented here."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List

from ..capabilities import HARD_CEILING, ceiling_of
from ..eval.models import Grade
from ..permissions.engine import Evidence, License, Thresholds, aggregate, decide, issue_license, wilson_lcb

EVENTS = ("pass", "fail", "false_claim", "harmful", "forbidden", "dup")


def make_grade(cap: str, event: str, agent_id: str, agent_kind: str = "scripted_baseline", n: int = 0, synthetic: bool = True) -> Grade:
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    kw = dict(task_success=event == "pass", false_claims=int(event == "false_claim"), unverifiable_claims=0,
              verified_claims=int(event == "pass"), harmful_effects=int(event == "harmful"),
              duplicate_effects=int(event == "dup"), missing_effects=int(event == "fail"),
              attempted_forbidden_tools=int(event == "forbidden"), claims_malformed=False)
    outcome = {"pass": "PASS", "fail": "FAIL_TASK", "false_claim": "FALSE_CLAIM"}.get(event, "HARMFUL")
    h = hashlib.sha256(f"{agent_id}|{cap}|{event}|{n}".encode()).hexdigest()
    return Grade("sim", f"{agent_id}-{n}", agent_id, agent_kind, cap, outcome=outcome, verdict_hash=h, synthetic=synthetic, **kw)


def history_grades(agent_id: str, history: Dict[str, Dict[str, int]], agent_kind: str = "scripted_baseline") -> List[Grade]:
    """history = {capability: {"pass": 17, "false_claim": 1, ...}} -> graded records."""
    out, n = [], 0
    for cap, counts in history.items():
        for ev, k in counts.items():
            for _ in range(k):
                out.append(make_grade(cap, ev, agent_id, agent_kind, n=n)); n += 1
    return out


def timeline(capability: str, events: Iterable[str], agent_id: str = "trust-lab") -> Dict[str, Any]:
    if capability not in HARD_CEILING:
        raise ValueError("unknown capability")
    th, steps, grades = Thresholds(), [], []
    prev_level = None
    for i, ev in enumerate(events):
        grades.append(make_grade(capability, ev, agent_id, n=i))
        e = aggregate(grades)[capability]
        d = decide(capability, e, th)
        lcb = wilson_lcb(e.successes, e.trials)
        steps.append({"i": i + 1, "event": ev, "level": d.level.name, "earned": d.earned.name, "ceiling": d.ceiling.name,
                      "lcb": round(lcb, 4), "trials": e.trials, "successes": e.successes, "reasons": d.reasons,
                      "changed": prev_level is not None and prev_level != d.level.name,
                      "from": prev_level, "revoked": prev_level is not None and d.level.name == "BLOCKED" and prev_level != "BLOCKED"})
        prev_level = d.level.name
    cur = steps[-1] if steps else {"level": "APPROVAL_REQUIRED" if not capability.startswith("read.") else ceiling_of(capability).name,
                                    "earned": "-", "ceiling": ceiling_of(capability).name, "lcb": 0.0, "trials": 0, "successes": 0,
                                    "reasons": ["no evidence yet: writes start at APPROVAL_REQUIRED"]}
    return {"capability": capability, "ceiling": ceiling_of(capability).name, "steps": steps, "current": cur,
            "thresholds": {"min_trials_auto": th.min_trials_auto, "min_success_lcb": th.min_success_lcb,
                           "min_trials_block": th.min_trials_block, "block_success_ub": th.block_success_ub}}


def license_view(lic: License, extra_caps: Iterable[str] = ()) -> List[Dict[str, Any]]:
    rows = []
    for cap in sorted(set(HARD_CEILING) | set(lic.decisions)):
        d = lic.decisions.get(cap)
        rows.append({"capability": cap, "level": lic.level_for(cap).name, "ceiling": ceiling_of(cap).name,
                     "earned": d.earned.name if d else "-", "trials": d.evidence.trials if d else 0,
                     "successes": d.evidence.successes if d else 0, "reasons": d.reasons if d else ["no evidence: default policy"]})
    return rows
