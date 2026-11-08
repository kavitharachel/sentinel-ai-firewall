"""python -m roadworthy.dashboard            -> serve on http://127.0.0.1:8765  (loopback only)
   python -m roadworthy.dashboard --selftest -> run every attack headless and print a table (exit 1 if any gets through)"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m roadworthy.dashboard")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--public", action="store_true", help="public judges' demo: LLM hard-off, host/origin allow-list, rate limits, binds 0.0.0.0 on $PORT")
    a = ap.parse_args(argv)
    if a.public:
        os.environ["SENTINEL_PUBLIC"] = "1"
    if a.selftest:
        from .attacks import run_all
        rows = run_all()
        for r in rows:
            print(f"{r['id']}  {'BLOCKED ' if r['blocked'] else 'GOT-THROUGH'}  {r['layer']:20s} {r['title']}")
        bad = [r for r in rows if not r["blocked"]]
        print(f"\n{len(rows) - len(bad)}/{len(rows)} attacks stopped (SIMULATED world; no PayPal calls).")
        return 1 if bad else 0
    from .server import serve
    from .service import is_public
    if is_public():
        try:
            port_env = int(os.environ.get("PORT", a.port))
        except ValueError:
            port_env = a.port
        srv, port = serve(port_env)
        print(f"SENTINEL PUBLIC demo on 0.0.0.0:{port}  hosts={os.environ.get('SENTINEL_PUBLIC_HOST') or '(loopback only: set SENTINEL_PUBLIC_HOST)'}  LLM: disabled  PayPal: not contacted")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0
    srv, port = serve(a.port)
    print(f"SENTINEL dashboard: http://127.0.0.1:{port}   (loopback only; simulations + frozen evidence; no PayPal writes)  Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
