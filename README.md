# SENTINEL (package: `roadworthy`) — an AI financial-transaction firewall
*PayPal AI Hackathon 2026 entry.*

AI agents that touch money make claims ("I refunded it", "the invoice is paid") and propose actions. SENTINEL (1) puts a
fail-closed **gateway** between the agent and the payment tools, (2) **independently verifies** what the agent claims against
recorded evidence, and (3) lets permissions be **earned from evidence and revoked on harm**.

> **Read this first — what is real and what is simulated**
>
> | Label | Meaning |
> |---|---|
> | `SIMULATED` | An invented merchant world (play money) run by the real gateway, verifier and trust engine. **The enforcement you see in the demo is simulated. It does not protect any live PayPal transaction.** |
> | `LIVE-RECORDED` | A frozen record of real PayPal **sandbox** responses (one human-approved sandbox order creation plus read-only status checks). Never refreshed by the dashboard. The public build shows an identifier-free summary only. |
> | `LLM-RECORDED` | A genuine hosted-model run recorded once on the maintainer's machine and replayed. The page never calls a model. Tamper-evident, not tamper-proof (see `scripts/verify_llm_evidence.py`). Shown only if such a file has been published. |
>
> **What the PayPal sandbox write path protects, and what stays simulated**
>
> * **Real (sandbox only):** the single PayPal write path, `wallet-run` (`roadworthy/wallet_run.py`), is gated by the same `ToolGateway`
>   as the simulation. A sandbox order of exactly 2.00 USD is only created if (1) the environment flag `SENTINEL_ALLOW_SANDBOX_WRITE=1`
>   is set, (2) the operator types the confirmation phrase, and (3) the gateway returns an approval. The gateway checks, in order:
>   environment, known tool, capability ceiling, parameter hard limits (refund <= $25, order <= $100, USD only), licence level, human approval.
>   The write client cannot POST without a one-time permit issued by the gateway's executor, and only two requests are allow-listed.
>   One human-approved sandbox order was created this way and is recorded as `LIVE-RECORDED`.
> * **Still simulated:** the dashboard, Agent Console, Attack Lab (16 attacks), Financial Time Machine and Trust Engine run against an invented
>   play-money merchant. They do not call PayPal. The public demo cannot import the write path at all.
> * **Not built / not claimed:** production money movement, live-mode (non-sandbox) PayPal access, a gateway deployed in front of a real merchant's
>   PayPal account, or any claim of protection for real merchants. Refunds are not implemented in the live path.
> * **Verification status of live runs:** the PayPal read-only check (OAuth + Transaction Search) was run by the maintainer on 2026-11-06 and is
>   `LIVE, USER-REPORTED` (not independently reproduced). A fresh hosted-model smoke test is separate and is labelled only after it is run.

## Try it
```powershell
python -m roadworthy.dashboard            # http://127.0.0.1:8765 (loopback only, no dependencies)
python -m roadworthy.dashboard --selftest # every attack, headless; exit 1 if any gets through
```
Open **AI Agent Workflow → Agent Console** and press one of the three one-click scenarios (unsafe refund, false payment claim,
prompt injection). They run offline with a clearly labelled rule-based fallback; no API key is needed.
Optional: a real model can *propose* actions locally if you opt in (see `DASHBOARD.md`); it can never execute anything.

## Live demo (hosted, simulation only)
**https://sentinel-ai-firewall-g6zl.onrender.com/** (health check: `https://sentinel-ai-firewall-g6zl.onrender.com/api/health`)

Hosted on Render's Free plan from `deploy/Dockerfile`; source: https://github.com/kavitharachel/sentinel-ai-firewall. The first request after about 15 minutes idle can take about a minute (free-tier cold start).
What you see there is labelled `SIMULATED`: play money, no PayPal call, no model call (the LLM is hard-off in public mode, no API key or PayPal credential is configured on the service).
The `LIVE-RECORDED` and `LLM-RECORDED` panels are frozen records replayed from `public_data/`; nothing on the hosted page is `LIVE`.
Hosted-demo checks (observed through a browser on the deployed URL, not a second network): `/api/health` returned 200 with `public: true` and `writes_to_paypal: false`; the Agent Console scenarios "Unsafe refund", "False payment claim" and "Prompt injection" ran and were denied or marked CONTRADICTED with the ledger unchanged; security headers (CSP, X-Frame-Options, nosniff, HSTS and others) were present.

