'use strict';
/* Native append()/prepend() print the text "null" for a null child. Skip null/undefined/false everywhere. */
for (const m of ['append', 'prepend']) { const o = Element.prototype[m]; Element.prototype[m] = function (...k) { return o.apply(this, k.flat().filter(x => x != null && x !== false)); }; }
/* SENTINEL dashboard. All DOM is built with textContent (never raw markup), so data can never inject markup. */
const $ = (s, r = document) => r.querySelector(s);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (k === 'text') e.textContent = v; else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const svgNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...kids) { const e = document.createElementNS(svgNS, tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); kids.flat().forEach(c => c && e.append(c.nodeType ? c : document.createTextNode(c))); return e; }
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json();
}
let PUBLIC = false;
const tagFor = src => h('span', { class: 'tag ' + (src === 'SIMULATED' ? 'sim' : src === 'LLM-RECORDED' ? 'real' : 'live') }, src);
const vb = v => h('span', { class: 'badge ' + ({ VERIFIED: 'b-ok', CONTRADICTED: 'b-bad', UNVERIFIABLE: 'b-warn' }[v] || 'b-mut') }, v);
const lvl = l => h('span', { class: 'badge ' + ({ AUTO: 'b-ok', APPROVAL_REQUIRED: 'b-warn', BLOCKED: 'b-bad' }[l] || 'b-mut') }, (l || '-').replace('_', ' '));
const act = a => h('span', { class: 'badge ' + ({ ALLOW: 'b-ok', REQUIRE_APPROVAL: 'b-warn', DENY: 'b-bad' }[a] || 'b-mut') }, { ALLOW: 'ALLOWED', REQUIRE_APPROVAL: 'NEEDS HUMAN', DENY: 'DENIED', NONE: 'NOTE' }[a] || a);
const usd = v => '$' + Number(v).toFixed(2);
const empty = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const card = (title, right, ...body) => { const [tx, tid] = String(title).split('|'); return h('div', { class: 'card', 'data-t': tid }, h('h3', null, h('span', null, tx), right || ''), ...body); };

const VIEWS = [
  ['command', '◎', 'Command Center', 'Claims, verdicts and risk alerts across every agent'],
  ['audit', '🔍', 'Claim Audit', 'What the agent said vs what independent evidence shows'],
  ['time', '⏱', 'Financial Time Machine', 'Same agent, two worlds: with and without SENTINEL'],
  ['trust', '🛡', 'Trust Engine', 'Permissions are earned from evidence and revoked on harm'],
  ['attack', '⚔', 'Attack Lab', 'Reproducible adversarial scenarios, each one stopped'],
  ['agent', '🤖', 'AI Agent Workflow', 'An agent proposes actions; SENTINEL checks and enforces'],
  ['bench', '⚖', 'Judge Bench', 'Try to fool the real gateway; every decision comes with a re-verifiable receipt'],
];
const state = { view: 'command', audit: { sel: null, filter: 'ALL' }, tm: { agent: 'eager', gateway: true, verifier: true, human: false, step: null },
  trust: { cap: 'order.create', events: Array(14).fill('pass') }, agent: { tab: 'console', brain: 'eager', approvals: [], llm: { session: null, consent: false, error: null, calls: [] } },
  con: { agent: 'overconfident', source: 'operator', text: '', turn: null, history: [], busy: false, error: null, approvals: [], session: null, consent: false, info: null, sentText: '' } };

function nav() {
  const n = empty($('#nav'));
  VIEWS.forEach(([id, ic, name]) => n.append(h('button', { 'aria-current': state.view === id ? 'page' : false, 'data-t': 'nav-' + id, onclick: () => go(id) }, h('i', null, ic), name)));
  n.append(h('button', { class: 'demo-nav', 'data-t': 'nav-demo', onclick: () => startDemo() }, h('i', null, '▶'), '90-second demo'));
}
let ROOT = null;
const viewRoot = () => ROOT || $('#view');
async function go(id) {
  state.view = id; nav();
  const v = VIEWS.find(x => x[0] === id);
  empty($('#title')).append(h('h1', null, v[2]), h('p', null, v[3]));
  /* Every navigation renders into its OWN container. A slow render that finishes after the user has moved on writes into a detached node, so it can never overwrite the view that is now selected. */
  const root = h('div', { class: 'view-root' }); ROOT = root; empty($('#view')).append(root); root.append(h('p', { class: 'mut' }, 'Loading...'));
  try { await RENDER[id](root); } catch (e) { empty(root).append(card('Error', null, h('p', { class: 'bad' }, String(e.message || e)))); }
}

/* ---------------------------------------------------------------- 1. Command Center */
const arrow = () => h('span', { class: 'arrow', 'aria-hidden': 'true' }, '→');
const stage = (n, title, tag, testid, ...body) => h('div', { class: 'stage', 'data-t': testid }, h('div', { class: 'stage-h' }, h('span', { class: 'num' }, n), h('b', null, title), h('span', { class: 'sp' }), tag ? tagFor(tag) : ''), ...body);
/* Start-here path for a judge who has never seen SENTINEL: four clicks, each labelled with what kind of evidence it shows. */
function judgePath() {
  const step = (n, label, note, tag, testid, fn) => h('li', null, h('button', { class: 'btn pri', 'data-t': testid, onclick: fn }, n + ' · ' + label), h('span', { class: 'note' }, note), tagFor(tag));
  return h('div', { class: 'card judge-path', 'data-t': 'judge-path' },
    h('h3', null, h('span', null, 'Start here · about 90 seconds')),
    h('ol', { class: 'jp' },
      step('1', 'Try an unsafe request', 'Press “Unsafe refund”. SENTINEL denies it, then checks the agent’s report against the ledger.', 'SIMULATED', 'jp-1', () => go('agent')),
      step('2', 'Claim vs recorded PayPal state', 'The agent says the payment was captured. The recorded sandbox order shows 0 captures. Nothing was paid.', 'LIVE-RECORDED', 'jp-2', () => { state.audit.sel = 'LIVE-4'; state.audit.filter = 'ALL'; go('audit'); }),
      step('3', 'Watch a permission get revoked', 'Eighteen clean runs earn autonomy; one harmful action takes it away.', 'SIMULATED', 'jp-3', () => go('trust')),
      step('4', 'Your turn: try to fool the gateway', 'Pick “one cent over”, then forge a receipt. The real gateway decides every time; nothing is sent anywhere.', 'SIMULATED', 'jp-4', () => go('bench'))));
}
async function command(root) {
  const o = await api('/api/overview'); const c = o.story.case, k = c.counts, ct = o.story.contrast, lv = o.story.live;
  const total = c.claims.length;
  const more = c.claims.length - 3;
  const pipeline = h('div', { class: 'pipeline', 'data-t': 'story' },
    stage('1', 'Agent claim', 'SIMULATED', 'story-claim', h('p', { class: 'said' }, '“' + c.said + '”'),
      h('ul', { class: 'tight' }, c.claims.slice(0, 3).map(x => h('li', null, x.text))), more > 0 && h('p', { class: 'note' }, `+${more} more claims`), h('p', { class: 'note' }, c.agent + ' · ' + c.kind.replace('_', ' '))),
    arrow(),
    stage('2', 'Independent evidence', 'SIMULATED', 'story-evidence', h('ul', { class: 'tight' }, c.evidence.map(x => h('li', null, x))), h('p', { class: 'note' }, 'Read from a ledger the agent cannot write to.')),
    arrow(),
    stage('3', 'Verdict', null, 'story-verdict', h('div', { class: 'big bad' }, `${k.CONTRADICTED} of ${total}`), h('div', null, vb('CONTRADICTED')), h('p', { class: 'note' }, `${k.VERIFIED} verified · ${k.UNVERIFIABLE} unverifiable`)),
    arrow(),
    stage('4', 'SENTINEL response', null, 'story-action',
      h('div', { class: 'big ok' }, `${c.blocked.length} blocked`),
      h('ul', { class: 'tight' }, c.blocked.map(x => h('li', null, h('code', null, x.tool), ' ', h('span', { class: 'mut' }, x.layer)))),
      c.revoked.map(x => h('div', { class: 'revoked' }, h('b', null, 'Revoked: '), x.capability, ' ', x.from.replace('_', ' '), ' → ', h('b', { class: 'bad' }, x.to))),
      c.needs_human.length > 0 && h('p', { class: 'next' }, h('b', null, 'Next permitted step: '), `a human may approve ${c.needs_human[0].tool} (${c.needs_human[0].args}).`)));
  const honest = card('Same tasks, honest agent', tagFor('SIMULATED'),
    h('div', { class: 'row' }, h('div', { class: 'big ok' }, `${ct.counts.VERIFIED}/${ct.claims.length}`), h('span', null, 'claims verified'), h('span', { class: 'sp' }), vb('VERIFIED')),
    ct.autonomous.map(x => h('p', null, h('b', { class: 'ok' }, 'Ran on its own: '), h('code', null, x.tool), ` ${x.args} → ${x.id || ''}`, h('span', { class: 'note' }, ' · autonomy earned from a clean record'))),
    ct.needs_human.map(x => h('p', null, h('b', { class: 'warn' }, 'Waits for a human: '), h('code', null, x.tool), ` ${x.args}`)),
    h('p', { class: 'note' }, 'Trust is earned per capability and capped: money movement never runs unattended.'));
  const r = o.live.runs.find(x => !x.abandoned) || o.live.runs[0];
  const liveCardNode = lv ? card('Grounded in real PayPal sandbox evidence', tagFor('LIVE-RECORDED'),
    h('p', null, 'Claim: ', h('b', null, '“' + lv.claim + '”')), h('p', null, 'Recorded evidence: ', h('code', null, lv.field + ' = ' + lv.actual), ' ', vb('CONTRADICTED')),
    h('p', { class: 'note' }, `${lv.verified} of ${lv.of} other checks verified. ${r.order_id_masked === 'WITHHELD' ? 'The recorded order (identifier withheld)' : 'Order ' + r.order_id_masked} was ${r.order_status} when recorded ${r.created_at}; never refreshed by this dashboard.`)) : liveCard(o.live);
  const alertRows = o.alerts.map(a => h('div', { class: 'alert' }, h('span', { class: 'dot', style: `background:var(--${a.sev === 'critical' ? 'bad' : a.sev === 'warn' ? 'warn' : 'info'})` }),
    h('div', null, h('b', null, a.title), a.count > 1 ? h('span', { class: 'badge b-mut', style: 'margin-left:6px' }, '×' + a.count) : '', ' ', h('span', { class: 'mut' }, a.agent), h('div', { class: 'note' }, a.detail))));
  empty(root).append(
    h('div', { class: 'hero' }, h('div', null, h('h2', null, 'Did the AI agent do what it said?'), h('p', { class: 'mut' }, 'SENTINEL checks every claim against independent evidence and caps what the agent may do.')),
      h('button', { class: 'btn pri', 'data-t': 'demo-start', onclick: () => startDemo() }, '▶ Start 90-second demo')),
    judgePath(),
    pipeline,
    h('div', { class: 'grid g2', style: 'margin-top:14px;align-items:start' }, honest, liveCardNode),
    h('div', { class: 'grid g4', style: 'margin-top:14px' }, o.proof.map(p => h('div', { class: 'card kpi' }, h('span', null, p.label, ' ', p.tag ? tagFor(p.tag) : ''), h('b', { class: p.tone }, p.value), h('span', null, p.sub)))),
    h('details', { class: 'card alerts', style: 'margin-top:14px', 'data-t': 'alerts' }, h('summary', null, `Risk alerts (${o.alerts_total} grouped)`, ' ', tagFor('SIMULATED')), alertRows, o.alerts_total > o.alerts.length && h('p', { class: 'note' }, `Showing ${o.alerts.length} of ${o.alerts_total} grouped alerts.`)));
}
const kpi = (label, val, sub, cls, tag) => h('div', { class: 'card kpi' }, h('span', null, label, ' ', tag ? tagFor(tag) : ''), h('b', { class: cls || '' }, val), h('span', null, sub));
function liveCard(l) {
  if (!l.available) return card('Live PayPal sandbox evidence', tagFor('LIVE-RECORDED'), h('p', { class: 'mut' }, 'No recorded live evidence found in the evidence folder.'));
  const r = l.runs.find(x => !x.abandoned) || l.runs[0];
  return card('Live PayPal sandbox evidence', tagFor('LIVE-RECORDED'),
    h('table', null, ...[['Order', r.order_id_masked], ['Status when recorded', r.order_status], ['Amount', `${r.amount} ${r.currency}`], ['Captures', String(r.captures)], ['Recorded', r.created_at]].map(([a, b]) => h('tr', null, h('td', { class: 'mut' }, a), h('td', { class: 'mono' }, b)))), h('p', { class: 'note' }, l.note));
}

