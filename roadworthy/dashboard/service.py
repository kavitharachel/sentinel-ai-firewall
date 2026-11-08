"""Builds every dashboard payload from the real engine. Pure functions over deterministic simulations plus the frozen
live-evidence loader, and the opt-in LLM agent session handling. No PayPal client, no PayPal credentials."""
from __future__ import annotations

import os
import secrets
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..capabilities import HARD_CEILING, MAX_ORDER_TOTAL, MAX_REFUND_AMOUNT
from ..permissions.engine import diff_licenses
from ..redact import scrub
from . import live as livemod
from .agents import AGENT_IDS, BRAINS, TASKS, LLMBrain, run_agent, short_args
from . import console
from .attacks import list_attacks, run_all, run_attack
from .audit import AuditLog, tampered_copy, verify_chain
from .narrate import SIM as _SIM, _claim_sentence, _enforcement, _evidence_sentence
from .llm import CallBudget, DEFAULT_BUDGET, LLMClient, LLMError, load_config
from .timemachine import replay
from .trust import EVENTS, license_view, timeline

SIM = "SIMULATED"
PUBLIC_DATA = Path(__file__).resolve().parent.parent.parent / "public_data"


def is_public() -> bool:
    """Public-demo mode. Read on every call so it cannot be bypassed by import order. Real LLM calls are impossible in this mode."""
    return os.environ.get("SENTINEL_PUBLIC", "") == "1"


