"""Tenant portal pages: /app/login, /app/signup, /app (overview), /app/keys.

Plain HTML + vanilla JS over the /app/api endpoints, Chart.js from a CDN, no build step. All dynamic
text is inserted with textContent or esc() — never raw HTML from the API.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["portal"], include_in_schema=False)

CHART_JS = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"

CSS = """
:root{--bg:#f5f6fa;--surface:#fff;--ink:#14161f;--muted:#6b7080;--line:#e6e8ef;--accent:#4f46e5;
--accent-ink:#fff;--accent-soft:#eef0ff;--ok:#12805c;--ok-soft:#e7f6ef;--warn:#b54708;--warn-soft:#fff4e5;
--bad:#c4320a;--bad-soft:#fdeceb;--shadow:0 1px 2px rgba(16,24,40,.04),0 4px 16px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0e1017;--surface:#161925;--ink:#e8eaf2;--muted:#9aa0b4;
--line:#262a3a;--accent:#7c74ff;--accent-soft:#232445;--ok:#4cc38a;--ok-soft:#14291f;--warn:#f5a524;
--warn-soft:#2d2112;--bad:#ff6b5b;--bad-soft:#2e1513;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--surface) 88%,transparent);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top-in{max-width:1120px;margin:0 auto;padding:0 24px;height:60px;display:flex;align-items:center;gap:28px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.01em;color:var(--ink)}
.brand:hover{text-decoration:none}
.logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#4f46e5,#06b6d4);
display:grid;place-items:center;color:#fff;font-size:14px;font-weight:800}
.nav{display:flex;gap:4px}
.nav a{color:var(--muted);padding:6px 12px;border-radius:8px;font-weight:500}
.nav a:hover{background:var(--bg);color:var(--ink);text-decoration:none}
.nav a.on{color:var(--ink);background:var(--bg)}
.who{margin-left:auto;display:flex;align-items:center;gap:12px;color:var(--muted);font-size:.9rem}
main{max-width:1120px;margin:0 auto;padding:32px 24px 64px}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:24px}
h1{font-size:1.6rem;letter-spacing:-.02em;margin:0} h2{font-size:1.05rem;margin:0 0 4px}
.sub{color:var(--muted);margin:4px 0 0}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-size:.78rem;
font-weight:600;background:var(--accent-soft);color:var(--accent)}
.badge.ok{background:var(--ok-soft);color:var(--ok)} .badge.bad{background:var(--bad-soft);color:var(--bad)}
.badge.muted{background:var(--bg);color:var(--muted)}
.grid>*{min-width:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:20px}
.grid{display:grid;gap:16px}
.kpis{grid-template-columns:repeat(4,1fr)} @media (max-width:860px){.kpis{grid-template-columns:repeat(2,1fr)}}
.two{grid-template-columns:1.6fr 1fr} @media (max-width:860px){.two{grid-template-columns:1fr}}
.kpi .k{color:var(--muted);font-size:.8rem;font-weight:500}
.kpi .v{font-size:1.65rem;font-weight:700;letter-spacing:-.02em;margin-top:6px;font-variant-numeric:tabular-nums}
.kpi .h{color:var(--muted);font-size:.8rem;margin-top:2px}
.meter{height:10px;border-radius:999px;background:var(--bg);overflow:hidden;display:flex;margin:14px 0 10px}
.meter i{display:block;height:100%}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:.82rem}
.legend b{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px;vertical-align:0}
.section{margin-top:16px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--muted);font-weight:500;font-size:.8rem;padding:10px 12px;border-bottom:1px solid var(--line)}
td{padding:12px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);text-align:center;padding:28px 0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);padding:8px 14px;border-radius:10px;font:inherit;font-weight:600;
font-size:.9rem;cursor:pointer;transition:background .15s,border-color .15s}
.btn:hover{background:var(--bg)} .btn:disabled{opacity:.5;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.07)}
.btn.danger{color:var(--bad)} .btn.sm{padding:5px 10px;font-size:.82rem;border-radius:8px}
input,select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
label{display:block;font-weight:500;font-size:.88rem;margin:14px 0 6px}
.notice{border-radius:12px;padding:12px 16px;margin-bottom:16px;display:flex;gap:10px;align-items:flex-start}
.notice.warn{background:var(--warn-soft);color:var(--warn)} .notice.bad{background:var(--bad-soft);color:var(--bad)}
.notice.ok{background:var(--ok-soft);color:var(--ok)}
.reveal{background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent);
border-radius:12px;padding:16px;margin-bottom:16px}
.reveal .row{display:flex;gap:8px;margin-top:10px}
.reveal input{flex:1;font-family:ui-monospace,Menlo,monospace;font-size:.88rem}
.auth{min-height:100vh;display:grid;place-items:center;padding:24px;
background:radial-gradient(60rem 30rem at 10% -10%,var(--accent-soft),transparent),var(--bg)}
.auth .card{width:100%;max-width:400px;padding:32px}
.auth h1{margin:18px 0 4px} .auth .btn{width:100%;margin-top:20px;padding:11px}
.auth input{width:100%} .auth .alt{margin-top:18px;text-align:center;color:var(--muted);font-size:.9rem}
.err{color:var(--bad);font-size:.88rem;margin-top:12px;min-height:1.2em}
.chart{position:relative;height:280px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);
padding:10px 16px;border-radius:10px;font-size:.9rem;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:1}
"""

JS = """
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd = (n, dp) => {
  n = Number(n || 0); dp = dp ?? (n !== 0 && Math.abs(n) < 0.01 ? 6 : 2);
  return '$' + n.toLocaleString(undefined, {minimumFractionDigits: dp, maximumFractionDigits: dp});
};
const int = (n) => Number(n || 0).toLocaleString();
const ago = (iso) => {
  if (!iso) return 'never';
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return 'just now'; if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago'; return Math.floor(s / 86400) + ' d ago';
};
const day = (iso) => new Date(iso).toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric'});
async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', credentials: 'same-origin', headers: {}};
  if (init.method !== 'GET') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body || {}); }
  const r = await fetch('/app/api' + path, init);
  if (r.status === 401 && !opts.noRedirect) { location.href = '/app/login'; throw new Error('signed out'); }
  const j = r.status === 204 ? {} : await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || ('HTTP ' + r.status));
  return j;
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); setTimeout(() => t.classList.remove('on'), 1800); }
async function shell(active) {
  const me = await api('/me');
  $('#who').textContent = me.user.email;
  $('#nav-' + active).classList.add('on');
  $('#logout').onclick = async () => { await api('/logout', {method: 'POST'}); location.href = '/app/login'; };
  return me;
}
const PALETTE = ['#4f46e5', '#06b6d4', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#ec4899', '#64748b'];
"""


def _doc(title: str, body: str, script: str, charts: bool = False) -> HTMLResponse:
    chart_tag = f'<script src="{CHART_JS}"></script>' if charts else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect width=%2732%27 height=%2732%27 rx=%278%27 fill=%27%234f46e5%27/%3E%3Ctext x=%2716%27 y=%2722%27 font-size=%2716%27 font-family=%27Arial%27 font-weight=%27bold%27 fill=%27white%27 text-anchor=%27middle%27%3EL%3C/text%3E%3C/svg%3E"><title>{title} · LedgerLLM</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
{chart_tag}<style>{CSS}</style></head><body>{body}<div class="toast" id="toast"></div>
<script>{JS}{script}</script></body></html>"""
    )


