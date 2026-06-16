"use strict";

const VENUES = ["hyperliquid", "lighter", "pacifica"];
const VLABEL = { hyperliquid: "Hyperliquid", lighter: "Lighter", pacifica: "Pacifica" };
const VCOLOR = { hyperliquid: "#5eead4", lighter: "#a78bfa", pacifica: "#fbbf24" };
const VOL_STOPS = [0, 5e6, 25e6, 50e6, 100e6, 250e6, 500e6, 1e9, 2e9, 7e9];
const D = {};  // loaded JSON

const $ = (s) => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const fmtUsd = (v) => v >= 1e9 ? `$${(v/1e9).toFixed(v%1e9?1:0)}B` : v >= 1e6 ? `$${(v/1e6).toFixed(0)}M` : v >= 1e3 ? `$${(v/1e3).toFixed(0)}k` : `$${v}`;
const rungLabel = (r) => r < 1e6 ? `$${r/1e3}k` : `$${r/1e6}M`;

async function load() {
  const names = ["meta", "gap", "calc", "history", "stress", "coverage"];
  const res = await Promise.all(names.map((n) => fetch(`data/${n}.json`).then((r) => r.json())));
  names.forEach((n, i) => (D[n] = res[i]));
}

// ---------- hero ----------
function renderHero() {
  $("#headline").textContent = D.meta.headline;
  const ago = Math.round((Date.now() / 1000 - D.meta.generated_at) / 3600);
  $("#updated").textContent = `updated ${ago <= 0 ? "just now" : ago + "h ago"}`;
}

// ---------- The Gap ----------
let gapAsset = "BTC", gapChart;
function renderGapTabs() {
  const tabs = $("#gap-tabs"); tabs.innerHTML = "";
  D.meta.assets.filter((a) => D.gap[a]).forEach((a) => {
    const b = el("button", a === gapAsset ? "on" : "", a);
    b.onclick = () => { gapAsset = a; renderGapTabs(); renderGapTable(); renderGapChart(); };
    tabs.appendChild(b);
  });
}
function cheapestVenue(cells) {
  let best = null;
  for (const v of VENUES) { const c = cells[v]; if (c && !c.sparse && !c.bad_realized && (best === null || c.realized < best[1])) best = [v, c.realized]; }
  return best ? best[0] : null;
}
function renderGapTable() {
  const t = $("#gap-table"); t.innerHTML = "";
  const head = el("tr"); head.appendChild(el("th", "", "rung"));
  VENUES.forEach((v) => head.appendChild(el("th", "", VLABEL[v]))); t.appendChild(head);
  const rungs = D.gap[gapAsset];
  for (const r of D.calc.rungs) {
    const cells = rungs[String(r)]; if (!cells) continue;
    const best = cheapestVenue(cells);
    const tr = el("tr"); tr.appendChild(el("td", "rung", rungLabel(r)));
    for (const v of VENUES) {
      const c = cells[v];
      if (!c) { tr.appendChild(el("td", "muted", "–")); continue; }
      if (c.bad_realized) { const td = el("td", "muted", "n/a"); td.title = "thin/stale cell — realized non-physical, suppressed"; tr.appendChild(td); continue; }
      const g = c.feed_limited ? `<span class="badge">fl</span>` : `${c.gap >= 0 ? "+" : ""}${c.gap.toFixed(2)}`;
      const star = c.sparse ? "<sup>*</sup>" : "";
      const td = el("td", v === best ? "best" : "", `${c.realized.toFixed(2)} <span class="g">(${g})</span>${star}`);
      tr.appendChild(td);
    }
    t.appendChild(tr);
  }
}
function renderGapChart() {
  const rungs = D.gap[gapAsset];
  const labels = D.calc.rungs.filter((r) => rungs[String(r)]).map(rungLabel);
  const data = {
    labels,
    datasets: VENUES.map((v) => ({
      label: VLABEL[v], backgroundColor: VCOLOR[v],
      data: D.calc.rungs.filter((r) => rungs[String(r)]).map((r) => {
        const c = rungs[String(r)][v]; return c && !c.bad_realized ? c.realized : null;
      }),
    })),
  };
  if (gapChart) gapChart.destroy();
  gapChart = new Chart($("#gap-chart"), {
    type: "bar", data,
    options: { responsive: true, plugins: { legend: { labels: { color: "#cbd5e1" } } },
      scales: { x: { ticks: { color: "#94a3b8" }, grid: { display: false } },
                y: { title: { display: true, text: "realized cost (bps)", color: "#94a3b8" },
                     ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } } } },
  });
}