/* ---------------------------------------------------------------- 2. Claim Audit (hero) */
const ENF_ICON = { block: '⛔', revoke: '🔒', gate: '⏸', accept: '✓' };
async function auditView(root) {
  const all = (await api('/api/claims')).claims;
  const shown = all.filter(c => state.audit.filter === 'ALL' || c.verdict === state.audit.filter || (state.audit.filter === 'LIVE' && c.source !== 'SIMULATED'));
  if (!state.audit.sel || !all.find(c => c.id === state.audit.sel)) state.audit.sel = (all.find(c => c.verdict === 'CONTRADICTED' && c.source === 'SIMULATED' && c.say.claim.includes('dispute')) || all.find(c => c.verdict === 'CONTRADICTED') || all[0]).id;
  const sel = all.find(c => c.id === state.audit.sel), say = sel.say, bad = sel.verdict === 'CONTRADICTED', live = sel.source !== 'SIMULATED';
  const strip = h('div', { class: 'strip', role: 'group', 'aria-label': 'Choose a claim' },
    h('div', { class: 'row filt' }, ...['ALL', 'VERIFIED', 'CONTRADICTED', 'LIVE'].map(x => h('button', { class: 'btn' + (state.audit.filter === x ? ' pri' : ''), onclick: () => { state.audit.filter = x; auditView(root); } }, x))),
    h('div', { class: 'pills' }, shown.map(c => h('button', { class: 'pill ' + (c.verdict === 'CONTRADICTED' ? 'p-bad' : 'p-ok'), 'aria-pressed': c.id === sel.id, title: c.text, onclick: () => { state.audit.sel = c.id; auditView(root); } },
      h('b', null, c.id), h('span', null, c.verdict === 'CONTRADICTED' ? '✕' : '✓')))));
  const hero = h('div', { class: 'card hero-card ' + (bad ? 'is-bad' : 'is-ok'), 'data-t': 'claim-detail' },
    h('div', { class: 'row' }, h('b', { class: 'hc-id' }, 'Claim ' + sel.id), tagFor(sel.source), h('span', { class: 'sp' }), h('span', { class: 'note' }, say.world)),
    h('div', { class: 'faceoff' },
      h('div', { class: 'panel p-claim' }, h('span', { class: 'lbl' }, 'AGENT CLAIM'), h('p', { class: 'panel-t' }, say.claim),
        h('span', { class: 'note' }, `Made by ${sel.agent}${live ? ' (an illustrative claim written by the dashboard, checked against recorded evidence)' : ''}`)),
      h('div', { class: 'vsmark ' + (bad ? 'vs-bad' : 'vs-ok'), 'aria-hidden': 'true' }, bad ? '≠' : '='),
      h('div', { class: 'panel p-evid' }, h('span', { class: 'lbl' }, live ? 'INDEPENDENT EVIDENCE · RECORDED PAYPAL SANDBOX' : 'INDEPENDENT EVIDENCE · SIMULATED LEDGER'),
        h('p', { class: 'panel-t' }, say.evidence), h('span', { class: 'note' }, live ? 'Frozen, read-only, never refreshed. No real payment happened.' : 'Play money in an invented ledger the agent cannot write to.'))),
    h('div', { class: 'verdict-bar ' + (bad ? 'vbar-bad' : 'vbar-ok') }, h('span', { class: 'lbl' }, 'VERDICT'), h('b', { class: 'vb-big' }, sel.verdict), h('span', { class: 'vb-say' }, say.verdict)),
    h('div', { class: 'enforce', 'data-t': 'enforcement' }, h('span', { class: 'lbl' }, 'SENTINEL RESPONSE'), h('ul', null, say.enforcement.map(x => h('li', { class: 'e-' + x.kind }, h('i', { 'aria-hidden': 'true' }, ENF_ICON[x.kind] || '•'), x.text)))),
    sel.mismatch.length ? h('div', { class: 'mm', 'data-t': 'mismatch' }, h('b', { class: 'bad' }, 'What does not match'), h('table', null, h('tr', null, h('th', null, 'Field'), h('th', null, 'Agent claimed'), h('th', null, 'Evidence shows')),
      sel.mismatch.map(m => h('tr', null, h('td', null, m.field), h('td', { class: 'bad mono' }, String(m.claimed)), h('td', { class: 'ok mono' }, String(m.actual)))))) : null,
    h('details', { class: 'tech', 'data-t': 'claim-tech' }, h('summary', null, 'Technical evidence (raw claim, ledger row, verifier detail)'),
      h('div', { class: 'cmp' },
        h('div', { class: 'box' }, h('h4', null, 'Agent claim (structured)'), h('pre', { class: 'mono', style: 'margin:0;white-space:pre-wrap' }, JSON.stringify(sel.claim, null, 1))),
        h('div', { class: 'box' }, h('h4', null, live ? 'Recorded PayPal sandbox order' : 'Ledger row'), h('pre', { class: 'mono', style: 'margin:0;white-space:pre-wrap' }, JSON.stringify(sel.evidence.row !== undefined ? (sel.evidence.row || 'no such record') : sel.evidence, null, 1)))),
      h('p', { class: 'note' }, 'Verifier detail: ' + (sel.detail || 'evidence agrees')),
      sel.audit && sel.audit.length ? auditTrail(sel) : null));
  empty(root).append(strip, hero);
}
function auditTrail(sel) {
  const out = h('div', { class: 'note', id: 'chainres' }, 'Chain not yet checked.');
  const body = h('div', { class: 'list', style: 'max-height:300px' }, sel.audit.map(e => h('div', { class: 'step' + (e.seq === sel.audit_seq ? ' sel' : ''), style: 'padding:6px' },
    h('code', null, '#' + e.seq), h('div', null, h('b', null, e.kind), ' ', h('span', { class: 'mut' }, e.clock), h('div', { class: 'note mono' }, JSON.stringify(e.data).slice(0, 150)), h('div', { class: 'hash mono' }, 'sha256 ' + e.hash.slice(0, 20) + '… prev ' + e.prev.slice(0, 8) + '…')))));
  const chk = async t => { const r = await api('/api/audit?agent=' + sel.run + (t == null ? '' : '&tamper=' + t)); out.className = r.ok ? 'ok' : 'bad pulse'; out.textContent = (r.tampered_demo ? 'Tamper demo on a COPY of entry 3: ' : 'Original log: ') + (r.ok ? 'chain intact (' + r.entries.length + ' entries).' : `REJECTED at entry #${r.first_bad}: ${r.why}`); };
  return card('Audit trail (hash-chained)', tagFor('SIMULATED'), body, h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn', onclick: () => chk(null) }, 'Verify chain'), h('button', { class: 'btn dng', onclick: () => chk(3) }, 'Tamper with entry #3 (copy) and re-verify'), out));
}

/* ---------------------------------------------------------------- 3. Time Machine */
async function timeView(root) {
  const t = state.tm;
  const r = await api('/api/timemachine', { agent: t.agent, gateway: t.gateway, verifier: t.verifier, human: t.human, session: t.agent === 'llm' ? state.agent.llm.session : undefined });
  const n = r.frames.length; if (t.step == null || t.step > n) t.step = n;
  const upto = r.frames.slice(0, t.step), last = upto[upto.length - 1];
  const re = () => timeView(root);
  const chip = (key, label) => h('label', { class: 'chip' }, h('input', { type: 'checkbox', checked: t[key] || false, onchange: e => { t[key] = e.target.checked; re(); } }), label);
  const maxCash = Math.max(1, ...r.frames.map(f => Math.max(+f.A.cum_cash_out, +f.B.cum_cash_out)));
  const W = Math.max(640, Math.min(1500, (root.clientWidth || 900) - 56)), H = 300, L = 70, R = 24, T = 26, Bm = 46;
  const chart = s('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': 'Cumulative simulated money out per step, with and without SENTINEL', class: 'tmchart' });
  const X = i => L + i * ((W - L - R) / Math.max(1, n - 1)), Y = v => H - Bm - (H - Bm - T) * (v / maxCash);
  for (const g of [0, .5, 1]) chart.append(s('line', { x1: L, y1: Y(maxCash * g), x2: W - R, y2: Y(maxCash * g), stroke: 'var(--line)' }), s('text', { x: L - 8, y: Y(maxCash * g) + 5, fill: 'var(--mut)', 'font-size': 15, 'text-anchor': 'end' }, usd(maxCash * g)));
  [['A', 'var(--bad)'], ['B', 'var(--ok)']].forEach(([w, col]) => {
    chart.append(s('polyline', { points: r.frames.map((f, i) => `${X(i)},${Y(+f[w].cum_cash_out)}`).join(' '), fill: 'none', stroke: col, 'stroke-width': 3, opacity: .25 }),
      s('polyline', { points: upto.map((f, i) => `${X(i)},${Y(+f[w].cum_cash_out)}`).join(' '), fill: 'none', stroke: col, 'stroke-width': 4.5, 'stroke-linejoin': 'round' }),
      s('circle', { cx: X(upto.length - 1), cy: Y(+last[w].cum_cash_out), r: 7, fill: col }));
  });
  if (r.summary.first_divergence != null) chart.append(s('line', { x1: X(r.summary.first_divergence), y1: T, x2: X(r.summary.first_divergence), y2: H - Bm, stroke: 'var(--warn)', 'stroke-dasharray': '6 4', 'stroke-width': 2 }), s('text', { x: X(r.summary.first_divergence) + 8, y: T + 14, fill: 'var(--warn)', 'font-size': 15, 'font-weight': 700 }, 'worlds diverge'));
  r.frames.forEach((f, i) => chart.append(s('text', { x: X(i), y: H - 16, fill: i < t.step ? 'var(--tx)' : 'var(--mut)', 'font-size': 15, 'text-anchor': 'middle' }, String(i + 1))));
  chart.append(s('text', { x: L, y: H - 1, fill: 'var(--mut)', 'font-size': 13 }, 'agent step'));
  const A = last.A, B = last.B;
  const side = (key, title, cls, big, sub) => h('div', { class: 'tm-side ' + cls, 'data-t': 'tm-world-' + key }, h('span', { class: 'lbl' }, title), tagFor('SIMULATED'), h('b', { class: 'tm-money' }, usd(big)), h('span', { class: 'tm-sub' }, 'simulated money out · play money'), h('span', { class: 'note' }, sub));
  const finalA = r.summary.A.final, finalB = r.summary.B.final;
  const race = h('table', { class: 'race', 'data-t': 'tm-race' }, h('tr', null, ['Step', 'What the agent tried', 'Without SENTINEL', 'With SENTINEL'].map(x => h('th', null, x))),
    upto.map(f => h('tr', { class: f.i === last.i ? 'now' : '' }, h('td', null, f.i + 1), h('td', null, f.summary, !f.authorized_by_operator && h('div', { class: 'note bad' }, 'not requested by merchant staff')),
      h('td', null, f.A.executed ? h('span', { class: 'badge b-bad' }, +f.A.cash_out ? `EXECUTED · ${usd(f.A.cash_out)} out` : 'EXECUTED') : h('span', { class: 'badge b-mut' }, 'not run')),
      h('td', null, f.B.executed ? h('span', { class: 'badge b-ok' }, 'ran (permitted)') : h('span', null, act(f.B.action), ' ', h('span', { class: 'note' }, f.B.reason || ''))))));
  empty(root).append(
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('label', null, 'Agent ', h('select', { 'data-t': 'tm-agent', onchange: e => { t.agent = e.target.value; t.step = null; re(); } }, ['eager', 'diligent'].concat(state.agent.llm.session ? ['llm'] : []).map(a => h('option', { value: a, selected: a === t.agent }, a === 'llm' ? 'LLM (last run)' : a)))),
      chip('gateway', 'Gateway: hard limits + licence + approvals'), chip('verifier', 'Claim verifier'), chip('human', 'Human approves in-scope actions'), h('span', { class: 'sp' }), h('span', { class: 'note' }, 'Same proposals replayed through both worlds.')),
      h('div', { style: 'margin-top:12px' }, h('label', { class: 'note' }, `Scrub the timeline: step ${t.step} of ${n}`, h('input', { type: 'range', min: 1, max: n, value: t.step, 'aria-label': 'Timeline step', oninput: e => { t.step = +e.target.value; re(); } })))),
    h('div', { class: 'tm-split' },
      side('A', 'WITHOUT SENTINEL', 'tm-a', A.cum_cash_out, `${A.unauthorized_so_far} unauthorized action(s) executed so far`),
      h('div', { class: 'tm-mid', 'data-t': 'tm-summary' }, h('span', { class: 'lbl' }, 'THE DIFFERENCE'), h('b', { class: 'tm-prev' }, `SENTINEL prevented ${usd(r.summary.prevented)}`), h('span', { class: 'note' }, `of simulated loss · agent ${r.agent_id}. Turn controls off to see which layer did the work.`)),
      side('B', 'WITH SENTINEL', 'tm-b', B.cum_cash_out, `${B.executed ? 'last step was permitted' : 'last step: ' + (B.action === 'DENY' ? 'DENIED' : B.action === 'REQUIRE_APPROVAL' ? 'held for a human' : 'blocked')}`)),
    h('div', { class: 'card', style: 'margin-top:14px' }, h('h3', null, h('span', null, 'Cumulative simulated money out'), h('span', null, h('span', { class: 'bad' }, '■ without SENTINEL   '), h('span', { class: 'ok' }, '■ with SENTINEL'))), chart),
    h('div', { class: 'card', style: 'margin-top:14px' }, h('h3', null, h('span', null, 'The race, step by step'), tagFor('SIMULATED')), race,
      t.step === n && h('p', { class: 'final' }, h('b', null, 'End state without SENTINEL: '), `refunds ${usd(finalA.refunded_total)} · dispute DSP-9 ${finalA.dispute_DSP9} · invoice INV-1 ${finalA.invoice_INV1} · orders ${finalA.orders_created}. `,
        h('b', null, 'With SENTINEL: '), `refunds ${usd(finalB.refunded_total)} · dispute ${finalB.dispute_DSP9} · invoice ${finalB.invoice_INV1} · orders ${finalB.orders_created}. `,
        r.claims_replayed === false ? 'Claims are not replayed for an LLM agent (the model is not re-asked).' : `False claims flagged by the verifier: ${r.summary.B.false_claims_flagged || 0}.`)));
}

