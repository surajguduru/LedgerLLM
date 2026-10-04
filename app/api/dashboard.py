"""GET /dashboard — per-tenant usage/billing page. Paste an API key; it calls /v1/usage client-side.

OWNER: Yashraj. Base is a minimal working page. Add: cost-by-day chart, by-model breakdown, budget gauge,
recent audit events, CSV statement download. Keep it dependency-free (Chart.js from CDN is fine).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>LedgerLLM — tenant usage</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 16px;color:#222}
 input{width:420px;padding:8px;font-family:monospace} button{padding:8px 14px}
 table{border-collapse:collapse;margin-top:16px} td,th{border:1px solid #ddd;padding:6px 10px;text-align:left}
 .warn{color:#b00} .muted{color:#777}
</style></head><body>
<h1>LedgerLLM — tenant usage</h1>
<p class="muted">Paste a tenant API key. Nothing is stored; the page calls <code>GET /v1/usage</code> with it.</p>
<input id="key" placeholder="llk_..."> <button onclick="load()">Load</button>
<div id="out"></div>
<script>
async function load(){
  const key=document.getElementById('key').value.trim();
  const r=await fetch('/v1/usage',{headers:{'X-API-Key':key}});
  const j=await r.json(); const out=document.getElementById('out');
  if(!r.ok){out.innerHTML='<p class="warn">'+(j.error?j.error.code+': '+j.error.message:r.status)+'</p>';return;}
  const pct=j.limit_usd?Math.round(100*j.spent_usd/j.limit_usd):0;
  let h=`<h2>${j.tenant_name} <span class="muted">(${j.plan}, ${j.period})</span></h2>
  <table><tr><th>Budget</th><td>$${j.limit_usd.toFixed(4)}</td></tr>
  <tr><th>Spent</th><td>$${j.spent_usd.toFixed(6)} (${pct}%) ${j.warning?'<span class=warn>⚠ soft warning</span>':''}</td></tr>
  <tr><th>Reserved (in flight)</th><td>$${j.reserved_usd.toFixed(6)}</td></tr>
  <tr><th>Remaining</th><td>$${j.remaining_usd.toFixed(6)}</td></tr>
  <tr><th>Requests</th><td>${j.requests}</td></tr>
  <tr><th>Tokens in / out</th><td>${j.input_tokens} / ${j.output_tokens}</td></tr></table>`;
  h+='<h3>By model</h3><table><tr><th>Model</th><th>Calls</th><th>Cost</th></tr>'+
     j.by_model.map(m=>`<tr><td>${m.model}</td><td>${m.requests}</td><td>$${m.cost_usd.toFixed(6)}</td></tr>`).join('')+'</table>';
  h+='<h3>By purpose</h3><table><tr><th>Purpose</th><th>Calls</th><th>Cost</th></tr>'+
     j.by_purpose.map(m=>`<tr><td>${m.purpose}</td><td>${m.calls}</td><td>$${m.cost_usd.toFixed(6)}</td></tr>`).join('')+'</table>';
  out.innerHTML=h;
}
</script></body></html>"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return _PAGE
