#!/usr/bin/env python3
"""Record ONE genuine hosted-LLM run of the three scenarios into public_data/llm_evidence.json.

Run it yourself, in the shell where the key lives (it is read from the environment by the existing client and never printed or stored):

    $env:SENTINEL_LLM_ENABLE="1"; $env:SENTINEL_LLM_PROVIDER="openai"; $env:SENTINEL_LLM_MODEL="gpt-4.1-mini"; $env:SENTINEL_LLM_API_KEY="<key>"
    python scripts/record_llm_evidence.py            # up to 6 paid model calls, hard-capped
    python scripts/verify_llm_evidence.py public_data/llm_evidence.json

Nothing is invented: the file is built only from what service.console_run returned for real model calls. If any call fails, nothing is written.
Refuses to run if SENTINEL_PUBLIC=1, if the LLM is not configured, or if the output already exists (pass --overwrite).
Never contacts PayPal. The recorded enforcement is SIMULATED.
"""
import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAX_CALLS = 6


def build(service, console, llm_evidence, now=None):
    """Pure given a working service: run the scenarios and return the sealed document. Raises on any failure."""
    st = service.llm_status()
    if not st["enabled"]:
        raise SystemExit("NOT RUN. " + "; ".join(st["reasons"]))
    info = service.console_info()
    session, scs = None, []
    for sc in console.SCENARIOS:
        sg = next(x for x in info["suggestions"] if x["id"] == sc["suggestion"])
        r = service.console_run(sg["text"], sg["source"], "llm", [], True, session)
        session = r["llm"]["session"]
        calls = [{"purpose": c["purpose"], "model": c["model"], "host": c["host"], "prompt_sha256": c["prompt_sha256"], "response_sha256": c["response_sha256"],
                  "bytes_out": c["bytes_out"], "bytes_in": c["bytes_in"]} for c in r["llm"]["calls"]]
        scs.append({"id": sc["id"], "label": sc["label"], "prompt": sg["text"], "source": sg["source"], "model_calls": calls,
                    "proposals": [{k: p.get(k) for k in ("tool", "args_short", "decision", "outcome", "executed", "layer")} for p in r["proposals"] if p["tool"]],
                    "verdict": r["verdict"]["label"], "ledger_unchanged": bool(r["evidence"]["unchanged"]), "cash_out": r["verdict"]["simulated_cash_out"]})
    total = sum(len(s["model_calls"]) for s in scs)   # each console_run builds its own client, so calls are per scenario
    doc = {"schema": llm_evidence.SCHEMA, "recorded_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "provider": st["provider"], "model": st["model"], "endpoint_host": st["destination_host"], "total_model_calls": total, "scenarios": scs,
           "enforcement": "SIMULATED (play-money ledger; no PayPal call was made)",
           "recorder": {"script": "scripts/record_llm_evidence.py", "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    return llm_evidence.seal(doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "public_data" / "llm_evidence.json"))
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    if os.environ.get("SENTINEL_PUBLIC") == "1":
        print("Refusing: SENTINEL_PUBLIC=1 disables real LLM calls.")
        return 2
    out = Path(a.out)
    if out.exists() and not a.overwrite:
        print(f"Refusing: {out} exists (use --overwrite).")
        return 2
    os.environ.setdefault("SENTINEL_LLM_MAX_CALLS", str(MAX_CALLS))
    if int(os.environ["SENTINEL_LLM_MAX_CALLS"]) > MAX_CALLS:
        os.environ["SENTINEL_LLM_MAX_CALLS"] = str(MAX_CALLS)
    from roadworthy.dashboard import console, llm_evidence, service
    try:
        doc = build(service, console, llm_evidence)
    except service.ServiceError as e:
        print(f"FAILED (nothing written): HTTP {e.status}: {e.message}")
        return 1
    bad = llm_evidence.check(doc)
    if bad:
        print("FAILED integrity checks (nothing written):", bad)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    import json
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Recorded {doc['total_model_calls']} genuine model call(s) with {doc['model']} -> {out}\nself_sha256 {doc['self_sha256']}\nNow run: python scripts/verify_llm_evidence.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