def _shell(active: str, content: str) -> str:
    return f"""<header class="top"><div class="top-in">
<a class="brand" href="/app"><span class="logo">L</span>LedgerLLM</a>
<nav class="nav"><a id="nav-overview" href="/app">Overview</a><a id="nav-keys" href="/app/keys">API keys</a>
<a href="/docs" target="_blank" rel="noopener">API docs ↗</a></nav>
<div class="who"><span id="who"></span><button class="btn sm" id="logout">Sign out</button></div>
</div></header><main>{content}</main>"""


def _auth(title: str, sub: str, fields: str, button: str, alt: str, script: str) -> HTMLResponse:
    body = f"""<div class="auth"><form class="card" id="form" novalidate>
<a class="brand" href="/app"><span class="logo">L</span>LedgerLLM</a>
<h1>{title}</h1><p class="sub">{sub}</p>{fields}
<button class="btn primary" id="submit" type="submit">{button}</button>
<div class="err" id="err" role="alert"></div><div class="alt">{alt}</div></form></div>"""
    return _doc(title, body, script)


_AUTH_SUBMIT = """
$('#form').onsubmit = async (e) => {
  e.preventDefault(); $('#err').textContent = ''; $('#submit').disabled = true;
  try { await submit(); } catch (err) { $('#err').textContent = err.message; }
  finally { $('#submit').disabled = false; }
};
"""


