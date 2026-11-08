"""Hash-chained audit log. Deterministic (simulated clock, no wall time) so runs are reproducible."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

GENESIS = "0" * 64


def sim_clock(seq: int) -> str:
    s = 9 * 3600 + seq * 7
    return f"2026-10-31T{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z (sim)"


def _h(body: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def append(self, kind: str, data: Dict[str, Any]) -> int:
        seq = len(self.entries)
        body = {"seq": seq, "clock": sim_clock(seq), "kind": kind, "data": deepcopy(data),
                "prev": self.entries[-1]["hash"] if self.entries else GENESIS}
        self.entries.append({**body, "hash": _h(body)})
        return seq


def verify_chain(entries: List[Dict[str, Any]]) -> Tuple[bool, Optional[int], str]:
    prev = GENESIS
    for i, e in enumerate(entries):
        body = {k: e[k] for k in ("seq", "clock", "kind", "data", "prev")}
        if e.get("seq") != i or e.get("prev") != prev:
            return False, i, "chain link broken (entry removed, reordered or relinked)"
        if _h(body) != e.get("hash"):
            return False, i, "entry content does not match its hash (entry was altered)"
        prev = e["hash"]
    return True, None, "chain intact"


def tampered_copy(entries: List[Dict[str, Any]], index: int) -> List[Dict[str, Any]]:
    """Demonstration only: returns an altered COPY (the original log is never modified)."""
    c = deepcopy(entries)
    if c:
        i = min(max(index, 0), len(c) - 1)
        c[i]["data"] = {**c[i]["data"], "tampered": True, "reason": "attacker rewrote history"}
    return c