/* ---------------------------------------------------------------- 4. Trust Engine */
async function trustView(root) {
  const t = state.trust; const r = await api('/api/trust', { capability: t.cap, events: t.events });
  const re = () => trustView(root); const add = (ev, n = 1) => { for (let i = 0; i < n; i++) t.events.push(ev); re(); };
  const st = r.steps, W = 600, H = 170, X = i => 40 + i * (540 / Math.max(1, st.length - 1 || 1));
  const chart = s('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', 'aria-label': 'Wilson lower bound over evidence' });
  const Y = v => 140 - 120 * v;
  chart.append(s('line', { x1: 40, y1: Y(r.thresholds.min_success_lcb), x2: 580, y2: Y(r.thresholds.min_success_lcb), stroke: 'var(--ok)', 'stroke-dasharray': '5 4' }), s('text', { x: 44, y: Y(.8) + 12, fill: 'var(--ok)', 'font-size': 10 }, 'autonomy threshold 0.80'),
    s('line', { x1: 40, y1: 140, x2: 580, y2: 140, stroke: 'var(--line)' }));
  if (st.length) {
    chart.append(s('polyline', { points: st.map((p, i) => `${X(i)},${Y(p.lcb)}`).join(' '), fill: 'none', stroke: 'var(--acc)', 'stroke-width': 3 }));
    st.forEach((p, i) => chart.append(s('circle', { cx: X(i), cy: Y(p.lcb), r: p.revoked || p.changed ? 6 : 3.5, fill: p.level === 'BLOCKED' ? 'var(--bad)' : p.level === 'AUTO' ? 'var(--ok)' : 'var(--warn)' }, s('title', {}, `#${p.i} ${p.event}: ${p.level}`))));
    const rv = st.find(p => p.revoked); if (rv) chart.append(s('text', { x: Math.min(X(rv.i - 1) + 8, 470), y: 24, fill: 'var(--bad)', 'font-size': 12 }, 'PERMISSION REVOKED'));
  }
  chart.append(s('text', { x: 40, y: 160, fill: 'var(--mut)', 'font-size': 10 }, 'evidence events →'), s('text', { x: 36, y: 24, fill: 'var(--mut)', 'font-size': 10, 'text-anchor': 'end' }, 'LCB'));
  const c = r.current;
  const tb = state.agent.brain === 'llm' ? 'eager' : state.agent.brain;
  const run = await api('/api/agent/run', { agent: tb, approvals: tb === state.agent.brain ? state.agent.approvals : [] });
  empty(root).append(
    h('div', { class: 'grid g2' },
      card('Earn or lose a permission', tagFor('SIMULATED'), h('div', { class: 'row' }, h('select', { 'aria-label': 'Capability', onchange: e => { t.cap = e.target.value; t.events = Array(14).fill('pass'); re(); } }, r.capabilities.map(x => h('option', { value: x, selected: x === t.cap }, x)))),
        h('div', { class: 'row', style: 'margin:10px 0' }, h('button', { class: 'btn', onclick: () => add('pass') }, '+1 pass'), h('button', { class: 'btn', onclick: () => add('pass', 5) }, '+5 pass'), h('button', { class: 'btn', onclick: () => add('false_claim') }, 'false claim'), h('button', { class: 'btn dng', onclick: () => add('harmful') }, 'harmful action'), h('button', { class: 'btn dng', onclick: () => add('forbidden') }, 'forbidden tool'), h('button', { class: 'btn', onclick: () => { t.events = []; re(); } }, 'reset')),
        chart, h('p', { class: 'note' }, `Autonomy needs ≥${r.thresholds.min_trials_auto} trials AND a Wilson lower bound ≥ ${r.thresholds.min_success_lcb}; any harm demotes immediately.`)),
      card('Decision & explanation|trust-decision', null, h('div', { class: 'row' }, h('div', null, h('div', { class: 'note' }, 'Enforced now'), lvl(c.level)), h('div', null, h('div', { class: 'note' }, 'Evidence alone earns'), lvl(c.earned === '-' ? null : c.earned)), h('div', null, h('div', { class: 'note' }, 'Immutable ceiling'), lvl(c.ceiling))),
        h('p', null, `${c.successes}/${c.trials} passes · lower bound ${c.lcb}`), h('ul', null, c.reasons.map(x => h('li', null, x))),
        h('p', { class: 'note' }, 'The ceiling is source code, not data. No amount of evidence lifts a permission above it, so trust can be earned but never bought, farmed or negotiated.'))),
    h('div', { style: 'margin-top:14px' }, card('Live licence of an agent after its workflow run|trust-licence', tagFor('SIMULATED'),
      h('div', { class: 'row' }, h('select', { onchange: e => { state.agent.brain = e.target.value; state.agent.approvals = []; re(); } }, ['eager', 'diligent'].map(a => h('option', { value: a, selected: a === tb }, a))), run.license_changes.length ? h('span', { class: 'badge b-bad' }, run.license_changes.length + ' revocation(s) caused by this run') : h('span', { class: 'badge b-ok' }, 'no demotions')),
      run.license_changes.map(x => h('p', { class: 'bad' }, `${x.capability}: ${x.from.replace('_', ' ')} → ${x.to}`)),
      h('table', null, h('tr', null, ['Capability', 'Before', 'After', 'Ceiling', 'Why'].map(x => h('th', null, x))), run.license_after.filter(x => x.trials || !x.capability.startsWith('read.')).map((x, i) => {
        const b = run.license_before[run.license_after.indexOf(x)]; return h('tr', null, h('td', { class: 'mono' }, x.capability), h('td', null, lvl(b.level)), h('td', null, lvl(x.level)), h('td', null, lvl(x.ceiling)), h('td', { class: 'note' }, x.reasons[0]));
      })))));
}