@router.get("/app/login")
def login_page() -> HTMLResponse:
    return _auth(
        "Welcome back",
        "Sign in to manage your API keys and usage.",
        """<label for="email">Work e-mail</label><input id="email" type="email" autocomplete="email" required>
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password" required>""",
        "Sign in",
        'New to LedgerLLM? <a href="/app/signup">Create an account</a>',
        _AUTH_SUBMIT
        + """
async function submit() {
  await api('/login', {method: 'POST', noRedirect: true,
    body: {email: $('#email').value, password: $('#password').value}});
  location.href = '/app';
}""",
    )


@router.get("/app/signup")
def signup_page() -> HTMLResponse:
    return _auth(
        "Create your account",
        "Free plan: $0.50 of model spend a month, 2 API keys. No card needed.",
        """<label for="company">Company <span class="sub">(optional)</span></label><input id="company" autocomplete="organization">
<label for="email">Work e-mail</label><input id="email" type="email" autocomplete="email" required>
<label for="password">Password</label><input id="password" type="password" autocomplete="new-password" minlength="10" required>
<p class="sub" style="font-size:.8rem">At least 10 characters.</p>""",
        "Create account",
        'Already have an account? <a href="/app/login">Sign in</a>',
        _AUTH_SUBMIT
        + """
async function submit() {
  const j = await api('/signup', {method: 'POST', noRedirect: true, body: {
    email: $('#email').value, password: $('#password').value, company: $('#company').value}});
  sessionStorage.setItem('ledger_new_key', j.api_key);
  location.href = '/app/keys';
}""",
    )


