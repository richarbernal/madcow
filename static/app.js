import {
  bollinger, movingAverage, rsi, returns, stdev, correlation, maxDrawdown, beta,
} from "./indicators.js";

const LWC = window.LightweightCharts;
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Configuración (persistida en localStorage y parcialmente en la URL)
// ---------------------------------------------------------------------------
const DEFAULTS = {
  symbol: "AAPL",
  compare: "",
  range: "1y",
  interval: "1d",
  chartType: "candles",
  demo: false,
  theme: "dark",
  cmpMode: "perf",
  sideTab: "watch",
  vol: true,
  ext: true, // pre/post-mercado (acciones de EE. UU.)
  bb: { on: true, period: 20, mult: 2, color: "#7e57c2" },
  rsi: { on: true, period: 14, high: 70, low: 30 },
  mas: [
    { on: true, type: "SMA", period: 20, color: "#f7a600" },
    { on: true, type: "SMA", period: 50, color: "#2196f3" },
    { on: true, type: "SMA", period: 200, color: "#e91e63" },
  ],
};
const MA_COLORS = ["#f7a600", "#2196f3", "#e91e63", "#00bcd4", "#8bc34a", "#ff5722"];
const COLOR_A = "#2962ff";
const COLOR_B = "#ff9800";
const STORE_KEY = "madcow.settings.v1";

function loadSettings() {
  let s = structuredClone(DEFAULTS);
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (saved) s = { ...s, ...saved, bb: { ...s.bb, ...saved.bb }, rsi: { ...s.rsi, ...saved.rsi } };
  } catch { /* sin almacenamiento */ }
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("s")) s.symbol = h.get("s").toUpperCase();
  if (h.has("c")) s.compare = (h.get("c") || "").toUpperCase();
  if (h.get("r")) s.range = h.get("r");
  if (h.get("i")) s.interval = h.get("i");
  return s;
}
const S = loadSettings();

function save() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(S)); } catch { /* ignorar */ }
  const h = new URLSearchParams({ s: S.symbol, r: S.range, i: S.interval });
  if (S.compare) h.set("c", S.compare);
  history.replaceState(null, "", "#" + h.toString());
}

// ---------------------------------------------------------------------------
// Utilidades de formato
// ---------------------------------------------------------------------------
const nf = (d = 2) => new Intl.NumberFormat("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "—" : nf(d).format(v));
const pct = (v, d = 2) => (v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + nf(d).format(v * 100) + " %");
const fmtVol = (v) => {
  if (v == null) return "—";
  if (v >= 1e9) return fmt(v / 1e9, 2) + " B";
  if (v >= 1e6) return fmt(v / 1e6, 2) + " M";
  if (v >= 1e3) return fmt(v / 1e3, 1) + " K";
  return String(v);
};
const fmtDate = (t) => {
  const d = new Date(t * 1000);
  const opts = S.interval === "1h"
    ? { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "short", year: "numeric" };
  return d.toLocaleString("es-ES", opts);
};
const annFactor = () => ({ "1h": 252 * 7, "1d": 252, "1wk": 52, "1mo": 12 })[S.interval] || 252;
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------------------------------------------------------------------------
// Datos
// ---------------------------------------------------------------------------
let dataA = null; // { symbol, name, currency, bars, demo }
let dataB = null;
let indA = null;  // indicadores calculados para A

async function fetchSeries(symbol) {
  const q = new URLSearchParams({ symbol, range: S.range, interval: S.interval });
  if (S.demo) q.set("demo", "1");
  if (S.ext && S.interval === "1h") q.set("rth", "0");
  let res = await fetch("/api/chart?" + q);
  let body = await res.json().catch(() => ({}));
  if (!res.ok && body.offline && !S.demo) {
    const reason = `${body.source || "fuente de datos"}: ${body.error || "error desconocido"}`;
    q.set("demo", "1");
    res = await fetch("/api/chart?" + q);
    body = await res.json();
    body.offlineFallback = reason;
  }
  if (!res.ok) throw new Error(body.error || `Error ${res.status}`);
  if (!body.bars?.length) throw new Error(`Sin datos para ${symbol}`);
  return body;
}

function computeIndicators(bars) {
  const closes = bars.map((b) => b.close);
  return {
    closes,
    bb: bollinger(closes, S.bb.period, S.bb.mult),
    mas: S.mas.map((m) => movingAverage(closes, m.period, m.type)),
    rsi: rsi(closes, S.rsi.period),
  };
}

// ---------------------------------------------------------------------------
// Gráficos
// ---------------------------------------------------------------------------
function chartTheme() {
  return {
    layout: { background: { type: "solid", color: cssVar("--bg") }, textColor: cssVar("--text"), fontSize: 11 },
    grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
    rightPriceScale: { borderColor: cssVar("--border"), minimumWidth: 76 },
    timeScale: { borderColor: cssVar("--border"), timeVisible: S.interval === "1h", secondsVisible: false },
  };
}

function makeChart(el) {
  return LWC.createChart(el, {
    autoSize: true,
    localization: { locale: "es-ES", dateFormat: "dd MMM 'yy" },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    handleScroll: true,
    handleScale: true,
    ...chartTheme(),
  });
}

const priceChart = makeChart($("priceChart"));
const rsiChart = makeChart($("rsiChart"));
const cmpChart = makeChart($("cmpChart"));
let priceSeries = []; // series creadas en el gráfico de precio
let mainSeries = null;
let rsiSeries = null;
let cmpSeries = [];

// Sincroniza desplazamiento/zoom entre precio y RSI
let syncing = false;
function syncRange(from, to) {
  from.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (syncing || !r) return;
    syncing = true;
    to.timeScale().setVisibleLogicalRange(r);
    syncing = false;
  });
}
syncRange(priceChart, rsiChart);
syncRange(rsiChart, priceChart);

const toLine = (bars, vals) => bars.map((b, i) => (vals[i] == null ? { time: b.time } : { time: b.time, value: vals[i] }));

function clearSeries(chart, list) {
  for (const s of list) chart.removeSeries(s);
  list.length = 0;
}

