"""GET /dashboard — per-tenant usage/billing page. Paste an API key; it calls /v1/usage client-side.

OWNER: Yashraj. Served by the API rather than as a separate app so there is one deployable and one
URL (decision D14). Dependency-free apart from Chart.js off a CDN; nothing is persisted in the
browser, so the pasted key never outlives the tab.

Three panels depend on fields Naresh is still adding to GET /v1/usage (`by_day`, `last_requests`) and
on GET /v1/usage/statement.csv. They degrade to a short "pending" note instead of breaking, and start
working on their own once those ship -- no change needed here.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>LedgerLLM — tenant usage</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 :root{--ink:#222;--muted:#777;--line:#ddd;--warn:#b00;--ok:#1a7f37;--bg:#fafafa}
 body{font-family:system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 16px;color:var(--ink)}
 h1{margin-bottom:4px} h2{margin-top:0;font-size:1.25rem} h3{margin:24px 0 8px;font-size:1rem}
 input{width:420px;padding:8px;font-family:monospace} button{padding:8px 14px;cursor:pointer}
 table{border-collapse:collapse;margin-top:8px;width:100%}
 td,th{border:1px solid var(--line);padding:6px 10px;text-align:left}
 th{background:var(--bg)} td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 .muted{color:var(--muted)} .warn{color:var(--warn)} .ok{color:var(--ok)}
 .banner{border:1px solid var(--warn);background:#fff5f5;color:var(--warn);
         padding:10px 14px;border-radius:6px;margin:16px 0}
 .grid{display:grid;grid-template-columns:260px 1fr;gap:24px;align-items:start;margin-top:16px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-top:16px}
 .card{border:1px solid var(--line);border-radius:6px;padding:10px 12px}
 .card .k{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
 .card .v{font-size:1.3rem;font-variant-numeric:tabular-nums;margin-top:2px}
 .pending{border:1px dashed var(--line);border-radius:6px;padding:12px;color:var(--muted);font-size:.9rem}
 .bar{height:10px;border-radius:5px;background:#eee;overflow:hidden;margin-top:6px}
 .bar>i{display:block;height:100%}
</style></head><body>
<h1>LedgerLLM — tenant usage</h1>
<p class="muted">Paste a tenant API key. Nothing is stored; the page calls
<code>GET /v1/usage</code> with it from your browser.</p>
<input id="key" placeholder="llk_..." autocomplete="off">
<button id="load">Load</button>
<div id="out"></div>

<script>
const $ = (id) => document.getElementById(id);
const usd = (n, dp = 6) => '$' + Number(n).toFixed(dp);
let charts = [];

// Chart.js keeps a registry per canvas; destroy old instances or a reload leaks them and the
// tooltips from the previous render stay alive.
function resetCharts() { charts.forEach(c => c.destroy()); charts = []; }

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function rows(items, cols) {
  if (!items || !items.length) return '<p class="muted">No activity yet this period.</p>';
  const head = cols.map(c => `<th class="${c.num ? 'num' : ''}">${c.label}</th>`).join('');
  const body = items.map(it =>
    '<tr>' + cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.cell(it)}</td>`).join('') + '</tr>'
  ).join('');
  return `<table><tr>${head}</tr>${body}</table>`;
}

async function load() {
  const key = $('key').value.trim();
  const out = $('out');
  resetCharts();
  if (!key) { out.innerHTML = '<p class="warn">Paste an API key first.</p>'; return; }

  let r, j;
  try {
    r = await fetch('/v1/usage', { headers: { 'X-API-Key': key } });
    j = await r.json();
  } catch (e) {
    out.innerHTML = '<p class="warn">Could not reach the API: ' + esc(e.message) + '</p>';
    return;
  }
  if (!r.ok) {
    const err = j.error ? esc(j.error.code) + ': ' + esc(j.error.message) : 'HTTP ' + r.status;
    out.innerHTML = '<p class="warn">' + err + '</p>';
    return;
  }

  // committed = spent + in-flight reservations, which is what the budget check actually compares
  // against the limit (reserve-then-settle, decision D2).
  const committed = j.spent_usd + j.reserved_usd;
  const pct = j.limit_usd ? Math.min(100, 100 * committed / j.limit_usd) : 0;

  out.innerHTML = `
    ${j.warning ? `<div class="banner"><strong>Soft warning.</strong> ${usd(committed)} of
      ${usd(j.limit_usd, 2)} committed this period (past the 80% threshold). Requests still succeed
      until the budget is exhausted, then they return <code>402 budget_exceeded</code>.</div>` : ''}

    <h2>${esc(j.tenant_name)} <span class="muted">· ${esc(j.plan)} plan · ${esc(j.period)}</span></h2>

    <div class="grid">
      <div>
        <canvas id="gauge" width="240" height="240"></canvas>
        <p style="text-align:center;margin:4px 0 0" class="${j.warning ? 'warn' : 'ok'}">
          <strong>${pct.toFixed(1)}%</strong> of budget committed</p>
      </div>
      <div>
        <table>
          <tr><th>Monthly budget</th><td class="num">${usd(j.limit_usd, 2)}</td></tr>
          <tr><th>Spent (settled)</th><td class="num">${usd(j.spent_usd)}</td></tr>
          <tr><th>Reserved (in flight)</th><td class="num">${usd(j.reserved_usd)}</td></tr>
          <tr><th>Remaining</th><td class="num">${usd(j.remaining_usd)}</td></tr>
        </table>
        <div class="bar">
          <i style="width:${pct}%;background:${j.warning ? '#b00' : '#1a7f37'}"></i>
        </div>
        <p class="muted" style="font-size:.85rem">Reserved is budget held for requests still running.
        It returns to zero when each one settles or fails.</p>
      </div>
    </div>

    <div class="cards">
      <div class="card"><div class="k">Requests</div><div class="v">${j.requests}</div></div>
      <div class="card"><div class="k">Tokens in</div><div class="v">${j.input_tokens.toLocaleString()}</div></div>
      <div class="card"><div class="k">Tokens out</div><div class="v">${j.output_tokens.toLocaleString()}</div></div>
      <div class="card"><div class="k">Avg cost/req</div><div class="v">${
        j.requests ? usd(j.spent_usd / j.requests) : '—'}</div></div>
    </div>

    <h3>Spend by day</h3>
    <div id="byday"></div>

    <h3>By model</h3>
    ${rows(j.by_model, [
      { label: 'Model', cell: m => esc(m.model) },
      { label: 'Requests', num: true, cell: m => m.requests },
      { label: 'Cost', num: true, cell: m => usd(m.cost_usd) },
    ])}

    <h3>By purpose</h3>
    <p class="muted" style="font-size:.85rem;margin:0">
      <code>completion</code> is the summary you asked for. <code>guardrail</code> is the safety
      classifier's own tokens, billed to you. <code>judge</code> is our quality sampling and is
      <em>not</em> billed to you.</p>
    ${rows(j.by_purpose, [
      { label: 'Purpose', cell: p => esc(p.purpose) },
      { label: 'Calls', num: true, cell: p => p.calls },
      { label: 'Cost', num: true, cell: p => usd(p.cost_usd) },
    ])}

    <h3>Recent requests</h3>
    <div id="recent"></div>

    <h3>Statement</h3>
    <div id="statement"></div>
  `;

  drawGauge(j);
  drawByDay(j);
  drawRecent(j);
  drawStatement(key);
}

function drawGauge(j) {
  // spent / reserved / remaining as one doughnut: the three numbers the budget check uses.
  const remaining = Math.max(0, j.limit_usd - j.spent_usd - j.reserved_usd);
  charts.push(new Chart($('gauge'), {
    type: 'doughnut',
    data: {
      labels: ['Spent', 'Reserved', 'Remaining'],
      datasets: [{
        data: [j.spent_usd, j.reserved_usd, remaining],
        backgroundColor: ['#b00', '#e3a008', '#e8e8e8'],
        borderWidth: 0,
      }],
    },
    options: {
      cutout: '62%',
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12 } },
        tooltip: { callbacks: { label: c => c.label + ': ' + usd(c.parsed) } },
      },
    },
  }));
}

function drawByDay(j) {
  const el = $('byday');
  // Naresh's by_day series. Absent today -> say so rather than render an empty chart.
  if (!Array.isArray(j.by_day)) {
    el.innerHTML = '<div class="pending">Waiting on <code>by_day</code> in <code>GET /v1/usage</code>' +
      ' (owner: Naresh). This chart renders automatically once the field is present.</div>';
    return;
  }
  if (!j.by_day.length) { el.innerHTML = '<p class="muted">No spend yet this period.</p>'; return; }
  el.innerHTML = '<canvas id="daychart" height="90"></canvas>';
  charts.push(new Chart($('daychart'), {
    type: 'bar',
    data: {
      labels: j.by_day.map(d => d.day),
      datasets: [{ label: 'Cost (USD)', data: j.by_day.map(d => d.cost_usd), backgroundColor: '#4a6fa5' }],
    },
    options: {
      scales: { y: { beginAtZero: true, ticks: { callback: v => usd(v, 4) } } },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => usd(c.parsed.y) } },
      },
    },
  }));
}

function drawRecent(j) {
  const el = $('recent');
  if (!Array.isArray(j.last_requests)) {
    el.innerHTML = '<div class="pending">Waiting on <code>last_requests</code> in' +
      ' <code>GET /v1/usage</code> (owner: Naresh).</div>';
    return;
  }
  el.innerHTML = rows(j.last_requests.slice(0, 10), [
    { label: 'When', cell: q => esc(q.created_at || '') },
    { label: 'Request', cell: q => '<code>' + esc(String(q.request_id || '').slice(0, 12)) + '</code>' },
    { label: 'Status', cell: q => q.status_code === 200
        ? '200' : '<span class="warn">' + esc(q.status_code) + ' ' + esc(q.error_code || '') + '</span>' },
    { label: 'Model', cell: q => esc(q.model || '—') },
    { label: 'Latency', num: true, cell: q => (q.latency_ms ?? '—') + ' ms' },
    { label: 'Cost', num: true, cell: q => q.cost_usd != null ? usd(q.cost_usd) : '—' },
  ]);
}

async function drawStatement(key) {
  const el = $('statement');
  // The CSV route needs the API key as a header, so a plain link cannot fetch it -- download the
  // body as a blob instead. Probe first so a missing route shows a note, not a broken button.
  let probe;
  try {
    probe = await fetch('/v1/usage/statement.csv', { headers: { 'X-API-Key': key } });
  } catch {
    el.innerHTML = '<div class="pending">Statement endpoint unreachable.</div>';
    return;
  }
  if (probe.status === 404) {
    el.innerHTML = '<div class="pending">Waiting on <code>GET /v1/usage/statement.csv</code>' +
      ' (owner: Naresh).</div>';
    return;
  }
  if (!probe.ok) {
    el.innerHTML = '<div class="pending">Statement unavailable (HTTP ' + probe.status + ').</div>';
    return;
  }
  const csv = await probe.text();
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  el.innerHTML = '<a id="dl" download="ledgerllm-statement.csv">Download CSV statement</a>';
  const a = $('dl');
  a.href = url;
  a.textContent = 'Download CSV statement (' + (csv.split('\\n').length - 1) + ' rows)';
}

$('load').addEventListener('click', load);
$('key').addEventListener('keydown', e => { if (e.key === 'Enter') load(); });
</script></body></html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return _PAGE
