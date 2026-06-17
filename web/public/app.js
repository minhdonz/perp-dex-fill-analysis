"use strict";

const VENUES = ["hyperliquid", "lighter", "pacifica"];
const VLABEL = { hyperliquid: "Hyperliquid", lighter: "Lighter", pacifica: "Pacifica" };
const VCOLOR = { hyperliquid: "#5fb3b3", lighter: "#9b8cf0", pacifica: "#d6a85c" };
const AXIS = "#6b6f78", GRID = "#1c1e22", LEGEND_FG = "#9296a0";
if (window.Chart) {
  Chart.defaults.font.family = "JetBrains Mono, ui-monospace, monospace";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = AXIS;
  // hover anywhere along the x-axis surfaces every series, no need to land on a point
  Chart.defaults.interaction = { mode: "index", intersect: false, axis: "x" };
  Chart.defaults.hover = { mode: "index", intersect: false };
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: "#15171a", borderColor: "#2b2e33", borderWidth: 1,
    titleColor: "#e7e9ec", bodyColor: "#cdd0d5", titleFont: { weight: "500" },
    padding: 10, cornerRadius: 8, usePointStyle: true, boxPadding: 6,
    callbacks: { label: (c) => {
      const n = c.dataset.n?.[c.dataIndex];
      return ` ${c.dataset.label}  ${c.parsed.y != null ? c.parsed.y.toFixed(2) + " bps" : "n/a"}${n != null ? `  ·  n=${n}` : ""}`;
    } },
  });
  // vertical crosshair guide, drawn only for line charts
  Chart.register({
    id: "crosshair",
    afterDatasetsDraw(chart) {
      if (chart.config.type !== "line") return;
      const act = chart.tooltip?.getActiveElements?.() || [];
      if (!act.length) return;
      const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
      ctx.save();
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom);
      ctx.lineWidth = 1; ctx.strokeStyle = "rgba(203,178,133,.35)"; ctx.stroke();
      ctx.restore();
    },
  });
}
const VOL_STOPS = [0, 5e6, 25e6, 50e6, 100e6, 250e6, 500e6, 1e9, 2e9, 7e9];
const D = {};  // loaded JSON

const $ = (s) => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const fmtUsd = (v) => v >= 1e9 ? `$${(v/1e9).toFixed(v%1e9?1:0)}B` : v >= 1e6 ? `$${(v/1e6).toFixed(0)}M` : v >= 1e3 ? `$${(v/1e3).toFixed(0)}k` : `$${v}`;
const fmtSigned = (v) => { const s = v < 0 ? "−" : "+", a = Math.abs(v); return s + (a >= 1e6 ? `$${(a/1e6).toFixed(1)}M` : a >= 1e3 ? `$${(a/1e3).toFixed(0)}k` : `$${a.toFixed(0)}`); };
const rungLabel = (r) => r < 1e6 ? `$${r/1e3}k` : `$${r/1e6}M`;

