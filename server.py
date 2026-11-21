#!/usr/bin/env python3
"""Servidor de MadCow Charts.

Sirve el frontend estático y hace de proxy a la API de gráficos de Yahoo Finance
(el navegador no puede llamarla directamente por CORS). Solo usa la librería
estándar de Python: no hace falta instalar nada.

Uso:
    python3 server.py            # http://localhost:8000
    python3 server.py --port 9000
"""
import argparse
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

VALID_RANGES = {"1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"}
VALID_INTERVALS = {"1h", "1d", "1wk", "1mo"}

_cache: dict = {}
CACHE_TTL = 60  # segundos


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_yahoo(symbol: str, rng: str, interval: str) -> dict:
    qs = urllib.parse.urlencode({"range": rng, "interval": interval, "includePrePost": "false", "events": "div,splits"})
    data = _get_json(YAHOO_CHART.format(symbol=urllib.parse.quote(symbol)) + "?" + qs)
    chart = data.get("chart", {})
    if chart.get("error"):
        raise ValueError(chart["error"].get("description") or "Símbolo no encontrado")
    result = (chart.get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise ValueError(f"Sin datos para {symbol}")

    meta = result.get("meta", {})
    q = result["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(result["timestamp"]):
        o, h, l, c, v = (q.get(k, [None])[i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        bars.append({"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v or 0})

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency", ""),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        "bars": bars,
        "demo": False,
    }


def demo_series(symbol: str, rng: str, interval: str) -> dict:
    """Datos simulados (paseo aleatorio geométrico) para cuando no hay conexión."""
    days = {"1mo": 22, "3mo": 64, "6mo": 128, "ytd": 225, "1y": 252, "2y": 504,
            "5y": 1260, "10y": 2520, "max": 2520}[rng]
    step = {"1h": 3600, "1d": 86400, "1wk": 7 * 86400, "1mo": 30 * 86400}[interval]
    n = {"1h": days * 7, "1d": days, "1wk": max(days // 5, 10), "1mo": max(days // 21, 6)}[interval]
    n = min(n, 3000)
    rnd = random.Random(symbol.upper())
    price = rnd.uniform(20, 400)
    drift, vol = rnd.uniform(-0.0002, 0.0008), rnd.uniform(0.012, 0.03)
    vol *= math.sqrt(step / 86400)
    now = int(time.time()) // step * step
    bars = []
    for i in range(n):
        o = price
        c = o * math.exp(drift + vol * rnd.gauss(0, 1))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        bars.append({"time": now - (n - i) * step, "open": o, "high": h, "low": l,
                     "close": c, "volume": int(rnd.uniform(1e6, 2e7))})
        price = c
    return {"symbol": symbol.upper(), "name": f"{symbol.upper()} (simulado)", "currency": "USD",
            "exchange": "DEMO", "bars": bars, "demo": True}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silencioso

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/api/chart":
            return self.api_chart(params)
        if parsed.path == "/api/search":
            return self.api_search(params)
        return super().do_GET()

    def api_chart(self, p: dict):
        symbol = p.get("symbol", "").strip().upper()
        rng = p.get("range", "1y")
        interval = p.get("interval", "1d")
        if not symbol or len(symbol) > 20:
            return self._send_json({"error": "Símbolo no válido"}, 400)
        if rng not in VALID_RANGES or interval not in VALID_INTERVALS:
            return self._send_json({"error": "Rango o intervalo no válido"}, 400)
        if interval == "1h" and rng in {"5y", "10y", "max"}:
            rng = "2y"  # Yahoo limita el intradía a ~730 días

        if p.get("demo") == "1":
            return self._send_json(demo_series(symbol, rng, interval))

        key = (symbol, rng, interval)
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return self._send_json(hit[1])
        try:
            data = fetch_yahoo(symbol, rng, interval)
        except ValueError as e:
            return self._send_json({"error": str(e)}, 404)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return self._send_json({"error": f"Símbolo no encontrado: {symbol}"}, 404)
            return self._send_json({"error": f"Yahoo Finance respondió {e.code}", "offline": True}, 502)
        except Exception as e:  # sin red, timeout, etc.
            return self._send_json({"error": f"No se pudo conectar con Yahoo Finance ({e.__class__.__name__})",
                                    "offline": True}, 502)
        _cache[key] = (time.time(), data)
        return self._send_json(data)

    def api_search(self, p: dict):
        q = p.get("q", "").strip()
        if not q:
            return self._send_json({"results": []})
        try:
            qs = urllib.parse.urlencode({"q": q, "quotesCount": 8, "newsCount": 0})
            data = _get_json(YAHOO_SEARCH + "?" + qs)
        except Exception:
            return self._send_json({"results": []})
        results = [
            {"symbol": r.get("symbol"), "name": r.get("shortname") or r.get("longname") or "",
             "exchange": r.get("exchDisp", ""), "type": r.get("typeDisp", "")}
            for r in data.get("quotes", []) if r.get("symbol")
        ]
        return self._send_json({"results": results})


def main():
    ap = argparse.ArgumentParser(description="MadCow Charts")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MadCow Charts en http://{args.host}:{args.port}  (Ctrl+C para salir)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