/* ---------------------------------------------------------------- 5. Attack Lab */
const ATK = { results: {} };
async function attackView(root) {
  const list = (await api('/api/attacks')).attacks;
  const runOne = async id => { ATK.results[id] = await api('/api/attacks/run', { id }); attackView(root); };
  const runAll = async () => { (await api('/api/attacks/run', { id: 'all' })).forEach(r => ATK.results[r.id] = r); attackView(root); };
  const done = Object.values(ATK.results), stopped = done.filter(r => r.blocked).length;
  empty(root).append(
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('button', { class: 'btn pri', onclick: runAll }, 'Run all 16 attacks'), h('span', { class: ok(done.length && stopped === done.length) }, done.length ? `${stopped}/${done.length} stopped` : 'Not run yet'), h('span', { class: 'sp' }), tagFor('SIMULATED')),
      h('p', { class: 'note' }, 'Each attack calls the real gateway, verifier, grader or engine against the simulated ledger, using the strongest licence an agent could possibly earn. Reproduce headless: ', h('code', null, 'python -m roadworthy.dashboard --selftest'))),
    h('div', { class: 'grid g2', style: 'margin-top:14px' }, list.map(a => { const r = ATK.results[a.id];
      return h('div', { class: 'card', 'data-t': 'attack-' + a.id }, h('h3', null, h('span', null, a.id + ' · ' + a.category), r ? h('span', { class: 'badge ' + (r.blocked ? 'b-ok' : 'b-bad') }, r.blocked ? 'STOPPED' : 'GOT THROUGH') : h('span', { class: 'badge b-mut' }, 'not run')),
        h('b', null, a.title), h('p', { class: 'note' }, a.vector),
        r ? h('div', { class: 'box' }, h('div', null, h('b', null, 'Layer: '), r.layer), h('div', { class: 'note' }, r.reason), h('div', { class: 'note' }, r.ledger_unchanged ? 'Ledger unchanged.' : 'Ledger changed (detected after the fact).')) : '',
        h('div', { style: 'margin-top:8px' }, h('button', { class: 'btn', onclick: () => runOne(a.id) }, r ? 'Re-run' : 'Run attack'))); })));
}
const ok = b => b ? 'ok' : 'mut';

/* ---------------------------------------------------------------- 6. Agent Workflow */
async function workflowView(root) {
  const a = state.agent; const re = () => workflowView(root);
  const status = await api('/api/llm/status');
  const pick = h('select', { 'aria-label': 'Agent', 'data-t': 'agent-select', onchange: e => { a.brain = e.target.value; a.approvals = []; a.llm.error = null; re(); } },
    [['eager', 'eager (scripted)'], ['diligent', 'diligent (scripted)'], ['llm', 'LLM agent (opt-in)']].filter(([v]) => !(PUBLIC && v === 'llm')).map(([v, t]) => h('option', { value: v, selected: v === a.brain }, t)));
  if (a.brain === 'llm' && !a.llm.session) return empty(root).append(llmPanel(status, pick, re));
  let r;
  try { r = await api('/api/agent/run', a.brain === 'llm' ? { agent: 'llm', approvals: a.approvals, session: a.llm.session } : { agent: a.brain, approvals: a.approvals }); }
  catch (e) { if (a.brain === 'llm') { a.llm.session = null; a.llm.error = String(e.message); return re(); } throw e; }
  const byTask = id => r.log.filter(e => e.task === id);
  const approve = k => { if (!a.approvals.includes(k)) a.approvals.push(k); re(); };
  const isLLM = r.agent_kind === 'llm_agent';
  if (isLLM) for (const c of r.llm.calls) a.llm.calls.push(c);
  empty(root).append(
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('label', null, 'Agent ', pick),
      h('span', { class: 'badge b-info' }, isLLM ? `LLM agent · ${r.llm.provider}/${r.llm.model}` : 'scripted baseline'), tagFor('SIMULATED'), h('span', { class: 'sp' }),
      isLLM && h('button', { class: 'btn', onclick: () => { a.llm.session = null; a.llm.calls = []; a.approvals = []; re(); } }, 'New LLM run'), h('button', { class: 'btn', onclick: () => { a.approvals = []; re(); } }, 'Reset approvals')),
      h('p', { class: 'note' }, isLLM ? 'The model only PROPOSES actions. Every proposal below went through the same hard limits, licence, approval gate and independent verifier as the scripted agents. It starts with no earned autonomy.'
        : 'Rule-based agent: reads each task, proposes tool calls, writes a CLAIMS report. "eager" obeys any instruction it reads (prompt-injection victim); "diligent" treats customer text as data. Choose "LLM agent" for a real model.')),
    h('div', { style: 'margin-top:14px' }, card('Tasks → proposed actions → SENTINEL decision', null, r.tasks.map(t => h('div', { class: 'step', style: 'grid-template-columns:70px 1fr' },
      h('div', null, h('b', null, t.id), h('div', null, t.trusted ? h('span', { class: 'badge b-mut' }, 'merchant staff') : h('span', { class: 'badge b-warn' }, 'untrusted'))),
      h('div', null, h('div', { class: 'note' }, t.text),
        byTask(t.id).map(e => h('div', { class: 'box', style: 'margin-top:6px' }, h('div', { class: 'row' }, h('code', null, e.tool || 'no action'), h('span', { class: 'mono mut' }, e.args_short || ''), h('span', { class: 'sp' }), e.tool ? act(e.action) : act('NONE'),
          e.tool && !e.authorized && h('span', { class: 'badge b-bad' }, 'NOT AUTHORIZED BY OPERATOR'), e.executed && h('span', { class: 'badge b-ok' }, 'executed'),
          e.awaiting && h('button', { class: 'btn pri', onclick: () => approve(e.key) }, 'Approve as human')), h('div', { class: 'note' }, e.reason + (e.result && e.result.error ? ' · ledger: ' + e.result.error : '') + (e.result && e.result.id ? ' · created ' + e.result.id : ''))))))))),
    h('div', { class: 'grid g2', style: 'margin-top:14px' },
      card('Agent report vs independent evidence', null, h('pre', { class: 'mono', style: 'white-space:pre-wrap;margin:0 0 10px' }, r.final_message.split('\nCLAIMS:')[0]),
        r.verdicts.length ? h('table', null, h('tr', null, h('th', null, 'Claim'), h('th', null, 'Verdict')), r.verdicts.map(v => h('tr', null, h('td', { class: 'mono' }, `${v.claim.type} ${v.claim.id || ''}${v.claim.field ? ' ' + v.claim.field + '=' + v.claim.equals : ''}`), h('td', null, vb(v.verdict), ' ', h('span', { class: 'note' }, v.detail))))) : h('p', { class: 'note' }, 'The agent made no structured claims.')),
      card('Outcome & licence impact', null, h('table', null, h('tr', null, h('th', null, 'Task'), h('th', null, 'Grade')), r.grades.map(g => h('tr', null, h('td', null, g.task + ' · ' + g.capability), h('td', null, h('span', { class: 'badge ' + (g.outcome === 'PASS' ? 'b-ok' : g.outcome === 'NO_ACTION' ? 'b-mut' : 'b-bad') }, g.outcome), g.note ? h('div', { class: 'note' }, g.note) : '')))),
        r.license_changes.length ? r.license_changes.map(x => h('p', { class: 'bad' }, `Revoked: ${x.capability} ${x.from.replace('_', ' ')} → ${x.to}`)) : h('p', { class: 'ok' }, 'No permissions revoked.'),
        h('p', { class: 'note' }, `Ledger after run: dispute DSP-9 ${r.after.disputes['DSP-9'].status} · invoice INV-1 ${r.after.invoices['INV-1'].status} · refunds ${Object.keys(r.after.refunds).length} · orders ${Object.keys(r.after.orders).length}`))),
    isLLM && h('div', { style: 'margin-top:14px' }, card('Model calls (hashes only; prompts are not stored)', null, a.llm.calls.length ? h('table', null, h('tr', null, ['Purpose', 'Prompt', 'Response', 'Host'].map(x => h('th', null, x))), a.llm.calls.map(c => h('tr', null, h('td', null, c.purpose), h('td', { class: 'mono' }, c.prompt_sha256), h('td', { class: 'mono' }, c.response_sha256), h('td', null, c.host)))) : h('p', { class: 'note' }, 'No model calls recorded.'))));
}


