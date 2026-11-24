"""Fixtures comunes. Los tests no necesitan TWS: usan el simulador de fake_tws.py."""
from __future__ import annotations

import base64
import json
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ib_async  # noqa: E402

import fake_tws  # noqa: E402
import ibkr  # noqa: E402
import server  # noqa: E402
from alerts import AlertEngine  # noqa: E402
from histcache import HistCache  # noqa: E402


@pytest.fixture
def tws(monkeypatch):
    """TWS simulado y limpio para cada test (devuelve su estado, que el test puede cambiar)."""
    state = fake_tws.FakeTWS()
    monkeypatch.setattr(fake_tws, "STATE", state)
    monkeypatch.setattr(ib_async, "IB", fake_tws.FakeIB)
    monkeypatch.setattr(ibkr, "RETRY_AFTER", 0)  # reintentar la conexión sin esperar
    monkeypatch.setattr(ibkr, "now", lambda: state.now.timestamp())  # el reloj del simulador
    return state


@pytest.fixture
def source(tws):
    """Fuente IBKR conectada al TWS simulado."""
    src = ibkr.IBKRSource()
    yield src
    src.close()


@pytest.fixture
def app(tws, tmp_path, monkeypatch):
    """Módulo `server` configurado con IBKR simulado, caché y favoritos en una carpeta temporal."""
    src = ibkr.IBKRSource()
    monkeypatch.setattr(server, "SOURCE", src)
    monkeypatch.setattr(server, "SOURCE_NAME", ibkr.IBKRSource.name)
    monkeypatch.setattr(server, "SOURCE_KEY", "ibkr")
    monkeypatch.setattr(server, "CACHE_TTL", 120)
    monkeypatch.setattr(server, "CACHE", HistCache(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(server, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(server, "PASSWORD", "")
    monkeypatch.setattr(server, "EXTRA_HOSTS", set())
    monkeypatch.setattr(server, "_cache", {})
    # Alertas sin hilo periódico: los tests llaman a check() cuando quieren (y sin caché, ttl=0)
    monkeypatch.setattr(server, "ALERTS", AlertEngine(
        tmp_path / "alerts.json", fetch=lambda symbol, rng: server.get_chart(symbol, rng, "1d", ttl=0)))
    yield server
    src.close()


class Client:
    """Cliente HTTP mínimo contra el servidor de pruebas."""

    def __init__(self, port: int):
        self.port = port

    def request(self, method, path, body=None, headers=None, password=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = dict(headers or {})
        if password is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(f"yo:{password}".encode()).decode()
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode("utf-8", "replace")
        return res.status, data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def put(self, path, body, **kw):
        return self.request("PUT", path, body=body, **kw)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


@pytest.fixture
def http(app):
    """Servidor HTTP real en un puerto libre; las peticiones llegan desde «este PC»."""
    srv = _serve(app.Handler)
    yield Client(srv.server_address[1])
    srv.shutdown()


class RemoteHandler(server.Handler):
    """Como si la petición llegara desde otro dispositivo de la red local."""

    def _is_local(self) -> bool:
        return False


@pytest.fixture
def http_remote(app):
    srv = _serve(RemoteHandler)
    yield Client(srv.server_address[1])
    srv.shutdown()
