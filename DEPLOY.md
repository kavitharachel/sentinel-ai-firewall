# Deploying the SENTINEL public judges' demo

**Do not deploy the repository as-is.** The image is built by `deploy/Dockerfile`, a multi-stage build whose first stage runs
`python scripts/build_public.py` (an allow-listed artifact: dashboard code, static files, `public_data/`, licence files) and whose
final stage contains nothing else. The build fails on any secret shape, denied path or forbidden import.

## What the public server is (and is not)
* Everything interactive is a **simulation**. No PayPal call exists in the artifact (the build fails if client/auth/wallet_run/capture/profiles/txsearch/snapshot/cli are reachable).
* **No paid LLM calls.** `SENTINEL_PUBLIC=1` ignores every `SENTINEL_LLM_*` variable. No API key is needed or read.
* PayPal evidence shown is `public_data/public_evidence.json`: identifier-free, sandbox, frozen.
* The enforcement shown does **not** protect live PayPal transactions. The page says so in a banner.

## 1. Produce and check the artifact
```bash
python scripts/make_public_evidence.py            # only if evidence changed; review the printed JSON
python scripts/build_public.py                    # writes build/public + MANIFEST.sha256.json; exit 1 on any problem
python scripts/build_public.py --canary "<a secret you know>"   # optional: fail if that string appears anywhere
python -m pytest tests/test_public_mode.py tests/test_public_build.py tests/test_llm_evidence.py
```
## 2. (Optional) publish your genuine model run
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\record_llm_evidence.ps1
```
One hidden key prompt (or it reuses `SENTINEL_LLM_API_KEY` if already set), up to 6 paid calls (hard-capped), writes
`public_data\llm_evidence.json` and verifies it. The key is never saved or printed. Then compare `recorded_at`, model and call count
with your provider's usage dashboard (the independent corroboration) and rebuild. The page labels it `LLM-RECORDED` and states it is
replayed and tamper-evident, not tamper-proof. Never edit the file by hand.

## 3. Run the container (from the repository root)
```bash
docker build -f deploy/Dockerfile -t sentinel-demo .
docker run --rm -p 8080:8080 -e SENTINEL_PUBLIC_HOST=localhost:8080 -e SENTINEL_TRUSTED_PROXY=0 sentinel-demo
```
(`SENTINEL_TRUSTED_PROXY=0` when nothing sits in front of the container; the image default `1` is for platforms that add `X-Forwarded-For`.)

## Environment variables (all optional except the host)
| Variable | Default | Purpose |
|---|---|---|
| `SENTINEL_PUBLIC` | `1` in the image | Public mode. LLM hard-off, allow-lists, limits. |
| `SENTINEL_PUBLIC_HOST` | none (loopback only) | Comma-separated public hostnames (with `:port` if non-default). Anything else gets 403. **Set this.** |
| `PORT` | `8080` | Listen port (binds 0.0.0.0 in public mode). |
| `SENTINEL_TRUSTED_PROXY` | `1` in the image | Trust `X-Forwarded-For` / `X-Forwarded-Proto` (set only behind a proxy that appends them). |
| `SENTINEL_TRUSTED_HOPS` | 1 (use **3** on Render) | How many proxies append to `X-Forwarded-For`; the client IP is that entry counted from the right. Wrong value = all visitors share one rate-limit bucket (too low) or spoofable IPs (too high). On Render the chain is client, Cloudflare edge, Render load balancer, so the tested value is `3` (1 and 2 gave unstable keys; 3 gave one stable key that did not change with a spoofed `X-Forwarded-For`). Other hosts differ: run the check below. |
| `SENTINEL_RATE_POST_PER_MIN` / `SENTINEL_RATE_GET_PER_MIN` | 60 / 600 | Per-IP limits (429). |
| `SENTINEL_RATE_GLOBAL_POST_PER_MIN` | 900 | All visitors combined (429). |
| `SENTINEL_MAX_CONCURRENT` | 8 | Simultaneous simulations (503 beyond). |

## Recommended hosting
* Any container host with managed HTTPS (Render, Fly.io, Cloud Run, Railway). `deploy/render.yaml` is the Render example (verify its keys against the host's current docs). The deployed judges' demo ran on Render's **Free** plan (Docker, auto-deploy off): `https://sentinel-ai-firewall-g6zl.onrender.com/`. Free services sleep after about 15 minutes idle, so the first request can take about a minute.
* On Render, `SENTINEL_PUBLIC_HOST` must be the exact generated hostname including its random suffix (here `sentinel-ai-firewall-g6zl.onrender.com`); a guessed name returns 403 "bad host".
* **One instance only**: the rate limiter and run history are in-process. Do not autoscale above 1 without moving limits to the edge.
* Terminate TLS at the host/CDN; set `SENTINEL_PUBLIC_HOST` to the exact public hostname (and any custom domain). HSTS is added only when the proxy says the request was HTTPS.
* Health check: `GET /api/health` (exempt from the host allow-list so platform probes work; still rate-limited) → `{"ok": true, "writes_to_paypal": false, "public": true}`.
* Put a CDN/WAF rate limit in front for volumetric abuse; the in-process limits are a second layer, not a DDoS defence.
* Set no secrets on the service. If the host UI shows any secret/env var named `*KEY*`, `*SECRET*`, `PAYPAL*`, delete it.
* Logs: the server logs no request bodies or prompts. Keep platform access logs short-lived.

## Pre-launch checklist
- [ ] `build_public.py` exit 0, `git status` shows nothing from `evidence/` staged.
- [ ] Browser check of the deployed URL at phone width and desktop; banner visible; Agent Console scenarios run.
- [ ] `curl -I https://<host>/` shows CSP, X-Frame-Options, Permissions-Policy; `curl -H "Host: evil.test" https://<ip>/` is 403.
- [ ] Repository public + `LICENSE` visible (Devpost requirement); demo video; this URL in the submission.

## Verify the rate limiter sees the right client (SENTINEL_TRUSTED_HOPS)
`GET /api/health` returns an opaque `client_key` (salted hash of the IP the limiter keys on). After the first deploy:
```bash
curl -s https://<host>/api/health                                   # note client_key  (K1)
curl -s -H "X-Forwarded-For: 1.2.3.4" https://<host>/api/health     # must equal K1 (a spoofed header must not change it)
# from a different network (phone on mobile data):  curl -s https://<host>/api/health   # must DIFFER from K1
```
Same key from different networks means too few hops are trusted (everyone shares one bucket): raise `SENTINEL_TRUSTED_HOPS`. (Render: the second-network comparison has not been run; only the spoofed-header comparison was.)
A key that changes with the spoofed header means too many hops are trusted: lower it.