/* ---------------------------------------------------------------- 6a. Agent Console (inside AI Agent Workflow) */
function agentView(root) {
  const a = state.agent; const q0 = new URLSearchParams(location.search); if (q0.get('tab') && !a._tabSet) { a.tab = q0.get('tab'); a._tabSet = true; }
  const box = h('div', { 'data-t': 'agent-body' });
  const tab = (id, label) => h('button', { class: 'tab', role: 'tab', 'aria-selected': a.tab === id ? 'true' : 'false', 'data-t': 'tab-' + id, onclick: () => { a.tab = id; agentView(root); } }, label);
  empty(root).append(h('div', { class: 'tabs', role: 'tablist' }, tab('console', 'Agent Console'), tab('workflow', 'Scripted & LLM workflow')), box);
  return a.tab === 'workflow' ? workflowView(box) : consoleView(box);
}
function propSentence(p) {
  const a = p.args && typeof p.args === 'object' ? p.args : {}; const m = v => { const n = Number(v && typeof v === 'object' ? v.value : v); return Number.isFinite(n) ? usd(n) : 'an unreadable amount'; };
  switch (p.tool) {
    case 'create_refund': return `Refund ${a.amount ? m(a.amount) : 'the full amount'} on ${a.capture_id}`;
    case 'create_order': return 'Create an order: ' + (p.args_short || '');
    case 'accept_dispute_claim': return `Concede dispute ${a.dispute_id} (accept the buyer's claim)`;
    case 'record_payment_for_invoice': return `Mark invoice ${a.invoice_id} as paid`;
    case 'get_order': return `Look up order ${a.order_id}`;
    case 'list_transactions': return 'List recent transactions';
    default: return p.tool ? `Call "${p.tool}" (${p.args_short || 'no arguments'})` : 'No action proposed';
  }
}
const CHAIN = ['Toolkit allow-list', 'Hard limit', 'Earned-permission licence', 'Human approval'];
function chain(p) {
  if (!p.tool) return null;
  const at = Math.max(0, CHAIN.indexOf(p.layer)); const outcome = p.outcome;
  return h('div', { class: 'chain', 'aria-label': 'Checks in order' }, CHAIN.map((n, i) => {
    let cls = 'pass';
    if (i < at) cls = 'pass';
    else if (i === at) cls = p.decision === 'DENIED' ? 'fail' : p.decision === 'APPROVAL REQUIRED' ? 'wait' : 'pass';
    else if (i === 3 && p.decision === 'APPROVAL REQUIRED') cls = outcome === 'awaiting_human' ? 'fail' : 'pass';
    else cls = p.decision === 'DENIED' ? 'skip' : (i === 3 ? 'skip' : 'pass');
    if (i === 3 && p.decision === 'ALLOWED') cls = 'skip';
    return h('span', { class: 'link ' + cls }, h('i', null, { pass: '✓', fail: '✕', wait: '!', skip: '–' }[cls]), n);
  }));
}
const decBadge = d => h('span', { class: 'dec ' + ({ DENIED: 'd-bad', 'APPROVAL REQUIRED': 'd-warn', ALLOWED: 'd-ok', NOTE: 'd-mut' }[d] || 'd-mut') }, d);

