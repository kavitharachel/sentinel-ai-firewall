"""Compute the actual state change between two snapshots. Effects are what really happened in PayPal."""
from __future__ import annotations
from typing import Any, Dict, List

Effect = Dict[str, Any]


def diff_snapshots(before: Dict[str, Dict[str, Dict[str, Any]]], after: Dict[str, Dict[str, Dict[str, Any]]]) -> List[Effect]:
    effects: List[Effect] = []
    for section in sorted(set(before) | set(after)):
        b, a = before.get(section, {}), after.get(section, {})
        if "__unavailable__" in b or "__unavailable__" in a:
            effects.append({"section": section, "id": "*", "kind": "unobservable"})
            continue
        for _id in sorted(set(b) | set(a)):
            if _id not in b:
                effects.append({"section": section, "id": _id, "kind": "created", **{f"new_{k}": v for k, v in a[_id].items()}})
            elif _id not in a:
                effects.append({"section": section, "id": _id, "kind": "deleted", **{f"old_{k}": v for k, v in b[_id].items()}})
            else:
                for f in sorted(set(b[_id]) | set(a[_id])):
                    if b[_id].get(f) != a[_id].get(f):
                        effects.append({"section": section, "id": _id, "kind": "changed", "field": f,
                                        "from": b[_id].get(f), "to": a[_id].get(f)})
    return effects


def matches(effect: Effect, matcher: Dict[str, Any]) -> bool:
    """Subset match: every key in matcher must equal the effect's value ('*' matches anything)."""
    return all(v == "*" or effect.get(k) == v for k, v in matcher.items())