async function load() {
  const names = ["meta", "gap", "calc", "history", "stress", "coverage", "funding", "makers"];
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
      if (c.bad_realized) { const td = el("td", "muted", "n/a"); td.setAttribute("data-tip", `thin or stale cell: realized non-physical, suppressed${c.n != null ? ` (n=${c.n})` : ""}`); tr.appendChild(td); continue; }
      const g = c.feed_limited ? `<span class="badge">fl</span>` : Math.max(c.gap, 0).toFixed(2);
      const star = c.sparse ? "<sup>*</sup>" : "";
      const td = el("td", v === best ? "best" : "", `${c.realized.toFixed(2)} <span class="g">(${g})</span>${star}`);
      if (c.n != null) td.setAttribute("data-tip", `n=${c.n} clips`);
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
    datasets: VENUES.map((v) => {
      const shown = D.calc.rungs.filter((r) => rungs[String(r)]);
      return {
        label: VLABEL[v], backgroundColor: VCOLOR[v],
        borderRadius: 3, maxBarThickness: 40, categoryPercentage: 0.7, barPercentage: 0.9,
        data: shown.map((r) => { const c = rungs[String(r)][v]; return c && !c.bad_realized ? c.realized : null; }),
        n: shown.map((r) => { const c = rungs[String(r)][v]; return c && !c.bad_realized ? c.n : null; }),
      };
    }),
  };
  if (gapChart) gapChart.destroy();
  gapChart = new Chart($("#gap-chart"), {
    type: "bar", data,
    options: { responsive: true, plugins: { legend: { labels: { color: LEGEND_FG, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded" } } },
      scales: { x: { ticks: { color: AXIS }, grid: { display: false } },
                y: { title: { display: true, text: "realized cost (bps)", color: AXIS },
                     ticks: { color: AXIS }, grid: { color: GRID } } } },
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
  const fundWin = D.calc.funding_window_days ?? 14;
  $("#c-vol-val").textContent = fmtUsd(vol) + " / 14d";
  $("#c-hold-val").textContent = hold + " d";
  const rows = [];
  for (const v of VENUES) {
    const slip = ((D.calc.slippage[asset] || {})[String(rung)] || {})[v];
    if (slip == null) continue;
    const fee = feeBps(v, vol), fund = fundingBps(v, asset, hold);
    const apr = (D.calc.funding_apr[v] || {})[asset];
    // side-adjusted annual rate, so its sign matches the funding bps shown
    const effApr = apr == null ? null : (side === "long" ? 1 : -1) * apr;
    rows.push({ v, slip, fee, fund, effApr, rt: 2 * (slip + fee) + fund });
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
       <div class="cres-d">slip ${(2 * r.slip).toFixed(2)} · fee ${(2 * r.fee).toFixed(2)} · funding ${r.fund >= 0 ? "+" : ""}${r.fund.toFixed(2)}${r.effApr != null ? ` (${r.effApr >= 0 ? "+" : ""}${(r.effApr * 100).toFixed(1)}% APR, ${fundWin}d avg)` : ""}${r.fund < 0 ? ", paid to you" : ""}${r.v === "pacifica" ? " · realized (book feed-limited)" : ""}${r.v === "lighter" ? " · standard acct (0 fee)" : ""}</div>`;
    box.appendChild(row);
  });
  if (!rows.length) box.innerHTML = '<p class="muted">No clips of this size observed for this asset in-window.</p>';
  const fundAsOf = D.calc.funding_as_of
    ? new Date(D.calc.funding_as_of * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC"
    : null;
  $("#c-note").textContent = `All figures are bps of notional (round-trip = enter + hold + exit). Fees: HL/Pacifica priced at your volume tier; Lighter standard account (0). Funding is shown as bps charged over your hold, with the venue's annualized rate (APR) in parentheses; negative = you're paid. Funding APR is a trailing ${fundWin}-day average${fundAsOf ? `, as of ${fundAsOf}` : ""}.`;
}

// ---------- Funding & carry ----------
let fundAsset = "BTC";
function fundAssets() {
  return D.meta.assets.filter((a) => D.funding.assets[a] && Object.keys(D.funding.assets[a]).length);
}
function renderFundTabs() {
  const tabs = $("#fund-tabs"); tabs.innerHTML = "";
  const assets = fundAssets();
  if (!assets.includes(fundAsset)) fundAsset = assets[0];
  assets.forEach((a) => {
    const b = el("button", a === fundAsset ? "on" : "", a);
    b.onclick = () => { fundAsset = a; renderFundTabs(); renderFundTable(); };
    tabs.appendChild(b);
  });
}
function stabBand(flip) {
  if (flip == null) return ["", ""];
  if (flip < 0.15) return ["stable", "s-ok"];
  if (flip <= 0.40) return ["variable", "s-mid"];
  return ["whippy", "s-bad"];
}
function renderFundTable() {
  const t = $("#fund-table"); t.innerHTML = "";
  const consistencyTip = "Share of hours in the window where funding was positive (longs paying). Near 100% or 0% means consistently one direction; near 50% means it keeps switching.";
  const stabilityTip = "Flip rate: share of consecutive hours where the funding rate changed sign. Lower is a more dependable carry; higher means the rate keeps flipping.";
  const volatilityTip = "Standard deviation of the venue's hourly funding rate over the window, in basis points per hour. Higher means the rate swings more from hour to hour, even if its average is steady.";
  t.appendChild(el("tr", "", `<th>venue</th><th>funding APR</th><th>direction</th><th data-tip="${consistencyTip}">consistency</th><th data-tip="${stabilityTip}">stability (flip rate)</th><th data-tip="${volatilityTip}">volatility</th><th>hrs</th>`));
  const cells = D.funding.assets[fundAsset] || {};
  for (const v of VENUES) {
    const c = cells[v];
    if (!c) { t.appendChild(el("tr", "", `<td>${VLABEL[v]}</td><td class="muted" colspan="6">no data</td>`)); continue; }
    const apr = c.apr * 100;
    const dir = c.apr >= 0 ? `<span class="chip chip-long">longs pay</span>` : `<span class="chip chip-short">shorts pay</span>`;
    const pos = c.pct_positive == null ? "–" : `${Math.round(c.pct_positive * 100)}% of hrs +`;
    const [word] = stabBand(c.flip_rate);
    const flip = c.flip_rate == null ? "–" : `${(c.flip_rate * 100).toFixed(2)}% <span class="muted">${word}</span>`;
    const vol = c.std_hourly == null ? "–" : `${(c.std_hourly * 1e4).toFixed(2)} bps/hr`;
    t.appendChild(el("tr", "",
      `<td>${VLABEL[v]}</td><td>${apr >= 0 ? "+" : ""}${apr.toFixed(1)}%</td><td>${dir}</td><td>${pos}</td><td>${flip}</td><td>${vol}</td><td>${c.n ?? "–"}</td>`));
  }
}
function renderFundNote() {
  const win = D.funding.window_days ?? 14;
  const asOf = D.funding.as_of
    ? new Date(D.funding.as_of * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : null;
  $("#fund-note").textContent = `Funding APR is the trailing ${win}-day average${asOf ? `, as of ${asOf}` : ""}. Positive APR means longs pay shorts. Flip rate is how often the hourly rate changed sign; higher means less dependable as carry. Volatility is the standard deviation of the hourly rate.`;
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
      const mn = Object.fromEntries(vd[v].daily.map((d) => [d.t, d.n]));
      return { label: VLABEL[v], borderColor: VCOLOR[v], backgroundColor: VCOLOR[v],
               borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, spanGaps: true,
               tension: 0.3, data: allDates.map((d) => m[d] ?? null),
               n: allDates.map((d) => mn[d] ?? null) };
    }),
  };
  if (trackChart) trackChart.destroy();
  trackChart = new Chart($("#track-chart"), {
    type: "line", data,
    options: { responsive: true, plugins: { legend: { labels: { color: LEGEND_FG, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded" } } },
      scales: { x: { ticks: { color: AXIS }, grid: { display: false } },
                y: { title: { display: true, text: metric + " (bps)", color: AXIS },
                     ticks: { color: AXIS }, grid: { color: GRID } } } },
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
    const mn = Object.fromEntries((vd[v]?.session || []).map((s) => [s.b, s.n]));
    for (let b = 0; b < 12; b++) {
      const val = m[b];
      const c = el("div", "hm-c", val == null ? "" : val.toFixed(2));
      if (val != null) c.style.background = `rgba(95,179,179,${0.06 + 0.5 * Math.min(1, val / max)})`;
      c.setAttribute("data-tip", val == null ? "no data" : `${val.toFixed(2)} bps · n=${mn[b]}`);
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
const METHOD_DESC = {
  exact: ["Exact", "Lighter publishes the taker order ID, so every fill of one order is grouped back together precisely."],
  identity: ["By taker", "Hyperliquid publishes the taker but not order IDs, so fills from the same taker within a few milliseconds are grouped as one order."],
  heuristic: ["Inferred", "Pacifica publishes neither order IDs nor the taker, so a fast run of same-side trades is read as one sweep."],
};
function renderMethod() {
  const t = $("#cov-table"); t.innerHTML = "";
  t.appendChild(el("tr", "", "<th>venue</th><th>how a trade is reconstructed</th><th>data reliability (clean 2h windows)</th>"));
  D.coverage.venues.forEach((c) => {
    const cell = c.clean_pct == null ? "n/a"
      : `${c.clean_pct}% clean (${Math.round(c.clean_pct / 100 * c.windows)} of ${c.windows})`;
    const [name, gloss] = METHOD_DESC[c.method] || [c.method, ""];
    const mcell = `<strong>${name}</strong>${gloss ? `<br><span class="muted mgloss">${gloss}</span>` : ""}`;
    t.appendChild(el("tr", "", `<td>${VLABEL[c.venue]}</td><td>${mcell}</td><td>${cell}</td>`));
  });
  const ul = $("#seams"); ul.innerHTML = "";
  D.coverage.seams.forEach((s) => ul.appendChild(el("li", "", s)));
}

// ---------- Maker analysis ----------
let makerChart, makersRendered = false;
function renderMakerChart(d) {
  const top = d.makers.slice(0, 12);
  if (makerChart) makerChart.destroy();
  makerChart = new Chart($("#mk-chart"), {
    type: "bar",
    data: { labels: top.map((m) => m.short), datasets: [{
      label: "maker volume", backgroundColor: "#5fb3b3", borderRadius: 3,
      maxBarThickness: 22, data: top.map((m) => m.maker_ntl) }] },
    options: { indexAxis: "y", responsive: true,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => ` ${fmtUsd(c.parsed.x)} maker volume` } } },
      scales: {
        x: { title: { display: true, text: `maker volume (${d.days}d)`, color: AXIS },
             ticks: { color: AXIS, callback: (v) => fmtUsd(v) }, grid: { color: GRID } },
        y: { ticks: { color: AXIS }, grid: { display: false } } } },
  });
}
function renderMakers() {
  const d = D.makers; if (!d || !d.makers.length) return;
  const days = d.days;
  $("#mk-sub").textContent =
    `${d.makers.length} makers shown of ${d.makers_seen.toLocaleString()} seen over the last `
    + `${days} days, gated to maker/taker volume ≥ ${d.min_ratio}. Together the makers we observe `
    + `ran ${fmtUsd(d.total_maker_ntl)} of maker-side notional on the tracked coins.`;
  renderMakerChart(d);
  const tips = {
    vol: `Maker-side notional filled over the last ${days} days on the 5 coins we stream; share is of all observed maker notional.`,
    markets: "Distinct markets the maker currently holds a position in (true breadth; the trade tape only sees 5 coins).",
    oi: "Open interest now: the sum of the maker's absolute position notional across all markets.",
    funding: "Net funding over the window: negative = paid out, positive = received.",
    pnl: "Most recent full day's profit or loss, from the public portfolio endpoint.",
    rate: "The maker's actual fee rate. Negative = a rebate (Hyperliquid pays them to provide liquidity).",
    rebate: "Estimated maker rebate earned over the window: maker volume × rebate rate.",
  };
  const t = $("#mk-table"); t.innerHTML = "";
  t.appendChild(el("tr", "",
    `<th>#</th><th>maker</th><th data-tip="${tips.vol}">maker vol (${days}d)</th>`
    + `<th data-tip="${tips.markets}">markets</th><th data-tip="${tips.oi}">OI</th>`
    + `<th>acct value</th><th data-tip="${tips.funding}">funding (${days}d)</th>`
    + `<th data-tip="${tips.pnl}">last-day PnL</th><th data-tip="${tips.rate}">maker rate</th>`
    + `<th data-tip="${tips.rebate}">rebate (${days}d)</th>`));
  for (const m of d.makers) {
    const link = `https://app.hyperliquid.xyz/explorer/address/${m.address}`;
    const share = `<span class="muted">${(m.maker_share * 100).toFixed(1)}%</span>`;
    const sgn = (v) => v == null ? "-" : `<span class="${v >= 0 ? "up" : "down"}">${fmtSigned(v)}</span>`;
    const rate = m.maker_bps == null ? "-"
      : `${m.maker_bps > 0 ? "+" : ""}${m.maker_bps.toFixed(2)} bps${m.maker_bps < 0 ? ' <span class="muted">rebate</span>' : ""}`;
    t.appendChild(el("tr", "",
      `<td>${m.rank}</td>`
      + `<td><a href="${link}" target="_blank" rel="noopener">${m.short}</a></td>`
      + `<td>${fmtUsd(m.maker_ntl)} ${share}</td>`
      + `<td>${m.markets ?? "-"}</td>`
      + `<td>${m.oi == null ? "-" : fmtUsd(m.oi)}</td>`
      + `<td>${m.account_value == null ? "-" : fmtUsd(m.account_value)}</td>`
      + `<td>${sgn(m.funding_net)}</td>`
      + `<td>${sgn(m.last_day_pnl)}</td>`
      + `<td>${rate}</td>`
      + `<td>${m.rebate_window == null ? "-" : fmtUsd(m.rebate_window)}</td>`));
  }
  const asOf = d.as_of
    ? new Date(d.as_of * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : null;
  $("#mk-note").textContent =
    `Volume and share come from the trade tape (5 coins); OI, account value, funding, PnL, and `
    + `fees are polled from Hyperliquid's public per-address endpoints${asOf ? `, as of ${asOf}` : ""}. `
    + `Addresses link to the Hyperliquid explorer.`;
}
function setMode(maker) {
  $("#view-taker").hidden = maker;
  $("#view-maker").hidden = !maker;
  $("#taker-nav").style.display = maker ? "none" : "";
  $("#mode-taker").classList.toggle("on", !maker);
  $("#mode-maker").classList.toggle("on", maker);
  if (maker && !makersRendered) { renderMakers(); makersRendered = true; }
  window.scrollTo(0, 0);
}

// ---------- tooltips (custom; native title is slow and clipped by overflow) ----------
function initTooltips() {
  const tip = el("div"); tip.id = "tip"; document.body.appendChild(tip);
  const place = (target) => {
    const r = target.getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, innerWidth - tw - 8));
    let top = r.top - th - 8;
    if (top < 8) top = r.bottom + 8;
    tip.style.left = left + "px"; tip.style.top = top + "px";
  };
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest("[data-tip]"); if (!t) return;
    tip.textContent = t.getAttribute("data-tip"); tip.style.display = "block";
    place(t); requestAnimationFrame(() => { place(t); tip.classList.add("show"); });
  });
  document.addEventListener("mouseout", (e) => {
    const t = e.target.closest("[data-tip]"); if (!t) return;
    if (e.relatedTarget && t.contains(e.relatedTarget)) return;  // moving within same cell
    tip.classList.remove("show"); tip.style.display = "none";
  });
}

// ---------- wiring ----------
function fillSelect(sel, opts, val, labelFn) {
  sel.innerHTML = ""; opts.forEach((o) => { const e = el("option"); e.value = o; e.textContent = labelFn ? labelFn(o) : o; sel.appendChild(e); });
  if (val != null) sel.value = val;
}
function init() {
  initTooltips();
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

  renderFundTabs(); renderFundTable(); renderFundNote();

  const assetsWithHist = D.meta.assets.filter((a) => D.history[a] && Object.keys(D.history[a]).length);
  fillSelect($("#t-asset"), assetsWithHist.length ? assetsWithHist : ["BTC"], "BTC");
  fillSelect($("#t-rung"), D.calc.rungs, 100000, rungLabel);
  $("#t-asset").onchange = $("#t-rung").onchange = $("#t-metric").onchange = renderTrack;
  renderTrack();

  renderStress(); renderMethod();

  if (D.meta.has_makers && D.makers && D.makers.makers.length) {
    $("#mode-taker").onclick = () => setMode(false);
    $("#mode-maker").onclick = () => setMode(true);
  } else {
    $("#mode-maker").hidden = true;
  }
}

load().then(init).catch((e) => { $("#headline").textContent = "Failed to load data."; console.error(e); });
