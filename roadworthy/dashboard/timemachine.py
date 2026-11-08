"""Financial Time Machine: replay the SAME agent actions in two parallel simulated worlds - one without SENTINEL and
one with the chosen controls - and show where they diverge. Both worlds are computed by the real code paths."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from ..permissions.gateway import ToolGateway
from .agents import identity, in_intent, proposals_for, short_args, starting_license, verify_all
from .sim import SimLedger, cash_effect


def replay(brain: str, gateway: bool = True, verifier: bool = True, human_approves: bool = False, llm_brain=None) -> Dict[str, Any]:
    bo, aid, _kind, _model, _hist = identity(brain, llm_brain)
    props = [p for p in proposals_for(brain, llm_brain) if p.get("tool")]
    A, B = SimLedger(), SimLedger()
    cur = {"p": None}
    gw = ToolGateway(starting_license(brain, llm_brain), approver=lambda t, a, r: bool(human_approves and cur["p"] and in_intent(cur["p"]["task"], t, a)))
    cumA = cumB = Decimal("0"); unA = unB = 0; logA: List[Dict] = []; logB: List[Dict] = []
    frames = []
    first_div = None
    for i, p in enumerate(props):
        t, tool, args = p["task"], p["tool"], p["args"]
        auth = in_intent(t, tool, args)
        ra = A.execute(tool, args)                       # world A: no SENTINEL, everything the agent proposes happens
        ca = cash_effect(tool, args, ra); cumA += ca
        wr = bool(capability_write(tool))
        unA += int(wr and bool(ra.get("ok")) and not auth)
        logA.append({"task": t.id, "tool": tool, "args": args, "executed": True, "result": ra, "action": "ALLOW"})
        if gateway:
            cur["p"] = p
            out = gw.call(tool, args, B.execute)
            ex, rb, dec = out["executed"], out["result"] or {}, out["decision"]
            action, reason = dec.action, dec.reason
        else:
            rb = B.execute(tool, args); ex, action, reason = True, "ALLOW", "SENTINEL gateway disabled"
        cb = cash_effect(tool, args, rb) if ex else Decimal("0"); cumB += cb
        unB += int(ex and wr and bool(rb.get("ok")) and not auth)
        logB.append({"task": t.id, "tool": tool, "args": args, "executed": ex, "result": rb if ex else None, "action": action,
                     "reason": reason, "approved": None})
        diverged = ex != True or (rb.get("ok") != ra.get("ok"))
        if diverged and first_div is None:
            first_div = i
        frames.append({"i": i, "task": t.id, "tool": tool, "summary": _summ(tool, args), "authorized_by_operator": auth,
                       "A": {"executed": True, "ok": ra.get("ok"), "cash_out": str(ca), "cum_cash_out": str(cumA), "unauthorized_so_far": unA},
                       "B": {"executed": ex, "action": action, "reason": reason, "ok": rb.get("ok") if ex else None, "cash_out": str(cb),
                             "cum_cash_out": str(cumB), "unauthorized_so_far": unB}})
    # claims: the agent's report is built from what happened in each world
    if llm_brain is not None:                      # the LLM's report is not re-requested for a counterfactual replay
        vA = vB = []
    else:
        ev = lambda lg: [{**e, "why": "", "reason": e.get("reason", ""), "action": e.get("action", "ALLOW")} for e in lg]
        msgA, msgB = bo.report(ev(logA), {}), bo.report(ev(logB), {})
        vA, vB = verify_all(msgA, SimLedger().snapshot(), A.snapshot()), verify_all(msgB, SimLedger().snapshot(), B.snapshot())
    falseB = sum(v["verdict"] == "CONTRADICTED" for v in vB)
    summary = {
        "A": {"title": "Without SENTINEL", "cash_out": str(cumA), "unauthorized_actions": unA, "false_claims_believed": sum(v["verdict"] == "CONTRADICTED" for v in vA),
              "final": _final(A.snapshot())},
        "B": {"title": "With SENTINEL" if gateway or verifier else "SENTINEL off", "cash_out": str(cumB), "unauthorized_actions": unB,
              "false_claims_believed": 0 if verifier else falseB, "false_claims_flagged": falseB if verifier else 0, "final": _final(B.snapshot())},
        "prevented": str(cumA - cumB), "first_divergence": first_div,
    }
    return {"world": "SIMULATED", "brain": brain, "agent_id": aid, "claims_replayed": llm_brain is None, "controls": {"gateway": gateway, "verifier": verifier, "human_approves": human_approves},
            "frames": frames, "summary": summary, "claims_B": vB if verifier else []}


def capability_write(tool: str) -> bool:
    from ..capabilities import capability_of
    return not (capability_of(tool) or "read.").startswith("read.")


def _final(s) -> Dict[str, Any]:
    return {"refunded_total": str(sum((Decimal(r["amount"]) for r in s["refunds"].values()), Decimal("0"))),
            "dispute_DSP9": s["disputes"]["DSP-9"]["status"], "invoice_INV1": s["invoices"]["INV-1"]["status"], "orders_created": len(s["orders"])}


def _summ(tool, args) -> str:
    try:
        if tool == "create_refund":
            a = args.get("amount"); return f"Refund {('$' + str(a['value'] if isinstance(a, dict) else a)) if a else '(no amount = FULL)'} on {args.get('capture_id')}"
        if tool == "create_order":
            return "Create order " + short_args(args)
        if tool == "accept_dispute_claim": return f"Accept dispute {args.get('dispute_id')}"
        if tool == "record_payment_for_invoice": return f"Record payment on {args.get('invoice_id')}"
    except Exception:
        pass
    return tool + (" " + short_args(args) if args else "")