def _load_live(live_dir: Optional[Path]) -> Dict[str, Any]:
    if is_public():
        return livemod.load_public_summary(PUBLIC_DATA / "public_evidence.json")      # never the raw evidence folder
    return livemod.load_live(live_dir)


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _clean_run(r: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(r)
    before, after = r["license_before"], r.pop("license_after")
    r["license_before"], r["license_after"] = license_view(before), license_view(after)
    r["license_changes"] = [{"capability": c, "from": a, "to": b} for c, (a, b) in diff_licenses(before, after).items()]
    r["grades"] = [{k: v for k, v in g.items() if k != "grade"} for g in r["grades"]]
    for e in r["log"]:
        e["args_short"] = short_args(e.get("args")) if e.get("args") is not None else None
    r["tasks"] = [{"id": t.id, "source": t.source, "trusted": t.trusted, "text": t.text} for t in TASKS]
    return r


# ---------------------------------------------------------------------------------------------- opt-in LLM agent
_TRANSPORT = None                                   # tests inject a fake transport; production uses llm._http_post
_SESSIONS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_BUDGET: Optional[CallBudget] = None
MAX_SESSIONS = 8
CONSENT = ("The simulated task text (including the simulated customer email) and the tool list will be sent to {host}. "
           "No PayPal data, live evidence or credentials are sent.")


def _budget() -> CallBudget:
    global _BUDGET
    if _BUDGET is None:
        try:
            limit = int(os.environ.get("SENTINEL_LLM_MAX_CALLS", DEFAULT_BUDGET))
        except ValueError:
            limit = DEFAULT_BUDGET
        _BUDGET = CallBudget(max(1, min(limit, 500)))
    return _BUDGET


PUBLIC_LLM_OFF = "Public demo: real LLM calls are disabled on this server. The scripted fallback and the recorded genuine run are available."


def _cfg():
    from .llm import LLMConfig
    if is_public():
        return LLMConfig(False, "", "", "", [PUBLIC_LLM_OFF])        # no provider, no model, no key is ever read in public mode
    return load_config(os.environ)


def llm_evidence_info() -> Dict[str, Any]:
    from . import llm_evidence
    return llm_evidence.load()


def _public_llm() -> Dict[str, Any]:
    return {"enabled": False, "provider": None, "model": None, "destination_host": None, "key_configured": False, "key_needed": False,
            "reasons": [PUBLIC_LLM_OFF], "budget": {"limit": 0, "used": 0}, "consent_text": "", "public": True, "sessions": 0, "world": SIM,
            "guarantee": "Real LLM calls are disabled on the public server. Enforcement shown is simulated."}


def llm_status() -> Dict[str, Any]:
    if is_public():
        return _public_llm()
    cfg = _cfg()
    b = _budget()
    return {**cfg.public(), "budget": {"limit": b.limit, "used": b.used}, "consent_text": CONSENT.format(host=cfg.host or "the configured provider"),
            "sessions": len(_SESSIONS), "world": SIM,
            "guarantee": "The model only proposes. Every proposal goes through the same hard limits, licence, approval gate and independent verifier as the scripted agents."}


def _llm_brain(session: Optional[str], consent: bool, instruction: Optional[str] = None):
    if is_public():
        raise ServiceError(403, PUBLIC_LLM_OFF)
    cfg = _cfg()
    if not cfg.enabled:
        raise ServiceError(409, "The LLM agent is not enabled. " + "; ".join(cfg.reasons))
    if session:
        sess = _SESSIONS.get(session)
        if sess is None:
            raise ServiceError(409, "Unknown or expired LLM session; start a new run")
        if (sess["provider"], sess["model"]) != (cfg.provider, cfg.model):
            raise ServiceError(409, "LLM configuration changed; start a new run")
        _SESSIONS.move_to_end(session)
        cache, sid = sess["cache"], session
    else:
        if consent is not True:
            raise ServiceError(400, "Consent required: " + CONSENT.format(host=cfg.host))
        sid, cache = secrets.token_hex(8), {}
        _SESSIONS[sid] = {"cache": cache, "provider": cfg.provider, "model": cfg.model}
        while len(_SESSIONS) > MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    client = LLMClient(cfg, _TRANSPORT, _budget())
    if instruction is not None:
        return console.ConsoleLLMBrain(client, cache, cfg.provider, cfg.model, instruction=instruction), sid, cfg
    return LLMBrain(client, cache, cfg.provider, cfg.model), sid, cfg


def agent_run(brain: str, approvals: Optional[List[str]] = None, consent: bool = False, session: Optional[str] = None) -> Dict[str, Any]:
    ok = {a for a in (approvals or []) if isinstance(a, str) and len(a) < 12}
    if brain == "llm":
        lb, sid, cfg = _llm_brain(session, consent is True)
        try:
            r = run_agent("llm", ok, llm_brain=lb)
        except LLMError as e:
            if not session:
                _SESSIONS.pop(sid, None)
            raise ServiceError(e.status, scrub(str(e)))
        out = _clean_run(r)
        out["llm"] = {"provider": cfg.provider, "model": cfg.model, "host": cfg.host, "session": sid, "calls_this_request": len(lb.client.calls),
                      "calls": lb.client.calls, "role": "proposes only; SENTINEL executes or refuses"}
        return out
    if brain not in BRAINS:
        raise ValueError("unknown agent")
    return _clean_run(run_agent(brain, ok))


def timemachine(brain: str, gateway: bool, verifier: bool, human: bool, session: Optional[str] = None) -> Dict[str, Any]:
    if brain == "llm":
        if not session:
            raise ServiceError(409, "Run the LLM agent first; the Time Machine replays its recorded proposals")
        lb, _sid, _cfg = _llm_brain(session, False)
        lb.client = None                          # a replay never calls the model
        try:
            return replay("llm", bool(gateway), bool(verifier), bool(human), llm_brain=lb)
        except LLMError as e:
            raise ServiceError(e.status, scrub(str(e)))
    if brain not in BRAINS:
        raise ValueError("unknown agent")
    return replay(brain, bool(gateway), bool(verifier), bool(human))


# ---------------------------------------------------------------------------------------------- claims
def _human(c: Dict[str, Any]) -> str:
    t = c["type"]
    if t == "created": return f"{c['section'][:-1].capitalize()} {c['id']} was created"
    if t == "state": return f"{c['section'][:-1].capitalize()} {c['id']}: {c['field']} = {c['equals']}"
    if t == "exists": return f"{c['id']} exists"
    if t == "count": return f"{c['section']} count = {c['equals']}"
    return "No state change occurred"


def _mismatch(c: Dict[str, Any], after: Dict[str, Any], verdict: str) -> List[Dict[str, Any]]:
    if verdict != "CONTRADICTED":
        return []
    row = (after.get(c.get("section"), {}) or {}).get(c.get("id"))
    if c["type"] == "state":
        return [{"field": c["field"], "claimed": c["equals"], "actual": (row or {}).get(c["field"], "(object not found)")}]
    if c["type"] == "created":
        return [{"field": "existence", "claimed": f"{c['id']} created", "actual": "no such record was created"}]
    return [{"field": c["type"], "claimed": str(c.get("equals", c.get("value", ""))), "actual": "differs"}]


def _evidence_line(c: Dict[str, Any], after: Dict[str, Any]) -> str:
    row = (after.get(c.get("section"), {}) or {}).get(c.get("id"))
    if c["type"] == "created":
        return f"No {c['section'][:-1]} {c['id']} exists in the ledger"
    if c["type"] == "state":
        return f"{c['section'][:-1].capitalize()} {c['id']} {c['field']} is {(row or {}).get(c['field'], 'missing')}"
    return "Ledger state differs"


def _narrate(entry: Dict[str, Any]) -> Dict[str, Any]:
    live = entry["source"] != SIM
    c, v = entry["claim"], entry["verdict"]
    entry["say"] = {
        "claim": _claim_sentence(c, live),
        "evidence": _evidence_sentence(entry, live),
        "verdict": {"VERIFIED": "The evidence backs the claim.", "CONTRADICTED": "The evidence contradicts the claim. The agent said something that did not happen.",
                    "UNVERIFIABLE": "There is not enough evidence to check this claim."}.get(v, v),
        "enforcement": _enforcement(entry, live),
        "world": ("Recorded PayPal sandbox evidence (frozen, read-only)" if live else "Simulated ledger: play money, nothing real"),
    }
    return entry


def claims(live_dir: Optional[Path] = None) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    for brain in ("diligent", "eager"):
        audit = AuditLog()
        r = run_agent(brain, audit=audit)
        for i, v in enumerate(r["verdicts"]):
            c = v["claim"]
            out.append({"id": f"{brain[:3].upper()}-{i + 1}", "source": SIM, "agent": r["agent_id"], "text": _human(c), "claim": c, "verdict": v["verdict"],
                        "detail": v["detail"], "mismatch": _mismatch(c, r["after"], v["verdict"]),
                        "evidence": {"section": c.get("section"), "id": c.get("id"), "row": (r["after"].get(c.get("section"), {}) or {}).get(c.get("id"))},
                        "audit_seq": v["audit_seq"], "audit": audit.entries, "run": brain})
    lv = _load_live(live_dir)
    for i, c in enumerate(livemod.live_claims(lv)):
        c["id"] = f"LIVE-{i + 1}"
        c["mismatch"] = ([{"field": c["claim"]["field"], "claimed": c["claim"]["equals"], "actual": c["evidence"]["actual"]}] if c["verdict"] == "CONTRADICTED" else [])
        c["audit"], c["audit_seq"], c["run"] = [], None, "live"
        out.append(c)
    return {"claims": [_narrate(e) for e in out], "live_available": lv["available"]}


# ---------------------------------------------------------------------------------------------- command center story
@lru_cache(maxsize=1)
def _attack_summary() -> Dict[str, int]:
    rows = run_all()
    return {"total": len(rows), "stopped": sum(1 for r in rows if r["blocked"])}


def _case(r: Dict[str, Any]) -> Dict[str, Any]:
    vs, after = r["verdicts"], r["after"]
    bad = [v for v in vs if v["verdict"] == "CONTRADICTED"]
    blocked = [{"tool": e["tool"], "why": e["reason"], "args": short_args(e["args"]), "layer": "hard limit" if e["reason"].startswith("hard limit") else "licence"}
               for e in r["log"] if e["tool"] and not e["executed"] and not e["awaiting"]]
    needs_human = [{"tool": e["tool"], "args": short_args(e["args"]), "key": e["key"]} for e in r["log"] if e["awaiting"] and e["authorized"]]
    autonomous = [{"tool": e["tool"], "args": short_args(e["args"]), "id": (e["result"] or {}).get("id")} for e in r["log"]
                  if e["executed"] and e["action"] == "ALLOW" and not (e["capability"] or "").startswith("read.")]
    changes = [{"capability": c, "from": a, "to": b} for c, (a, b) in diff_licenses(r["license_before"], r["license_after"]).items()]
    return {"agent": r["agent_id"], "kind": r["agent_kind"], "source": SIM,
            "said": ((r["final_message"].split("\nCLAIMS:")[0].strip().split(". ")[0].rstrip(".") + ".") if r["final_message"].strip() else "(no report)")[:140],
            "claims": [{"text": _human(v["claim"]), "verdict": v["verdict"]} for v in vs],
            "evidence": [_evidence_line(v["claim"], after) + (f" (agent said {v['claim']['equals']})" if v["claim"].get("type") == "state" else "") for v in bad[:3]],
            "counts": {k: sum(1 for v in vs if v["verdict"] == k) for k in ("VERIFIED", "CONTRADICTED", "UNVERIFIABLE")},
            "blocked": blocked, "revoked": changes, "needs_human": needs_human, "autonomous": autonomous}


def story(live_dir: Optional[Path] = None) -> Dict[str, Any]:
    eager, diligent = run_agent("eager"), run_agent("diligent")
    lv = _load_live(live_dir)
    lc = livemod.live_claims(lv)
    bad = next((c for c in lc if c["verdict"] == "CONTRADICTED"), None)
    live = None
    if bad:
        live = {"source": livemod.LIVE, "claim": bad["text"], "actual": bad["evidence"]["actual"], "field": bad["evidence"]["field"],
                "verified": sum(1 for c in lc if c["verdict"] == "VERIFIED"), "of": len(lc), "recorded_at": bad["evidence"]["recorded_at"]}
    return {"case": _case(eager), "contrast": _case(diligent), "live": live}


def overview(live_dir: Optional[Path] = None) -> Dict[str, Any]:
    st = story(live_dir)
    cl = claims(live_dir)["claims"]
    lv = _load_live(live_dir)
    verdicts = {"VERIFIED": 0, "CONTRADICTED": 0, "UNVERIFIABLE": 0}
    for c in cl:
        verdicts[c["verdict"]] += 1
    groups: Dict[tuple, Dict[str, Any]] = {}
    blocked = pending = unsafe_executed = 0
    for brain in ("diligent", "eager"):
        r = run_agent(brain)
        unsafe_executed += sum(1 for e in r["log"] if e["tool"] and e["executed"] and not e["authorized"])
        for e in r["log"]:
            if e["tool"] and not e["executed"]:
                if e["awaiting"]:
                    pending += 1; sev, title = "info", f"Approval needed: {e['tool']}"
                else:
                    blocked += 1; sev, title = ("critical" if e["capability"] in ("dispute.accept", "invoice.record_external") or not e["authorized"] else "warn"), f"Blocked: {e['tool']}"
                g = groups.setdefault((sev, title, r["agent_id"]), {"sev": sev, "agent": r["agent_id"], "title": title, "detail": e["reason"], "count": 0, "source": SIM})
                g["count"] += 1
        n = sum(1 for v in r["verdicts"] if v["verdict"] == "CONTRADICTED")
        if n:
            groups[("warn", "False claims detected", r["agent_id"])] = {"sev": "warn", "agent": r["agent_id"], "title": "False claims detected", "count": n, "source": SIM,
                                                                        "detail": "Reports of actions that never happened were contradicted by the ledger"}
    order = {"critical": 0, "warn": 1, "info": 2}
    alerts = sorted(groups.values(), key=lambda a: (order[a["sev"]], -a["count"]))
    tm = replay("eager", human_approves=False)["summary"]
    atk = _attack_summary()
    kpi = {"claims": len(cl), **{k.lower(): v for k, v in verdicts.items()}, "actions_blocked": blocked, "approvals_pending": pending,
           "loss_prevented_simulated": tm["prevented"], "attacks_stopped": atk["stopped"], "attacks_total": atk["total"]}
    proof = [{"label": "Claims that were false", "value": f"{verdicts['CONTRADICTED']} of {len(cl)}", "tone": "bad", "sub": "contradicted by independent evidence"},
             {"label": "Unsafe actions executed", "value": str(unsafe_executed), "tone": "ok" if unsafe_executed == 0 else "bad", "sub": f"{blocked} blocked at the tool boundary"},
             {"label": "Loss prevented", "value": "$" + format(float(tm["prevented"]), ".2f"), "tone": "ok", "sub": "Time Machine replay", "tag": SIM},
             {"label": "Attacks stopped", "value": f"{atk['stopped']}/{atk['total']}", "tone": "ok", "sub": "reproducible in the Attack Lab", "tag": SIM}]
    live_card = {"available": lv["available"], "label": livemod.LIVE, "note": lv["note"], "runs": lv["runs"], "profile_check": lv["profile_check"]}
    return {"story": st, "proof": proof, "kpi": kpi, "alerts": alerts[:6], "alerts_total": len(alerts), "live": live_card,
            "limits": {"max_refund": MAX_REFUND_AMOUNT, "max_order": MAX_ORDER_TOTAL, "currency": "USD", "environment": "SANDBOX"},
            "agents": [{"id": AGENT_IDS[b], "name": b} for b in BRAINS]}


def audit_verify(brain: str, tamper_index: Optional[int] = None) -> Dict[str, Any]:
    log = AuditLog()
    run_agent(brain, audit=log)
    entries = log.entries if tamper_index is None else tampered_copy(log.entries, tamper_index)
    ok, idx, why = verify_chain(entries)
    return {"entries": entries, "ok": ok, "first_bad": idx, "why": why, "tampered_demo": tamper_index is not None, "world": SIM}


def trust(capability: str, events: List[str]) -> Dict[str, Any]:
    if capability not in HARD_CEILING:
        raise ValueError("unknown capability")
    ev = [e for e in events if e in EVENTS][:200]
    return {**timeline(capability, ev), "events": ev, "capabilities": sorted(HARD_CEILING), "world": SIM}


def attacks() -> Dict[str, Any]:
    return {"attacks": list_attacks(), "world": SIM}


# ---------------------------------------------------------------------------------------------- Agent Console
def console_info() -> Dict[str, Any]:
    st = llm_status()
    llm = _public_llm() if is_public() else {k: st[k] for k in ("enabled", "provider", "model", "destination_host", "key_configured", "key_needed", "reasons", "budget", "consent_text", "enable_with")}
    return {"public": is_public(), "suggestions": console.SUGGESTIONS, "scenarios": console.SCENARIOS, "agents": console.AGENTS, "sources": console.SOURCES, "max_prompt": console.MAX_PROMPT,
            "llm": llm,
            "limits": {"max_refund": str(MAX_REFUND_AMOUNT), "max_order": str(MAX_ORDER_TOTAL), "currency": "USD"}, "world": SIM,
            "data_sent_to_model": "Only the instruction text, the tool list, and (for the report) one line per recorded outcome. No ledger dump, no PayPal data, no credentials.",
            "never_sent": "PayPal credentials, recorded sandbox evidence, ledger contents, your API key (it stays in the server environment)"}


def console_run(prompt: Any, source: Any, agent: Any, approvals: Optional[List[str]] = None, consent: bool = False, session: Optional[str] = None) -> Dict[str, Any]:
    ok = {a for a in (approvals or []) if isinstance(a, str) and len(a) < 24}
    try:
        text = console.clean_prompt(prompt)
        src, ag = str(source), str(agent)
        if src not in console.SOURCES or ag not in console.AGENTS:
            raise ValueError("unknown source or agent")
        if ag != "llm":
            return console.run_turn(text, src, ag, ok)
        lb, sid, cfg = _llm_brain(session, consent is True, text)
        try:
            out = console.run_turn(text, src, ag, ok, lb)
        except LLMError as e:
            if not session:
                _SESSIONS.pop(sid, None)
            raise ServiceError(e.status, scrub(str(e)))
        out["llm"] = {"provider": cfg.provider, "model": cfg.model, "host": cfg.host, "session": sid, "calls": lb.client.calls,
                      "role": "proposes only; SENTINEL executes or refuses"}
        return out
    except ValueError as e:
        raise ServiceError(400, str(e))
