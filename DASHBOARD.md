# SENTINEL dashboard

```powershell
python -m roadworthy.dashboard              # http://127.0.0.1:8765  (loopback only)
python -m roadworthy.dashboard --selftest   # runs all 16 attacks headless
```
Open `http://127.0.0.1:8765/?demo=1` (or press "Start 90-second demo") for the guided tour:
false claim -> independent verification (incl. LIVE-RECORDED PayPal sandbox evidence) -> revoked permission -> blocked attack -> Financial Time Machine.
Controls: Next / Back / Pause buttons, arrow keys, Space to pause, Esc to exit.

## Labels
`SIMULATED` = invented merchant world executed by the real gateway, verifier, grader and permission engine.
`LIVE-RECORDED` = frozen PayPal sandbox evidence read (never written or refreshed) from `evidence\us`.

## Opt-in LLM agent
Off by default. Enable it in the shell that STARTS the server; credentials never go through the dashboard:
```powershell
$env:SENTINEL_LLM_ENABLE = "1"
$env:SENTINEL_LLM_PROVIDER = "anthropic"          # or "openai" (any OpenAI-compatible endpoint)
$env:SENTINEL_LLM_MODEL = "<model name>"          # always explicit
$env:SENTINEL_LLM_API_KEY = "<key>"               # not needed for a local http://127.0.0.1 endpoint
$env:SENTINEL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"   # optional; https or loopback only; never a PayPal host
$env:SENTINEL_LLM_MAX_CALLS = "40"                # optional per-server-run call budget
python -m roadworthy.dashboard
```
The model only PROPOSES tool calls (parsed strictly from its reply). Every proposal runs through the same ToolGateway
(hard limits, earned licence, approval gate) and its report is checked by the same independent verifier. A new LLM agent
starts with zero earned autonomy. Only the simulated task text is sent to the provider; each run needs explicit consent in
the UI; prompts are never stored (the audit log keeps hashes).

## Agent Console (AI Agent Workflow, first tab)
Type any financial task or attack and watch one lifecycle: **instruction -> AI proposal -> SENTINEL decision -> independent evidence -> verdict**.

* **Who proposes:** the opt-in real LLM (same `llm.py` client, budget, consent and key handling as above) or the **offline scripted fallback**
  (two rule-based personas, "careful" and "overconfident", labelled SIMULATED and "not an AI model"). The fallback needs no key.
* **What is real vs simulated:** the proposals are real model output only when the LLM is enabled. The ledger is always SIMULATED play money.
  The gateway (hard limits, licence, approvals) and the verifier are the real engine. No PayPal call is ever made.
* **Authorisation is derived from the operator's own words** (`console.authorised`), never from the model. Text marked "customer message"
  authorises only read-only lookups. Policy lives in the gateway, so no wording in a prompt can change a hard limit, a licence or an approval gate.
* **Data sent to the model:** the instruction text, the tool list, and one line per recorded outcome for the report call. No ledger dump,
  no PayPal data, no credentials. The key is only in the request header; responses, status and audit entries never contain it.
* **Limits:** 600 characters per instruction, at most 6 proposals per instruction, the shared per-run LLM call budget, 8 sessions.
* API: `GET /api/console/info`, `POST /api/console/run {prompt, source: operator|customer, agent: careful|overconfident|llm, approvals, consent, session}`.

## Security boundaries
- No code path to PayPal: the package imports no PayPal client, credentials or wallet code (tested).
- `llm.py` is the only module allowed to open a network connection, only to the configured LLM host, no redirects followed.
- Server: 127.0.0.1 only, Host/Origin checks, JSON-only POST, 8 KB body cap, strict CSP, no raw-markup rendering.