function renderPrice(keepRange = true) {
  if (!dataA) return;
  const range = keepRange ? priceChart.timeScale().getVisibleLogicalRange() : null;
  clearSeries(priceChart, priceSeries);
  const bars = dataA.bars;
  const up = cssVar("--up"), down = cssVar("--down");

  if (S.vol) {
    const vs = priceChart.addHistogramSeries({
      priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
    });
    priceChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    vs.setData(bars.map((b) => ({
      time: b.time, value: b.volume,
      color: (b.close >= b.open ? up : down) + (b.ext ? "22" : "55"),
    })));
    priceSeries.push(vs);
  }

  if (S.bb.on) {
    const c = S.bb.color;
    const common = { color: c, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false };
    const u = priceChart.addLineSeries(common);
    const m = priceChart.addLineSeries({ ...common, lineStyle: LWC.LineStyle.Dashed });
    const l = priceChart.addLineSeries(common);
    u.setData(toLine(bars, indA.bb.upper));
    m.setData(toLine(bars, indA.bb.mid));
    l.setData(toLine(bars, indA.bb.lower));
    priceSeries.push(u, m, l);
  }

  S.mas.forEach((ma, i) => {
    if (!ma.on) return;
    const s = priceChart.addLineSeries({
      color: ma.color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
    });
    s.setData(toLine(bars, indA.mas[i]));
    priceSeries.push(s);
  });

  if (S.chartType === "line") {
    mainSeries = priceChart.addAreaSeries({
      lineColor: COLOR_A, topColor: COLOR_A + "44", bottomColor: COLOR_A + "05", lineWidth: 2,
    });
    mainSeries.setData(bars.map((b) => ({ time: b.time, value: b.close })));
  } else {
    mainSeries = priceChart.addCandlestickSeries({
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
    });
    // Velas de pre/post-mercado atenuadas, como en TradingView
    mainSeries.setData(bars.map((b) => {
      if (!b.ext) return b;
      const c = (b.close >= b.open ? up : down) + "66";
      return { ...b, color: c, borderColor: c, wickColor: c };
    }));
  }
  priceSeries.push(mainSeries);
  costLine = null; // la línea anterior se fue con la serie
  updatePositionOverlay();
  priceChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: S.vol ? 0.2 : 0.06 } });

  renderRsi();
  if (range) {
    priceChart.timeScale().setVisibleLogicalRange(range);
  } else {
    priceChart.timeScale().fitContent();
    rsiChart.timeScale().fitContent();
  }
  updateLegend(null);
}

function renderRsi() {
  $("rsiPane").hidden = !S.rsi.on;
  if (rsiSeries) { rsiChart.removeSeries(rsiSeries); rsiSeries = null; }
  if (!S.rsi.on || !dataA) return;
  rsiSeries = rsiChart.addLineSeries({ color: "#ab47bc", lineWidth: 2, priceLineVisible: false });
  rsiSeries.setData(toLine(dataA.bars, indA.rsi));
  const pl = (price, color, style) => rsiSeries.createPriceLine({
    price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title: "",
  });
  pl(S.rsi.high, cssVar("--down"), LWC.LineStyle.Dashed);
  pl(S.rsi.low, cssVar("--up"), LWC.LineStyle.Dashed);
  pl(50, cssVar("--muted"), LWC.LineStyle.Dotted);
  rsiSeries.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
  rsiChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.1, bottom: 0.05 } });
}

// ---------------------------------------------------------------------------
// Leyendas y crosshair
// ---------------------------------------------------------------------------
let timeIndex = new Map();

function legendItem(color, label, value) {
  return `<span class="item">${color ? `<i class="dot" style="background:${color}"></i>` : ""}${label} <b>${value}</b></span>`;
}

function updateLegend(idx) {
  if (!dataA) return;
  const bars = dataA.bars;
  const i = idx == null ? bars.length - 1 : idx;
  const b = bars[i];
  if (!b) return;
  const cls = b.close >= b.open ? "up" : "down";
  let html = `<span class="item"><b>${esc(dataA.symbol)}</b> <span class="muted">${fmtDate(b.time)}${b.ext ? " · fuera de horario" : ""}</span></span>`;
  html += `<span class="item ${cls}">A ${fmt(b.open)} M ${fmt(b.high)} m ${fmt(b.low)} C ${fmt(b.close)}</span>`;
  if (S.vol) html += legendItem(null, "Vol", fmtVol(b.volume));
  if (S.bb.on) {
    html += legendItem(S.bb.color, `BB(${S.bb.period}, ${S.bb.mult})`,
      `${fmt(indA.bb.upper[i])} / ${fmt(indA.bb.mid[i])} / ${fmt(indA.bb.lower[i])}`);
  }
  S.mas.forEach((m, k) => { if (m.on) html += legendItem(m.color, `${m.type} ${m.period}`, fmt(indA.mas[k][i])); });
  $("priceLegend").innerHTML = html;
  if (S.rsi.on) $("rsiLegend").innerHTML = legendItem("#ab47bc", `RSI(${S.rsi.period})`, fmt(indA.rsi[i]));
}

let crossSyncing = false;
priceChart.subscribeCrosshairMove((p) => {
  if (!dataA) return;
  const idx = p.time != null ? timeIndex.get(p.time) : null;
  updateLegend(idx);
  if (crossSyncing || !rsiSeries) return;
  crossSyncing = true;
  const v = idx != null ? indA.rsi[idx] : null;
  if (p.time != null && v != null) rsiChart.setCrosshairPosition(v, p.time, rsiSeries);
  else rsiChart.clearCrosshairPosition();
  crossSyncing = false;
});
rsiChart.subscribeCrosshairMove((p) => {
  if (!dataA) return;
  const idx = p.time != null ? timeIndex.get(p.time) : null;
  updateLegend(idx);
  if (crossSyncing || !mainSeries) return;
  crossSyncing = true;
  if (idx != null) mainSeries && priceChart.setCrosshairPosition(dataA.bars[idx].close, p.time, mainSeries);
  else priceChart.clearCrosshairPosition();
  crossSyncing = false;
});

// ---------------------------------------------------------------------------
// Cabecera de cotización y señales
// ---------------------------------------------------------------------------
function renderQuote() {
  const bars = dataA.bars;
  const last = bars[bars.length - 1], prev = bars[bars.length - 2] || last;
  const first = bars[0];
  const ch = last.close / prev.close - 1;
  const chPeriod = last.close / first.close - 1;
  $("qSymbol").textContent = dataA.symbol;
  $("qName").textContent = [dataA.name, dataA.exchange].filter(Boolean).join(" · ");
  $("qLast").textContent = `${fmt(last.close)} ${dataA.currency || ""}`;
  $("qChange").innerHTML =
    `<span class="${ch >= 0 ? "up" : "down"}">${pct(ch)}</span> ` +
    `<span class="muted small">periodo: </span><span class="${chPeriod >= 0 ? "up" : "down"} small">${pct(chPeriod)}</span>`;
  document.title = `${dataA.symbol} ${fmt(last.close)} · MadCow Charts`;
}

