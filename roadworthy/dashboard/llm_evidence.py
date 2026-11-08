"""Recorded genuine-LLM run: load + integrity-check the evidence file the user recorded with scripts/record_llm_evidence.py.

HONESTY RULES
  * This module never calls a model and never fabricates a run. If no file exists the answer is "not recorded".
  * The file is TAMPER-EVIDENT (self-hash, structural + invariant checks), not tamper-proof: anyone who can edit the file can
    also recompute the hash. The independent corroboration is the model provider's own usage log for the recorded time window.
  * The file holds hashes of prompts/responses, the proposed tool names/arguments and SENTINEL's decisions. It never holds a key,
    PayPal data, or raw model text.
Stdlib only. scripts/verify_llm_evidence.py carries its own copy of the checks so a reviewer can run it without this package.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "sentinel.llm-evidence/1"
PUBLIC_DATA = Path(__file__).resolve().parent.parent.parent / "public_data"
DEFAULT_PATH = PUBLIC_DATA / "llm_evidence.json"
MAX_BYTES = 256 * 1024
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SECRETISH = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}|bearer\s+\S{8,}|-----BEGIN [A-Z ]*KEY-----|api[_-]?key\s*[:=]")
_MODEL_RX = re.compile(r"^[A-Za-z0-9._:/-]{1,64}$")
GENUINE_HOSTS = {"openai": "api.openai.com", "anthropic": "api.anthropic.com"}      # a record made against any other endpoint is not a genuine provider run


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def self_hash(doc: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical({k: v for k, v in doc.items() if k != "self_sha256"})).hexdigest()


def seal(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc = {k: v for k, v in doc.items() if k != "self_sha256"}
    doc["self_sha256"] = self_hash(doc)
    return doc


def check(doc: Any) -> List[str]:
    """Return a list of problems (empty = every check passed)."""
    bad: List[str] = []
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    if doc.get("schema") != SCHEMA:
        bad.append("wrong schema")
    if not isinstance(doc.get("self_sha256"), str) or not _HEX64.match(doc["self_sha256"]) or doc["self_sha256"] != self_hash(doc):
        bad.append("self hash does not match the content")
    if not isinstance(doc.get("model"), str) or not _MODEL_RX.match(doc.get("model", "")):
        bad.append("model name missing or malformed")
    if doc.get("provider") not in ("openai", "anthropic"):
        bad.append("unknown provider")
    if not isinstance(doc.get("recorded_at"), str) or not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", doc.get("recorded_at", "")):
        bad.append("recorded_at must be UTC ISO-8601 (Z)")
    if doc.get("endpoint_host") != GENUINE_HOSTS.get(doc.get("provider")):
        bad.append("endpoint_host is not the provider's official API host")
    scs = doc.get("scenarios")
    if not isinstance(scs, list) or len(scs) != 3:
        return bad + ["expected exactly the three scenarios"]
    total = 0
    for sc in scs:
        sid = sc.get("id") if isinstance(sc, dict) else None
        if sid not in ("unsafe-refund", "false-claim", "injection"):
            bad.append(f"unknown scenario {sid!r}")
            continue
        calls = sc.get("model_calls")
        if not isinstance(calls, list) or not calls:
            bad.append(f"{sid}: no model call recorded")
            continue
        for c in calls:
            if not (isinstance(c, dict) and _HEX16.match(str(c.get("prompt_sha256", ""))) and _HEX16.match(str(c.get("response_sha256", "")))):
                bad.append(f"{sid}: malformed call hash")
            if isinstance(c, dict) and c.get("host") != doc.get("endpoint_host"):
                bad.append(f"{sid}: call host differs from endpoint_host")
            if isinstance(c, dict) and c.get("model") != doc.get("model"):
                bad.append(f"{sid}: call model differs from the recorded model")
        total += len(calls)
        for p in sc.get("proposals") or []:
            if p.get("decision") == "DENIED" and p.get("executed"):
                bad.append(f"{sid}: a DENIED proposal is marked executed")
        if sc.get("ledger_unchanged") is not True or sc.get("cash_out") != "0":
            bad.append(f"{sid}: simulated ledger was not untouched")
    if doc.get("total_model_calls") != total or not (1 <= total <= 6):
        bad.append("total_model_calls inconsistent (must equal the recorded calls, 1-6)")
    if _SECRETISH.search(json.dumps(doc)):
        bad.append("secret-shaped text found")
    return bad


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    base = {"label": "LLM-RECORDED", "replayed": True,
            "meaning": "A genuine model run that was recorded once on the maintainer's machine and is replayed here. No model is called by this page.",
            "limits": "Tamper-evident (self-hash and consistency checks), not tamper-proof. The model provider's own usage log for the recorded window is the independent corroboration. Enforcement in the run was SIMULATED."}
    try:
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            return {**base, "available": False, "note": "No recorded run is published yet."}
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {**base, "available": False, "note": "The recorded run could not be read."}
    problems = check(doc)
    if problems:
        return {**base, "available": False, "note": "A recorded run is present but failed its integrity checks and is not shown.", "problems": problems[:5]}
    return {**base, "available": True, "recorded_at": doc["recorded_at"], "provider": doc["provider"], "model": doc["model"], "endpoint_host": doc["endpoint_host"],
            "total_model_calls": doc["total_model_calls"], "self_sha256": doc["self_sha256"], "verified": True,
            "scenarios": [{"id": s["id"], "label": s.get("label", s["id"]), "prompt": str(s.get("prompt", ""))[:600], "model_calls": len(s["model_calls"]),
                           "proposals": [{k: p.get(k) for k in ("tool", "args_short", "decision", "outcome", "executed", "layer")} for p in s.get("proposals") or []],
                           "verdict": s.get("verdict"), "ledger_unchanged": True} for s in doc["scenarios"]],
            "verify_with": "python scripts/verify_llm_evidence.py public_data/llm_evidence.json"}