@router.get("/app")
def overview_page() -> HTMLResponse:
    content = """
<div class="head"><div><h1 id="title">Overview</h1><p class="sub" id="subtitle">&nbsp;</p></div>
<div style="display:flex;gap:8px;align-items:center"><select id="period" aria-label="Month"></select>
<a class="btn" id="csv" href="#">Download statement</a></div></div>
<div id="banner"></div>
<div class="grid kpis">
 <div class="card kpi"><div class="k">Spent this month</div><div class="v" id="k-spent">–</div><div class="h" id="k-limit"></div></div>
 <div class="card kpi"><div class="k">Remaining budget</div><div class="v" id="k-left">–</div><div class="h" id="k-pct"></div></div>
 <div class="card kpi"><div class="k">Requests</div><div class="v" id="k-req">–</div><div class="h" id="k-tok"></div></div>
 <div class="card kpi"><div class="k">Avg cost / request</div><div class="v" id="k-avg">–</div><div class="h">cache hits are free</div></div>
</div>
<div class="card section"><h2>Monthly budget</h2><p class="sub" id="b-text"></p>
 <div class="meter"><i id="m-spent" style="background:var(--accent)"></i><i id="m-res" style="background:#f59e0b"></i></div>
 <div class="legend"><span><b style="background:var(--accent)"></b>Spent</span><span><b style="background:#f59e0b"></b>Reserved (in flight)</span><span><b style="background:var(--line)"></b>Remaining</span></div></div>
<div class="grid two section">
 <div class="card"><h2>Daily spend</h2><p class="sub">Stacked by API key</p><div class="chart"><canvas id="daily"></canvas></div></div>
 <div class="card"><h2>Spend by key</h2><p class="sub">This month</p><div class="chart"><canvas id="bykey"></canvas></div></div>
</div>
<div class="card section"><h2>Usage by API key</h2>
 <table><thead><tr><th>Key</th><th class="num">Requests</th><th class="num">Tokens in / out</th><th class="num">Spend</th><th class="num">Share</th></tr></thead>
 <tbody id="keys"></tbody></table></div>
<div class="grid two section">
 <div class="card"><h2>Recent calls</h2><table><thead><tr><th>When</th><th>Model</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead><tbody id="recent"></tbody></table></div>
 <div class="card"><h2>By model</h2><table><thead><tr><th>Model</th><th class="num">Calls</th><th class="num">Spend</th></tr></thead><tbody id="models"></tbody></table></div>
</div>"""
    script = """
let charts = [];
async function load(period) {
  const q = period ? '?period=' + period : '';
  const u = await api('/usage' + q), s = u.summary;
  charts.forEach(c => c.destroy()); charts = [];
  if (!$('#period').options.length) {
    $('#period').innerHTML = u.periods.map(p => `<option value="${p}">${new Date(p + '-01T00:00:00').toLocaleDateString(undefined, {month: 'long', year: 'numeric'})}</option>`).join('');
    $('#period').onchange = () => load($('#period').value);
  }
  $('#csv').href = '/app/api/statement.csv?period=' + s.period;
  $('#title').textContent = s.tenant_name;
  $('#subtitle').innerHTML = `<span class="badge">${esc(s.plan[0].toUpperCase() + s.plan.slice(1))} plan</span>`;
  const committed = s.spent_usd + s.reserved_usd, pct = s.limit_usd ? 100 * committed / s.limit_usd : 0;
  $('#banner').innerHTML = pct >= 100 ? `<div class="notice bad"><strong>Budget exhausted.</strong>&nbsp;New requests return 402 until next month or a plan change.</div>`
    : s.warning ? `<div class="notice warn"><strong>Heads-up:</strong>&nbsp;${pct.toFixed(0)}% of this month's budget is committed. Requests keep working until it is used up.</div>` : '';
  $('#k-spent').textContent = usd(s.spent_usd); $('#k-limit').textContent = 'of ' + usd(s.limit_usd, 2) + ' budget';
  $('#k-left').textContent = usd(s.remaining_usd); $('#k-pct').textContent = pct.toFixed(1) + '% committed';
  $('#k-req').textContent = int(s.requests); $('#k-tok').textContent = int(s.input_tokens) + ' in · ' + int(s.output_tokens) + ' out tokens';
  $('#k-avg').textContent = s.requests ? usd(s.spent_usd / s.requests) : '–';
  $('#b-text').textContent = `${usd(s.spent_usd)} spent and ${usd(s.reserved_usd)} reserved of ${usd(s.limit_usd, 2)}.`;
  $('#m-spent').style.width = Math.min(100, 100 * s.spent_usd / (s.limit_usd || 1)) + '%';
  $('#m-res').style.width = Math.min(100, 100 * s.reserved_usd / (s.limit_usd || 1)) + '%';

  const keyName = {}; u.by_key.forEach((k, i) => keyName[k.key_id] = k.name + ' · ' + k.key_prefix.slice(-8));
  const used = u.by_key.filter(k => k.requests > 0);
  const days = [...new Set(u.daily_by_key.map(d => d.date))].sort();
  const muted = getComputedStyle(document.body).getPropertyValue('--muted'), grid = getComputedStyle(document.body).getPropertyValue('--line');
  Chart.defaults.color = muted; Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
  charts.push(new Chart($('#daily'), {type: 'bar', data: {labels: days.map(d => new Date(d + 'T00:00:00').toLocaleDateString(undefined, {day: 'numeric', month: 'short'})),
    datasets: used.map((k, i) => ({label: keyName[k.key_id], backgroundColor: PALETTE[i % PALETTE.length], borderRadius: 4, maxBarThickness: 56,
      data: days.map(d => (u.daily_by_key.find(x => x.date === d && x.key_id === k.key_id) || {}).cost_usd || 0)}))},
    options: {maintainAspectRatio: false, plugins: {legend: {position: 'bottom', labels: {boxWidth: 10}}, tooltip: {callbacks: {label: c => c.dataset.label + ': ' + usd(c.raw)}}},
      scales: {x: {stacked: true, grid: {display: false}}, y: {stacked: true, grid: {color: grid}, ticks: {callback: v => usd(v)}}}}}));
  charts.push(new Chart($('#bykey'), {type: 'doughnut', data: {labels: used.map(k => keyName[k.key_id]),
    datasets: [{data: used.map(k => k.cost_usd), backgroundColor: used.map((_, i) => PALETTE[i % PALETTE.length]), borderWidth: 0}]},
    options: {maintainAspectRatio: false, cutout: '68%', plugins: {legend: {position: 'bottom', labels: {boxWidth: 10}}, tooltip: {callbacks: {label: c => c.label + ': ' + usd(c.raw)}}}}}));

  $('#keys').innerHTML = u.by_key.length ? u.by_key.map((k, i) => `<tr>
    <td><div style="display:flex;align-items:center;gap:10px"><b style="width:9px;height:9px;border-radius:3px;background:${k.requests ? PALETTE[used.indexOf(k) % PALETTE.length] : 'var(--line)'}"></b>
    <div><div style="font-weight:600">${esc(k.name)} ${k.revoked_at ? '<span class="badge muted">revoked</span>' : ''}</div><div class="mono sub">${esc(k.key_prefix)}…</div></div></div></td>
    <td class="num">${int(k.requests)}</td><td class="num">${int(k.input_tokens)} / ${int(k.output_tokens)}</td>
    <td class="num">${usd(k.cost_usd)}</td><td class="num">${s.spent_usd ? (100 * k.cost_usd / s.spent_usd).toFixed(1) + '%' : '–'}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No keys yet.</td></tr>';
  $('#recent').innerHTML = s.last_requests.length ? s.last_requests.slice(0, 8).map(r => `<tr><td>${ago(r.created_at)}</td>
    <td>${esc(r.model)} ${r.billed ? '' : '<span class="badge muted">not billed</span>'}${r.status === 'cached' ? '<span class="badge ok">cache</span>' : ''}</td>
    <td class="num">${int(r.input_tokens + r.output_tokens)}</td><td class="num" style="${r.billed ? '' : 'color:var(--muted);text-decoration:line-through'}">${usd(r.cost_usd)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">No calls yet. Create a key and send your first request.</td></tr>';
  $('#models').innerHTML = s.by_model.length ? s.by_model.map(m => `<tr><td>${esc(m.model)}</td><td class="num">${int(m.requests)}</td><td class="num">${usd(m.cost_usd)}</td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">No activity this month.</td></tr>';
}
shell('overview').then(() => load()).catch(e => console.error(e));
"""
    return _doc("Overview", _shell("overview", content), script, charts=True)