const tag = (text, kind) => `<span class="tag ${kind}">${text}</span>`;

function bbInfo(closes, bb, i = closes.length - 1) {
  const u = bb.upper[i], l = bb.lower[i], m = bb.mid[i], c = closes[i];
  if (u == null) return null;
  return { pctB: (c - l) / (u - l), width: (u - l) / m, c, u, l };
}

function lastCross(fast, slow, bars) {
  for (let i = fast.length - 1; i > 0; i--) {
    if ([fast[i], slow[i], fast[i - 1], slow[i - 1]].some((v) => v == null)) return null;
    const now = fast[i] - slow[i], before = fast[i - 1] - slow[i - 1];
    if (now > 0 && before <= 0) return { type: "alcista", time: bars[i].time };
    if (now < 0 && before >= 0) return { type: "bajista", time: bars[i].time };
  }
  return null;
}

function renderSignals() {
  const { closes } = indA;
  const n = closes.length - 1;
  const c = closes[n];
  const rows = [];

  if (S.rsi.on) {
    const r = indA.rsi[n];
    const k = r == null ? tag("n/d", "neutral")
      : r >= S.rsi.high ? tag("Sobrecompra", "down")
      : r <= S.rsi.low ? tag("Sobreventa", "up") : tag("Neutral", "neutral");
    rows.push([`RSI(${S.rsi.period})`, `${fmt(r, 1)} ${k}`]);
  }
  if (S.bb.on) {
    const b = bbInfo(closes, indA.bb);
    if (b) {
      const k = b.c > b.u ? tag("Sobre banda sup.", "down") : b.c < b.l ? tag("Bajo banda inf.", "up") : tag("Dentro", "neutral");
      rows.push(["%B Bollinger", `${fmt(b.pctB, 2)} ${k}`]);
      rows.push(["Ancho de bandas", pct(b.width).replace("+", "")]);
    } else rows.push(["Bollinger", tag("Pocos datos", "neutral")]);
  }
  const active = S.mas.map((m, i) => ({ ...m, vals: indA.mas[i] })).filter((m) => m.on);
  for (const m of active) {
    const v = m.vals[n];
    if (v == null) { rows.push([`${m.type} ${m.period}`, tag("Pocos datos", "neutral")]); continue; }
    const d = c / v - 1;
    rows.push([`<i class="dot" style="background:${m.color};display:inline-block;width:8px;height:8px;border-radius:50%"></i> vs ${m.type} ${m.period}`,
      `${pct(d)} ${tag(d >= 0 ? "Encima" : "Debajo", d >= 0 ? "up" : "down")}`]);
  }
  const sorted = [...active].sort((a, b) => a.period - b.period);
  if (sorted.length >= 2) {
    const f = sorted[0], s = sorted[sorted.length - 1];
    const x = lastCross(f.vals, s.vals, dataA.bars);
    rows.push([`Cruce ${f.period}/${s.period}`, x ? `${tag(x.type === "alcista" ? "Alcista" : "Bajista", x.type === "alcista" ? "up" : "down")} <span class="muted small">${fmtDate(x.time)}</span>` : tag("Sin cruce", "neutral")]);
  }
  $("signalList").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
}

// ---------------------------------------------------------------------------
// Comparación
// ---------------------------------------------------------------------------
function alignKey(t) {
  if (S.interval === "1h") return t;
  const d = new Date(t * 1000);
  if (S.interval === "1mo") return d.getUTCFullYear() * 100 + d.getUTCMonth();
  if (S.interval === "1wk") {
    // agrupa por semana ISO (lunes)
    const day = (d.getUTCDay() + 6) % 7;
    return Math.floor((t - day * 86400) / 86400 / 7);
  }
  return d.toISOString().slice(0, 10);
}

function alignSeries(a, b) {
  const mapB = new Map(b.bars.map((x) => [alignKey(x.time), x]));
  const out = [];
  for (const x of a.bars) {
    const y = mapB.get(alignKey(x.time));
    if (y) out.push({ time: x.time, a: x.close, b: y.close });
  }
  return out;
}

