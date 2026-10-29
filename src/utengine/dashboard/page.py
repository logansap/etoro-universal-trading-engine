"""The dashboard's two pages (inline, no external assets, so the CSP can stay `default-src 'self'`)."""

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Engine dashboard: sign in</title>
<style>
:root{--bg:#0f1419;--card:#1a2129;--fg:#e6edf3;--mut:#8b98a5;--acc:#4f9cf9}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f8;--card:#fff;--fg:#1b2733;--mut:#5b6875;--acc:#1f6feb}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
form{background:var(--card);padding:28px;border-radius:12px;width:min(360px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:18px;margin:0 0 4px}p{color:var(--mut);margin:0 0 18px;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #3a4652;background:transparent;
color:var(--fg);font-size:15px}button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--acc);
color:#fff;font-size:15px;cursor:pointer}.msg{color:#f85149;font-size:13px;min-height:18px;margin-top:10px}
</style></head><body><form method="post" action="/login">
<h1>eToro Universal Engine</h1><p>Read-only dashboard. Nothing on this site can place, change or cancel an order.</p>
<input type="password" name="password" placeholder="Dashboard password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button><div class="msg">{msg}</div></form></body></html>"""

APP = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Engine dashboard</title>
<style>
:root{--bg:#0f1419;--card:#1a2129;--line:#2b3540;--fg:#e6edf3;--mut:#8b98a5;--acc:#4f9cf9;--pos:#3fb950;--neg:#f85149;
--warn:#d29922;--a:#4f9cf9;--b:#d2a8ff}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f8;--card:#fff;--line:#d8dee4;--fg:#1b2733;--mut:#5b6875;
--acc:#1f6feb;--pos:#1a7f37;--neg:#cf222e;--warn:#9a6700;--a:#1f6feb;--b:#8250df}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.mode{padding:7px 16px;font-weight:700;letter-spacing:.06em;text-align:center;font-size:13px;color:#fff}
.mode.SIMULATION{background:#1f6feb}.mode.SHADOW{background:#9a6700}.mode.LIVE{background:#cf222e}
.bar{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:8px 16px;flex-wrap:wrap}
.bar b{font-size:15px}.mut{color:var(--mut)}nav{display:flex;gap:4px;overflow-x:auto;padding:0 12px 8px}
nav a{padding:7px 12px;border-radius:8px;color:var(--mut);text-decoration:none;white-space:nowrap}
nav a.on{background:var(--card);color:var(--fg)}main{padding:16px;max-width:1280px;margin:0 auto}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(min(220px,100%),1fr))}
.grid>*,.two>*,section,main{min-width:0}p,li{overflow-wrap:anywhere}html,body{overflow-x:hidden}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}
.card h3{margin:0 0 6px;font-size:12px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}.row{display:flex;justify-content:space-between;gap:8px}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.badge.VERIFIED,.badge.DONE,.badge.CLEAN,.badge.READ_ONLY{color:var(--pos);border-color:var(--pos)}
.badge.STALE,.badge.UNRECONCILED,.badge.PENDING,.badge.UNVERIFIED,.badge.SHADOW,.badge.SIMULATED{color:var(--warn);border-color:var(--warn)}
.badge.UNAVAILABLE,.badge.BLOCKED,.badge.HALT,.badge.MISMATCH,.badge.REFUSED{color:var(--neg);border-color:var(--neg)}
section{margin-top:18px}section>h2{font-size:15px;margin:0 0 8px}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;text-align:left;
border-bottom:1px solid var(--line);white-space:nowrap;font-size:13px}th{color:var(--mut);font-weight:600}
td.wrap{white-space:normal;min-width:220px}.pos{color:var(--pos)}.neg{color:var(--neg)}
svg{width:100%;height:auto;display:block}.two{display:grid;gap:12px;grid-template-columns:1fr 1fr}
@media (max-width:760px){.two{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.big{font-size:17px}.card{padding:10px}main{padding:10px}.mode{font-size:11px;padding:6px 8px}.bar{padding:6px 10px}.bar b{font-size:14px}#asof{font-size:11px}}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0}
button.lo{background:transparent;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:5px 10px;cursor:pointer}
</style></head><body>
<header><div id="mode" class="mode">...</div>
<div class="bar"><div><b>eToro Universal Engine</b> <span class="mut" id="inst"></span></div>
<div class="row" style="align-items:center"><span class="mut" id="asof"></span>
<form method="post" action="/logout" style="margin:0"><button class="lo">Sign out</button></form></div></div>
<nav id="nav"></nav></header><main id="main"><p class="mut">Loading...</p></main>
<script>
const TABS=[["overview","Overview"],["strategies","Strategies"],["trades","Trades"],["operations","Operations"],["migration","Migration"]];
const $=s=>document.querySelector(s);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd=v=>v==null?'<span class="badge UNAVAILABLE">unavailable</span>':(v<0?"-":"")+"$"+Math.abs(+v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pct=v=>v==null?"n/a":((v*100).toFixed(2)+"%");
const sgn=v=>v==null?"":(v<0?"neg":"pos");
const badge=t=>`<span class="badge ${esc(t)}">${esc(t)}</span>`;
const card=(t,v,sub="")=>`<div class="card"><h3>${t}</h3><div class="big">${v}</div><div class="mut">${sub}</div></div>`;
function table(rows,cols,wrap=[]){if(!rows||!rows.length)return '<p class="mut">None.</p>';
 return '<div class="tw"><table><tr>'+cols.map(c=>`<th>${esc(c)}</th>`).join("")+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c];
 if(v!==null&&typeof v==="object")v=JSON.stringify(v);if(typeof v==="number")v=Math.abs(v)>=100?v.toFixed(2):(+v.toFixed(6));
 return `<td class="${wrap.includes(c)?"wrap":""}">${esc(v)}</td>`}).join("")+'</tr>').join("")+'</table></div>'}