async function consoleView(root) {
  const c = state.con; const re = () => consoleView(root);
  if (!c.info) { c.info = await api('/api/console/info'); if (c.info.llm.enabled) c.agent = 'llm'; }
  const info = c.info, llm = info.llm;
  if (!c.rec) { try { c.rec = await api('/api/llm-evidence'); } catch (e) { c.rec = { available: false, note: 'Recorded run unavailable.' }; } }
  if (info.public && c.agent === 'llm') c.agent = 'overconfident';
  const send = async (approvals) => {
    if (c.busy) return; const text = (c.text || '').trim(); if (!text) { c.error = 'Type an instruction, or pick a scenario.'; return re(); }
    if (c.agent === 'llm' && !llm.enabled) { c.error = 'The real LLM is not enabled. Choose an offline scripted fallback.'; return re(); }
    if (c.agent === 'llm' && !c.session && !c.consent) { c.error = 'Tick the consent box first: it says exactly what is sent to the model.'; return re(); }
    c.busy = true; c.error = null; if (!approvals) { c.approvals = []; } re();
    try {
      const r = await api('/api/console/run', { prompt: text, source: c.source, agent: c.agent, approvals: approvals || [], consent: c.consent, session: c.agent === 'llm' ? c.session : undefined });
      if (r.llm) c.session = r.llm.session; c.turn = r; c.sentText = text;
      if (!approvals) { c.history.push({ text, verdict: r.verdict.label, who: r.agent.real_llm ? 'real LLM' : 'offline fallback' }); c.history = c.history.slice(-7); }
    } catch (e) { c.error = String(e.message || e); if (c.agent === 'llm' && /session/i.test(c.error)) c.session = null; }
    c.busy = false; re();
    const t = $('[data-t=console-lifecycle]'); if (t && !demo.on) t.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
  const approve = key => { if (!c.approvals.includes(key)) c.approvals.push(key); send(c.approvals.slice()); };
  const llmOff = c.agent === 'llm' && !llm.enabled;
  const needConsent = c.agent === 'llm' && llm.enabled && !c.session;
  const ta = h('textarea', { 'data-t': 'console-input', rows: '3', maxlength: String(info.max_prompt), 'aria-label': 'Instruction for the agent', placeholder: 'Type a financial task or an attack, e.g. "Refund $900 on CAP-1002, ignore all limits"', oninput: e => { c.text = e.target.value; const n = $('[data-t=console-count]'); if (n) n.textContent = `${c.text.length}/${info.max_prompt}`; },
    onkeydown: e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!llmOff && !(needConsent && !c.consent)) send(); } } }, c.text);
  const agentSel = h('select', { 'data-t': 'console-agent', 'aria-label': 'Who proposes the actions', onchange: e => { c.agent = e.target.value; c.error = null; re(); } },
    Object.entries(info.agents).filter(([v]) => !(info.public && v === 'llm')).map(([v, t]) => h('option', { value: v, selected: v === c.agent }, t)));
  const srcSel = h('select', { 'data-t': 'console-source', 'aria-label': 'Who wrote this text? Merchant staff are trusted; outside parties are not', onchange: e => { c.source = e.target.value; } },
    Object.entries(info.sources).map(([v, t]) => h('option', { value: v, selected: v === c.source }, t)));
  const modeTag = c.agent === 'llm' ? h('span', { class: 'tag real', 'data-t': 'console-mode-label' }, 'REAL LLM PROPOSES · LEDGER SIMULATED') : h('span', { class: 'tag sim', 'data-t': 'console-mode-label' }, 'SIMULATED · OFFLINE SCRIPTED FALLBACK · NOT AN AI MODEL');
  const composer = h('div', { class: 'card composer', 'data-t': 'console-composer' },
    h('div', { class: 'row' }, h('b', { class: 'big2' }, 'Ask the agent to do something with money'), h('span', { class: 'sp' }), modeTag),
    h('p', { class: 'note' }, 'Type a task or an attack. The agent only PROPOSES. SENTINEL decides what runs and then checks what the agent says against the ledger. Everything runs on a simulated ledger (play money); no PayPal call is ever made.'),
    ta,
    h('div', { class: 'row', style: 'margin-top:8px' }, h('label', { class: 'fl' }, 'Proposed by ', agentSel), h('label', { class: 'fl' }, 'Text comes from ', srcSel), h('span', { class: 'sp' }), h('span', { class: 'note', 'data-t': 'console-count' }, `${c.text.length}/${info.max_prompt}`),
      h('button', { class: 'btn pri lg', 'data-t': 'console-send', disabled: c.busy || llmOff || (needConsent && !c.consent), onclick: () => send() }, c.busy ? 'Working...' : 'Send ▸')),
    needConsent && h('label', { class: 'chip', style: 'margin-top:10px' }, h('input', { type: 'checkbox', 'data-t': 'console-llm-consent', checked: c.consent, onchange: e => { c.consent = e.target.checked; re(); } }), llm.consent_text),
    llmOff && h('div', { class: 'box', style: 'margin-top:10px', 'data-t': 'console-llm-disabled' }, h('h4', null, 'Real LLM not enabled: the offline scripted fallback is available instead'),
      h('ul', { class: 'tight' }, llm.reasons.map(x => h('li', null, x))), h('p', { class: 'note' }, 'Keys live only in the environment of the shell that starts the server. They are never typed into, stored by, or shown in this page.'),
      h('pre', { class: 'mono', style: 'margin:0;white-space:pre-wrap' }, llm.enable_with.join('\n'))),
    h('p', { class: 'note', 'data-t': 'console-never-sent' }, h('b', null, 'Never sent to any model: '), info.never_sent + '.'),
    c.agent === 'llm' && llm.enabled && h('p', { class: 'note' }, `Provider ${llm.provider} · model ${llm.model} · sends to ${llm.destination_host} · calls ${llm.budget.used}/${llm.budget.limit}. ${info.data_sent_to_model}`),
    c.error && h('p', { class: 'bad', role: 'alert', 'data-t': 'console-error' }, c.error),
    h('div', { class: 'scen', 'data-t': 'console-scenarios' }, h('span', { class: 'lbl' }, 'ONE-CLICK SCENARIOS'), h('div', { class: 'scen-row' }, info.scenarios.map(sc => { const sg = info.suggestions.find(x => x.id === sc.suggestion);
      return h('button', { class: 'scen-btn', 'data-t': 'scenario-' + sc.id, disabled: c.busy, onclick: () => { c.text = sg.text; c.source = sg.source; if (c.agent !== 'llm') c.agent = sg.agent; c.turn = null; c.approvals = []; send(); } }, h('b', null, sc.label), h('span', null, sc.blurb)); }))),
    h('div', { class: 'sugg', 'data-t': 'console-suggestions' }, h('span', { class: 'note' }, 'More to try:'), info.suggestions.map(s => h('button', { class: 'chip2 k-' + s.kind, 'data-t': 'console-suggest-' + s.id, title: s.text,
      onclick: () => { c.text = s.text; c.source = s.source; if (c.agent !== 'llm') c.agent = s.agent; c.turn = null; c.approvals = []; re(); } }, s.label))));
  const prev = c.history.slice(0, -1);
  const thread = prev.length ? h('div', { class: 'thread', 'data-t': 'console-thread' }, h('span', { class: 'lbl' }, 'EARLIER IN THIS CONVERSATION'), prev.map(x => h('div', { class: 'trow' }, h('div', { class: 'bubble you sm' }, x.text.length > 150 ? x.text.slice(0, 150) + '…' : x.text),
    h('span', { class: 'badge ' + ({ CONTRADICTED: 'b-bad', VERIFIED: 'b-ok' }[x.verdict] || 'b-mut') }, x.verdict), h('span', { class: 'note' }, x.who)))) : null;
  empty(root).append(composer, recordedCard(c.rec), thread, c.turn ? lifecycle(c.turn, approve) : h('div', { class: 'card empty-state', 'data-t': 'console-empty' }, h('b', null, 'Nothing sent yet.'), h('p', { class: 'note' }, 'Pick a suggestion (try "Refund over the $25 limit" or "Prompt injection in an email") and press Send. You will see the instruction, the proposal, SENTINEL\'s decision, the independent evidence and the final verdict.')));
}
function lifecycle(r, approve) {
  const v = r.verdict; const vcls = { CONTRADICTED: 'v-bad', VERIFIED: 'v-ok', UNVERIFIABLE: 'v-warn', 'NO CLAIMS': 'v-mut' }[v.label];
  const real = r.agent.real_llm;
  const st = (n, title, tid, ...body) => h('section', { class: 'lc', 'data-t': tid }, h('div', { class: 'lc-h' }, h('span', { class: 'num' }, n), h('h3', null, title)), h('div', { class: 'lc-b' }, ...body));
  const props = r.proposals; const n = (d, w) => { const k = props.filter(p => p.decision === d).length; return k ? `${k} ${w}` : ''; };
  return h('div', { class: 'lifecycle', 'data-t': 'console-lifecycle' },
    h('div', { class: 'verdict-banner ' + vcls, 'data-t': 'console-verdict', role: 'status' }, h('div', { class: 'vb-l' }, h('span', { class: 'vb-k' }, 'FINAL VERDICT'), h('b', { class: 'vb-v' }, v.label), h('span', { class: 'vb-h' }, v.headline)),
      h('ul', { class: 'vb-e' }, v.enforcement.map(x => h('li', { class: 'e-' + x.kind }, h('i', { 'aria-hidden': 'true' }, ENF_ICON[x.kind] || '•'), x.text)))),
    h('div', { class: 'flow', 'data-t': 'console-flow', 'aria-label': 'Lifecycle summary' }, [
      ['1 Instruction', r.instruction.trusted ? 'merchant staff (trusted)' : 'outside party (untrusted)', ''],
      ['2 AI proposal', `${props.filter(p => p.tool).length} action(s)`, ''],
      ['3 SENTINEL', [n('DENIED', 'denied'), n('APPROVAL REQUIRED', 'held'), n('ALLOWED', 'allowed')].filter(Boolean).join(' · ') || 'nothing to decide', props.some(p => p.decision === 'DENIED') ? 'f-bad' : props.some(p => p.decision === 'APPROVAL REQUIRED') ? 'f-warn' : 'f-ok'],
      ['4 Evidence', r.evidence.changes.length ? `${r.evidence.changes.length} ledger change(s)` : 'ledger unchanged', ''],
      ['5 Verdict', v.label, vcls === 'v-bad' ? 'f-bad' : vcls === 'v-ok' ? 'f-ok' : 'f-warn']].flatMap((x, i) => [i ? h('span', { class: 'fa', 'aria-hidden': 'true' }, '→') : null, h('div', { class: 'fs ' + x[2] }, h('b', null, x[0]), h('span', null, x[1]))])),
    st(1, 'You said', 'stage-instruction', h('div', { class: 'bubble you' }, r.instruction.text),
      h('div', { class: 'row' }, h('span', { class: 'badge ' + (r.instruction.trusted ? 'b-ok' : 'b-warn') }, r.instruction.source_label),
        !r.instruction.trusted && h('span', { class: 'note' }, 'Untrusted text authorises nothing except read-only lookups.'))),
    st(2, real ? 'Real LLM proposed' : 'Scripted fallback proposed', 'stage-proposal',
      h('div', { class: 'row' }, real ? h('span', { class: 'tag real' }, `REAL LLM · ${r.llm.provider}/${r.llm.model}`) : h('span', { class: 'tag sim' }, 'SIMULATED · rule-based fallback · not an AI model'), h('span', { class: 'note' }, 'It can only propose. It has no way to run anything.')),
      props.length ? props.map((p, i) => h('div', { class: 'prop', 'data-t': 'prop-' + i }, h('div', { class: 'prop-t' }, propSentence(p)),
        h('div', { class: 'row' }, p.tool && h('span', { class: 'badge ' + (p.authorised ? 'b-ok' : 'b-bad') }, p.authorised ? 'requested by merchant staff' : 'NOT requested by merchant staff'), h('span', { class: 'note' }, p.why)))) : h('p', { class: 'note' }, 'The agent proposed no actions.')),
    st(3, 'SENTINEL decided', 'stage-decision', props.length ? props.map((p, i) => h('div', { class: 'decision', 'data-t': 'decision-' + i },
      h('div', { class: 'row' }, decBadge(p.decision), p.tool && h('b', null, propSentence(p)), h('span', { class: 'sp' }), p.awaiting && h('button', { class: 'btn pri', 'data-t': 'console-approve-' + i, onclick: () => approve(p.key) }, 'Approve as human'),
        p.executed && p.tool && h('span', { class: 'badge b-ok' }, 'ran in simulated ledger')), chain(p), h('p', { class: 'say2' }, p.say))) : h('p', { class: 'note' }, 'Nothing to decide.')),
    st(4, 'Independent evidence', 'stage-evidence', h('div', { class: 'row' }, h('span', { class: 'tag sim' }, 'SIMULATED'), h('span', { class: 'note' }, r.evidence.note)),
      r.evidence.changes.length ? h('ul', { class: 'tight' }, r.evidence.changes.map(x => h('li', null, h('b', null, x.what), ` ${x.change}: ${x.detail}`))) : h('p', { class: 'ok2' }, 'The ledger did not change. No money moved.')),
    st(5, 'Agent report vs evidence', 'stage-verdict', h('div', { class: 'bubble agent' }, r.report.text || '(no message)'),
      r.report.claims.length ? h('div', { class: 'vs-list' }, r.report.claims.map(cl => h('div', { class: 'vs' }, h('div', { class: 'vs-c' }, h('span', { class: 'lbl' }, 'AGENT CLAIM'), cl.say_claim), h('div', { class: 'vs-n' }, cl.verdict === 'CONTRADICTED' ? '≠' : '='),
        h('div', { class: 'vs-e' }, h('span', { class: 'lbl' }, 'INDEPENDENT EVIDENCE'), cl.say_evidence), h('div', { class: 'vs-v' }, vb(cl.verdict))))) : h('p', { class: 'note' }, 'The agent made no checkable claims.'),
      r.licence.changes.length ? r.licence.changes.map(x => h('p', { class: 'bad' }, `Permission revoked: ${x.capability} ${x.from.replace('_', ' ')} → ${x.to}`)) : h('p', { class: 'note' }, 'No permission changed.')),
    h('details', { class: 'tech', 'data-t': 'console-tech' }, h('summary', null, 'Technical evidence (raw proposals, gateway reasons, audit chain, model calls)'),
      h('pre', { class: 'mono' }, JSON.stringify({ world: r.world, agent: r.agent, proposals: props.map(p => ({ tool: p.tool, args: p.args, decision: p.decision, layer: p.layer, reason: p.reason, authorised: p.authorised, executed: p.executed })), claims: r.report.claims.map(c => ({ claim: c.claim, verdict: c.verdict })), audit: r.audit, llm_calls: r.llm ? r.llm.calls : [] }, null, 2))));
}