function renderCompare() {
  const block = $("compareBlock");
  $("clearCompare").hidden = !S.compare;
  if (!S.compare || !dataB || !dataA) { block.hidden = true; return; }
  block.hidden = false;
  $("cmpTitle").innerHTML =
    `<span style="color:${COLOR_A}">${esc(dataA.symbol)}</span> vs <span style="color:${COLOR_B}">${esc(dataB.symbol)}</span>`;
  document.querySelectorAll("#cmpModeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.cmpMode));

  const al = alignSeries(dataA, dataB);
  clearSeries(cmpChart, cmpSeries);
  if (al.length < 2) {
    $("cmpLegend").textContent = "No hay fechas en común entre ambos símbolos.";
    $("statsTable").innerHTML = "";
    return;
  }
  const A = al.map((x) => x.a), B = al.map((x) => x.b);
  const pctFmt = { type: "custom", formatter: (v) => nf(2).format(v) + " %", minMove: 0.01 };
  const line = (color, data, extra = {}) => {
    const s = cmpChart.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, ...extra });
    s.setData(data);
    cmpSeries.push(s);
    return s;
  };

  let legend = "";
  if (S.cmpMode === "perf") {
    const pa = A.map((v) => (v / A[0] - 1) * 100), pb = B.map((v) => (v / B[0] - 1) * 100);
    const sa = line(COLOR_A, al.map((x, i) => ({ time: x.time, value: pa[i] })), { priceFormat: pctFmt });
    line(COLOR_B, al.map((x, i) => ({ time: x.time, value: pb[i] })), { priceFormat: pctFmt });
    sa.createPriceLine({ price: 0, color: cssVar("--muted"), lineWidth: 1, lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: false });
    legend = legendItem(COLOR_A, dataA.symbol, pct(pa.at(-1) / 100)) + legendItem(COLOR_B, dataB.symbol, pct(pb.at(-1) / 100));
  } else if (S.cmpMode === "ratio") {
    const ratio = A.map((v, i) => v / B[i]);
    line(COLOR_A, al.map((x, i) => ({ time: x.time, value: ratio[i] })), { priceFormat: { type: "price", precision: 4, minMove: 0.0001 } });
    const avg = movingAverage(ratio, Math.min(50, Math.max(2, Math.floor(ratio.length / 4))));
    line(cssVar("--muted"), al.map((x, i) => (avg[i] == null ? { time: x.time } : { time: x.time, value: avg[i] })),
      { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, lastValueVisible: false, crosshairMarkerVisible: false });
    legend = legendItem(COLOR_A, `${dataA.symbol} / ${dataB.symbol}`, fmt(ratio.at(-1), 4)) +
      `<span class="item muted">Sube = ${esc(dataA.symbol)} lo hace mejor</span>`;
  } else {
    const ra = rsi(A, S.rsi.period), rb = rsi(B, S.rsi.period);
    const sa = line(COLOR_A, toLine(al, ra));
    line(COLOR_B, toLine(al, rb));
    for (const [p, c] of [[S.rsi.high, cssVar("--down")], [S.rsi.low, cssVar("--up")]]) {
      sa.createPriceLine({ price: p, color: c, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
    }
    for (const s of cmpSeries) s.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
    legend = legendItem(COLOR_A, `RSI ${dataA.symbol}`, fmt(ra.at(-1), 1)) + legendItem(COLOR_B, `RSI ${dataB.symbol}`, fmt(rb.at(-1), 1));
  }
  $("cmpLegend").innerHTML = legend;
  cmpChart.timeScale().fitContent();
  renderStats(A, B);
}

function renderStats(A, B) {
  const f = annFactor();
  const stat = (closes, data) => {
    const r = returns(closes);
    const ret = closes.at(-1) / closes[0] - 1;
    const vol = stdev(r) * Math.sqrt(f);
    const meanR = r.reduce((s, x) => s + x, 0) / r.length;
    const full = data.bars.map((b) => b.close);
    const rs = rsi(full, S.rsi.period).at(-1);
    const b = bbInfo(full, bollinger(full, S.bb.period, S.bb.mult));
    const mas = S.mas.filter((m) => m.on).map((m) => {
      const v = movingAverage(full, m.period, m.type).at(-1);
      return v == null ? null : full.at(-1) / v - 1;
    });
    return { last: full.at(-1), cur: data.currency, ret, vol, sharpe: (meanR * f) / vol, mdd: maxDrawdown(closes), rsi: rs, pctB: b?.pctB, mas };
  };
  const a = stat(A, dataA), b = stat(B, dataB);
  const rA = returns(A), rB = returns(B);
  const corr = correlation(rA, rB);
  const bet = beta(rA, rB);

  // marca en negrita el mejor valor (mayor es mejor salvo volatilidad)
  const best = (va, vb, higher = true) => {
    if (!isFinite(va) || !isFinite(vb) || va === vb) return ["", ""];
    const aWins = higher ? va > vb : va < vb;
    return aWins ? ["best", ""] : ["", "best"];
  };
  const colored = (v, s) => `<span class="${v >= 0 ? "up" : "down"}">${s}</span>`;
  const rows = [
    ["Último precio", `${fmt(a.last)} ${a.cur}`, `${fmt(b.last)} ${b.cur}`],
    ["Rentabilidad (periodo común)", colored(a.ret, pct(a.ret)), colored(b.ret, pct(b.ret)), best(a.ret, b.ret)],
    ["Volatilidad anualizada", pct(a.vol).replace("+", ""), pct(b.vol).replace("+", ""), best(a.vol, b.vol, false)],
    ["Máx. drawdown", pct(a.mdd), pct(b.mdd), best(a.mdd, b.mdd)],
    ["Rentab./riesgo (Sharpe, rf=0)", fmt(a.sharpe), fmt(b.sharpe), best(a.sharpe, b.sharpe)],
  ];
  if (S.rsi.on) rows.push([`RSI(${S.rsi.period})`, fmt(a.rsi, 1), fmt(b.rsi, 1)]);
  if (S.bb.on) rows.push([`%B Bollinger(${S.bb.period})`, fmt(a.pctB), fmt(b.pctB)]);
  S.mas.filter((m) => m.on).forEach((m, i) => {
    rows.push([`Distancia a ${m.type} ${m.period}`, a.mas[i] == null ? "—" : colored(a.mas[i], pct(a.mas[i])),
      b.mas[i] == null ? "—" : colored(b.mas[i], pct(b.mas[i]))]);
  });

  const head = `<thead><tr><th></th><th style="color:${COLOR_A}">${esc(dataA.symbol)}</th><th style="color:${COLOR_B}">${esc(dataB.symbol)}</th></tr></thead>`;
  const body = rows.map(([k, va, vb, cls = ["", ""]]) =>
    `<tr><td>${k}</td><td class="${cls[0]}">${va}</td><td class="${cls[1]}">${vb}</td></tr>`).join("");
  const extra =
    `<tr><td>Correlación de rendimientos</td><td colspan="2">${fmt(corr)}</td></tr>` +
    `<tr><td>Beta de ${esc(dataA.symbol)} respecto a ${esc(dataB.symbol)}</td><td colspan="2">${fmt(bet)}</td></tr>`;
  $("statsTable").innerHTML = head + "<tbody>" + body + extra + "</tbody>";
}

// ---------------------------------------------------------------------------
// Watchlist (favoritos): se guarda en el servidor, en watchlist.json
// ---------------------------------------------------------------------------
const SYMBOL_RE = /^[A-Z0-9.\-^=]{1,20}$/;
const WL = { symbols: [], quotes: new Map(), loaded: false, updated: null };
let dragSym = null;

async function wlFetch() {
  try {
    const res = await fetch("/api/watchlist");
    if (!res.ok) throw new Error(res.status);
    WL.symbols = (await res.json()).symbols || [];
  } catch {
    banner("No se pudo leer la watchlist del servidor.", "error");
  }
  WL.loaded = true;
  renderWatchlist();
  refreshQuotes();
}

// Cambia la lista y la guarda; si el servidor falla, se deshace el cambio.
async function wlSet(next) {
  const prev = WL.symbols;
  WL.symbols = next;
  renderWatchlist();
  try {
    const res = await fetch("/api/watchlist", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: next }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `error ${res.status}`);
    WL.symbols = body.symbols;
  } catch (e) {
    WL.symbols = prev;
    banner(`No se pudo guardar la watchlist: ${e.message}`, "error");
  }
  renderWatchlist();
}

