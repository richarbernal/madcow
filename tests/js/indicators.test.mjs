// Tests de static/indicators.js con el ejecutor de Node (node --test), sin dependencias.
import test from "node:test";
import assert from "node:assert/strict";
import {
  sma, ema, movingAverage, bollinger, rsi, returns, stdev, correlation, maxDrawdown, beta,
} from "../../static/indicators.js";

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} ≉ ${b}`);

test("SMA", () => {
  assert.deepEqual(sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  assert.deepEqual(sma([1, 2], 3), [null, null]);
});

test("EMA: arranca con la media simple y sigue con k = 2/(n+1)", () => {
  assert.deepEqual(ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3), [null, null, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(movingAverage([1, 2, 3, 4], 2, "EMA"), ema([1, 2, 3, 4], 2));
  assert.deepEqual(movingAverage([1, 2, 3, 4], 2, "SMA"), sma([1, 2, 3, 4], 2));
});

test("Bollinger: media ± k desviaciones típicas (poblacionales)", () => {
  const { mid, upper, lower } = bollinger([1, 2, 3, 4, 5], 5, 2);
  close(mid[4], 3);
  close(upper[4], 3 + 2 * Math.SQRT2);
  close(lower[4], 3 - 2 * Math.SQRT2);
  assert.equal(upper[3], null);
  const flat = bollinger([7, 7, 7, 7], 3, 2);
  assert.deepEqual([flat.lower[3], flat.mid[3], flat.upper[3]], [7, 7, 7]);
});

// Ejemplo clásico de RSI(14) de Wilder publicado por StockCharts
const WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
  46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
  44.22, 44.57, 43.42, 42.66, 43.13];
const PUBLISHED = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42,
  39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77];

test("RSI de Wilder: primer valor exacto", () => {
  // En los 14 primeros cambios las subidas suman 3,34 y las bajadas 1,40 (0,25 + 0,54 + 0,19 + 0,42)
  const r = rsi(WILDER, 14);
  assert.equal(r[13], null);
  close(r[14], 100 - 100 / (1 + 3.34 / 1.40), 1e-6);
});

test("RSI de Wilder: coincide con la tabla publicada", () => {
  // StockCharts redondea los promedios intermedios a 2 decimales: diferencias de hasta ~0,07
  rsi(WILDER, 14).slice(14).forEach((v, i) => close(v, PUBLISHED[i], 0.1));
});

test("RSI en casos extremos", () => {
  assert.equal(rsi([1, 2, 3, 4, 5, 6], 3).at(-1), 100);          // solo subidas
  assert.equal(rsi([6, 5, 4, 3, 2, 1], 3).at(-1), 0);            // solo bajadas
  assert.deepEqual(rsi([1, 2, 3], 3), [null, null, null]);        // pocos datos
});

test("Estadísticas para la comparación", () => {
  assert.deepEqual(returns([100, 110, 99]).map((x) => +x.toFixed(10)), [0.1, -0.1]);
  close(stdev([2, 4, 4, 4, 5, 5, 7, 9]), Math.sqrt(32 / 7));     // muestral (n − 1)
  const x = [1, 3, 2, 5, 4];
  close(correlation(x, x.map((v) => 2 * v + 1)), 1);
  close(correlation(x, x.map((v) => -v)), -1);
  close(maxDrawdown([100, 120, 90, 130, 65, 80]), -0.5);
  const r = [0.01, -0.02, 0.015, 0.003, -0.007];
  close(beta(r.map((v) => 2 * v), r), 2);
});