// ---------- Calculator ----------
let side = "long";
function feeBps(venue, vol) {
  const lad = D.calc.fee_ladders;
  if (venue === "lighter") return lad.lighter.standard_taker_bps;
  let taker = lad[venue][0][1];
  for (const [cut, t] of lad[venue]) if (vol >= cut) taker = t;
  return taker * 1e4;
}
function fundingBps(venue, asset, holdDays) {
  const apr = (D.calc.funding_apr[venue] || {})[asset];
  if (apr == null) return 0;
  return (side === "long" ? 1 : -1) * apr * (holdDays / 365) * 1e4;
}
function renderCalc() {
  const asset = $("#c-asset").value, rung = +$("#c-size").value;
  const vol = VOL_STOPS[+$("#c-vol").value], hold = +$("#c-hold").value;
  $("#c-vol-val").textContent = fmtUsd(vol) + " / 14d";
  $("#c-hold-val").textContent = hold + " d";
  const rows = [];
  for (const v of VENUES) {
    const slip = ((D.calc.slippage[asset] || {})[String(rung)] || {})[v];
    if (slip == null) continue;
    const fee = feeBps(v, vol), fund = fundingBps(v, asset, hold);
    rows.push({ v, slip, fee, fund, rt: 2 * (slip + fee) + fund });
  }
  rows.sort((a, b) => a.rt - b.rt);
  const maxRt = Math.max(1, ...rows.map((r) => Math.abs(r.rt)));
  const box = $("#c-results"); box.innerHTML = "";
  rows.forEach((r, i) => {
    const dollars = Math.abs(r.rt) / 1e4 * rung;
    const seg = (val, cls) => `<span class="seg ${cls}" style="width:${Math.abs(val) / maxRt * 100}%"></span>`;
    const row = el("div", "cres" + (i === 0 ? " win" : ""));
    row.innerHTML =
      `<div class="cres-h"><b>${VLABEL[r.v]}</b>${i === 0 ? '<span class="tag">cheapest</span>' : ""}
       <span class="cres-net">${r.rt >= 0 ? "+" : ""}${r.rt.toFixed(2)} bps <em>(~$${dollars.toFixed(0)})</em></span></div>
       <div class="bar">${seg(2 * r.slip, "s-slip")}${seg(2 * r.fee, "s-fee")}${r.fund > 0 ? seg(r.fund, "s-fund") : ""}</div>
       <div class="cres-d">slip ${(2 * r.slip).toFixed(2)} · fee ${(2 * r.fee).toFixed(2)} · funding ${r.fund >= 0 ? "+" : ""}${r.fund.toFixed(2)}${r.fund < 0 ? " (paid to you)" : ""}${r.v === "pacifica" ? " · realized (book feed-limited)" : ""}${r.v === "lighter" ? " · standard acct (0 fee)" : ""}</div>`;
    box.appendChild(row);
  });
  if (!rows.length) box.innerHTML = '<p class="muted">No clips of this size observed for this asset in-window.</p>';
  $("#c-note").textContent = "Round-trip = enter + hold + exit. Fees: HL/Pacifica priced at your volume tier; Lighter standard account (0). Funding over your hold (negative = you're paid).";
}

// ---------- Track record ----------
let trackChart;
function renderTrack() {
  const asset = $("#t-asset").value, rung = $("#t-rung").value, metric = $("#t-metric").value;
  const vd = ((D.history[asset] || {})[rung]) || {};
  const allDates = [...new Set(VENUES.flatMap((v) => (vd[v]?.daily || []).map((d) => d.t)))].sort();
  const data = {
    labels: allDates,
    datasets: VENUES.filter((v) => vd[v]).map((v) => {
      const m = Object.fromEntries(vd[v].daily.map((d) => [d.t, d[metric]]));
      return { label: VLABEL[v], borderColor: VCOLOR[v], backgroundColor: VCOLOR[v],
               spanGaps: true, tension: 0.2, data: allDates.map((d) => m[d] ?? null) };
    }),
  };
  if (trackChart) trackChart.destroy();
  trackChart = new Chart($("#track-chart"), {
    type: "line", data,
    options: { responsive: true, plugins: { legend: { labels: { color: "#cbd5e1" } } },
      scales: { x: { ticks: { color: "#94a3b8" }, grid: { display: false } },
                y: { title: { display: true, text: metric + " (bps)", color: "#94a3b8" },
                     ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } } } },
  });
  renderHeatmap(vd);
}
function renderHeatmap(vd) {
  const hm = $("#heatmap"); hm.innerHTML = "";
  const labels = ["00", "02", "04", "06", "08", "10", "12", "14", "16", "18", "20", "22"];
  let max = 0.1;
  VENUES.forEach((v) => (vd[v]?.session || []).forEach((s) => (max = Math.max(max, s.realized))));
  hm.appendChild(el("div", "hm-corner", ""));
  labels.forEach((l) => hm.appendChild(el("div", "hm-h", l)));
  VENUES.forEach((v) => {
    hm.appendChild(el("div", "hm-v", VLABEL[v]));
    const m = Object.fromEntries((vd[v]?.session || []).map((s) => [s.b, s.realized]));
    for (let b = 0; b < 12; b++) {
      const val = m[b];
      const c = el("div", "hm-c", val == null ? "" : val.toFixed(1));
      if (val != null) c.style.background = `rgba(94,234,212,${0.12 + 0.8 * Math.min(1, val / max)})`;
      c.title = val == null ? "no data" : `${val.toFixed(2)} bps`;
      hm.appendChild(c);
    }
  });
}