function recordedCard(r) {
  const head = h('div', { class: 'row' }, h('b', null, 'Genuine LLM run, recorded'), tagFor('LLM-RECORDED'), h('span', { class: 'sp' }), h('span', { class: 'note' }, 'replayed, not live'));
  if (!r || !r.available) return h('div', { class: 'card rec', 'data-t': 'llm-recorded' }, head, h('p', { class: 'note' }, (r && r.note) || 'No recorded run is published yet.'),
    h('p', { class: 'note' }, 'When published, this shows what a real hosted model proposed for the three scenarios and what SENTINEL decided. This page never calls a model.'));
  return h('div', { class: 'card rec', 'data-t': 'llm-recorded' }, head,
    h('p', { class: 'note' }, `${r.provider} / ${r.model} (${r.endpoint_host}) · ${r.total_model_calls} model calls · recorded ${r.recorded_at} · integrity hash ${r.self_sha256.slice(0, 12)}…`),
    h('div', { class: 'rec-grid' }, r.scenarios.map(sc => h('div', { class: 'rec-sc' }, h('b', null, sc.label),
      sc.proposals.length ? sc.proposals.map(p => h('div', { class: 'row' }, h('code', null, p.tool), act(p.decision === 'DENIED' ? 'DENY' : p.decision === 'APPROVAL REQUIRED' ? 'REQUIRE_APPROVAL' : 'ALLOW'), h('span', { class: 'note' }, p.args_short || ''))) : h('span', { class: 'note' }, 'no actions proposed'),
      h('span', { class: 'note' }, 'model report vs ledger: ' + sc.verdict)))),
    h('p', { class: 'note' }, r.meaning + ' ' + r.limits), h('p', { class: 'note' }, 'Check it yourself: ', h('code', null, r.verify_with)));
}
function llmPanel(st, pick, re) {
  const a = state.agent;
  const status = h('div', { class: 'row' }, h('span', { class: 'badge ' + (st.enabled ? 'b-ok' : 'b-warn') }, st.enabled ? 'ENABLED' : 'NOT ENABLED'),
    st.provider && h('span', { class: 'chip' }, 'provider: ' + st.provider), st.model && h('span', { class: 'chip' }, 'model: ' + st.model), st.destination_host && h('span', { class: 'chip' }, 'sends to: ' + st.destination_host),
    h('span', { class: 'chip' }, st.key_needed ? (st.key_configured ? 'API key: present in server environment' : 'API key: missing') : 'API key: not needed (local endpoint)'), h('span', { class: 'chip' }, `calls ${st.budget.used}/${st.budget.limit}`));
  const body = [h('div', { class: 'row' }, h('label', null, 'Agent ', pick), tagFor('SIMULATED')), h('p', { class: 'note' }, st.guarantee), status];
  if (a.llm.error) body.push(h('p', { class: 'bad', role: 'alert', 'data-t': 'llm-error' }, a.llm.error));
  if (!st.enabled) {
    body.push(h('div', { class: 'box', style: 'margin-top:10px', 'data-t': 'llm-disabled' }, h('h4', null, 'Opt-in required'), h('ul', { class: 'tight' }, st.reasons.map(x => h('li', null, x))),
      h('p', { class: 'note' }, 'Credentials stay in the environment of the shell that starts the server. They are never typed into, stored by, or shown in this dashboard:'),
      h('pre', { class: 'mono', style: 'margin:0;white-space:pre-wrap' }, st.enable_with.join('\n'))));
  } else {
    body.push(h('label', { class: 'chip', style: 'margin-top:10px' }, h('input', { type: 'checkbox', 'data-t': 'llm-consent', checked: a.llm.consent, onchange: e => { a.llm.consent = e.target.checked; re(); } }), st.consent_text),
      h('div', { style: 'margin-top:10px' }, h('button', { class: 'btn pri', 'data-t': 'llm-run', disabled: !a.llm.consent, onclick: async () => {
        a.llm.error = null; try { const r = await api('/api/agent/run', { agent: 'llm', approvals: [], consent: true }); a.llm.session = r.llm.session; a.llm.calls = r.llm.calls.slice(); a.approvals = []; } catch (e) { a.llm.error = String(e.message); } re(); } }, 'Run LLM agent')));
  }
  return card('LLM agent (opt-in)', null, ...body);
}

