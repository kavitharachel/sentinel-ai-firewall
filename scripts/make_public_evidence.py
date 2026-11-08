#!/usr/bin/env python3
"""Build public_data/public_evidence.json: an identifier-free summary of the frozen PayPal SANDBOX evidence.

    python scripts/make_public_evidence.py [--evidence evidence/us] [--out public_data/public_evidence.json]

The output is built from an explicit WHITELIST of fields. Order ids, capture ids, merchant ids, request ids, nonces, URLs, custom ids and
credential fingerprints are never copied. The only things derived from identifiers are booleans ("payee matches expected") and the first
16 hex characters of each source file's SHA-256, so a reviewer who holds the private files can confirm which records this summarises.
The script refuses to write if any output string looks like an identifier. Reads local files only; contacts nothing.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "sentinel.public-evidence/1"
SAFE = re.compile(r"^[A-Za-z0-9_ :./\-]{0,40}$")
IDISH = re.compile(r"[A-Z0-9]{12,}")        # PayPal order/merchant/capture ids are long upper-case alphanumerics
RUN = re.compile(r"^wallet_run_[A-Za-z0-9]+\.json$")


def _load(p: Path):
    raw = p.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()[:16]


def summarise(evidence_dir: Path) -> dict:
    runs = sorted(f for f in evidence_dir.iterdir() if RUN.match(f.name))
    if not runs:
        raise SystemExit(f"no wallet_run_*.json in {evidence_dir}")
    run, run_h = _load(runs[-1])
    prof, prof_h = _load(evidence_dir / "profile_check.json") if (evidence_dir / "profile_check.json").is_file() else (None, "")
    expected = (prof or {}).get("expected_merchant_id")
    out = {"schema": SCHEMA, "recorded_at": run.get("created_at"), "state": run.get("state"), "order_status": run.get("order_status"),
           "amount": run.get("amount"), "currency": run.get("currency"), "captures": 1 if run.get("capture_id") else 0,
           "payee_matches_expected": (run.get("payee_merchant_id") == expected) if expected and run.get("payee_merchant_id") else None,
           "source_sha256": {"wallet_run": run_h}}
    if prof:
        out["profile_check"] = {"status": prof.get("status"), "checked_at": prof.get("checked_at"), "refund_scope": bool(prof.get("refund_scope")),
                                "transaction_search_http": prof.get("txsearch_http")}
        out["source_sha256"]["profile_check"] = prof_h
    out["provenance"] = ("Frozen PayPal SANDBOX responses: one human-approved sandbox order creation and read-only status checks, "
                         "made earlier on the maintainer's machine. Identifiers are withheld. Not live; not production money.")
    return out


def assert_identifier_free(doc) -> None:
    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        elif isinstance(o, str):
            yield o
    for s in walk(doc):
        if not SAFE.match(s) and len(s) < 300 and not s.startswith("Frozen PayPal"):
            raise SystemExit(f"refusing to write: value fails the safe-string check: {s[:12]}...")
        if IDISH.search(s):
            raise SystemExit("refusing to write: an identifier-shaped value is present")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=str(ROOT / "evidence" / "us"))
    ap.add_argument("--out", default=str(ROOT / "public_data" / "public_evidence.json"))
    a = ap.parse_args(argv)
    doc = summarise(Path(a.evidence))
    assert_identifier_free(doc)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}\n" + json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