@router.get("/app/keys")
def keys_page() -> HTMLResponse:
    content = """
<div class="head"><div><h1>API keys</h1><p class="sub">Keys authenticate your requests to <code>POST /v1/summarize</code>. Usage is for the current month.</p></div>
<span class="badge" id="count"></span></div>
<div id="reveal"></div>
<div class="card"><form id="create" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
 <input id="name" placeholder="Key name, e.g. production or ci-pipeline" maxlength="100" style="flex:1;min-width:240px">
 <button class="btn primary" id="create-btn" type="submit">Create key</button></form>
 <div class="err" id="err"></div></div>
<div class="card section"><table><thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th>
 <th class="num">Requests</th><th class="num">Spend</th><th>Status</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="card section"><h2>Quick start</h2><p class="sub">Send a request with any live key:</p>
<pre class="mono" style="background:var(--bg);padding:14px;border-radius:10px;overflow:auto;margin:10px 0 0">curl -s <span id="host"></span>/v1/summarize \\
  -H "X-API-Key: $LEDGERLLM_KEY" -H "Content-Type: application/json" \\
  -d '{"url": "https://en.wikipedia.org/wiki/Token_bucket", "style": "tldr"}'</pre></div>"""
    script = """
$('#host').textContent = location.origin;
function reveal(raw, label) {
  $('#reveal').innerHTML = `<div class="reveal"><strong>${esc(label)}</strong> Copy it now; for your security it will not be shown again.
    <div class="row"><input id="raw" readonly value="${esc(raw)}"><button class="btn primary" id="copy">Copy</button></div></div>`;
  $('#copy').onclick = async () => { await navigator.clipboard.writeText(raw); toast('Key copied'); };
  $('#raw').onclick = (e) => e.target.select();
}
async function load() {
  const k = await api('/keys');
  $('#count').textContent = `${k.live} / ${k.limit} live keys`;
  const full = k.live >= k.limit;
  $('#create-btn').disabled = full;
  $('#err').textContent = full ? 'You have reached your plan\\'s key limit. Revoke a key to create a new one.' : '';
  const keys = [...k.keys].sort((a, b) => (!!a.revoked_at - !!b.revoked_at) || (b.created_at > a.created_at ? 1 : -1));
  $('#rows').innerHTML = keys.length ? keys.map(x => `<tr style="${x.revoked_at ? 'opacity:.55' : ''}">
    <td style="font-weight:600">${esc(x.name)}</td><td class="mono">${esc(x.key_prefix)}…</td>
    <td>${day(x.created_at)}</td><td>${ago(x.last_used_at)}</td>
    <td class="num">${int(x.requests)}</td><td class="num">${usd(x.cost_usd)}</td>
    <td>${x.revoked_at ? '<span class="badge muted">Revoked</span>' : '<span class="badge ok">Active</span>'}</td>
    <td class="num" style="white-space:nowrap">${x.revoked_at ? '' : `<button class="btn sm" data-rotate="${x.key_id}" data-name="${esc(x.name)}">Rotate</button>
      <button class="btn sm danger" data-revoke="${x.key_id}" data-name="${esc(x.name)}">Revoke</button>`}</td></tr>`).join('')
    : '<tr><td colspan="8" class="empty">No keys yet.</td></tr>';
}
$('#rows').onclick = async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.revoke && confirm(`Revoke "${b.dataset.name}"? Requests using it will start failing immediately.`)) {
      await api(`/keys/${b.dataset.revoke}/revoke`, {method: 'POST'}); toast('Key revoked');
    } else if (b.dataset.rotate && confirm(`Rotate "${b.dataset.name}"? The current key stops working and a new one is issued.`)) {
      const j = await api(`/keys/${b.dataset.rotate}/rotate`, {method: 'POST'}); reveal(j.api_key, 'Your rotated key.');
    }
  } catch (err) { $('#err').textContent = err.message; }
  load();
};
$('#create').onsubmit = async (e) => {
  e.preventDefault(); $('#err').textContent = '';
  try {
    const j = await api('/keys', {method: 'POST', body: {name: $('#name').value.trim() || 'default'}});
    $('#name').value = ''; reveal(j.api_key, 'Your new key is ready.');
  } catch (err) { $('#err').textContent = err.message; }
  load();
};
shell('keys').then(() => {
  const fresh = sessionStorage.getItem('ledger_new_key');
  if (fresh) { sessionStorage.removeItem('ledger_new_key'); reveal(fresh, 'Welcome! Here is your first API key.'); }
  load();
}).catch(e => console.error(e));
"""
    return _doc("API keys", _shell("keys", content), script)


@router.get("/app/")
def trailing_slash() -> RedirectResponse:
    return RedirectResponse("/app")
