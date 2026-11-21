// Cálculo de indicadores técnicos. Todas las funciones reciben un array de
// números y devuelven un array de la misma longitud (null donde no hay datos).

export function sma(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function ema(values, period) {
  const out = new Array(values.length).fill(null);
  if (values.length < period) return out;
  const k = 2 / (period + 1);
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export function movingAverage(values, period, type = "SMA") {
  return type === "EMA" ? ema(values, period) : sma(values, period);
}

// Bandas de Bollinger: media simple ± mult * desviación típica poblacional.
export function bollinger(values, period = 20, mult = 2) {
  const mid = sma(values, period);
  const upper = new Array(values.length).fill(null);
  const lower = new Array(values.length).fill(null);
  for (let i = period - 1; i < values.length; i++) {
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += (values[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / period);
    upper[i] = mid[i] + mult * sd;
    lower[i] = mid[i] - mult * sd;
  }
  return { mid, upper, lower };
}

// RSI de Wilder.
export function rsi(values, period = 14) {
  const out = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  gain /= period; loss /= period;
  const calc = () => (loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
  out[period] = calc();
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = calc();
  }
  return out;
}

// ---- Estadísticas para la comparación ----

export function returns(values) {
  const r = [];
  for (let i = 1; i < values.length; i++) r.push(values[i] / values[i - 1] - 1);
  return r;
}

export function stdev(arr) {
  if (arr.length < 2) return NaN;
  const m = arr.reduce((a, b) => a + b, 0) / arr.length;
  return Math.sqrt(arr.reduce((a, b) => a + (b - m) ** 2, 0) / (arr.length - 1));
}

export function correlation(a, b) {
  const n = Math.min(a.length, b.length);
  if (n < 3) return NaN;
  const ma = a.reduce((s, x) => s + x, 0) / n;
  const mb = b.reduce((s, x) => s + x, 0) / n;
  let num = 0, da = 0, db = 0;
  for (let i = 0; i < n; i++) {
    num += (a[i] - ma) * (b[i] - mb);
    da += (a[i] - ma) ** 2;
    db += (b[i] - mb) ** 2;
  }
  return num / Math.sqrt(da * db);
}

export function maxDrawdown(values) {
  let peak = -Infinity, mdd = 0;
  for (const v of values) {
    peak = Math.max(peak, v);
    mdd = Math.min(mdd, v / peak - 1);
  }
  return mdd;
}

export function beta(assetRet, benchRet) {
  const n = Math.min(assetRet.length, benchRet.length);
  const ma = assetRet.reduce((s, x) => s + x, 0) / n;
  const mb = benchRet.reduce((s, x) => s + x, 0) / n;
  let cov = 0, varB = 0;
  for (let i = 0; i < n; i++) {
    cov += (assetRet[i] - ma) * (benchRet[i] - mb);
    varB += (benchRet[i] - mb) ** 2;
  }
  return cov / varB;
}
