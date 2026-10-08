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
:root{color-scheme:light dark;--bg:#f5f6fa;--surface:#fff;--ink:#14161f;--muted:#6b7080;--line:#e6e8ef;--accent:#4f46e5;
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
.nav a,.who .btn,.brand{white-space:nowrap}
.nav{min-width:0;overflow-x:auto;scrollbar-width:none}
@media (max-width:860px){.top-in{gap:14px;padding:0 16px}#who{display:none}.nav a{padding:6px 8px}main{padding:24px 16px 48px}}
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
select{appearance:none;-webkit-appearance:none;padding-right:38px;text-overflow:ellipsis;cursor:pointer;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%238b90a3' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 14px center;background-size:12px}
select option{background:var(--surface);color:var(--ink)}
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
.seg{display:inline-flex;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-weight:600;font-size:.85rem;
padding:6px 12px;border-radius:7px;cursor:pointer}
#style button{flex:1}
.seg button.on{background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgba(16,24,40,.08)}
.play{grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);align-items:stretch}
.stack{display:flex;flex-direction:column;gap:16px;min-width:0}
@media (max-width:960px){.play{grid-template-columns:1fr}}
.field{margin-top:20px} form>.field:first-child,form>.row2:first-child{margin-top:0}
.field label{margin:0 0 6px;display:flex;justify-content:space-between;align-items:baseline}
.field label small{color:var(--muted);font-weight:400}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:20px;align-items:start}
.row2>.field{margin-top:0}
@media (max-width:560px){.row2{grid-template-columns:1fr}.row2>.field+.field{margin-top:20px}.result{min-height:260px}}
.hint{display:block;font-size:.78rem;color:var(--muted);margin-top:6px}
textarea{width:100%;font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;outline:none;resize:vertical;min-height:180px;line-height:1.5}
textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.field select,.field input[type=text],.field input[type=url]{width:100%}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;margin:14px 0 8px;padding:0;border:0;
border-radius:999px;background:var(--line);box-shadow:none;cursor:pointer}
input[type=range]:focus{box-shadow:0 0 0 3px var(--accent-soft)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;
background:var(--accent);border:3px solid var(--surface);box-shadow:0 0 0 1px var(--accent)}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--accent);
border:3px solid var(--surface);box-shadow:0 0 0 1px var(--accent)}
.check{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.88rem;margin:0}
.check input{-webkit-appearance:none;appearance:none;width:18px;height:18px;padding:0;margin:0;flex:none;
border:1.5px solid var(--muted);border-radius:5px;background:var(--surface);cursor:pointer;display:grid;place-items:center}
.check input:checked{background:var(--accent);border-color:var(--accent)}
.check input:checked::after{content:"";width:5px;height:9px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.check input:focus-visible{box-shadow:0 0 0 3px var(--accent-soft)}
.check{cursor:pointer}
.result{flex:1;min-height:420px;display:flex;flex-direction:column}
.placeholder{flex:1;display:grid;place-items:center;text-align:center;color:var(--muted);padding:40px 20px}
.summary{font-size:1rem;line-height:1.65} .summary ul{padding-left:20px;margin:8px 0} .summary li{margin:4px 0}
.summary p{margin:0 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
.chip{background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:.8rem;color:var(--muted)}
.chip b{color:var(--ink);font-weight:600}
.spin{width:28px;height:28px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);
animation:sp .8s linear infinite;margin:0 auto 12px} @keyframes sp{to{transform:rotate(360deg)}}
.history td{padding:9px 12px;font-size:.88rem;cursor:pointer} .history tr:hover td{background:var(--bg)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);
padding:10px 16px;border-radius:10px;font-size:.9rem;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:1}
"""

JS = """
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Cents when the amount is whole cents, else up to micro-USD (the ledger's unit): $0.499601 left of
// $0.50 must not round back to $0.50 after a few sub-cent requests.
const usd = (n, dp) => '$' + Number(n || 0).toLocaleString(undefined,
  {minimumFractionDigits: dp ?? 2, maximumFractionDigits: dp ?? 6});
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
<nav class="nav"><a id="nav-overview" href="/app">Overview</a><a id="nav-playground" href="/app/playground">Playground</a><a id="nav-keys" href="/app/keys">API keys</a>
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
        "Free plan: $0.50 of model spend a month. No card needed.",
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
  $('#count').textContent = `${k.live} live key${k.live === 1 ? '' : 's'}`;
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


@router.get("/app/playground")
def playground_page() -> HTMLResponse:
    content = """
<div class="head"><div><h1>Playground</h1><p class="sub">Run real <code>POST /v1/summarize</code> calls with one of your keys.
Rate limits, budget, guardrails and caching apply, and every call is billed to the key you pick.</p></div></div>
<div class="grid play">
 <form class="card" id="form">
  <div class="row2">
   <div class="field"><label for="key">API key</label><select id="key"></select><small class="hint">Usage is billed to this key</small></div>
   <div class="field"><label for="model">Model</label><select id="model"></select><small class="hint" id="price">&nbsp;</small></div>
  </div>
  <div class="field"><label>Source <span class="seg" id="src"><button type="button" data-v="text" class="on">Text</button><button type="button" data-v="url">URL</button></span></label>
   <textarea id="text" placeholder="Paste an article, a report, meeting notes…"></textarea>
   <input id="url" type="url" placeholder="https://en.wikipedia.org/wiki/Token_bucket" style="display:none;width:100%">
   <div style="margin-top:8px;display:flex;justify-content:space-between;align-items:center"><a href="#" id="sample" style="font-size:.85rem">Use a sample text</a><small class="hint" id="chars" style="margin:0"></small></div></div>
  <div class="row2">
   <div class="field"><label>Style</label><span class="seg" id="style" style="display:flex;width:100%"><button type="button" data-v="bullets" class="on">Bullets</button><button type="button" data-v="paragraph">Paragraph</button><button type="button" data-v="tldr">TL;DR</button></span></div>
   <div class="field"><label for="words">Max words <small id="words-v">150</small></label><input id="words" type="range" min="20" max="600" step="10" value="150"></div>
  </div>
  <div class="field"><label for="instructions">Instructions <small>optional</small></label>
   <input id="instructions" type="text" maxlength="500" placeholder="e.g. focus on pricing changes"></div>
  <div class="field" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding-top:20px;border-top:1px solid var(--line)">
   <label class="check"><input type="checkbox" id="bypass"> Bypass cache</label>
   <button class="btn primary" id="run" type="submit" style="min-width:140px">Summarize</button></div>
 </form>
 <div class="stack">
  <div class="card result" id="result"><div class="placeholder"><div><div style="font-size:2rem">✨</div>
   <p style="margin:6px 0 0">Your summary will appear here.</p><p class="sub" style="font-size:.85rem">Pick a key and a model, add some text or a URL, then press Summarize.</p></div></div></div>
  <div class="card" id="hist-card" style="display:none"><h2>This session</h2>
   <table class="history"><thead><tr><th>When</th><th>Model</th><th>Key</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead><tbody id="hist"></tbody></table></div>
 </div>
</div>"""
    script = r"""
const SAMPLE = `LedgerLLM is a multi-tenant API that exposes a summarization feature behind API keys. Each tenant has a plan
with a requests-per-minute limit and a monthly budget in US dollars. Every request's token cost is booked to a ledger in
integer micro-USD, so a bill can always be reproduced. Before calling the model, the service atomically reserves the
worst-case cost of the request; afterwards it settles the actual cost and releases the rest. A burst of concurrent
requests therefore cannot overspend the budget. Inputs are screened for prompt injection, outputs are moderated, logs
are redacted, and identical requests are served from a per-tenant cache at no charge.`;
let models = {}, keys = {}, history = [];
const pick = (id) => $(id + ' .on').dataset.v;
function seg(id, onChange) {
  $(id).onclick = (e) => { const b = e.target.closest('button'); if (!b) return;
    $(id).querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); onChange && onChange(b.dataset.v); };
}
seg('#src', v => { $('#text').style.display = v === 'text' ? '' : 'none'; $('#url').style.display = v === 'url' ? '' : 'none'; $('#sample').style.visibility = v === 'text' ? 'visible' : 'hidden'; $('#chars').textContent = ''; });
seg('#style');
const fill = () => { const w = $('#words'), pct = 100 * (w.value - w.min) / (w.max - w.min);
  w.style.background = `linear-gradient(to right, var(--accent) ${pct}%, var(--line) ${pct}%)`; $('#words-v').textContent = w.value; };
$('#words').oninput = fill; fill();
$('#text').oninput = () => $('#chars').textContent = $('#text').value.length ? int($('#text').value.length) + ' characters' : '';
$('#sample').onclick = (e) => { e.preventDefault(); $('#text').value = SAMPLE.replace(/\n/g, ' '); $('#text').oninput(); };
$('#model').onchange = () => { const m = models[$('#model').value];
  $('#price').textContent = m ? `${usd(m.input_usd_per_mtok, 2)} in · ${usd(m.output_usd_per_mtok, 2)} out per 1M tokens` : ''; };
function renderSummary(text) {
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.length && lines.every(l => /^[-*•]\s/.test(l))) return '<ul>' + lines.map(l => `<li>${esc(l.replace(/^[-*•]\s+/, ''))}</li>`).join('') + '</ul>';
  return lines.map(l => `<p>${esc(l)}</p>`).join('');
}
const FRIENDLY = {
  budget_exceeded: "This month's budget is used up for this tenant. Calls resume next month or after a plan change.",
  rate_limited: 'Too many requests for this key this minute. Wait a moment and try again.',
  blocked_input: 'The input guardrail flagged this request as a likely prompt injection, so no model was called and nothing was billed.',
  model_not_allowed: "That model isn't available on your plan.",
  fetch_blocked: "That URL points somewhere we don't fetch from (private or internal address).",
  fetch_failed: "We couldn't fetch that URL.",
  upstream_error: 'The model provider failed. Nothing was billed; try again shortly.',
  invalid_api_key: 'That key is revoked. Pick a live key.',
  validation_error: 'Check the inputs.'
};
function show(r) {
  const u = r.usage, b = r.budget, g = r.guardrails || {};
  const verdict = (v) => v && v.blocked ? `<span class="badge bad">${esc(v.category || 'flagged')}</span>` : '<span class="badge ok">pass</span>';
  $('#result').innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
     <h2>Summary</h2><button class="btn sm" id="copy-sum">Copy</button></div>
    ${b.warning ? `<div class="notice warn">Over 80% of this month's budget is committed.</div>` : ''}
    <div class="summary">${renderSummary(r.summary)}</div>
    <div class="chips">
      <span class="chip">model <b>${esc(u.model)}</b></span>
      ${u.fallback_from ? `<span class="chip">fell back from <b>${esc(u.fallback_from)}</b></span>` : ''}
      <span class="chip">cost <b>${usd(u.cost_usd)}</b></span>
      <span class="chip">tokens <b>${int(u.input_tokens)}</b> in · <b>${int(u.output_tokens)}</b> out</span>
      <span class="chip">model latency <b>${int(u.latency_ms)} ms</b></span>
      ${u.cached ? '<span class="chip"><b>served from cache</b> · free</span>' : ''}
      ${r.source.truncated ? '<span class="chip"><b>input truncated</b></span>' : ''}
      ${r.source.strategy && r.source.strategy !== 'full' ? `<span class="chip">strategy <b>${esc(r.source.strategy)}</b></span>` : ''}
      <span class="chip">budget <b>${usd(b.spent_usd)}</b> of ${usd(b.limit_usd, 2)}</span>
      <span class="chip">input guardrail ${verdict(g.input)}</span><span class="chip">output ${verdict(g.output)}</span>
    </div>
    <p class="sub" style="font-size:.78rem;margin-top:12px">request ${esc(r.request_id)} · prompt ${esc(u.prompt_version)} · prices ${esc(u.price_version)}</p>`;
  $('#copy-sum').onclick = async () => { await navigator.clipboard.writeText(r.summary); toast('Summary copied'); };
}
function addHistory(r, keyId) {
  history.unshift({at: new Date(), r, keyId}); history = history.slice(0, 8);
  $('#hist-card').style.display = '';
  $('#hist').innerHTML = history.map((h, i) => `<tr data-i="${i}"><td>${h.at.toLocaleTimeString()}</td><td>${esc(h.r.usage.model)}</td>
    <td>${esc((keys[h.keyId] || {}).name || '')}</td><td class="num">${int(h.r.usage.input_tokens + h.r.usage.output_tokens)}</td>
    <td class="num">${h.r.usage.cached ? 'cached' : usd(h.r.usage.cost_usd)}</td></tr>`).join('');
}
$('#hist').onclick = (e) => { const tr = e.target.closest('tr'); if (tr) show(history[+tr.dataset.i].r); };
$('#form').onsubmit = async (e) => {
  e.preventDefault();
  const src = pick('#src'), body = {key_id: $('#key').value, model: $('#model').value, style: pick('#style'),
    max_words: +$('#words').value, bypass_cache: $('#bypass').checked};
  if ($('#instructions').value.trim()) body.instructions = $('#instructions').value.trim();
  if (src === 'text') { if (!$('#text').value.trim()) return toast('Add some text first'); body.text = $('#text').value; }
  else { if (!$('#url').value.trim()) return toast('Add a URL first'); body.url = $('#url').value.trim(); }
  $('#run').disabled = true; $('#run').textContent = 'Summarizing…';
  $('#result').innerHTML = '<div class="placeholder"><div><div class="spin"></div>Calling the model…</div></div>';
  try {
    const r = await fetch('/app/api/playground/summarize', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (r.status === 401 && (await r.clone().json()).error.code !== 'invalid_api_key') { location.href = '/app/login'; return; }
    const j = await r.json();
    if (!r.ok) {
      const c = (j.error || {}).code || 'error', retry = r.headers.get('Retry-After');
      $('#result').innerHTML = `<div class="placeholder"><div style="max-width:420px"><div style="font-size:2rem">⚠️</div>
        <h2 style="margin-top:6px">${esc(c.replace(/_/g, ' '))}</h2><p class="sub">${esc(FRIENDLY[c] || '')}</p>
        <p class="mono sub" style="font-size:.8rem;margin-top:8px">HTTP ${r.status} · ${esc((j.error || {}).message || '')}${retry ? ' · retry after ' + esc(retry) + ' s' : ''}</p></div></div>`;
      return;
    }
    show(j); addHistory(j, body.key_id);
  } catch (err) { $('#result').innerHTML = `<div class="placeholder"><div><p>${esc(err.message)}</p></div></div>`; }
  finally { $('#run').disabled = false; $('#run').textContent = 'Summarize'; }
};
shell('playground').then(async () => {
  const [m, k] = await Promise.all([api('/models'), api('/keys')]);
  m.models.forEach(x => models[x.id] = x);
  $('#model').innerHTML = m.models.map(x => `<option value="${esc(x.id)}" ${x.id === m.default ? 'selected' : ''}>${esc(x.id)}${x.id === 'mock' ? ' (offline test model)' : ''}</option>`).join('');
  $('#model').onchange();
  const live = k.keys.filter(x => !x.revoked_at);
  live.forEach(x => keys[x.key_id] = x);
  $('#key').innerHTML = live.length ? live.map(x => `<option value="${esc(x.key_id)}">${esc(x.name)} · ${esc(x.key_prefix)}…</option>`).join('')
    : '<option value="">No live keys: create one first</option>';
  $('#run').disabled = !live.length;
}).catch(e => console.error(e));
"""
    return _doc("Playground", _shell("playground", content), script)


@router.get("/app/")
def trailing_slash() -> RedirectResponse:
    return RedirectResponse("/app")
