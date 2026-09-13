"use strict";

// ---------- state & settings ----------
const S = { live: null, full: null, history: null, lastAlertId: 0, seenAlerts: new Set(), tab: "live" };
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
const settings = { minNet: store.get("minNet", 0), onlyTradeable: store.get("onlyTradeable", true), sound: store.get("sound", false) };

// ---------- formatting ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtS = (x) => x == null ? "–" : x < 1 ? `${Math.round(x * 1000)} ms` : x < 120 ? `${x.toFixed(1)} s` : x < 7200 ? `${(x / 60).toFixed(1)} m` : `${(x / 3600).toFixed(1)} h`;
const money = (x) => x == null ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(Math.abs(x) > 0 && Math.abs(x) < 0.01 ? 4 : 2)}`;
const cents = (x) => x == null ? "–" : `${(x * 100).toFixed(x * 100 % 1 ? 1 : 0)}¢`;
const pct = (x) => x == null ? "–" : `${Math.round(x * 100)}%`;
const num = (x, d = 0) => x == null ? "–" : Number(x).toLocaleString(undefined, { maximumFractionDigits: d });
const clock = (t) => t == null ? "–" : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayClock = (t) => t == null ? "–" : new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const signClass = (x) => x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "";
const KIND_LABEL = { ladder: "ladder", set_short: "short basket", set_long: "long basket", yes_no_cross: "YES/NO cross" };
const legsText = (legs) => (legs || []).map(([side, t]) => `${side} ${t}`).join(" + ");

// ---------- tabs ----------
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  S.tab = b.dataset.tab;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll("main > section").forEach((s) => { s.hidden = s.id !== `tab-${S.tab}`; });
  renderFull(); renderHistory();
});

// ---------- controls ----------
$("minNet").value = settings.minNet;
$("onlyTradeable").checked = settings.onlyTradeable;
$("minNet").addEventListener("change", (e) => { settings.minNet = Number(e.target.value) || 0; store.set("minNet", settings.minNet); });
$("onlyTradeable").addEventListener("change", (e) => { settings.onlyTradeable = e.target.checked; store.set("onlyTradeable", settings.onlyTradeable); });
const soundBtn = $("soundBtn");
const paintSound = () => { soundBtn.textContent = settings.sound ? "Sound on" : "Sound off"; soundBtn.classList.toggle("on", settings.sound); };
soundBtn.addEventListener("click", () => { settings.sound = !settings.sound; store.set("sound", settings.sound); paintSound(); if (settings.sound) beep(); });
paintSound();
const notifyBtn = $("notifyBtn");
const paintNotify = () => {
  if (!("Notification" in window)) { notifyBtn.textContent = "Notifications unsupported"; notifyBtn.disabled = true; return; }
  const on = Notification.permission === "granted";
  notifyBtn.textContent = on ? "Notifications on" : Notification.permission === "denied" ? "Notifications blocked" : "Enable notifications";
  notifyBtn.classList.toggle("on", on);
};
notifyBtn.addEventListener("click", async () => { if ("Notification" in window) { await Notification.requestPermission(); paintNotify(); } });
paintNotify();

let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 880; g.gain.setValueAtTime(0.08, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.25);
    o.connect(g).connect(audioCtx.destination); o.start(); o.stop(audioCtx.currentTime + 0.26);
  } catch { /* audio unavailable */ }
}

function shouldNotify(a) {
  return a.live && (a.best_net ?? 0) >= settings.minNet && (!settings.onlyTradeable || a.passes_filter);
}

// ---------- header & KPIs ----------
function renderHeader(h) {
  const st = $("status");
  const cls = h.status === "live" ? "live" : h.status === "catching up" ? "catching" : h.status;
  st.className = `status ${cls}`; st.textContent = h.status;
  $("meta").innerHTML = [
    `last poll <b>${h.last_poll_age == null ? "–" : fmtS(h.last_poll_age)}</b> ago`,
    `<b>${h.batches_per_s ? h.batches_per_s.toFixed(1) : "–"}</b> batches/s`,
    `RTT <b>${fmtS(h.rtt_p50)}</b>`,
    `failed <b>${num(h.failed)}</b> / ${num(h.batches)}`,
    `<b>${h.markets}</b> markets · ${h.events} events`,
    `since <b>${dayClock(h.t_first)}</b>`,
  ].map((x) => `<span>${x}</span>`).join("");
}

function kpiTile(label, value, sub) {
  return `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub || ""}</div></div>`;
}

function renderKpis(k) {
  $("kpis").innerHTML = [
    kpiTile("Open now", num(k.active), "violations in the latest poll"),
    kpiTile("Episodes", num(k.episodes), "before fees, this recording"),
    kpiTile("Positive after fees", num(k.positive), "at the best size the book allowed"),
    kpiTile("Tradeable", num(k.tradeable), "positive and passing the liquidity filter"),
    kpiTile("Median lifetime", k.median_lo == null ? "–" : `${fmtS(k.median_lo)}–${fmtS(k.median_hi)}`, "bounds from the poll interval"),
    kpiTile("Catch probability", pct(k.catch_km), `vs reaction time ~${fmtS(k.budget?.typical)}`),
    kpiTile("Replay P&L", `<span class="${signClass(k.replay_pnl)}">${money(k.replay_pnl)}</span>`, `${num(k.replay_signals)} simulated signals`),
  ].join("");
}

// ---------- open violations ----------
function filterBadge(ok) { return ok ? `<span class="badge good">tradeable</span>` : `<span class="badge warn">thin</span>`; }

function renderOpen(active) {
  if (!active.length) { $("openTable").innerHTML = `<div class="empty">No violation is visible in the latest poll.</div>`; return; }
  $("openTable").innerHTML = `<table><thead><tr>
    <th>market</th><th>kind</th><th class="num">edge</th><th class="num">net after fees</th><th class="num">size</th><th class="num">spread</th><th class="num">open for</th><th></th>
  </tr></thead><tbody>${active.map((a, i) => `<tr class="clickable" data-open="${i}">
    <td><div>${esc(a.title || a.event_ticker)}</div><div class="legs">${esc(legsText(a.legs))}</div></td>
    <td><span class="badge kind">${KIND_LABEL[a.kind] || a.kind}</span></td>
    <td class="num">${cents(a.gross_top)}</td>
    <td class="num ${signClass(a.best_net)}">${money(a.best_net)}${a.best_q ? `<div class="legs">${num(a.best_q, 2)} baskets</div>` : ""}</td>
    <td class="num">${num(a.top_size, 2)}</td>
    <td class="num">${cents(a.max_leg_spread)}</td>
    <td class="num">${a.left_censored ? "≥ " : ""}${fmtS(a.age)}</td>
    <td>${filterBadge(a.passes_filter)}</td>
  </tr>`).join("")}</tbody></table>`;
  $("openTable").querySelectorAll("tr[data-open]").forEach((tr) => tr.addEventListener("click", () => openBooks(active[Number(tr.dataset.open)])));
}

// ---------- alerts ----------
function replayVerdict(r) {
  if (!r) return `<span class="badge info">replay pending</span>`;
  const cls = r.outcome === "full" ? "good" : r.outcome === "miss" ? "kind" : "bad";
  return `<span class="badge ${cls}">${r.outcome}${r.book_unchanged ? " · certain" : ""}</span> <span class="${signClass(Number(r.pnl))}">${money(Number(r.pnl))}</span>`;
}

function alertHtml(a, fresh) {
  return `<div class="alert${fresh ? " fresh" : ""}" data-alert="${a.id}">
    <div class="row1"><span class="title">${esc(a.title || a.event_ticker)}</span><span class="badge kind">${KIND_LABEL[a.kind] || a.kind}</span>${filterBadge(a.passes_filter)}${a.live ? "" : `<span class="badge kind">from backfill</span>`}<span class="time">${dayClock(a.t)}</span></div>
    <div class="row2"><span>edge <b>${cents(a.gross_top)}</b></span><span>net <b class="${signClass(a.best_net)}">${money(a.best_net)}</b></span><span>size ${num(a.top_size, 2)}</span><span>spread ${cents(a.max_leg_spread)}</span><span>${replayVerdict(a.replay)}</span></div>
    <div class="legs">${esc(legsText(a.legs))}</div>
  </div>`;
}

function renderAlertsList() {
  const alerts = S.full?.alerts || [];
  $("alerts").innerHTML = alerts.length ? alerts.map((a) => alertHtml(a, false)).join("")
    : `<div class="empty">No violation has been positive after fees yet. Alerts appear here the moment one is.</div>`;
}

function onNewAlerts(alerts) {
  for (const a of alerts) {
    if (S.seenAlerts.has(a.id)) continue;
    S.seenAlerts.add(a.id);
    S.lastAlertId = Math.max(S.lastAlertId, a.id);
    if (!S.full) continue;
    S.full.alerts = [a, ...(S.full.alerts || [])];
    const list = $("alerts");
    if (list.querySelector(".empty")) list.innerHTML = "";
    list.insertAdjacentHTML("afterbegin", alertHtml(a, true));
    if (shouldNotify(a)) {
      if (settings.sound) beep();
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(`${KIND_LABEL[a.kind] || a.kind}: ${a.title || a.event_ticker}`,
          { body: `edge ${cents(a.gross_top)} · net ${money(a.best_net)} · size ${num(a.top_size, 2)}`, tag: `alert-${a.id}` });
      }
    }
  }
}

// ---------- books modal ----------
async function openBooks(item) {
  const tickers = [...new Set((item.legs || []).map(([, t]) => t))];
  const books = await Promise.all(tickers.map((t) => fetch(`/api/book?ticker=${encodeURIComponent(t)}`).then((r) => r.ok ? r.json() : null)));
  const bookHtml = (b) => {
    if (!b) return "";
    const asksFrom = (bids) => bids.map(([p, s]) => [1 - p, s]);
    const rows = (levels) => levels.length ? levels.map(([p, s]) => `<tr><td class="num">${cents(p)}</td><td class="num">${num(s, 2)}</td></tr>`).join("") : `<tr><td colspan="2" class="empty">empty</td></tr>`;
    return `<div class="book"><h4>${esc(b.ticker)}</h4><table>
      <thead><tr><th>YES ask</th><th class="num">size</th></tr></thead><tbody>${rows(asksFrom(b.no_bids))}</tbody>
      <thead><tr><th>YES bid</th><th class="num">size</th></tr></thead><tbody>${rows(b.yes_bids)}</tbody></table></div>`;
  };
  $("modalRoot").innerHTML = `<div class="modal-backdrop" id="backdrop"><div class="modal">
    <header><h3>${esc(item.title || item.event_ticker)}</h3><span class="badge kind">${KIND_LABEL[item.kind] || item.kind}</span><div class="spacer"></div><button class="btn" id="closeModal">Close</button></header>
    <div class="content"><div class="legs">${esc(legsText(item.legs))}</div>
      <p class="note">Latest recorded books, best level first. A YES ask at p is a NO bid at 1 − p. Buying the basket takes the asks of every leg.</p>
      <div class="books">${books.map(bookHtml).join("")}</div></div></div></div>`;
  const close = () => { $("modalRoot").innerHTML = ""; };
  $("closeModal").addEventListener("click", close);
  $("backdrop").addEventListener("click", (e) => { if (e.target.id === "backdrop") close(); });
}

// ---------- charts (SVG) ----------
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function chartFrame(el, height) {
  const width = Math.max(320, el.clientWidth || 600);
  return { width, height, m: { l: 44, r: 16, t: 14, b: 30 } };
}

function attachHover(el, svg, onMove) {
  let tip = el.querySelector(".tooltip");
  if (!tip) { tip = document.createElement("div"); tip.className = "tooltip"; tip.hidden = true; el.appendChild(tip); }
  const cross = svg.querySelector(".crosshair");
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const x = (e.clientX - r.left) * (svg.viewBox.baseVal.width / r.width);
    const out = onMove(x);
    if (!out) { tip.hidden = true; if (cross) cross.setAttribute("opacity", 0); return; }
    tip.innerHTML = out.html; tip.hidden = false;
    const left = Math.min(r.width - tip.offsetWidth - 4, Math.max(0, (out.x / svg.viewBox.baseVal.width) * r.width + 12));
    tip.style.left = `${left}px`; tip.style.top = `8px`;
    if (cross) { cross.setAttribute("x1", out.x); cross.setAttribute("x2", out.x); cross.setAttribute("opacity", 1); }
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; if (cross) cross.setAttribute("opacity", 0); });
}

function niceTicks(max, n = 4) {
  if (max <= 0) return [0, 1];
  const step0 = max / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 5, 10].map((k) => k * mag).find((s) => s >= step0);
  const ticks = []; for (let v = 0; v <= max + 1e-9; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function survivalChart(el, grid, budget) {
  if (!grid || !grid.length) { el.innerHTML = `<div class="empty">No episodes yet.</div>`; return; }
  const { width, height, m } = chartFrame(el, 280);
  const w = width - m.l - m.r, h = height - m.t - m.b;
  const x0 = Math.log(grid[0].t), x1 = Math.log(grid[grid.length - 1].t);
  const X = (t) => m.l + ((Math.log(t) - x0) / (x1 - x0)) * w;
  const Y = (p) => m.t + (1 - p) * h;
  const stepPath = (key) => grid.map((g, i) => (i ? `H${X(g.t).toFixed(1)}V${Y(g[key]).toFixed(1)}` : `M${X(g.t).toFixed(1)},${Y(g[key]).toFixed(1)}`)).join("");
  let band = grid.map((g, i) => `${i ? "H" : "M"}${X(g.t).toFixed(1)}${i ? "V" : ","}${Y(g.hi).toFixed(1)}`).join("");
  band += [...grid].reverse().map((g) => `H${X(g.t).toFixed(1)}V${Y(g.lo).toFixed(1)}`).join("") + "Z";
  const nice = [0.1, 0.3, 1, 3, 10, 30, 60, 300, 600, 1800, 3600, 10800, 36000, 86400];
  const labels = ["100ms", "300ms", "1s", "3s", "10s", "30s", "1m", "5m", "10m", "30m", "1h", "3h", "10h", "1d"];
  const xt = nice.map((t, i) => [t, labels[i]]).filter(([t]) => t >= grid[0].t && t <= grid[grid.length - 1].t);
  const budgets = budget ? [["typical", budget.typical], ["worst", budget.worst]].filter(([, t]) => t >= grid[0].t && t <= grid[grid.length - 1].t) : [];
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Survival curve of violation lifetimes">
    <g class="axis">${[0, 0.25, 0.5, 0.75, 1].map((p) => `<line class="gridline" x1="${m.l}" x2="${width - m.r}" y1="${Y(p)}" y2="${Y(p)}"/><text x="${m.l - 6}" y="${Y(p) + 4}" text-anchor="end">${Math.round(p * 100)}%</text>`).join("")}
      ${xt.map(([t, l]) => `<line class="gridline" x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${m.t + h}"/><text x="${X(t)}" y="${height - 10}" text-anchor="middle">${l}</text>`).join("")}</g>
    <path d="${band}" fill="${css("--band-1")}" stroke="none"/>
    <path d="${stepPath("km")}" fill="none" stroke="${css("--series-1")}" stroke-width="2"/>
    ${budgets.map(([name, t], i) => `<line x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${m.t + h}" stroke="${css("--text-muted")}" stroke-dasharray="4 3"/><text class="label" x="${X(t) + 4}" y="${m.t + h - 8 - i * 14}">${name} ${fmtS(t)}</text>`).join("")}
    <line class="crosshair" y1="${m.t}" y2="${m.t + h}" stroke="${css("--text-muted")}" opacity="0"/>
  </svg>`;
  attachHover(el, el.querySelector("svg"), (x) => {
    if (x < m.l || x > width - m.r) return null;
    const t = Math.exp(x0 + ((x - m.l) / w) * (x1 - x0));
    let g = grid[0]; for (const p of grid) { if (p.t <= t) g = p; else break; }
    return { x: X(g.t), html: `<b>longer than ${fmtS(g.t)}</b><br>Kaplan–Meier ${pct(g.km)}<br><span class="muted">bounds ${pct(g.lo)} – ${pct(g.hi)}</span>` };
  });
}

function timelineChart(el, timeline) {
  if (!timeline || !timeline.length) { el.innerHTML = `<div class="empty">Waiting for data.</div>`; return; }
  const { width, height, m } = chartFrame(el, 220);
  const w = width - m.l - m.r, h = height - m.t - m.b;
  const t0 = timeline[0].t, t1 = Math.max(timeline[timeline.length - 1].t, t0 + 60);
  const ymax = Math.max(1, ...timeline.map((d) => d.active));
  const ticks = niceTicks(ymax);
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * w;
  const Y = (v) => m.t + (1 - v / ticks[ticks.length - 1]) * h;
  const line = (key) => timeline.map((d, i) => `${i ? "H" : "M"}${X(d.t).toFixed(1)}${i ? "V" : ","}${Y(d[key]).toFixed(1)}`).join("");
  const nx = Math.min(6, Math.max(2, Math.floor(w / 110)));
  const xt = Array.from({ length: nx + 1 }, (_, i) => t0 + ((t1 - t0) * i) / nx);
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Violations per minute">
    <g class="axis">${ticks.map((v) => `<line class="gridline" x1="${m.l}" x2="${width - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v}</text>`).join("")}
      ${xt.map((t) => `<text x="${X(t)}" y="${height - 10}" text-anchor="middle">${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text>`).join("")}</g>
    <path d="${line("active")}" fill="none" stroke="${css("--series-1")}" stroke-width="2"/>
    <path d="${line("positive")}" fill="none" stroke="${css("--series-2")}" stroke-width="2"/>
    <line class="crosshair" y1="${m.t}" y2="${m.t + h}" stroke="${css("--text-muted")}" opacity="0"/>
  </svg>`;
  attachHover(el, el.querySelector("svg"), (x) => {
    if (x < m.l || x > width - m.r) return null;
    const t = t0 + ((x - m.l) / w) * (t1 - t0);
    let d = timeline[0]; for (const p of timeline) { if (p.t <= t) d = p; else break; }
    return { x: X(d.t), html: `<b>${new Date(d.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</b><br>all, before fees: ${d.active}<br>positive after fees: ${d.positive}` };
  });
}

function hbarChart(el, items, color) {
  if (!items.length) { el.innerHTML = ""; return; }
  const width = Math.max(320, el.clientWidth || 600), rowH = 26, labelW = 90, m = { t: 6, r: 48 };
  const height = m.t * 2 + items.length * rowH;
  const max = Math.max(1, ...items.map((d) => d.value));
  const W = width - labelW - m.r;
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">${items.map((d, i) => {
    const y = m.t + i * rowH, bw = Math.max(2, (d.value / max) * W);
    return `<g><title>${esc(d.label)}: ${d.value}</title><text class="label" x="${labelW - 8}" y="${y + 16}" text-anchor="end">${esc(d.label)}</text>
      <rect x="${labelW}" y="${y + 5}" width="${bw}" height="14" rx="4" fill="${color}"/>
      <text class="label" x="${labelW + bw + 6}" y="${y + 16}">${d.value}</text></g>`;
  }).join("")}</svg>`;
}

function latencyChart(el, lat, interval, budget) {
  if (!lat) { el.innerHTML = `<div class="empty">Run <code>python -m mispricing.latency</code> to measure it.</div>`; return; }
  const parts = [
    ["detection lag", interval / 2, "--series-1"], ["fetch books", lat.parts.get_total.p50, "--series-2"],
    ["compute", lat.parts.compute_batch.p50, "--series-3"], ["order round trip", lat.parts.post_total.p50, "--series-4"],
  ];
  const total = parts.reduce((s, p) => s + p[1], 0);
  const width = Math.max(320, el.clientWidth || 600), m = 16, W = width - 2 * m, height = 86;
  let x = m;
  const segs = parts.map(([label, v, c]) => {
    const w = Math.max(2, (v / total) * W - 2), seg = { label, v, c, x, w };
    x += (v / total) * W; return seg;
  });
  el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Reaction time breakdown">
    ${segs.map((s) => `<g><title>${s.label}: ${fmtS(s.v)}</title><rect x="${s.x}" y="14" width="${s.w}" height="18" rx="4" fill="${css(s.c)}"/></g>`).join("")}
    ${segs.filter((s) => s.w > 60).map((s) => `<text class="label" x="${s.x}" y="50">${s.label}</text><text class="label" x="${s.x}" y="64">${fmtS(s.v)}</text>`).join("")}
    <text class="label" x="${width - m}" y="10" text-anchor="end">typical total ${fmtS(budget ? budget.typical : total)}</text>
  </svg>`;
}

// ---------- analysis / episodes / history ----------
function renderFull() {
  const f = S.full; if (!f) return;
  if (S.tab === "live") { timelineChart($("timelineChart"), f.timeline); return; }
  if (S.tab === "analysis") {
    const st = f.stats;
    $("survivalHint").textContent = `${num(st.episodes)} episodes · poll ${fmtS(f.health.interval)}`;
    survivalChart($("survivalChart"), st.survival, st.budget);
    $("latencyHint").textContent = f.latency ? `measured ${f.latency.measured_utc.replace("T", " ").replace("Z", " UTC")} via ${f.latency.edge}` : "";
    latencyChart($("latencyChart"), f.latency, f.health.interval, st.budget);
    $("latencyTable").innerHTML = f.latency ? `<table><thead><tr><th>part</th><th class="num">p50</th><th class="num">p95</th></tr></thead><tbody>
      <tr><td>detection lag (uniform over the poll)</td><td class="num">${fmtS(f.health.interval / 2)}</td><td class="num">${fmtS(f.health.interval)}</td></tr>
      <tr><td>fetch books (warm GET)</td><td class="num">${fmtS(f.latency.parts.get_total.p50)}</td><td class="num">${fmtS(f.latency.parts.get_total.p95)}</td></tr>
      <tr><td>compute (quotes + detectors)</td><td class="num">${fmtS(f.latency.parts.compute_batch.p50)}</td><td class="num">${fmtS(f.latency.parts.compute_batch.p95)}</td></tr>
      <tr><td>order round trip (rejected POST, lower bound)</td><td class="num">${fmtS(f.latency.parts.post_total.p50)}</td><td class="num">${fmtS(f.latency.parts.post_total.p95)}</td></tr>
      <tr><td><b>budget</b></td><td class="num"><b>${fmtS(st.budget.typical)}</b></td><td class="num"><b>${fmtS(st.budget.worst)}</b></td></tr></tbody></table>
      <p class="note">Catch probability: definitely ${pct(st.catch?.definitely)}, possibly ${pct(st.catch?.possibly)}, Kaplan–Meier ${pct(st.catch?.km)}.</p>` : "";
    const r = f.replay;
    $("replaySummary").innerHTML = r.signals ? `<table><tbody>
      <tr><td>signals</td><td class="num">${r.signals}</td></tr>
      <tr><td>full fill rate</td><td class="num">${pct(r.full_fill_rate)}</td></tr>
      <tr><td>P&amp;L the detector expected</td><td class="num">${money(r.planned)}</td></tr>
      <tr><td>simulated P&amp;L</td><td class="num ${signClass(r.pnl)}">${money(r.pnl)}</td></tr>
      <tr><td>simulated P&amp;L, certain fills only</td><td class="num ${signClass(r.pnl_certain)}">${money(r.pnl_certain)}</td></tr></tbody></table>`
      : `<div class="empty">No signal was positive after fees yet, so nothing has been replayed.</div>`;
    hbarChart($("replayChart"), ["full", "partial", "legged", "miss"].map((k) => ({ label: k, value: r.outcomes[k] || 0 })).filter((d) => r.signals), css("--series-1"));
    $("replayTable").innerHTML = r.recent.length ? `<table><thead><tr><th>detected</th><th>signal</th><th class="num">baskets</th><th class="num">expected</th><th>outcome</th><th class="num">hedged</th><th class="num">P&amp;L</th><th class="num">reaction</th></tr></thead><tbody>
      ${r.recent.map((x) => `<tr><td>${dayClock(Number(x.t_detect))}</td><td><div>${esc(KIND_LABEL[x.kind] || x.kind)}</div><div class="legs">${esc(x.signature.split("|").slice(1).join(" + "))}</div></td>
        <td class="num">${num(Number(x.planned_q), 2)}</td><td class="num">${money(Number(x.planned_net))}</td>
        <td><span class="badge ${x.outcome === "full" ? "good" : x.outcome === "miss" ? "kind" : "bad"}">${x.outcome}</span> ${x.book_unchanged ? `<span class="badge kind">certain</span>` : ""}</td>
        <td class="num">${num(Number(x.hedged), 2)}</td><td class="num ${signClass(Number(x.pnl))}">${money(Number(x.pnl))}</td><td class="num">${fmtS(Number(x.reaction_s))}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Nothing replayed yet.</div>`;
    $("kindTable").innerHTML = `<table><thead><tr><th>kind</th><th class="num">episodes</th><th class="num">positive after fees</th><th class="num">tradeable</th></tr></thead><tbody>
      ${Object.entries(st.by_kind).map(([k, v]) => `<tr><td>${KIND_LABEL[k]}</td><td class="num">${v.episodes}</td><td class="num">${v.positive}</td><td class="num">${v.tradeable}</td></tr>`).join("")}</tbody></table>
      <p class="note">${num(st.events)} distinct events. Episodes on the same event are not independent observations.</p>`;
  }
  if (S.tab === "episodes") renderEpisodes();
  $("fVol").textContent = `${f.filters.min_volume_24h}`;
  $("fSize").textContent = `${f.filters.min_top_size}`;
  $("fSpread").textContent = `${Math.round(f.filters.max_spread * 100)}¢`;
}

function renderEpisodes() {
  const f = S.full; if (!f) return;
  const kind = $("epKind").value, positive = $("epPositive").checked;
  const rows = f.episodes.filter((e) => (!kind || e.kind === kind) && (!positive || e.max_best_net > 0)).slice(0, 500);
  $("episodesTable").innerHTML = rows.length ? `<table><thead><tr><th>started</th><th>market</th><th>kind</th><th class="num">lifetime</th><th class="num">max edge</th><th class="num">best net</th><th class="num">size at start</th><th></th></tr></thead><tbody>
    ${rows.map((e) => `<tr><td>${dayClock(e.start)}</td><td><div>${esc(e.title || e.event_ticker)}</div><div class="legs">${esc(e.signature.split("|").slice(1).join(" + "))}</div></td>
      <td><span class="badge kind">${KIND_LABEL[e.kind] || e.kind}</span></td>
      <td class="num">${fmtS(e.lo)} – ${e.hi == null ? "?" : fmtS(e.hi)}</td><td class="num">${cents(e.max_gross)}</td>
      <td class="num ${signClass(e.max_best_net)}">${money(e.max_best_net)}</td><td class="num">${num(e.first_top_size, 2)}</td><td>${filterBadge(e.passes_filter)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No closed episodes match.</div>`;
}
$("epKind").addEventListener("change", renderEpisodes);
$("epPositive").addEventListener("change", renderEpisodes);

function renderHistory() {
  if (S.tab !== "history") return;
  const hsty = S.history;
  if (!hsty) { $("historyHeadline").textContent = "No history study found. Run: python -m mispricing.history --days 7 && python -m mispricing.report --source history"; return; }
  const g = hsty.summary.groups, d = hsty.summary.data;
  $("historyHeadline").textContent = hsty.summary.headline;
  $("historyKpis").innerHTML = [
    kpiTile("Window", `${Math.round((d.t_last - d.t_first) / 86400)} days`, `${dayClock(d.t_first)} → ${dayClock(d.t_last)}`),
    kpiTile("Episodes", num(g.all.episodes), `${g.all.events} events`),
    kpiTile("Positive after fees", num(g.all.net_best_pos), "depth unknown: 1 or 100 contracts assumed"),
    kpiTile("After liquidity filter", num(g["stale-quote filtered"].net_best_pos), "positive after fees"),
    kpiTile("Median lifetime", hsty.median_lo == null ? "–" : `${fmtS(hsty.median_lo)}–${fmtS(hsty.median_hi)}`, "1-minute resolution"),
  ].join("");
  survivalChart($("historySurvival"), hsty.survival, null);
  const kinds = {};
  hsty.episodes.forEach((e) => { const k = (kinds[e.kind] ||= { n: 0, pos: 0 }); k.n++; if (e.max_best_net > 0) k.pos++; });
  $("historyKinds").innerHTML = `<table><thead><tr><th>kind</th><th class="num">episodes</th><th class="num">positive after fees</th></tr></thead><tbody>
    ${Object.entries(kinds).map(([k, v]) => `<tr><td>${KIND_LABEL[k] || k}</td><td class="num">${v.n}</td><td class="num">${v.pos}</td></tr>`).join("")}</tbody></table>`;
  $("historyTable").innerHTML = `<table><thead><tr><th>started</th><th>signal</th><th class="num">lifetime</th><th class="num">max edge</th><th class="num">best net</th><th class="num">24h volume</th></tr></thead><tbody>
    ${hsty.episodes.slice(0, 500).map((e) => `<tr><td>${dayClock(e.start)}</td><td><div>${esc(e.event_ticker)}</div><div class="legs">${esc(e.signature.split("|").slice(1).join(" + "))}</div></td>
      <td class="num">${fmtS(e.lo)} – ${e.hi == null ? "?" : fmtS(e.hi)}</td><td class="num">${cents(e.max_gross)}</td><td class="num ${signClass(e.max_best_net)}">${money(e.max_best_net)}</td><td class="num">${num(e.min_leg_volume_24h)}</td></tr>`).join("")}</tbody></table>`;
}

// ---------- data flow ----------
function applyLive(p) {
  S.live = p;
  renderHeader(p.health);
  renderKpis(p.kpis);
  renderOpen(p.active);
  onNewAlerts(p.alerts);
}

async function loadFull() {
  try {
    const f = await (await fetch("/api/full")).json();
    const firstLoad = !S.full;
    S.full = f;
    if (firstLoad) { f.alerts.forEach((a) => { S.seenAlerts.add(a.id); S.lastAlertId = Math.max(S.lastAlertId, a.id); }); }
    renderAlertsList();
    renderFull();
  } catch (e) { console.warn("full refresh failed", e); }
}

async function loadHistory() {
  try { S.history = await (await fetch("/api/history")).json(); renderHistory(); } catch (e) { console.warn(e); }
}

function connect() {
  const es = new EventSource(`/api/stream?since=${S.lastAlertId}`);
  es.addEventListener("live", (e) => applyLive(JSON.parse(e.data)));
  es.onerror = () => {
    es.close();
    $("status").className = "status stalled"; $("status").textContent = "disconnected";
    setTimeout(connect, 3000);
  };
}

(async function start() {
  await loadFull();
  try { applyLive(await (await fetch(`/api/live?since=${S.lastAlertId}`)).json()); } catch { /* stream will retry */ }
  connect();
  loadHistory();
  setInterval(loadFull, 10000);
  setInterval(loadHistory, 300000);
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { renderFull(); renderHistory(); }, 150); });
})();
