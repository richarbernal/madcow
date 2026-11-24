"""Servidor HTTP: endpoints, favoritos y seguridad (cabecera Host, contraseña, cartera)."""
from __future__ import annotations

import json
import socket
import time

import pytest


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def test_serves_the_app(http):
    status, body = http.get("/")
    assert status == 200 and "<title>MadCow Charts</title>" in body
    assert http.get("/app.js")[0] == 200


def test_chart(http):
    status, d = http.get("/api/chart?symbol=SAN.MC&range=6mo&interval=1d")
    assert status == 200 and d["source"] == "Interactive Brokers" and d["currency"] == "EUR"
    assert len(d["bars"]) > 100 and not d.get("stale")


def test_chart_since_returns_only_recent_bars(http):
    _, full = http.get("/api/chart?symbol=AAPL&range=1y&interval=1d")
    since = full["bars"][-3]["time"]
    _, recent = http.get(f"/api/chart?symbol=AAPL&range=1y&interval=1d&live=1&since={since}")
    assert recent["bars"] == full["bars"][-3:]


@pytest.mark.parametrize("query, status", [
    ("symbol=ZZZZ&range=1y&interval=1d", 404),          # símbolo desconocido
    ("symbol=SAP.DE&range=1y&interval=1d", 404),        # sin permisos de datos
    ("symbol=bad%20sym&range=1y&interval=1d", 400),
    ("symbol=AAPL&range=7y&interval=1d", 400),
    ("symbol=AAPL&range=1y&interval=5m", 400),
])
def test_chart_errors(http, query, status):
    code, d = http.get("/api/chart?" + query)
    assert code == status and d["error"]


def test_chart_tws_closed_without_saved_data(http, tws):
    tws.offline = True
    status, d = http.get("/api/chart?symbol=AAPL&range=1y&interval=1d")
    assert status == 502 and d["offline"] is True and "TWS" in d["error"]


def test_demo_mode(http, tws):
    tws.offline = True
    status, d = http.get("/api/chart?symbol=AAPL&range=1y&interval=1d&demo=1")
    assert status == 200 and d["demo"] is True and d["source"] == "datos simulados"


def test_quotes(http):
    status, d = http.get("/api/quotes?symbols=AAPL,SAN.MC,SAP.DE,ZZZZ&range=1y&ext=1")
    assert status == 200
    q = {x["symbol"]: x for x in d["quotes"]}
    assert len(q["AAPL"]["closes"]) == 365 and q["AAPL"]["name"] == "APPLE INC"
    assert q["AAPL"]["ext"]["session"] == "post"            # el simulador está en post-mercado
    assert "ext" not in q["SAN.MC"]                         # Madrid no tiene sesión extendida
    assert "permisos" in q["SAP.DE"]["error"] and "no reconoce" in q["ZZZZ"]["error"]
    assert http.get("/api/quotes?symbols=AAPL&range=9y")[0] == 400


def test_search(http):
    _, d = http.get("/api/search?q=san")
    assert [r["symbol"] for r in d["results"]] == ["SAN.MC", "SAN", "^IBEX35", "BRK-B"]


def test_positions_on_this_pc(http):
    status, d = http.get("/api/positions")
    assert status == 200 and d["available"] is True and d["base"] == "EUR" and len(d["positions"]) == 5


def test_no_access_outside_static(http):
    assert http.get("/../server.py")[0] == 404
    assert http.get("/%2e%2e/server.py")[0] == 404


# ---------------------------------------------------------------------------
# Favoritos
# ---------------------------------------------------------------------------
def test_watchlist_roundtrip(http, app):
    assert http.get("/api/watchlist") == (200, {"symbols": []})
    status, d = http.put("/api/watchlist", {"symbols": ["aapl", "SAN.MC", "AAPL", " ^ibex "]})
    assert status == 200 and d["symbols"] == ["AAPL", "SAN.MC", "^IBEX"]   # mayúsculas, sin duplicados
    assert json.loads(app.WATCHLIST_FILE.read_text("utf-8"))["symbols"] == ["AAPL", "SAN.MC", "^IBEX"]
    assert http.get("/api/watchlist")[1]["symbols"] == ["AAPL", "SAN.MC", "^IBEX"]


@pytest.mark.parametrize("body, headers, status", [
    ('{"symbols": ["AAPL"]}', {"Content-Type": "text/plain"}, 415),  # obliga al preflight CORS
    ('{"symbols": ["BAD SYM"]}', {"Content-Type": "application/json"}, 400),
    ('{"symbols": "AAPL"}', {"Content-Type": "application/json"}, 400),
    ("no es json", {"Content-Type": "application/json"}, 400),
])
def test_watchlist_rejects_bad_requests(http, body, headers, status):
    assert http.request("PUT", "/api/watchlist", body=body, headers=headers)[0] == status


def test_damaged_watchlist_file_is_kept_aside(http, app):
    app.WATCHLIST_FILE.write_text("{esto no es json", "utf-8")
    assert http.get("/api/watchlist")[1]["symbols"] == []
    backups = list(app.WATCHLIST_FILE.parent.glob("watchlist.json.dañado-*"))
    assert len(backups) == 1 and backups[0].read_text("utf-8") == "{esto no es json"


# ---------------------------------------------------------------------------
# Cabecera Host (protección contra DNS rebinding)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host, status", [
    ("localhost:8000", 200),
    ("127.0.0.1:8000", 200),
    ("192.168.1.34:8000", 200),
    ("[::1]:8000", 200),
    (f"{socket.gethostname().lower().split('.')[0]}.local:8000", 200),
    ("evil.example.com", 403),
    (f"{socket.gethostname().lower().split('.')[0]}.evil.com", 403),
    ("", 403),
])
def test_host_header(http, host, status):
    assert http.get("/api/watchlist", headers={"Host": host})[0] == status


def test_allow_host(http, app):
    app.EXTRA_HOSTS.add("bolsa.casa")
    assert http.get("/api/watchlist", headers={"Host": "bolsa.casa:8000"})[0] == 200


def test_head_is_checked_too(http):
    assert http.request("HEAD", "/", headers={"Host": "evil.example.com"})[0] == 403


# ---------------------------------------------------------------------------
# Acceso desde otros dispositivos de la red
# ---------------------------------------------------------------------------
def test_lan_without_password_hides_positions(http_remote):
    assert http_remote.get("/api/watchlist")[0] == 200
    status, d = http_remote.get("/api/positions")
    assert status == 403 and d["available"] is False and "--password" in d["reason"]


def test_lan_with_password(http_remote, http, app):
    app.PASSWORD = "tortuga42"
    assert http_remote.get("/api/watchlist")[0] == 401
    t0 = time.monotonic()
    assert http_remote.get("/api/watchlist", password="mal")[0] == 401
    assert time.monotonic() - t0 >= 0.9                                    # frena los intentos a ciegas
    assert http_remote.get("/api/watchlist", password="tortuga42")[0] == 200
    assert http_remote.put("/api/watchlist", {"symbols": ["HACK"]})[0] == 401
    assert http_remote.put("/api/watchlist", {"symbols": ["MSFT"]}, password="tortuga42")[0] == 200
    status, d = http_remote.get("/api/positions", password="tortuga42")
    assert status == 200 and d["available"] is True
    assert http.get("/api/watchlist")[0] == 200                            # este PC no la necesita
