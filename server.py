#!/usr/bin/env python3
"""Servidor de MadCow Charts.

Sirve el frontend estático y hace de proxy a la API de gráficos de Yahoo Finance
(el navegador no puede llamarla directamente por CORS). Solo usa la librería
estándar de Python: no hace falta instalar nada.

La watchlist (favoritos) se guarda en watchlist.json junto a este archivo,
así que se conserva entre sesiones y navegadores.

Uso:
    python3 server.py            # http://localhost:8000
    python3 server.py --port 9000
    python3 server.py --watchlist ~/mis-favoritos.json
    python3 server.py --check    # diagnóstico de la conexión con Yahoo Finance
"""
import argparse
import json
import math
import os
import platform
import random
import re
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,20}$")
WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
MAX_WATCHLIST = 300
_wl_lock = threading.Lock()


class OfflineError(Exception):
    """No se pudo contactar con Yahoo Finance (sin red, timeout, 5xx…)."""


def read_watchlist() -> list:
    try:
        data = json.loads(WATCHLIST_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Archivo dañado (p. ej. editado a mano): se aparta para no perderlo.
        backup = WATCHLIST_FILE.with_name(f"{WATCHLIST_FILE.name}.dañado-{int(time.time())}")
        WATCHLIST_FILE.replace(backup)
        print(f"Aviso: {WATCHLIST_FILE.name} no era JSON válido; copiado a {backup.name}")
        return []
    symbols = data.get("symbols", []) if isinstance(data, dict) else []
    return [s for s in symbols if isinstance(s, str) and SYMBOL_RE.match(s)]


def write_watchlist(symbols: list) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_FILE.with_name(WATCHLIST_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"symbols": symbols, "updated": int(time.time())}, indent=2), "utf-8")
    os.replace(tmp, WATCHLIST_FILE)  # escritura atómica


def _ssl_context() -> ssl.SSLContext:
    """Contexto TLS con verificación. Si está instalado `certifi`, añade sus certificados
    (soluciona el típico CERTIFICATE_VERIFY_FAILED del Python de python.org en Mac)."""
    ctx = ssl.create_default_context()
    try:
        import certifi  # opcional: pip3 install certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


SSL_CTX = _ssl_context()


