#!/usr/bin/env python3
"""Servidor de MadCow Charts.

Sirve el frontend estático y obtiene las cotizaciones de la fuente de datos:

  * Interactive Brokers (por defecto): a través de TWS o IB Gateway abierto en
    este ordenador, en modo solo lectura. Necesita: pip install ib_async
  * Yahoo Finance (--source yahoo): sin dependencias, pero Yahoo bloquea a menudo
    las peticiones automáticas (error 429).

La watchlist (favoritos) se guarda en watchlist.json junto a este archivo,
así que se conserva entre sesiones y navegadores.

Uso:
    python3 server.py            # http://localhost:8000
    python3 server.py --port 9000
    python3 server.py --watchlist ~/mis-favoritos.json
    python3 server.py --check    # diagnóstico de la conexión con la fuente de datos
    python3 server.py --ib-port 7497   # forzar puerto de TWS/IB Gateway (por defecto se detecta)
    python3 server.py --clear-cache    # borrar la caché de datos históricos
    python3 server.py --lan --password secreto   # accesible desde la red local, con contraseña

Los datos históricos se guardan en cache.sqlite: la segunda vez solo se piden las
velas nuevas y, si TWS está cerrado, se muestran los datos guardados.
"""
import argparse
import base64
import datetime as dt
import hmac
import ipaddress
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

import ibkr
from histcache import HistCache

STATIC_DIR = Path(__file__).resolve().parent / "static"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

VALID_RANGES = {"1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"}
VALID_INTERVALS = {"1h", "1d", "1wk", "1mo"}

_cache: dict = {}
CACHE_TTL = 60  # segundos
LIVE_TTL = 20   # segundos: caché más corta para el gráfico en vivo (?live=1)

SOURCE = None                  # IBKRSource, o None para usar Yahoo Finance
SOURCE_NAME = "Yahoo Finance"
SOURCE_KEY = "yahoo"
BASE_DIR = Path(__file__).resolve().parent
CACHE = HistCache(None)        # se sustituye en main() por la caché en disco
RANGE_DAYS = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653}
YAHOO_SINCE_RANGES = ((5, "5d"), (31, "1mo"), (92, "3mo"), (183, "6mo"), (366, "1y"), (731, "2y"))

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


def fetch_yahoo(symbol: str, rng: str, interval: str, rth: bool = True) -> dict:
    qs = urllib.parse.urlencode({"range": rng, "interval": interval, "events": "div,splits",
                                 "includePrePost": "false" if rth else "true"})
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


def fetch_source(symbol: str, rng: str, interval: str, rth: bool, since: int | None = None) -> dict:
    """Pide velas a la fuente activa, sin caché. Con `since`, solo las recientes.
    Lanza ValueError (símbolo) u OfflineError (sin conexión con la fuente)."""
    if SOURCE is not None:
        try:
            return SOURCE.chart(symbol, rng, interval, rth, since)
        except ibkr.Unavailable as e:
            log_error(symbol, str(e))
            raise OfflineError(str(e)) from e
        except ibkr.SymbolError as e:
            log_error(symbol, str(e))
            raise
    if since:
        days = (time.time() - since) // 86400 + 2
        rng = next((r for d, r in YAHOO_SINCE_RANGES if days <= d), rng)
    try:
        return fetch_yahoo(symbol, rng, interval, rth)
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