function wlAdd(sym) {
  sym = sym.trim().toUpperCase();
  if (!sym) return;
  if (!SYMBOL_RE.test(sym)) { banner(`Símbolo no válido: ${sym}`, "error"); return; }
  if (WL.symbols.includes(sym)) { highlightRow(sym); return; }
  wlSet([...WL.symbols, sym]).then(() => refreshQuotes([sym]));
}
const wlRemove = (sym) => wlSet(WL.symbols.filter((s) => s !== sym));
const wlToggle = (sym) => (WL.symbols.includes(sym) ? wlRemove(sym) : wlAdd(sym));

function highlightRow(sym) {
  const li = [...$("watchList").children].find((x) => x.dataset.sym === sym);
  if (!li) return;
  li.scrollIntoView({ block: "nearest" });
  li.animate([{ background: "rgba(245, 179, 1, .35)" }, { background: "transparent" }], 900);
}

let quotesToken = 0;
async function refreshQuotes(only = null) {
  const syms = only || WL.symbols;
  if (!syms.length) return;
  const token = only ? quotesToken : ++quotesToken;
  try {
    const q = new URLSearchParams({ symbols: syms.join(",") });
    if (S.demo) q.set("demo", "1");
    if (S.ext) q.set("ext", "1");
    const res = await fetch("/api/quotes?" + q);
    const body = await res.json();
    if (token !== quotesToken) return; // llegó una petición más reciente
    for (const x of body.quotes || []) WL.quotes.set(x.symbol, x);
    if (!only) WL.updated = new Date();
  } catch { /* se reintenta en el siguiente ciclo */ }
  renderWatchlist();
}