def describe_error(e: BaseException) -> str:
    """Traduce un error de red a un mensaje que explique la causa real."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 429:
            return ("Yahoo Finance respondió 429 (Too Many Requests): está bloqueando "
                    "las peticiones automáticas desde tu conexión")
        if e.code in (401, 403):
            return f"Yahoo Finance respondió {e.code}: acceso denegado"
        return f"Yahoo Finance respondió {e.code} {e.reason}"
    reason = getattr(e, "reason", e)
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return ("error de certificados SSL de Python. En Mac ejecuta «Install Certificates.command» "
                "(carpeta de Python en Aplicaciones) o instala certifi con: pip3 install certifi")
    if isinstance(reason, ssl.SSLError):
        return f"error SSL ({text}); ¿hay un antivirus o proxy inspeccionando HTTPS?"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "tiempo de espera agotado al conectar con Yahoo Finance"
    if isinstance(reason, socket.gaierror):
        return "no se pudo resolver la dirección de Yahoo Finance (¿sin Internet o DNS bloqueado?)"
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
        return "la conexión fue rechazada o cortada (¿firewall, antivirus o proxy?)"
    return f"{type(reason).__name__}: {text}"


_last_log = {"msg": None, "t": 0.0}


def log_error(symbol: str, msg: str) -> None:
    """Escribe el error en la terminal sin repetir el mismo mensaje más de una vez por minuto."""
    now = time.time()
    if msg != _last_log["msg"] or now - _last_log["t"] > 60:
        print(f"[{time.strftime('%H:%M:%S')}] Error al pedir {symbol}: {msg}", file=sys.stderr, flush=True)
        _last_log.update(msg=msg, t=now)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
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


def get_chart(symbol: str, rng: str, interval: str) -> dict:
    """Datos de Yahoo con caché corta. Lanza ValueError (símbolo) u OfflineError (red)."""
    key = (symbol, rng, interval)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        data = fetch_yahoo(symbol, rng, interval)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            raise ValueError(f"Símbolo no encontrado: {symbol}") from e
        msg = describe_error(e)
        log_error(symbol, msg)
        raise OfflineError(msg) from e
    except ValueError:
        raise
    except Exception as e:  # sin red, timeout, certificados, etc.
        msg = describe_error(e)
        log_error(symbol, msg)
        raise OfflineError(msg) from e
    _cache[key] = (time.time(), data)
    return data


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
        if parsed.path == "/api/quotes":
            return self.api_quotes(params)
        if parsed.path == "/api/watchlist":
            with _wl_lock:
                return self._send_json({"symbols": read_watchlist()})
        return super().do_GET()

    def do_PUT(self):
        if urllib.parse.urlparse(self.path).path != "/api/watchlist":
            return self._send_json({"error": "No encontrado"}, 404)
        # Exigir JSON obliga al navegador a hacer preflight CORS, que este servidor
        # no acepta: así otra web no puede modificar tu watchlist.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send_json({"error": "Se esperaba JSON"}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 50_000:
            return self._send_json({"error": "Demasiado grande"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "JSON no válido"}, 400)
        symbols = body.get("symbols") if isinstance(body, dict) else None
        if not isinstance(symbols, list) or len(symbols) > MAX_WATCHLIST:
            return self._send_json({"error": f"Se esperaba una lista de hasta {MAX_WATCHLIST} símbolos"}, 400)
        clean = []
        for s in symbols:
            s = s.strip().upper() if isinstance(s, str) else ""
            if not SYMBOL_RE.match(s):
                return self._send_json({"error": f"Símbolo no válido: {s!r}"}, 400)
            if s not in clean:
                clean.append(s)
        with _wl_lock:
            write_watchlist(clean)
        return self._send_json({"symbols": clean})

    def api_chart(self, p: dict):
        symbol = p.get("symbol", "").strip().upper()
        rng = p.get("range", "1y")
        interval = p.get("interval", "1d")
        if not SYMBOL_RE.match(symbol):
            return self._send_json({"error": "Símbolo no válido"}, 400)
        if rng not in VALID_RANGES or interval not in VALID_INTERVALS:
            return self._send_json({"error": "Rango o intervalo no válido"}, 400)
        if interval == "1h" and rng in {"5y", "10y", "max"}:
            rng = "2y"  # Yahoo limita el intradía a ~730 días

        if p.get("demo") == "1":
            return self._send_json(demo_series(symbol, rng, interval))

        try:
            return self._send_json(get_chart(symbol, rng, interval))
        except ValueError as e:
            return self._send_json({"error": str(e)}, 404)
        except OfflineError as e:
            return self._send_json({"error": str(e), "offline": True}, 502)

    def api_quotes(self, p: dict):
        """Resumen de varios símbolos para la watchlist: últimos ~3 meses de cierres diarios."""
        symbols = [s.strip().upper() for s in p.get("symbols", "").split(",") if s.strip()][:MAX_WATCHLIST]
        demo = p.get("demo") == "1"

        def one(sym: str) -> dict:
            if not SYMBOL_RE.match(sym):
                return {"symbol": sym, "error": "Símbolo no válido"}
            try:
                d = demo_series(sym, "3mo", "1d") if demo else get_chart(sym, "3mo", "1d")
            except ValueError as e:
                return {"symbol": sym, "error": str(e)}
            except OfflineError:
                d = demo_series(sym, "3mo", "1d")  # sin conexión: simulado, marcado como demo
            bars = d["bars"]
            return {"symbol": sym, "name": d["name"], "currency": d["currency"], "demo": d["demo"],
                    "time": bars[-1]["time"], "closes": [round(b["close"], 6) for b in bars]}

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(one, symbols))
        return self._send_json({"quotes": results})

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


def run_check() -> int:
    """Diagnóstico: muestra el entorno y prueba una petición real a Yahoo Finance."""
    print("MadCow · diagnóstico de conexión con Yahoo Finance\n")
    print(f"Python ........ {platform.python_version()} ({sys.executable})")
    print(f"Sistema ....... {platform.platform()}")
    print(f"OpenSSL ....... {ssl.OPENSSL_VERSION}")
    try:
        import certifi
        certifi_info = f"instalado ({certifi.where()})"
    except ImportError:
        certifi_info = "no instalado"
    print(f"certifi ....... {certifi_info}")
    print(f"Certificados .. {SSL_CTX.cert_store_stats().get('x509_ca', 0)} autoridades cargadas")
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https", "all")}
    print(f"Proxy ......... {', '.join(f'{k}={v}' for k, v in proxies.items()) or 'ninguno'}\n")

    ok = False
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/AAPL?range=5d&interval=1d"
        print(f"Probando {host} … ", end="", flush=True)
        try:
            data = _get_json(url)
            n = len(data["chart"]["result"][0]["timestamp"])
            print(f"OK ({n} sesiones de AAPL recibidas)")
            ok = True
        except Exception as e:
            print(f"FALLO\n    → {describe_error(e)}")
    print("\nResultado:", "la conexión funciona." if ok else
          "no se pudo obtener datos. Copia este texto completo y compártelo.")
    return 0 if ok else 1


def main():
    global WATCHLIST_FILE
    ap = argparse.ArgumentParser(description="MadCow Charts")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--watchlist", type=Path, default=WATCHLIST_FILE,
                    help="archivo donde guardar los favoritos (por defecto watchlist.json)")
    ap.add_argument("--check", action="store_true", help="diagnosticar la conexión con Yahoo Finance y salir")
    args = ap.parse_args()
    if args.check:
        sys.exit(run_check())
    WATCHLIST_FILE = args.watchlist.expanduser().resolve()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MadCow Charts en http://{args.host}:{args.port}  (Ctrl+C para salir)")
    print(f"Watchlist: {WATCHLIST_FILE}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