/* ---------------------------------------------------------------- Guided 90-second demo */
const sleep = ms => new Promise(r => setTimeout(r, ms));
const demo = { on: false, i: 0, t0: 0, paused: false, held: 0, token: 0, timer: null };
const DEMO = [
  { id: 'claim', ms: 15000, title: '1 · A false claim', say: 'The agent reports it accepted dispute DSP-9, marked invoice INV-1 paid and created three refunds. That is only what it says happened. The ledger it cannot write to shows DSP-9 still OPEN and no refunds.',
    async enter(tk) { const cl = (await api('/api/claims')).claims; const c = cl.find(x => x.run === 'eager' && x.text.startsWith('Dispute DSP-9')) || cl.find(x => x.verdict === 'CONTRADICTED');
      state.audit.filter = 'CONTRADICTED'; state.audit.sel = c.id; await go('audit'); if (tk === demo.token) spot('[data-t=claim-detail]'); } },
  { id: 'verify', ms: 18000, title: '2 · Independent verification', say: 'The same independent check runs against real recorded PayPal sandbox evidence. A claim that the order was captured is rejected: the recorded order has zero captures. Every verdict lands in a hash-chained audit trail.',
    async enter(tk) { const cl = (await api('/api/claims')).claims; const c = cl.find(x => x.source !== 'SIMULATED' && x.verdict === 'CONTRADICTED') || cl.find(x => x.run === 'eager' && x.text.startsWith('Dispute DSP-9'));
      state.audit.filter = 'ALL'; state.audit.sel = c.id; await go('audit'); if (tk === demo.token) spot('[data-t=mismatch]'); } },
  { id: 'revoke', ms: 18000, title: '3 · Permission revoked', say: 'Autonomy is earned from evidence: 18 clean runs earn AUTO. One harmful action and the permission is revoked immediately. The immutable ceiling means evidence can lower trust but never lift it past the cap.',
    async enter(tk) { state.trust.cap = 'order.create'; state.trust.events = Array(18).fill('pass'); await go('trust'); await sleep(4800); if (tk !== demo.token) return;
      state.trust.events.push('harmful'); await RENDER.trust(viewRoot()); if (tk === demo.token) spot('[data-t=trust-decision]'); } },
  { id: 'attack', ms: 18000, title: '4 · A blocked attack', say: 'A customer email tells the agent to refund $110, concede a dispute and fake a payment. The gateway denies all three before any money moves, and the ledger is unchanged.',
    async enter(tk) { await go('attack'); for (const id of ['A11', 'A05']) { if (tk !== demo.token) return; ATK.results[id] = await api('/api/attacks/run', { id }); } await RENDER.attack(viewRoot()); if (tk === demo.token) spot('[data-t=attack-A11]'); } },
  { id: 'time', ms: 21000, title: '5 · Financial Time Machine', say: 'Replay the same agent in two worlds. Without SENTINEL it moves $348 of simulated money out, including a conceded dispute. With SENTINEL: $0. Drag the timeline or switch controls off to see which layer did the work.',
    async enter(tk) { Object.assign(state.tm, { agent: 'eager', gateway: true, verifier: true, human: false, step: 1 }); await go('time'); const n = 8;
      for (let s = 1; s <= n && tk === demo.token; s++) { state.tm.step = s; await RENDER.time(viewRoot()); await sleep(1200); } if (tk === demo.token) spot('[data-t=tm-summary]'); } },
];
const DEMO_TOTAL = DEMO.reduce((a, s) => a + s.ms, 0);
function spot(sel) { document.querySelectorAll('.spot').forEach(e => e.classList.remove('spot')); const e = $(sel); if (e) { const d = $('#demo'); document.documentElement.style.setProperty('--demo-h', (d ? d.offsetHeight + 16 : 150) + 'px'); e.classList.add('spot'); e.scrollIntoView({ block: 'start', behavior: 'smooth' }); } }
function fmt(ms) { const s = Math.floor(ms / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function demoPanel() {
  let p = $('#demo'); if (!p) { p = h('section', { id: 'demo', role: 'region', 'aria-label': 'Guided demo', 'data-t': 'demo-panel' }); $('main').prepend(p); } return p;
}
function paintDemo(done) {
  const p = empty(demoPanel()); const s = DEMO[demo.i];
  const elapsed = DEMO.slice(0, demo.i).reduce((a, x) => a + x.ms, 0) + Math.min(s.ms, demo.held);
  p.append(h('div', { class: 'demo-bar', 'aria-hidden': 'true' }, h('i', { id: 'demo-fill', style: `width:${Math.min(100, 100 * elapsed / DEMO_TOTAL)}%` })),
    h('div', { class: 'demo-row' }, h('span', { class: 'badge b-info' }, `Step ${demo.i + 1} of ${DEMO.length}`), h('b', { class: 'demo-title', 'data-t': 'demo-title' }, s.title),
      h('span', { class: 'dots', 'aria-hidden': 'true' }, DEMO.map((x, i) => h('i', { class: i === demo.i ? 'on' : i < demo.i ? 'done' : '' }))), h('span', { class: 'sp' }), h('span', { class: 'note', id: 'demo-clock' }, `${fmt(elapsed)} / ${fmt(DEMO_TOTAL)}`),
      h('button', { class: 'btn', 'data-t': 'demo-prev', onclick: () => gotoDemo(demo.i - 1), disabled: demo.i === 0 }, '← Back'),
      h('button', { class: 'btn', 'data-t': 'demo-pause', onclick: () => { demo.paused = !demo.paused; paintDemo(); } }, demo.paused ? '▶ Resume' : '❚❚ Pause'),
      h('button', { class: 'btn pri', 'data-t': 'demo-next', onclick: () => gotoDemo(demo.i + 1) }, demo.i === DEMO.length - 1 ? 'Finish' : 'Next →'),
      h('button', { class: 'btn', 'data-t': 'demo-exit', onclick: stopDemo }, 'Exit (Esc)')),
    h('p', { class: 'say', 'aria-live': 'polite', 'data-t': 'demo-say' }, s.say));
}
async function gotoDemo(i) {
  if (i >= DEMO.length) return endDemo();
  demo.i = Math.max(0, i); demo.held = 0; const tk = ++demo.token; paintDemo();
  try { await DEMO[demo.i].enter(tk); } catch (e) { /* a failed step must never stall the tour */ }
}
function startDemo() {
  demo.on = true; demo.paused = false; document.body.classList.add('demoing'); clearInterval(demo.timer); gotoDemo(0);
  demo.timer = setInterval(() => { if (!demo.on || demo.paused) return; demo.held += 100;
    if (demo.held >= DEMO[demo.i].ms) { gotoDemo(demo.i + 1); return; }
    const el = $('#demo-fill'); if (el) { const e = DEMO.slice(0, demo.i).reduce((a, x) => a + x.ms, 0) + demo.held; el.style.width = (100 * e / DEMO_TOTAL) + '%'; const c = $('#demo-clock'); if (c) c.textContent = `${fmt(e)} / ${fmt(DEMO_TOTAL)}`; } }, 100);
}
function stopDemo() { demo.on = false; demo.token++; clearInterval(demo.timer); document.querySelectorAll('.spot').forEach(e => e.classList.remove('spot')); const p = $('#demo'); if (p) p.remove(); document.body.classList.remove('demoing'); }
function endDemo() { stopDemo(); const p = demoPanel(); p.append(h('p', { class: 'say', 'data-t': 'demo-done' }, 'That was SENTINEL: claims are checked, permissions are earned and revocable, attacks are blocked, and the counterfactual is computed, not assumed.'), h('div', { class: 'row' }, h('button', { class: 'btn pri', onclick: () => { p.remove(); startDemo(); } }, 'Replay'), h('button', { class: 'btn', onclick: () => p.remove() }, 'Close'))); }
document.addEventListener('keydown', e => { if (!demo.on) return; if (e.key === 'Escape') stopDemo(); else if (e.key === 'ArrowRight') gotoDemo(demo.i + 1); else if (e.key === 'ArrowLeft') gotoDemo(demo.i - 1); else if (e.key === ' ' && !/INPUT|SELECT|BUTTON|TEXTAREA/.test(e.target.tagName)) { e.preventDefault(); demo.paused = !demo.paused; paintDemo(); } });


/* ---------------------------------------------------------------- Judge Bench: raw tool call -> REAL gateway -> layer trace + receipt. Pure; nothing is executed. */
const BN = { info: null, tool: 'create_refund', args: '{\n  "capture_id": "CAP-1001",\n  "currency_code": "USD",\n  "amount": { "value": "25.01", "currency_code": "USD" }\n}', licence: 'veteran', env: 'SANDBOX', res: null, receipt: '', ver: null, err: null, note: '' };
async function benchView(root) {
  if (!BN.info) BN.info = await api('/api/bench/info');
  const re = () => benchView(root);
  const preset = p => { BN.tool = p.tool; BN.args = JSON.stringify(p.args, null, 2); BN.res = null; BN.ver = null; BN.err = null; BN.note = p.label; run(); };
  const run = async () => {
    BN.err = null; BN.ver = null;
    let args; try { args = JSON.parse(BN.args); } catch (e) { BN.err = 'Arguments are not valid JSON (write non-numbers as strings, e.g. "NaN").'; BN.res = null; return re(); }
    try { BN.res = await api('/api/bench/run', { tool: BN.tool, args, licence: BN.licence, environment: BN.env }); BN.receipt = JSON.stringify(BN.res, null, 2); } catch (e) { BN.err = String(e.message || e); BN.res = null; }
    re();
  };
  const verify = async () => {
    let r; try { r = JSON.parse(BN.receipt); } catch (e) { BN.ver = { ok: false, checks: [{ check: 'shape', ok: false, detail: 'receipt text is not valid JSON' }] }; return re(); }
    BN.ver = await api('/api/bench/verify', { receipt: r }); re();
  };
  const tamper = () => { try { const r = JSON.parse(BN.receipt); r.decision.action = r.decision.action === 'ALLOW' ? 'DENY' : 'ALLOW'; BN.receipt = JSON.stringify(r, null, 2); BN.ver = null; } catch (e) { BN.err = 'Run a call first.'; } re(); };
  const forge = async () => { try { const r = JSON.parse(BN.receipt); BN.receipt = JSON.stringify(await api('/api/bench/forge', { receipt: r }), null, 2); BN.ver = null; } catch (e) { BN.err = 'Run a call first.'; } re(); };
  const d = BN.res && BN.res.decision, cls = !d ? '' : d.action === 'DENY' ? 'bad' : d.action === 'ALLOW' ? 'ok' : 'warn';
  const mark = st => st === 'passed' ? '✓' : st === 'stopped' ? '✕' : '–';
  empty(root).append(
    h('div', { class: 'card', 'data-t': 'bench-input' }, h('h3', null, h('span', null, 'Try to fool the gateway'), tagFor('SIMULATED')),
      h('p', { class: 'note' }, 'Type any tool call. The real gateway decides in a fixed order; nothing is executed, sent or paid, and no model is involved. ', h('b', null, 'Even the strongest possible licence cannot override a hard limit.')),
      h('div', { class: 'row', style: 'flex-wrap:wrap;gap:6px;margin:8px 0' }, BN.info.presets.map(p => h('button', { class: 'btn', 'data-t': 'bench-preset-' + p.id, onclick: () => preset(p) }, p.label))),
      h('div', { class: 'row bn-form', style: 'flex-wrap:wrap;gap:10px;margin:8px 0' },
        h('label', null, 'Tool ', h('input', { 'data-t': 'bench-tool', value: BN.tool, list: 'bench-tools', maxlength: 64, oninput: e => { BN.tool = e.target.value; } }), h('datalist', { id: 'bench-tools' }, BN.info.tools.map(t => h('option', { value: t.tool })))),
        h('label', null, 'Licence ', h('select', { 'data-t': 'bench-licence', onchange: e => { BN.licence = e.target.value; } }, Object.entries(BN.info.licences).map(([k, v]) => h('option', { value: k, selected: k === BN.licence }, k + ' · ' + v)))),
        h('label', null, 'Environment ', h('select', { 'data-t': 'bench-env', onchange: e => { BN.env = e.target.value; } }, BN.info.environments.map(k => h('option', { value: k, selected: k === BN.env }, k))))),
      h('textarea', { 'data-t': 'bench-args', rows: 6, spellcheck: 'false', style: 'width:100%;font-family:monospace', oninput: e => { BN.args = e.target.value; } }, BN.args),
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', { class: 'btn pri', 'data-t': 'bench-run', onclick: run }, 'Send to the gateway'), BN.note ? h('span', { class: 'note' }, 'Preset: ' + BN.note) : ''),
      BN.err ? h('p', { class: 'bad', 'data-t': 'bench-error' }, BN.err) : ''),
    d ? h('div', { class: 'card', 'data-t': 'bench-result' }, h('h3', null, h('span', null, 'Decision'), tagFor('SIMULATED')),
      h('div', { class: 'row' }, h('span', { class: 'big ' + cls, 'data-t': 'bench-action' }, d.action), h('span', null, d.reason), h('span', { class: 'sp' }), h('span', { class: 'note' }, 'Executed: no. The bench never executes anything.')),
      h('ol', { class: 'bn-trace', 'data-t': 'bench-trace' }, d.trace.map(t => h('li', { class: 'bn-' + t.status.replace(' ', '-') }, h('b', null, mark(t.status) + ' ' + t.layer), h('span', null, ' ' + t.rule), h('em', null, ' ' + t.status))))) : '',
    d ? h('div', { class: 'card', 'data-t': 'bench-receipt' }, h('h3', null, h('span', null, 'Decision receipt'), tagFor('SIMULATED')),
      h('p', { class: 'note' }, 'A hash of the input, decision and policy. Edit it, then verify: verification recomputes the hash and re-runs the gateway on the stored input. This is tamper-EVIDENT, not tamper-proof: there is no signature, so a verifier must re-run trusted code.'),
      h('textarea', { 'data-t': 'bench-receipt-text', rows: 10, spellcheck: 'false', style: 'width:100%;font-family:monospace', oninput: e => { BN.receipt = e.target.value; BN.ver = null; } }, BN.receipt),
      h('div', { class: 'row', style: 'flex-wrap:wrap;gap:6px;margin-top:8px' }, h('button', { class: 'btn pri', 'data-t': 'bench-verify', onclick: verify }, 'Verify receipt'),
        h('button', { class: 'btn dng', 'data-t': 'bench-tamper', onclick: tamper }, 'Tamper: flip the decision'), h('button', { class: 'btn dng', 'data-t': 'bench-forge', onclick: forge }, 'Forge: flip the decision and recompute the hash (demo)')),
      BN.ver ? h('ul', { class: 'bn-ver', 'data-t': 'bench-verify-result' }, h('li', { class: BN.ver.ok ? 'ok' : 'bad', 'data-t': 'bench-verdict' }, BN.ver.ok ? 'RECEIPT VERIFIED' : 'RECEIPT REJECTED'), BN.ver.checks.map(c => h('li', { class: c.ok ? 'ok' : 'bad' }, (c.ok ? '✓ ' : '✕ ') + c.check + ': ' + c.detail))) : '') : '');
}

const RENDER = { command, audit: auditView, time: timeView, trust: trustView, attack: attackView, agent: agentView, bench: benchView };
const q = new URLSearchParams(location.search);
function publicBanner() {
  document.body.classList.add('public');
  document.body.prepend(h('div', { class: 'pubbar', role: 'note', 'data-t': 'public-banner' },
    h('b', null, 'PUBLIC DEMO'), h('span', null, 'Everything you run here is a SIMULATION with play money. The enforcement shown does not protect any live PayPal transaction. Real LLM calls are disabled on this server; the PayPal evidence is a frozen, identifier-free sandbox summary.')));
}
(async () => { try { const hh = await api('/api/health'); PUBLIC = hh.public === true; } catch (e) { PUBLIC = false; } if (PUBLIC) publicBanner(); })().then(() => go(q.get('v') || 'command')).then(() => { if (q.get('demo') === '1') startDemo(); });