const fmtStamp = (ts) => new Date(ts * 1000).toLocaleString("es-ES",
  { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtPrice = (v) => fmt(v, v >= 10000 ? 0 : v < 1 ? 4 : 2);

function sparkline(vals) {
  const w = 40, h = 20;
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const pts = vals.map((v, i) =>
    `${((i / Math.max(1, vals.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`).join(" ");
  const color = vals.at(-1) >= vals[0] ? cssVar("--up") : cssVar("--down");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">` +
    `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

function renderWatchlist() {
  const star = $("starBtn");
  const fav = WL.symbols.includes(S.symbol);
  star.textContent = fav ? "★" : "☆";
  star.classList.toggle("on", fav);
  star.setAttribute("aria-pressed", String(fav));
  star.title = fav ? "Quitar de favoritos" : "Añadir a favoritos";
  $("watchEmpty").hidden = !WL.loaded || WL.symbols.length > 0;
  $("watchUpdated").textContent = WL.updated
    ? "Actualizado " + WL.updated.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" }) : "";
  if (dragSym) return; // no rehacer la lista a mitad de un arrastre

  $("watchList").innerHTML = WL.symbols.map((sym) => {
    const q = WL.quotes.get(sym);
    const ok = q && !q.error && q.closes?.length;
    let cells;
    if (!q) cells = `<span class="err">cargando…</span>`;
    else if (!ok) {
      const msg = q.error || "Sin datos";
      const short = /permis/i.test(msg) ? "Sin permisos de datos" : /no reconoce|no encontrado|no válido|no soportado/i.test(msg)
        ? "Símbolo no encontrado" : "Error al cargar";
      cells = `<span class="err" title="${esc(msg)}">⚠ ${short}</span>`;
    }
    else {
      const c = q.closes, last = c.at(-1), prev = c.at(-2) ?? last, chg = last / prev - 1;
      const r = rsi(c, S.rsi.period).at(-1);
      const rc = r == null ? "" : r >= S.rsi.high ? "down" : r <= S.rsi.low ? "up" : "";
      const e = S.ext ? q.ext : null;
      const [icon, label] = e ? EXT_LABELS[e.session] || EXT_LABELS.post : [];
      const extPrice = e ? `<small class="ext" title="${label} · ${fmtExtTime(e.time)}">${icon} ${fmtPrice(e.price)}</small>` : "";
      const extChg = e ? `<small class="ext ${e.change >= 0 ? "up" : "down"}">${pct(e.change)}</small>` : "";
      cells = sparkline(c.slice(-30)) +
        `<span class="num">${fmtPrice(last)}${extPrice}</span>` +
        `<span class="num ${chg >= 0 ? "up" : "down"}">${pct(chg)}${extChg}</span>` +
        `<span class="num ${rc}" title="RSI(${S.rsi.period})">${fmt(r, 0)}</span>`;
    }
    const name = ok ? q.name.replace(" (simulado)", "") : "";
    const cls = ["watch-row", sym === S.symbol && "active", sym === S.compare && "cmp"].filter(Boolean).join(" ");
    return `<li class="${cls}" draggable="true" data-sym="${esc(sym)}" title="${esc(name || sym)}">
      <span class="sym"><b>${esc(sym)}</b><small>${q?.demo ? `<span class="sim" title="Datos simulados">SIM</span>` : ""}${q?.stale ? `<span class="sim" title="Sin conexión: datos guardados del ${fmtStamp(q.updated_at)}">CACHÉ</span>` : ""}${esc(name)}</small></span>
      ${cells}
      <span class="actions">
        <button type="button" data-act="cmp" title="Comparar ${esc(S.symbol)} con ${esc(sym)}">vs</button>
        <button type="button" data-act="rm" class="rm" title="Quitar de favoritos">✕</button>
      </span></li>`;
  }).join("");
}

{
  const list = $("watchList");
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-sym]");
    if (!li) return;
    const sym = li.dataset.sym;
    const act = e.target.closest("button")?.dataset.act;
    if (act === "rm") return wlRemove(sym);
    if (act === "cmp") {
      if (sym === S.symbol) return;
      S.compare = sym;
      load().then(() => $("compareBlock").scrollIntoView({ behavior: "smooth", block: "start" }));
      return;
    }
    if (sym !== S.symbol) { S.symbol = sym; load(); }
  });

  // Reordenar arrastrando
  const clearMarks = () => list.querySelectorAll(".drop-before, .drop-after")
    .forEach((x) => x.classList.remove("drop-before", "drop-after"));
  const endDrag = () => { dragSym = null; clearMarks(); renderWatchlist(); };
  list.addEventListener("dragstart", (e) => {
    const li = e.target.closest("li[data-sym]");
    if (!li) return;
    dragSym = li.dataset.sym;
    li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragSym);
  });
  list.addEventListener("dragover", (e) => {
    if (!dragSym) return;
    e.preventDefault();
    clearMarks();
    const li = e.target.closest("li[data-sym]");
    if (!li || li.dataset.sym === dragSym) return;
    const r = li.getBoundingClientRect();
    li.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
  });
  list.addEventListener("drop", (e) => {
    e.preventDefault();
    const li = e.target.closest("li[data-sym]");
    const moving = dragSym;
    if (moving && li && li.dataset.sym !== moving) {
      const after = li.classList.contains("drop-after");
      const next = WL.symbols.filter((s) => s !== moving);
      next.splice(next.indexOf(li.dataset.sym) + (after ? 1 : 0), 0, moving);
      dragSym = null;
      clearMarks();
      wlSet(next);
    } else endDrag();
  });
  list.addEventListener("dragend", () => { if (dragSym) endDrag(); });

  $("starBtn").addEventListener("click", () => wlToggle(S.symbol));
  $("watchRefresh").addEventListener("click", () => refreshQuotes());

  // ↑ / ↓ recorren la watchlist (salvo si estás escribiendo en un campo)
  document.addEventListener("keydown", (e) => {
    if ((e.key !== "ArrowUp" && e.key !== "ArrowDown") || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.closest?.("input, select, textarea") || !WL.symbols.length) return;
    e.preventDefault();
    const n = WL.symbols.length, i = WL.symbols.indexOf(S.symbol);
    const j = e.key === "ArrowDown" ? (i < 0 ? 0 : Math.min(n - 1, i + 1)) : (i < 0 ? n - 1 : Math.max(0, i - 1));
    if (WL.symbols[j] !== S.symbol) { S.symbol = WL.symbols[j]; load(); }
    list.children[j]?.scrollIntoView({ block: "nearest" });
  });

  // Refresco automático cada minuto mientras la pestaña esté visible
  setInterval(() => { if (document.visibilityState === "visible") { refreshQuotes(); loadExt(); loadPositions(); } }, 60_000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && (!WL.updated || Date.now() - WL.updated > 60_000)) refreshQuotes();
  });
}

// ---------------------------------------------------------------------------
// Pre/post-mercado del símbolo principal (solo acciones de EE. UU. con IBKR)
// ---------------------------------------------------------------------------
const EXT_LABELS = { pre: ["☀", "Pre-mercado"], post: ["☾", "Post-mercado"], overnight: ["☾", "Nocturno"] };
let extA = null;

function fmtExtTime(ts) {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString("es-ES", { weekday: "short" }) + " " + hm;
}

function renderExt() {
  const el = $("qExt");
  if (!extA || !S.ext || !dataA) { el.hidden = true; return; }
  const [icon, label] = EXT_LABELS[extA.session] || EXT_LABELS.post;
  el.hidden = false;
  el.innerHTML = `${icon} ${label}: <b>${fmtPrice(extA.price)} ${esc(dataA.currency || "")}</b> ` +
    `<span class="${extA.change >= 0 ? "up" : "down"}">${pct(extA.change)}</span> ` +
    `<span class="muted" title="Hora de la última vela de 5 minutos">· ${fmtExtTime(extA.time)}</span>`;
}

async function loadExt() {
  const sym = S.symbol;
  let ext = null;
  if (S.ext && !S.demo && dataA && !dataA.demo) {
    try {
      const res = await fetch("/api/quotes?" + new URLSearchParams({ symbols: sym, ext: "1" }));
      ext = (await res.json()).quotes?.[0]?.ext || null;
    } catch { /* se reintenta en el siguiente ciclo */ }
  }
  if (sym !== S.symbol) return; // el usuario ya cambió de símbolo
  extA = ext;
  renderExt();
}

// ---------------------------------------------------------------------------
// Cartera: posiciones de la cuenta de IBKR (valoración calculada por IBKR)
// ---------------------------------------------------------------------------
let POS = null;       // última respuesta de /api/positions
let costLine = null;  // línea de coste medio en el gráfico

function fmtMoney(v, cur, dec) {
  if (v == null || !isFinite(v)) return "—";
  const d = dec ?? (Math.abs(v) >= 1000 ? 0 : 2);
  try {
    return new Intl.NumberFormat("es-ES", { style: "currency", currency: cur || "EUR",
      minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
  } catch { return `${fmt(v, d)} ${cur || ""}`; }
}
const signedMoney = (v, cur) => (v > 0 ? "+" : "") + fmtMoney(v, cur);
const fmtQty = (q) => nf(Number.isInteger(q) ? 0 : 2).format(q);
const tone = (v) => ((v ?? 0) >= 0 ? "up" : "down");
const positionFor = (sym) => (POS?.available ? POS.positions.find((p) => p.symbol === sym && p.type === "STK") : null);

async function loadPositions() {
  try {
    const res = await fetch("/api/positions");
    POS = await res.json();
  } catch {
    POS = { available: false, reason: "No se pudo contactar con el servidor de la app." };
  }
  renderPositions();
  updatePositionOverlay();
}

function renderPositions() {
  const list = $("posList"), msg = $("posMsg");
  if (!POS?.available) {
    list.innerHTML = "";
    $("posSummary").innerHTML = "";
    $("posUpdated").textContent = "";
    msg.hidden = false;
    msg.textContent = POS ? POS.reason : "Cargando posiciones…";
    return;
  }
  const base = POS.base;
  $("posSummary").innerHTML = [
    ["Valor de las posiciones", fmtMoney(POS.total_value, base), ""],
    ["P&G latente", `<span class="${tone(POS.total_pnl)}">${signedMoney(POS.total_pnl, base)}</span>`,
      POS.total_pnl_pct == null ? "" : `<span class="${tone(POS.total_pnl_pct)}">${pct(POS.total_pnl_pct)}</span>`],
    ["Valor liquidativo", fmtMoney(POS.net_liquidation, base), ""],
    ["Efectivo", fmtMoney(POS.cash, base), ""],
  ].map(([k, v, extra]) => `<div class="kpi"><small>${k}</small><b>${v}</b> ${extra}</div>`).join("");

  const notes = [];
  if (!POS.positions.length) notes.push("No tienes posiciones abiertas.");
  if (POS.unvalued) notes.push(`${POS.unvalued} posición(es) aún sin valorar: TWS envía la valoración cada ~3 minutos.`);
  if (POS.multi_account) notes.push("Tienes varias cuentas: IBKR solo valora la principal.");
  msg.hidden = !notes.length;
  msg.textContent = notes.join(" ");

  list.innerHTML = POS.positions.map((p) => {
    const cls = ["pos-row", p.symbol && "link", p.symbol && p.symbol === S.symbol && "active"].filter(Boolean).join(" ");
    const kind = p.type !== "STK" ? `<span class="kind">${esc(p.type)}</span>` : "";
    const title = [p.name || p.label, p.account && `cuenta ${p.account}`].filter(Boolean).join(" · ");
    return `<li class="${cls}"${p.symbol ? ` data-sym="${esc(p.symbol)}"` : ""} title="${esc(title)}">
      <span><b>${esc(p.symbol || p.label)}${kind}</b><small>${fmtQty(p.qty)} × ${fmtMoney(p.price, p.currency, 2)}</small></span>
      <span class="num"><b>${fmtMoney(p.value, p.currency)}</b><small title="Coste medio">${fmtMoney(p.avg_price, p.currency, 2)}</small></span>
      <span class="num"><b class="${tone(p.pnl)}">${p.pnl == null ? "—" : signedMoney(p.pnl, p.currency)}</b>
        <small class="${tone(p.pnl_pct)}">${p.pnl_pct == null ? "—" : pct(p.pnl_pct)}</small></span></li>`;
  }).join("");
  $("posUpdated").textContent = "Actualizado " +
    new Date(POS.time * 1000).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
}

// Coste medio en el gráfico y resumen de tu posición junto a la cotización
function updatePositionOverlay() {
  if (costLine && mainSeries) { try { mainSeries.removePriceLine(costLine); } catch { /* serie ya rehecha */ } }
  costLine = null;
  const p = positionFor(S.symbol), el = $("qPos");
  if (!p || !mainSeries || !dataA || dataA.demo || p.currency !== dataA.currency) { el.hidden = true; return; }
  costLine = mainSeries.createPriceLine({
    price: p.avg_price, color: "#f5b301", lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
    axisLabelVisible: true, title: "Coste medio",
  });
  el.hidden = false;
  el.innerHTML = `<span class="muted">Tu posición:</span> ${fmtQty(p.qty)} · coste ${fmtMoney(p.avg_price, p.currency, 2)} · ` +
    `<span class="${tone(p.pnl)}">${p.pnl == null ? "—" : signedMoney(p.pnl, p.currency)}` +
    `${p.pnl_pct == null ? "" : ` (${pct(p.pnl_pct)})`}</span>`;
}

$("posList").addEventListener("click", (e) => {
  const sym = e.target.closest("li[data-sym]")?.dataset.sym;
  if (sym && sym !== S.symbol) { S.symbol = sym; load(); }
});
$("posRefresh").addEventListener("click", () => loadPositions());

// ---------------------------------------------------------------------------
// Carga y render global
// ---------------------------------------------------------------------------
function banner(msg, kind = "warn") {
  const el = $("banner");
  el.hidden = !msg;
  el.className = "banner" + (kind === "error" ? " error" : "");
  el.textContent = msg || "";
}

function recompute(keepRange = true) {
  if (!dataA) return;
  indA = computeIndicators(dataA.bars);
  timeIndex = new Map(dataA.bars.map((b, i) => [b.time, i]));
  renderPrice(keepRange);
  renderQuote();
  renderSignals();
  renderCompare();
  renderWatchlist();
  renderPositions();
}

let loadToken = 0;
async function load() {
  const token = ++loadToken;
  save();
  syncControls();
  renderWatchlist();
  if (dataA && dataA.symbol !== S.symbol) { extA = null; renderExt(); }
  document.body.classList.add("loading");
  const applyTheme = chartTheme();
  for (const c of [priceChart, rsiChart, cmpChart]) c.applyOptions(applyTheme);
  try {
    const [a, b] = await Promise.all([
      fetchSeries(S.symbol),
      S.compare ? fetchSeries(S.compare).catch((e) => ({ error: e.message })) : Promise.resolve(null),
    ]);
    if (token !== loadToken) return;
    dataA = a;
    dataB = b && !b.error ? b : null;
    const msgs = [];
    const offline = a.offlineFallback || b?.offlineFallback;
    const stale = [a, b].find((x) => x?.stale);
    if (offline) msgs.push(`Sin conexión con los datos reales (${offline}). Se muestran DATOS SIMULADOS.`);
    else if (stale) msgs.push(`Sin conexión con ${stale.source} (${stale.stale_reason}). ` +
      `Se muestran los datos guardados (última actualización: ${fmtStamp(stale.updated_at)}).`);
    else if (a.demo) msgs.push("Modo demo: datos simulados, no reales.");
    if (b?.error) msgs.push(`No se pudo cargar ${S.compare}: ${b.error}`);
    banner(msgs.join(" "), b?.error ? "error" : "warn");
    $("dataSource").textContent = a.demo ? "simulados" : (a.source || "—") + (a.stale ? " (datos guardados)" : "");
    recompute(false);
    loadExt();
  } catch (e) {
    if (token !== loadToken) return;
    banner(`No se pudo cargar ${S.symbol}: ${e.message}`, "error");
  } finally {
    if (token === loadToken) document.body.classList.remove("loading");
  }
}

// ---------------------------------------------------------------------------
// Controles
// ---------------------------------------------------------------------------
function syncControls() {
  $("symbolInput").value = S.symbol;
  $("compareInput").value = S.compare;
  $("intervalSelect").value = S.interval;
  $("demoToggle").checked = S.demo;
  document.querySelectorAll("#rangeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.range === S.range));
  document.querySelectorAll("#chartTypeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.type === S.chartType));
  $("bbOn").checked = S.bb.on; $("bbPeriod").value = S.bb.period; $("bbMult").value = S.bb.mult; $("bbColor").value = S.bb.color;
  $("rsiOn").checked = S.rsi.on; $("rsiPeriod").value = S.rsi.period; $("rsiHigh").value = S.rsi.high; $("rsiLow").value = S.rsi.low;
  $("volOn").checked = S.vol;
  $("extOn").checked = S.ext;
  renderMaList();
}

function renderMaList() {
  const list = $("maList");
  list.innerHTML = "";
  S.mas.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "ma-row";
    row.innerHTML = `
      <label class="switch"><input type="checkbox" ${m.on ? "checked" : ""} data-k="on"><span></span></label>
      <select data-k="type"><option ${m.type === "SMA" ? "selected" : ""}>SMA</option><option ${m.type === "EMA" ? "selected" : ""}>EMA</option></select>
      <input type="number" min="2" max="500" value="${m.period}" data-k="period" title="Sesiones">
      <input type="color" value="${m.color}" data-k="color">
      <button class="del" title="Eliminar" data-k="del">✕</button>`;
    row.addEventListener("change", (e) => {
      const k = e.target.dataset.k;
      if (k === "on") m.on = e.target.checked;
      else if (k === "type") m.type = e.target.value;
      else if (k === "period") m.period = clampInt(e.target.value, 2, 500, m.period);
      else if (k === "color") m.color = e.target.value;
      save(); recompute();
    });
    row.querySelector(".del").addEventListener("click", () => {
      S.mas.splice(i, 1); save(); renderMaList(); recompute();
    });
    list.appendChild(row);
  });
}

function clampInt(v, lo, hi, fallback) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

$("addMa").addEventListener("click", () => {
  const used = new Set(S.mas.map((m) => m.color));
  S.mas.push({ on: true, type: "EMA", period: 9, color: MA_COLORS.find((c) => !used.has(c)) || "#9e9e9e" });
  save(); renderMaList(); recompute();
});

const bindSetting = (id, apply) => $(id).addEventListener("change", (e) => { apply(e.target); save(); recompute(); });
bindSetting("bbOn", (t) => (S.bb.on = t.checked));
bindSetting("bbPeriod", (t) => (S.bb.period = clampInt(t.value, 2, 200, 20)));
bindSetting("bbMult", (t) => (S.bb.mult = Math.min(5, Math.max(0.5, parseFloat(t.value) || 2))));
bindSetting("bbColor", (t) => (S.bb.color = t.value));
bindSetting("rsiOn", (t) => (S.rsi.on = t.checked));
bindSetting("rsiPeriod", (t) => (S.rsi.period = clampInt(t.value, 2, 100, 14)));
bindSetting("rsiHigh", (t) => (S.rsi.high = clampInt(t.value, 50, 100, 70)));
bindSetting("rsiLow", (t) => (S.rsi.low = clampInt(t.value, 0, 50, 30)));
bindSetting("volOn", (t) => (S.vol = t.checked));
$("extOn").addEventListener("change", (e) => {
  S.ext = e.target.checked;
  refreshQuotes();
  load(); // vuelve a pedir las velas de 1 hora con o sin horario extendido
});

$("rangeSeg").addEventListener("click", (e) => {
  const r = e.target.dataset.range;
  if (r && r !== S.range) { S.range = r; load(); }
});
$("intervalSelect").addEventListener("change", (e) => { S.interval = e.target.value; load(); });
$("chartTypeSeg").addEventListener("click", (e) => {
  const t = e.target.dataset.type;
  if (t && t !== S.chartType) { S.chartType = t; save(); syncControls(); renderPrice(); }
});
$("cmpModeSeg").addEventListener("click", (e) => {
  const m = e.target.dataset.mode;
  if (m) { S.cmpMode = m; save(); renderCompare(); }
});
$("demoToggle").addEventListener("change", (e) => {
  S.demo = e.target.checked;
  WL.quotes.clear(); // no mezclar datos reales y simulados
  refreshQuotes();
  load();
});
$("clearCompare").addEventListener("click", () => { S.compare = ""; dataB = null; load(); });
$("themeBtn").addEventListener("click", () => {
  S.theme = S.theme === "dark" ? "light" : "dark";
  applyTheme();
  save();
  recompute();
});

function setTab(t) {
  S.sideTab = t;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab-body").forEach((d) => (d.hidden = d.dataset.tab !== t));
}
document.querySelector(".tabs").addEventListener("click", (e) => {
  const t = e.target.closest("button")?.dataset.tab;
  if (t) { setTab(t); save(); if (t === "pos") loadPositions(); }
});

function applyTheme() {
  document.documentElement.dataset.theme = S.theme;
  $("themeBtn").textContent = S.theme === "dark" ? "☀" : "☾";
  for (const c of [priceChart, rsiChart, cmpChart]) c.applyOptions(chartTheme());
}

// ---- Búsqueda con sugerencias ----
function setupSearch(inputId, listId, formId, onPick) {
  const input = $(inputId), list = $(listId);
  let timer = null, items = [], active = -1;
  const close = () => { list.hidden = true; active = -1; };
  const draw = () => {
    list.innerHTML = items.map((r, i) =>
      `<li data-i="${i}" class="${i === active ? "active" : ""}"><b>${esc(r.symbol)}</b><span>${esc(r.name)} · ${esc(r.exchange)}</span></li>`).join("");
    list.hidden = !items.length;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 1 || S.demo) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const res = await fetch("/api/search?q=" + encodeURIComponent(q));
        items = (await res.json()).results || [];
      } catch { items = []; }
      active = -1;
      draw();
    }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); draw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(-1, active - 1); draw(); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    e.preventDefault();
    input.value = items[+li.dataset.i].symbol;
    close();
    onPick(input.value);
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  $(formId).addEventListener("submit", (e) => {
    e.preventDefault();
    if (!list.hidden && active >= 0) input.value = items[active].symbol;
    close();
    onPick(input.value);
    input.blur();
  });
}

setupSearch("symbolInput", "symbolSuggest", "symbolForm", (v) => {
  v = v.trim().toUpperCase();
  if (v && v !== S.symbol) { S.symbol = v; load(); }
});
setupSearch("watchInput", "watchSuggest", "watchForm", (v) => {
  wlAdd(v);
  $("watchInput").value = "";
});
setupSearch("compareInput", "compareSuggest", "compareForm", (v) => {
  v = v.trim().toUpperCase();
  if (v !== S.compare) { S.compare = v; if (!v) dataB = null; load(); }
});

window.addEventListener("hashchange", () => {
  const h = new URLSearchParams(location.hash.slice(1));
  const s = (h.get("s") || "").toUpperCase(), c = (h.get("c") || "").toUpperCase();
  if ((s && s !== S.symbol) || c !== S.compare) { if (s) S.symbol = s; S.compare = c; load(); }
});

applyTheme();
setTab(S.sideTab);
load();
wlFetch();
loadPositions();
