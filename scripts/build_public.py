#!/usr/bin/env python3
"""Build the strictly allow-listed PUBLIC artifact (default: build/public). Never copies the repository.

    python scripts/build_public.py [--out build/public] [--canary VALUE ...]

Allow-list, not deny-list: the only code copied is the import closure of the dashboard entry points (computed from the AST), and the build
FAILS if that closure reaches client / auth / wallet_run / capture / profiles / txsearch / snapshot / cli. Besides code it copies only:
dashboard static files, data/toolkit_manifest.json, public_data/*.json, LICENSE, NOTICE and DEPLOY.md.
Afterwards it (1) refuses any denied file name (*.xml, .env*, keys, evidence/, tests/, scripts/ ...), (2) scans every output file for
secret shapes and for every identifier found in the private evidence/ folder and any --canary value, (3) writes MANIFEST.sha256.json.
Exit 0 = artifact is clean. It reads local files only and contacts nothing.
"""
import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ["roadworthy.dashboard.__main__", "roadworthy.dashboard.server", "roadworthy.dashboard.service"]
FORBIDDEN_MODULES = {"client", "auth", "wallet_run", "capture", "profiles", "txsearch", "snapshot", "cli"}
DENY_NAME = re.compile(r"(?i)(\.xml$|^\.env|\.pem$|\.key$|\.p12$|\.pfx$|\.enc$|\.pyc$|\.log$|\.sqlite3?$|\.db$|^id_rsa|credentials|secret)")
DENY_PARTS = {"evidence", "tests", "scripts", "__pycache__", ".git", ".github", "node_modules", "venv", ".venv", "docs-private"}
SECRET_RX = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bA[A-Za-z0-9_-]{70,}\b",          # OpenAI-style key, PEM, PayPal-style client id
    r"(?i)authorization:\s*(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{16,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bAKIA[0-9A-Z]{16}\b",
    r"(?i)\"?(?:client_secret|api_key|password)\"?\s*[:=]\s*[\"'][A-Za-z0-9._~+/=-]{12,}[\"']")]
TEXT_EXT = {".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".toml", ".yaml", ""}


def module_file(mod: str):
    p = ROOT / Path(*mod.split("."))
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return p.with_suffix(".py") if p.with_suffix(".py").is_file() else None


def imports_of(path: Path, mod: str):
    pkg = mod.split(".") if path.name == "__init__.py" else mod.split(".")[:-1]
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names if a.name.split(".")[0] == "roadworthy"}
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg[: len(pkg) - (n.level - 1)]
                full = ".".join(base + ([n.module] if n.module else []))
            else:
                full = n.module or ""
            if full.split(".")[0] != "roadworthy":
                continue
            out.add(full)
            out |= {f"{full}.{a.name}" for a in n.names}
    return out


def closure(entry=ENTRY):
    seen, todo = {}, list(entry)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        f = module_file(m)
        if f is None:
            continue                                   # a name imported from a module (not a module itself)
        seen[m] = f
        parts = m.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]       # parent packages
        todo += sorted(imports_of(f, m))
    return seen


def private_literals():
    """Every identifier-like string in the private evidence/ folder (order ids, merchant ids, request ids, nonces, urls)."""
    out = set()
    ev = ROOT / "evidence"
    if ev.is_dir():
        for f in ev.rglob("*.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stack = [doc]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    stack += list(o.values())
                elif isinstance(o, list):
                    stack += o
                elif isinstance(o, str) and 8 <= len(o) <= 300 and re.search(r"[0-9]", o) and not o.startswith(("20", "REQUIRE", "PAYER")):
                    out.add(o)
    return out


def build(out: Path, canaries=()):
    problems = []
    mods = closure()
    bad = sorted(m for m in mods if set(m.split(".")) & FORBIDDEN_MODULES)
    if bad:
        raise SystemExit("BUILD FAILED: the import closure reaches forbidden module(s): " + ", ".join(bad))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = {}
    def put(src: Path, rel: str):
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files[rel] = src
    for m, f in sorted(mods.items()):
        put(f, f.relative_to(ROOT).as_posix())
    for f in sorted((ROOT / "roadworthy" / "dashboard" / "static").glob("*")):
        if f.is_file():
            put(f, f.relative_to(ROOT).as_posix())
    for rel in ["data/toolkit_manifest.json", "LICENSE", "NOTICE", "DEPLOY.md"]:
        if (ROOT / rel).is_file():
            put(ROOT / rel, rel)
        else:
            problems.append(f"missing required file {rel}")
    for f in sorted((ROOT / "public_data").glob("*.json")) if (ROOT / "public_data").is_dir() else []:
        put(f, f"public_data/{f.name}")
    problems += scan(out, canaries)
    mf = {rel: {"sha256": hashlib.sha256((out / rel).read_bytes()).hexdigest(), "bytes": (out / rel).stat().st_size} for rel in sorted(files)}
    (out / "MANIFEST.sha256.json").write_text(json.dumps({"schema": "sentinel.public-build/1", "files": mf, "modules": sorted(mods)}, indent=2, sort_keys=True) + "\n")
    return mods, mf, problems


def scan(out: Path, canaries=()):
    problems, lits = [], private_literals() | {c for c in canaries if c}
    for f in sorted(out.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(out)
        if set(rel.parts) & DENY_PARTS:
            problems.append(f"denied path: {rel}")
        if DENY_NAME.search(f.name) and f.name not in ("MANIFEST.sha256.json",):
            problems.append(f"denied file name: {rel}")
        if f.suffix.lower() not in TEXT_EXT and f.name not in ("Dockerfile", ".dockerignore", "LICENSE", "NOTICE"):
            problems.append(f"unexpected file type: {rel}")
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        for rx in SECRET_RX:
            if rx.search(txt):
                problems.append(f"secret-shaped text in {rel}: /{rx.pattern[:24]}/")
        for lit in lits:
            if lit in txt:
                problems.append(f"private identifier/canary present in {rel}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "public"))
    ap.add_argument("--canary", action="append", default=[])
    a = ap.parse_args(argv)
    mods, mf, problems = build(Path(a.out), a.canary)
    print(f"import closure: {len(mods)} modules; files copied: {len(mf)}; output: {a.out}")
    if problems:
        print("BUILD FAILED"); [print(" -", p) for p in problems]
        return 1
    print("CLEAN: no denied paths, no secret shapes, no private identifiers or canaries in the artifact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