def range_start(rng: str, now: float) -> int:
    if rng == "max":
        return 0
    if rng == "ytd":
        year = dt.datetime.fromtimestamp(now, dt.timezone.utc).year
        return int(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    return int(now - RANGE_DAYS[rng] * 86400)


def _from_cache(key: tuple, meta: dict, start: int) -> dict:
    return {"symbol": key[1], "name": meta["name"], "currency": meta["currency"],
            "exchange": meta["exchange"], "bars": CACHE.bars(key, start), "demo": False,
            "updated_at": meta["updated_at"]}


def _update_incremental(key: tuple, symbol: str, rng: str, interval: str, rth: bool, meta: dict, start: int):
    """Pide solo las velas nuevas y las añade a la caché. Si las velas que se solapan no
    coinciden con las guardadas (split, ajuste…), descarta la serie y la descarga entera."""
    tail = CACHE.last_bars(key, 4)
    data = fetch_source(symbol, rng, interval, rth, since=tail[0]["time"] if tail else start)
    fresh = {b["time"]: b["close"] for b in data["bars"]}
    for b in tail[:-1]:  # la última vela guardada puede estar incompleta: no se compara
        c = fresh.get(b["time"])
        if c is not None and abs(c / b["close"] - 1) > 0.02:
            print(f"[{time.strftime('%H:%M:%S')}] {symbol}: el histórico guardado no coincide "
                  "con el de la fuente (¿split o ajuste?); se vuelve a descargar.", flush=True)
            full = fetch_source(symbol, rng, interval, rth)
            CACHE.invalidate(key)
            CACHE.save(key, full, start)
            return
    CACHE.save(key, data, meta["covered_from"])


def get_chart(symbol: str, rng: str, interval: str, rth: bool = True, ttl: float | None = None) -> dict:
    """Velas del rango pedido, usando la caché en disco.

    - Si la caché cubre el rango y es reciente (CACHE_TTL), no se consulta la fuente.
    - Si cubre el rango pero está anticuada, se piden solo las velas nuevas.
    - Si no lo cubre, se pide el rango completo.
    - Sin conexión con la fuente, se devuelven los datos guardados con "stale": true.
    rth=False incluye el pre/post-mercado en las velas de 1 hora.
    """
    rth = rth or interval != "1h"
    key = (SOURCE_KEY, symbol, interval, int(rth))
    now = time.time()
    start = range_start(rng, now)
    meta = CACHE.series(key)
    covered = meta is not None and meta["covered_from"] <= start
    if covered and now - meta["updated_at"] < (CACHE_TTL if ttl is None else ttl):
        return _from_cache(key, meta, start)
    try:
        if covered:
            _update_incremental(key, symbol, rng, interval, rth, meta, start)
        else:
            data = fetch_source(symbol, rng, interval, rth)
            CACHE.save(key, data, start if meta is None else min(start, meta["covered_from"]), now)
    except OfflineError as e:
        if meta:
            return {**_from_cache(key, meta, start), "stale": True, "stale_reason": str(e)}
        raise
    return _from_cache(key, CACHE.series(key), start)


def get_extended(symbol: str):
    """Precio de pre/post-mercado (o None). Nunca lanza: es información accesoria."""
    if SOURCE is None:
        return None
    key = (symbol, "ext")
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        data = SOURCE.extended(symbol)
    except Exception:
        data = None
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


# ---------------------------------------------------------------------------
# Acceso desde la red local
# ---------------------------------------------------------------------------
PASSWORD = ""             # si no está vacía, se pide a los dispositivos que no sean este PC
EXTRA_HOSTS: set = set()  # nombres adicionales permitidos en la cabecera Host (--allow-host)
LOCAL_SUFFIXES = ("", ".local", ".lan", ".home", ".localdomain", ".home.arpa", ".fritz.box")


def host_allowed(header: str) -> bool:
    """Solo se aceptan peticiones dirigidas a una IP, a localhost o al nombre de este PC.
    Evita que una web maliciosa use 'DNS rebinding' para leer la app desde tu navegador."""
    host = (header or "").strip().lower()
    if host.startswith("["):                 # IPv6: [::1]:8000
        host = host[1:host.find("]")]
    elif host.count(":") == 1:               # quitar el puerto
        host = host.split(":")[0]
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host in EXTRA_HOSTS:
        return True
    name = socket.gethostname().lower().split(".")[0]
    return any(host == name + suffix for suffix in LOCAL_SUFFIXES)


def lan_addresses() -> list:
    """IPs de este PC en la red local (sin enviar nada por la red)."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP: no envía paquetes, solo elige la interfaz
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback)


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

    def _is_local(self) -> bool:
        """¿La petición viene de este mismo PC?"""
        return ipaddress.ip_address(self.client_address[0]).is_loopback

    def _allowed(self) -> bool:
        """Comprueba la cabecera Host y, si hay contraseña, la autenticación. Si no pasa, responde."""
        if not host_allowed(self.headers.get("Host", "")):
            self._send_json({"error": "Host no permitido. Usa la IP o el nombre de este PC "
                                      "(o añádelo con --allow-host)."}, 403)
            return False
        if not PASSWORD or self._is_local():
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                given = ""
            if hmac.compare_digest(given.encode(), PASSWORD.encode()):
                return True
            time.sleep(1)  # frena los intentos a ciegas
        body = "Se necesita la contraseña de MadCow (el usuario da igual).".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MadCow", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_HEAD(self):
        if self._allowed():
            super().do_HEAD()

    def do_GET(self):
        if not self._allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/api/chart":
            return self.api_chart(params)
        if parsed.path == "/api/search":
            return self.api_search(params)
        if parsed.path == "/api/quotes":
            return self.api_quotes(params)
        if parsed.path == "/api/positions":
            return self.api_positions()
        if parsed.path == "/api/watchlist":
            with _wl_lock:
                return self._send_json({"symbols": read_watchlist()})
        return super().do_GET()

    def do_PUT(self):
        if not self._allowed():
            return
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
            return self._send_json({**demo_series(symbol, rng, interval), "source": "datos simulados"})

        rth = p.get("rth") != "0"
        live = p.get("live") == "1"  # actualización automática del gráfico: caché más corta
        try:
            data = get_chart(symbol, rng, interval, rth, LIVE_TTL if live else None)
            if p.get("since", "").isdigit():  # solo las velas desde esa fecha (respuesta pequeña)
                since = int(p["since"])
                data = {**data, "bars": [b for b in data["bars"] if b["time"] >= since]}
            return self._send_json({**data, "source": SOURCE_NAME})
        except ValueError as e:
            return self._send_json({"error": str(e), "source": SOURCE_NAME}, 404)
        except OfflineError as e:
            return self._send_json({"error": str(e), "offline": True, "source": SOURCE_NAME}, 502)

    def api_positions(self):
        """Posiciones de la cuenta de IBKR. Son datos privados: desde otros dispositivos
        solo se sirven si la app está protegida con contraseña."""
        if SOURCE is None:
            return self._send_json({"available": False,
                                    "reason": "Las posiciones solo están disponibles con Interactive Brokers."})
        if not PASSWORD and not self._is_local():
            return self._send_json({"available": False, "reason":
                                    "Por seguridad, tus posiciones solo se muestran en el PC donde corre la app. "
                                    "Para verlas desde otros dispositivos, arráncala con --password."}, 403)
        try:
            return self._send_json(SOURCE.positions())
        except ibkr.Unavailable as e:
            return self._send_json({"available": False, "reason": f"No se pueden leer las posiciones: {e}"})

    def api_quotes(self, p: dict):
        """Resumen de varios símbolos para la watchlist: últimos ~3 meses de cierres diarios."""
        symbols = [s.strip().upper() for s in p.get("symbols", "").split(",") if s.strip()][:MAX_WATCHLIST]
        demo = p.get("demo") == "1"
        want_ext = p.get("ext") == "1"
        rng = p.get("range", "3mo")  # el escáner pide más historia (medias de 200 sesiones)
        if rng not in VALID_RANGES:
            return self._send_json({"error": "Rango no válido"}, 400)

        def one(sym: str) -> dict:
            if not SYMBOL_RE.match(sym):
                return {"symbol": sym, "error": "Símbolo no válido"}
            try:
                d = demo_series(sym, rng, "1d") if demo else get_chart(sym, rng, "1d")
            except ValueError as e:
                return {"symbol": sym, "error": str(e)}
            except OfflineError:
                d = demo_series(sym, rng, "1d")  # sin conexión: simulado, marcado como demo
            bars = d["bars"]
            out = {"symbol": sym, "name": d["name"], "currency": d["currency"], "demo": d["demo"],
                   "time": bars[-1]["time"], "closes": [round(b["close"], 6) for b in bars]}
            if d.get("stale"):
                out.update(stale=True, updated_at=d["updated_at"])
            ext = get_extended(sym) if want_ext and not d["demo"] and not d.get("stale") else None
            if ext and ext["session"] != "regular":
                # Referencia: último cierre del horario regular (hoy en post-mercado, ayer en pre-mercado)
                out["ext"] = {**ext, "change": ext["price"] / bars[-1]["close"] - 1}
            return out

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(one, symbols))
        return self._send_json({"quotes": results})

    def api_search(self, p: dict):
        q = p.get("q", "").strip()
        if not q or len(q) > 40:
            return self._send_json({"results": []})
        if SOURCE is not None:
            try:
                return self._send_json({"results": SOURCE.search(q)})
            except Exception:
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
    """Diagnóstico: muestra el entorno y prueba una petición real a la fuente de datos."""
    print(f"MadCow · diagnóstico de conexión con {SOURCE_NAME}\n")
    print(f"Python ........ {platform.python_version()} ({sys.executable})")
    print(f"Sistema ....... {platform.platform()}")
    if SOURCE is not None:
        ok = SOURCE.check()
        print("\nResultado:", "la conexión funciona." if ok else
              "hay problemas. Copia este texto completo y compártelo.")
        return 0 if ok else 1
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


def _report_connection():
    """Al arrancar, intenta conectar con IBKR en segundo plano e informa en la terminal."""
    try:
        SOURCE._run(SOURCE._connect(), timeout=60)
        print(f"Conectado a {ibkr.PORT_NAMES.get(SOURCE.connected_port, 'IBKR')} "
              f"en el puerto {SOURCE.connected_port}", flush=True)
    except ibkr.Unavailable as e:
        print(f"Aviso: {e}. Hasta que esté disponible se mostrarán los datos guardados (o simulados si no los hay).", flush=True)


def main():
    global WATCHLIST_FILE, SOURCE, SOURCE_NAME, SOURCE_KEY, CACHE_TTL, CACHE, PASSWORD
    ap = argparse.ArgumentParser(description="MadCow Charts")
    ap.add_argument("--host", default="127.0.0.1",
                    help="dirección en la que escuchar (por defecto solo este PC)")
    ap.add_argument("--lan", action="store_true",
                    help="permitir el acceso desde otros dispositivos de la red local (equivale a --host 0.0.0.0)")
    ap.add_argument("--password", default=os.environ.get("MADCOW_PASSWORD", ""),
                    help="contraseña para los dispositivos de la red (o variable de entorno MADCOW_PASSWORD)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NOMBRE",
                    help="nombre adicional con el que se accede a la app (p. ej. un nombre DNS propio)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--watchlist", type=Path, default=WATCHLIST_FILE,
                    help="archivo donde guardar los favoritos (por defecto watchlist.json)")
    ap.add_argument("--source", choices=("ibkr", "yahoo"), default="ibkr",
                    help="fuente de datos (por defecto ibkr: TWS o IB Gateway)")
    ap.add_argument("--ib-host", default="127.0.0.1", help="equipo donde corre TWS/IB Gateway")
    ap.add_argument("--ib-port", type=int, default=None,
                    help="puerto de la API (por defecto prueba 7496, 4001, 7497 y 4002)")
    ap.add_argument("--ib-client-id", type=int, default=117, help="clientId para la API de IBKR")
    ap.add_argument("--no-positions", action="store_true",
                    help="no leer las posiciones de la cuenta de IBKR")
    ap.add_argument("--check", action="store_true", help="diagnosticar la conexión con la fuente de datos y salir")
    ap.add_argument("--cache", type=Path, default=BASE_DIR / "cache.sqlite",
                    help="archivo de la caché de datos históricos (por defecto cache.sqlite)")
    ap.add_argument("--no-cache", action="store_true", help="no guardar datos históricos en disco")
    ap.add_argument("--clear-cache", action="store_true", help="borrar la caché de datos históricos y salir")
    args = ap.parse_args()
    cache_path = args.cache.expanduser().resolve()
    if args.clear_cache:
        removed = [p for p in (cache_path, Path(f"{cache_path}-wal"), Path(f"{cache_path}-shm")) if p.exists()]
        for p in removed:
            p.unlink()
        print(f"Caché borrada: {cache_path}" if removed else f"No había caché en {cache_path}")
        sys.exit(0)
    if args.source == "ibkr":
        SOURCE = ibkr.IBKRSource(args.ib_host, args.ib_port, args.ib_client_id,
                                 positions=not args.no_positions)
        SOURCE_NAME = ibkr.IBKRSource.name
        SOURCE_KEY = "ibkr"
        CACHE_TTL = 120
    if args.check:
        sys.exit(run_check())
    WATCHLIST_FILE = args.watchlist.expanduser().resolve()
    host = "0.0.0.0" if args.lan else args.host
    PASSWORD = args.password
    EXTRA_HOSTS.update(h.strip().lower() for h in args.allow_host if h.strip())
    srv = ThreadingHTTPServer((host, args.port), Handler)
    if host in ("127.0.0.1", "localhost", "::1"):
        print(f"MadCow Charts en http://localhost:{args.port}  (Ctrl+C para salir)")
    else:
        print(f"MadCow Charts en http://localhost:{args.port}  (Ctrl+C para salir)")
        urls = [f"http://{ip}:{args.port}" for ip in lan_addresses()] if host == "0.0.0.0" else [f"http://{host}:{args.port}"]
        urls.append(f"http://{socket.gethostname().lower()}:{args.port}")
        print("Desde otros dispositivos de tu red: " + "  ·  ".join(urls))
        if PASSWORD:
            print("Se pedirá contraseña a los demás dispositivos (el usuario da igual).")
        else:
            print("Aviso: sin contraseña, cualquiera conectado a tu red puede ver la app y cambiar tus "
                  "favoritos. Usa --password para protegerla.")
    print(f"Watchlist: {WATCHLIST_FILE}")
    print(f"Datos: {SOURCE_NAME}")
    if args.no_cache:
        print("Caché de históricos: desactivada (solo en memoria)")
    else:
        CACHE = HistCache(cache_path)
        n_series, n_bars = CACHE.stats()
        print(f"Caché de históricos: {cache_path} ({n_series} series, {n_bars} velas)")
    if SOURCE is not None:
        threading.Thread(target=_report_connection, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