function line(series,key,color,h=160){const pts=series.filter(p=>p[key]!=null);if(pts.length<2)return '<p class="mut">Not enough recorded closes for a chart yet.</p>';
 const ys=pts.map(p=>p[key]),mn=Math.min(...ys),mx=Math.max(...ys),W=600,pad=6,sx=i=>pad+i*(W-2*pad)/(pts.length-1),sy=y=>h-pad-(mx===mn?0.5:(y-mn)/(mx-mn))*(h-2*pad);
 return `<svg viewBox="0 0 ${W} ${h}" role="img" aria-label="${key}"><polyline fill="none" stroke="${color}" stroke-width="2" points="${pts.map((p,i)=>sx(i)+","+sy(p[key])).join(" ")}"/><text x="8" y="14" font-size="11" fill="currentColor" opacity=".6">${esc(key)} ${mn.toFixed(2)} .. ${mx.toFixed(2)}</text></svg>`}
async function J(u){const r=await fetch(u,{credentials:"same-origin"});if(r.status===401){location="/login";throw 0}const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
function head(m){$("#mode").className="mode "+m.mode;$("#mode").textContent=m.mode+" MODE · read-only dashboard · live trading "+m.live_trading.split(" ")[0];$("#inst").textContent=m.instance}
const V={};
V.overview=async()=>{const o=await J("/api/overview");head(o);const a=o.account||{};
 $("#asof").innerHTML=`broker snapshot ${esc(o.snapshot_as_of)} (${o.snapshot_age_s??"?"} s) ${badge(o.data_label)}`;
 const sl=Object.entries(o.sleeves||{});
 return `<div class="grid">${card("Account equity "+badge(o.data_label),usd(a.account_equity),a.reason||("capital reconciliation "+(a.status||"UNAVAILABLE")+" · unattributed "+usd(a.unattributed)))}
 ${card("Available cash",usd(a.available_cash),"reserved by pending opens "+usd(a.reserved_pending_opens))}
 ${card("Invested (margin)",usd(a.invested_amount),"unrealized P&L "+usd(a.unrealized_pnl))}
 ${card("Strategy capital",usd(a.strategy_equity),"sum of sleeves "+usd(a.sum_of_sleeve_equity))}
 ${card("Unclassified (suspense)",usd(a.suspense_unclassified),"not allocated, not spendable")}
 ${card("Foreign positions",usd(a.foreign_positions_value),"outside strategy capital")}</div>
 <section><h2>P&amp;L (excluding deposits)</h2><div class="grid">${card("Day",`<span class="${sgn(o.pnl.daily)}">${usd(o.pnl.daily)}</span>`)}
 ${card("Week",`<span class="${sgn(o.pnl.weekly)}">${usd(o.pnl.weekly)}</span>`)}${card("Month",`<span class="${sgn(o.pnl.monthly)}">${usd(o.pnl.monthly)}</span>`)}
 ${card("Lifetime",`<span class="${sgn(o.pnl.lifetime)}">${usd(o.pnl.lifetime)}</span>`,"TWR "+pct(o.pnl.twr_lifetime)+" · max DD "+pct(o.pnl.max_drawdown))}</div>
 <p class="mut">${esc(o.pnl.basis||o.pnl.note||"")}</p></section>
 <section class="two"><div class="card"><h3>Strategy equity</h3>${line(o.curve,"equity","var(--acc)")}</div><div class="card"><h3>Drawdown</h3>${line(o.curve,"drawdown","var(--neg)")}</div></section>
 <section><h2>Allocation: capital assigned vs invested</h2>${table(sl.map(([s,v])=>({sleeve:s,weight:v.weight,equity:v.equity,cash:v.cash,reserved:v.reserved,invested:v.invested_value,"invested share":v.assigned_vs_invested})),["sleeve","weight","equity","cash","reserved","invested","invested share"])}
 <p class="mut">Unallocated pool ${usd(o.unallocated_pool)} · open exceptions ${o.open_exceptions} · last reconciliation ${esc(o.last_reconciliation.status)} at ${esc(o.last_reconciliation.at)}</p></section>
 <section><h2>Capital flows</h2>${table(Object.entries(o.funding).map(([k,v])=>({kind:k,amount:v})),["kind","amount"])}</section>`};
V.strategies=async()=>{const s=await J("/api/strategies");head(s);let h="";
 for(const [id,x] of Object.entries(s.sleeves)){const d=x.decision||{};const st=x.status||{};
  h+=`<section><h2>${esc(id)} ${badge(s.data_label)}</h2><div class="grid">${card("Equity",usd(x.equity),"cash "+usd(x.cash))}
  ${card("Return (TWR)",x.performance?pct(x.performance.twr_return):"n/a",x.performance?("max DD "+pct(x.performance.max_drawdown)+" · "+x.performance.sessions+" closes"):"no close recorded")}
  ${card("Realized / unrealized",usd(x.realized_unrealized.realized)+" / "+usd(x.realized_unrealized.unrealized),x.realized_unrealized.method)}
  ${card("Status",st.crash_guard_active!==undefined?("crash guard "+(st.crash_guard_active?"ACTIVE":"inactive")):(st.regime||"n/a"),st.crash_guard_active!==undefined?("QQQ 10-day "+pct(st.qqq_10d_return)):esc((st.branch_path||[]).join(" → ")))}</div>
  <div class="two" style="margin-top:12px"><div><h3 class="mut">Holdings</h3>${table(Object.entries(x.holdings).map(([k,v])=>({instrument:k,...v})),["instrument","units","value","weight"])}</div>
  <div><h3 class="mut">Latest decision ${esc(d.session||"")} ${esc(d.status||"")}</h3>${table(Object.entries(d.targets||{}).map(([k,v])=>({target:k,weight:v})),["target","weight"])}
  ${Object.keys(x.cash_from_unavailable_instruments).length?'<p class="mut">Held in cash because the instrument is not executable on this account:</p>'+table(Object.entries(x.cash_from_unavailable_instruments).map(([k,v])=>({instrument:k,weight:v.weight,reason:v.reason})),["instrument","weight","reason"],["reason"]):""}</div></div>
  <details style="margin-top:8px"><summary class="mut">Decision explanation</summary><pre>${esc(JSON.stringify(d.explanation,null,1))}</pre></details></section>`}
 return h+`<section><h2>Position and exposure overlap</h2>${table(Object.entries(s.overlap).map(([k,v])=>({instrument:k,...v})),["instrument",...Object.keys(s.sleeves)])}
 <p class="mut">Combined exposure by instrument: ${esc(JSON.stringify(s.exposure_by_instrument))}</p></section>`};
V.trades=async()=>{const t=await J("/api/trades");head(t);
 return `<div class="grid">${card("Fees & financing",usd(t.fees_total))}${card("Pending / unresolved",t.pending_or_unresolved.length)}${card("Rejected / canceled",t.rejected.length)}${card("Partial fills",t.partial_fills.length)}</div>
 <section><h2>Orders (intents)</h2>${table(t.orders,["created_at","action","symbol","amount","units","status","broker_order_id","strategy_id","sleeves"],["sleeves"])}</section>
 <section><h2>Executions attributed to sleeves</h2>${table(t.fills_by_sleeve,["ts","sleeve","kind","symbol","units","cash","plan_id"])}</section>
 <section><h2>Fees, financing and adjustments</h2>${table(t.fees_and_financing,["ts","kind","amount","source","evidence"],["evidence"])}</section>`};
V.operations=async()=>{const o=await J("/api/operations");head(o);
 return `<div class="grid">${card("Engine heartbeat",o.heartbeat_age_s==null?"none":o.heartbeat_age_s+" s ago",esc(o.heartbeat?.ts||""))}
 ${card("Broker",badge(o.broker.label),esc(o.broker.source||"no snapshot")+" · "+esc(o.broker.connectivity||""))}
 ${card("Reconciliation",badge(o.reconciliation.status||"UNAVAILABLE"),esc(o.reconciliation.at||""))}
 ${card("Market data",badge(o.market_data?.status||"UNAVAILABLE"),esc(o.market_data?.session||""))}
 ${card("Audit chain",badge(o.audit_chain_ok?"VERIFIED":"MISMATCH"),esc(o.audit_chain)+" · schema "+o.schema_version)}
 ${card("Backups",o.backups.length,esc(o.backups.slice(-1)[0]||"none in this state directory"))}</div>
 <section><h2>Actionable exceptions</h2>${table(o.exceptions_open,["first_seen","severity","kind","subject","occurrences","message","action"],["message","action"])}</section>
 <section><h2>Halts</h2>${table(o.halts,["ts","kind","scope","sleeve","reason"],["reason"])}</section>
 <section><h2>Schedule</h2>${table(Object.entries(o.schedule).map(([k,v])=>({sleeve:k,next_decision:v.next_decision?.start,next_execution:v.next_execution?.start})),["sleeve","next_decision","next_execution"])}
 <p class="mut">Last decision: ${esc(JSON.stringify(o.last_decision))}</p></section>
 <section><h2>Alert history</h2>${table(o.alert_history,["ts","severity","kind","message","suppressed","delivered_at"],["message"])}</section>`};
V.migration=async()=>{const m=await J("/api/migration");head(m);const g=m.gold_miner_retirement;const tk=m.takeover;
 return `<section><h2>Gold Miner retirement readiness</h2><p class="mut">${esc(g.two_writer_rule)}</p><p>Two-writer risk now: ${g.two_writer_risk?badge("BLOCKED")+" the engine holds a write capability while Gold Miner is not verified frozen":badge("NONE")+" the engine holds no write capability; Gold Miner remains the only writer on this account"}</p>
 <p class="mut">Broker key: ${m.broker_key?badge(m.broker_key.status)+" "+esc(m.broker_key.at||""):badge("UNAVAILABLE")+" no key check recorded"}</p>
 ${m.pre_takeover?`<div class="card"><b>Before takeover</b>: the shadow sleeves are sized from the account's free cash, which Gold Miner (live) can also use at any time. Positions below are Gold Miner's, never the engine's; nothing here is closed, adopted or traded.</div>`:""}
 ${table(Object.entries(g.steps).map(([k,v])=>({step:k,status:v.status,evidence:v.evidence,reason:v.reason})),["step","status","evidence","reason"],["reason"])}</section>
 <section><h2>Foreign positions and pending orders</h2>${table(m.foreign_positions,["position_id","symbol","owner","is_buy","leverage","units","open_rate","amount","exposure_usd","unrealized_pnl","fees","close_rate","quote_at"])}<p class="mut">amount = margin invested; exposure = broker-reported current exposure; fees = fees to date incl. overnight financing (broker-reported).</p>${table(m.pending_orders,["order_id","instrument_id","amount","kind"])}</section>
 <section><h2>Full-account takeover preview</h2>${tk?`<p>Run <b>${esc(tk.run_id)}</b> ${badge(tk.state)} plan ${esc(tk.plan_hash)}</p>${table(tk.positions,["position_id","symbol","direction","leverage","units","exposure_usd","est_close_proceeds","proceeds_basis","action"])}
 <p class="mut">Capital: ${esc(JSON.stringify(tk.capital))}</p><p class="mut">Allocation: ${esc(JSON.stringify(tk.allocation))}</p>
 <h3 class="mut">Approvals</h3>${table(tk.approvals,["kind","approved_by","created_at","expires_at","status","status_reason"])}
 <h3 class="mut">Risks</h3><ul>${(tk.risks||[]).map(r=>`<li>${esc(r)}</li>`).join("")}</ul>`:'<p class="mut">No takeover preview recorded. Run <code>trading-engine takeover preview</code>.</p>'}</section>
 <section><h2>Required approvals and blockers</h2><div class="two"><div class="card"><h3>Approvals (each separate, bound, expiring)</h3><ul>${m.required_approvals.map(a=>`<li>${esc(a)}</li>`).join("")}</ul></div>
 <div class="card"><h3>Blockers</h3><ul>${m.blockers.map(a=>`<li>${esc(a)}</li>`).join("")}</ul></div></div></section>
 <section><h2>Audit trail</h2>${table(m.audit_trail.map(x=>({seq:x.seq,ts:x.ts,kind:x.kind,payload:x.payload})),["seq","ts","kind","payload"],["payload"])}</section>`};
async function show(){const tab=(location.hash||"#overview").slice(1);const t=TABS.find(x=>x[0]===tab)?tab:"overview";
 $("#nav").innerHTML=TABS.map(([k,l])=>`<a href="#${k}" class="${k===t?"on":""}">${l}</a>`).join("");
 try{$("#main").innerHTML=await V[t]()}catch(e){if(e)$("#main").innerHTML=`<div class="card"><h3>Data unavailable ${badge("UNAVAILABLE")}</h3><p>${esc(e.message||e)}</p><p class="mut">Nothing is estimated while the engine's state cannot be read.</p></div>`}}
addEventListener("hashchange",show);show();setInterval(()=>{if(!document.hidden)show()},30000);
</script></body></html>"""