## Public judges' demo
`python -m roadworthy.dashboard --public` (or `SENTINEL_PUBLIC=1`) hard-disables every LLM call, enforces a host/origin
allow-list, rate and concurrency limits and security headers, and reads only `public_data/`. See `DEPLOY.md`.

## Dry run: see the gateway's verdict with zero network calls
```powershell
python -m roadworthy wallet-run create --profile us --dry-run                          # the 2.00 USD order: REQUIRE_APPROVAL (exit 0)
python -m roadworthy wallet-run create --profile us --dry-run --what-if-currency EUR   # DENY (exit 3)
python -m roadworthy wallet-run create --profile us --dry-run --what-if-amount 2.01    # REFUSED_BY_REQUEST_GUARD (exit 3)
```
`--dry-run` builds the same order body, then asks the real hardened gateway and the write client's request guard for a verdict. It never
reads credentials, never requests an OAuth token, never opens a socket, and never writes run state. This is enforced, not just promised:
the dry-run path is given a token getter and a transport that raise immediately if called, and the tests in `tests/test_wallet_run_dryrun.py`
(21 tests) assert that neither is touched. A dry run never approves anything: it prints a verdict only. Exit codes: 0 = approval required,
3 = blocked or refused, 2 = misuse. Non-finite, negative, zero, non-USD and over-limit inputs cannot be auto-authorized.
On 2026-11-08 these five cases (2.00 USD, EUR, 2.01, 0, NaN) were also run with the network layer replaced by a function that raises: each printed `requests sent : 0` and no `evidence/` folder was created.

## Safety boundaries (enforced in code, covered by tests)
- Sandbox host is a constant; any other host is refused. The REST client (`client.py`) exposes `get()` only.
- The only PayPal write path (`wallet_run.py`, sandbox order creation and capture of the same 2.00 USD order) is separate, requires human approval through the gateway,
  and is **not importable** from the dashboard or the public build (an AST test and a build check enforce this).
- Hard limits sit above every licence: refund ≤ $25, order ≤ $100, USD only. Results can lower a permission, never lift it past the ceiling.
- Credentials are read from a DPAPI-encrypted file on the maintainer's Windows machine, in memory only; never logged, never sent to a model, never in the public build.
- A model (when enabled locally) only returns text that is parsed into *proposals*; it holds no tool, key or ledger.

## Layout
```
roadworthy/permissions/   earned-permission engine + fail-closed gateway
roadworthy/eval/          claims parser, snapshot diff, independent verifier, deterministic grader
roadworthy/dashboard/     stdlib HTTP dashboard: Agent Console, Claim Audit, Financial Time Machine, Trust Engine, Attack Lab
roadworthy/capabilities.py 46 official toolkit tools -> 17 capabilities, immutable ceilings
public_data/              identifier-free evidence summary (+ recorded LLM run, if published)
scripts/                  build_public.py, make_public_evidence.py, record/verify_llm_evidence.py, llm_smoke.py
data/toolkit_manifest.json static inventory of @paypal/agent-toolkit 1.11.0 (names only; no PayPal calls produced it)
```

## Tests
```powershell
python -m pip install -r requirements-dev.txt   # pytest (+ Playwright for the optional browser tests)
python -m pytest
```
Runtime has **no third-party dependencies**.

Last verified run (offline, 2026-11-08, fresh copy of the project): 505 tests collected, 504 passed, 0 failed, 1 skipped. The skipped test is the opt-in live PayPal sandbox check (`ROADWORTHY_LIVE=1`), which was not part of this run. The Attack Lab self-test (`python -m roadworthy.dashboard --selftest`) stops 16/16 scenarios in the simulated world. No live PayPal call or hosted-LLM call is part of these numbers.

## Licence
MIT (see `LICENSE`). Not affiliated with or endorsed by PayPal; see `NOTICE` for third-party attributions.