// ---------- Stress ----------
function renderStress() {
  const box = $("#stress-body"); box.innerHTML = "";
  const ev = D.stress.events || [];
  if (!ev.length) {
    box.innerHTML = `<p class="empty">No market-wide cascade above $${(D.stress.threshold / 1e6).toFixed(0)}M in the current window. The detector is live and fills in when one occurs.</p>`;
    return;
  }
  ev.slice().reverse().forEach((e) => {
    const card = el("div", "scard");
    const t = new Date(e.start * 1000).toISOString().slice(0, 16).replace("T", " ");
    card.appendChild(el("h3", "", `${t} UTC · $${e.liq.toLocaleString()} liquidated`));
    const tbl = el("table");
    tbl.appendChild(el("tr", "", "<th>asset</th><th>venue</th><th>stress</th><th>baseline</th><th>Δ</th>"));
    e.degr.forEach((d) => {
      const delta = d.baseline == null ? "n/a" : `${d.stress - d.baseline >= 0 ? "+" : ""}${(d.stress - d.baseline).toFixed(2)}`;
      tbl.appendChild(el("tr", "", `<td>${d.asset}</td><td>${VLABEL[d.venue]}</td><td>${d.stress.toFixed(2)}</td><td>${d.baseline == null ? "n/a" : d.baseline.toFixed(2)}</td><td>${delta}</td>`));
    });
    card.appendChild(tbl); box.appendChild(card);
  });
}

// ---------- Methodology ----------
function renderMethod() {
  const t = $("#cov-table"); t.innerHTML = "";
  t.appendChild(el("tr", "", "<th>venue</th><th>clip-size method</th><th>data reliability (clean 2h windows)</th>"));
  D.coverage.venues.forEach((c) => {
    const cell = c.clean_pct == null ? "n/a"
      : `${c.clean_pct}% clean (${Math.round(c.clean_pct / 100 * c.windows)} of ${c.windows})`;
    t.appendChild(el("tr", "", `<td>${VLABEL[c.venue]}</td><td>${c.method}</td><td>${cell}</td>`));
  });
  const ul = $("#seams"); ul.innerHTML = "";
  D.coverage.seams.forEach((s) => ul.appendChild(el("li", "", s)));
}

// ---------- wiring ----------
function fillSelect(sel, opts, val, labelFn) {
  sel.innerHTML = ""; opts.forEach((o) => { const e = el("option"); e.value = o; e.textContent = labelFn ? labelFn(o) : o; sel.appendChild(e); });
  if (val != null) sel.value = val;
}
function init() {
  renderHero();
  renderGapTabs(); renderGapTable(); renderGapChart();

  const assetsWithGap = D.meta.assets.filter((a) => D.gap[a]);
  fillSelect($("#c-asset"), assetsWithGap, "BTC");
  fillSelect($("#c-size"), D.calc.rungs, 100000, rungLabel);
  $("#c-vol").oninput = $("#c-hold").oninput = renderCalc;
  $("#c-asset").onchange = $("#c-size").onchange = renderCalc;
  document.querySelectorAll(".toggle button").forEach((b) => b.onclick = () => {
    side = b.dataset.side; document.querySelectorAll(".toggle button").forEach((x) => x.classList.toggle("on", x === b)); renderCalc();
  });
  renderCalc();

  const assetsWithHist = D.meta.assets.filter((a) => D.history[a] && Object.keys(D.history[a]).length);
  fillSelect($("#t-asset"), assetsWithHist.length ? assetsWithHist : ["BTC"], "BTC");
  fillSelect($("#t-rung"), D.calc.rungs, 100000, rungLabel);
  $("#t-asset").onchange = $("#t-rung").onchange = $("#t-metric").onchange = renderTrack;
  renderTrack();

  renderStress(); renderMethod();
}

load().then(init).catch((e) => { $("#headline").textContent = "Failed to load data."; console.error(e); });
