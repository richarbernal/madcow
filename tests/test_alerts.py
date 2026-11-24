"""Alertas (alerts.py), indicadores en Python (indicators.py) y su API HTTP."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

import alerts
import indicators
from alerts import AlertEngine

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Indicadores en Python: idénticos a los de JavaScript
# ---------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js no está instalado")
def test_python_indicators_match_javascript():
    rnd = random.Random(7)
    closes, p = [], 100.0
    for _ in range(300):
        p *= 1 + rnd.gauss(0, 0.02)
        closes.append(round(p, 4))
    script = """
      import { sma, ema, bollinger, rsi } from "./static/indicators.js";
      const c = JSON.parse(process.argv[1]);
      const bb = bollinger(c, 20, 2);
      console.log(JSON.stringify({ sma: sma(c, 50), ema: ema(c, 21), rsi: rsi(c, 14), mid: bb.mid, up: bb.upper, lo: bb.lower }));
    """
    r = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(closes)],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    mid, up, lo = indicators.bollinger(closes, 20, 2)
    py = {"sma": indicators.sma(closes, 50), "ema": indicators.ema(closes, 21), "rsi": indicators.rsi(closes, 14),
          "mid": mid, "up": up, "lo": lo}
    for name, values in py.items():
        for a, b in zip(values, js[name]):
            assert (a is None and b is None) or a == pytest.approx(b, rel=1e-12, abs=1e-9), name


def test_python_rsi_wilder_example():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert indicators.rsi(closes, 14)[14] == pytest.approx(100 - 100 / (1 + 3.34 / 1.40))


# ---------------------------------------------------------------------------
# Validación y textos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("data, error", [
    ({"symbol": "bad sym", "type": "price_above", "value": 10}, "Símbolo"),
    ({"symbol": "AAPL", "type": "nope"}, "Tipo"),
    ({"symbol": "AAPL", "type": "price_above"}, "precio"),
    ({"symbol": "AAPL", "type": "price_below", "value": -3}, "mayor que cero"),
    ({"symbol": "AAPL", "type": "rsi_below", "value": 120}, "entre 0 y 100"),
    ({"symbol": "AAPL", "type": "ma_cross_up", "ma_fast": 200, "ma_slow": 50}, "rápida"),
    ({"symbol": "AAPL", "type": "bb_lower", "bb_mult": 9}, "desviaciones"),
])
def test_validation(data, error):
    with pytest.raises(ValueError, match=error):
        alerts.normalize(data)


@pytest.mark.parametrize("data, text", [
    ({"symbol": "aapl", "type": "price_above", "value": 1234.5}, "El precio cruza por encima de 1.234,50"),
    ({"symbol": "AAPL", "type": "rsi_below"}, "RSI(14) cruza por debajo de 30"),
    ({"symbol": "AAPL", "type": "bb_upper"}, "Cierra por encima de la banda superior de Bollinger (20, 2,0)"),
    ({"symbol": "AAPL", "type": "ma_cross_up", "ma_type": "EMA", "ma_fast": 9, "ma_slow": 21},
     "Cruce alcista de medias: EMA 9 / EMA 21"),
    ({"symbol": "AAPL", "type": "price_below_ma"}, "El precio cruza por debajo de la media SMA 200"),
])
def test_describe(data, text):
    a = alerts.normalize(data)
    assert alerts.describe(a) == text and a["symbol"] == "AAPL"


def test_history_needed():
    assert alerts.history_needed(alerts.normalize({"symbol": "X", "type": "rsi_below"})) == "1y"
    assert alerts.history_needed(alerts.normalize({"symbol": "X", "type": "price_above_ma", "ma_period": 300})) == "2y"


# ---------------------------------------------------------------------------
# Evaluación con series sintéticas
# ---------------------------------------------------------------------------
def test_evaluate_conditions():
    up = [100 + i for i in range(250)]              # tendencia alcista limpia
    met, state, msg = alerts.evaluate(alerts.normalize({"symbol": "X", "type": "price_above", "value": 300}), up, "USD")
    assert met and state == "precio 349,00 USD" and "superado 300,00 USD" in msg
    assert alerts.evaluate(alerts.normalize({"symbol": "X", "type": "rsi_above", "value": 70}), up)[0]
    assert alerts.evaluate(alerts.normalize({"symbol": "X", "type": "ma_cross_up", "ma_fast": 20, "ma_slow": 50}), up)[0]
    assert not alerts.evaluate(alerts.normalize({"symbol": "X", "type": "price_below_ma", "ma_period": 50}), up)[0]
    spike = [100.0] * 30 + [130.0]                   # salto fuera de la banda superior
    assert alerts.evaluate(alerts.normalize({"symbol": "X", "type": "bb_upper"}), spike)[0]
    with pytest.raises(alerts.InsufficientData):
        alerts.evaluate(alerts.normalize({"symbol": "X", "type": "price_above_ma", "ma_period": 200}), up[:50])


# ---------------------------------------------------------------------------
# Motor: saltar al cruzar, una vez o repetidamente
# ---------------------------------------------------------------------------
class Market:
    """Fuente de datos controlable para el motor: el último cierre lo fija el test."""

    def __init__(self):
        self.price = {"AAPL": 100.0}
        self.stale = False

    def __call__(self, symbol, rng):
        if symbol not in self.price:
            raise ValueError(f"IBKR no reconoce el símbolo {symbol}")
        closes = [100.0] * 260 + [self.price[symbol]]
        return {"bars": [{"close": c} for c in closes], "currency": "USD", "stale": self.stale}


@pytest.fixture
def market():
    return Market()


@pytest.fixture
def engine(tmp_path, market):
    return AlertEngine(tmp_path / "alerts.json", fetch=market)


def test_fires_once_when_crossing(engine, market):
    a = engine.create({"symbol": "AAPL", "type": "price_above", "value": 110})
    assert a["status"]["met"] is False and a["armed"] is True        # estado inicial, sin saltar
    assert engine.check() == []
    market.price["AAPL"] = 111
    (event,) = engine.check()
    assert event["message"] == "AAPL: el precio ha superado 110,00 USD (111,00 USD)"
    a = engine.snapshot()["alerts"][0]
    assert a["enabled"] is False and a["triggered"] == 1             # de un solo uso: se desactiva
    market.price["AAPL"] = 100
    engine.check()
    market.price["AAPL"] = 115
    assert engine.check() == []


def test_repeating_alert_rearms_when_condition_stops(engine, market):
    engine.create({"symbol": "AAPL", "type": "price_above", "value": 110, "repeat": True})
    fired = []
    for price in (111, 112, 108, 113, 113):
        market.price["AAPL"] = price
        fired += engine.check()
    assert len(fired) == 2                     # cruza a 111, deja de cumplirse a 108, vuelve a cruzar a 113
    assert engine.snapshot()["alerts"][0]["enabled"] is True


def test_already_met_when_created_does_not_fire(engine, market):
    market.price["AAPL"] = 120
    engine.create({"symbol": "AAPL", "type": "price_above", "value": 110})
    assert engine.check() == []
    market.price["AAPL"] = 105
    engine.check()
    market.price["AAPL"] = 112
    assert len(engine.check()) == 1


def test_no_data_or_no_connection_never_fires(engine, market):
    engine.create({"symbol": "ZZZZ", "type": "price_above", "value": 1})
    a = engine.create({"symbol": "AAPL", "type": "price_above", "value": 110})
    market.stale = True
    market.price["AAPL"] = 200
    assert engine.check() == []
    status = {x["symbol"]: x["status"] for x in engine.snapshot()["alerts"]}
    assert "no reconoce" in status["ZZZZ"]["text"] and status["AAPL"]["error"] is True
    assert engine._get(a["id"])["armed"] is True       # el estado previo se conserva
    market.stale = False
    assert len(engine.check()) == 1                    # con datos frescos, salta


def test_reenabling_takes_current_state(engine, market):
    a = engine.create({"symbol": "AAPL", "type": "price_above", "value": 110})
    engine.update(a["id"], {"enabled": False})
    market.price["AAPL"] = 120
    assert engine.check() == []                        # desactivada
    engine.update(a["id"], {"enabled": True})
    assert engine.check() == []                        # ya se cumplía al reactivarla: no salta


def test_events_read_clear_and_persistence(engine, market, tmp_path):
    engine.create({"symbol": "AAPL", "type": "price_above", "value": 110, "repeat": True})
    for price in (111, 100, 112):
        market.price["AAPL"] = price
        engine.check()
    snap = engine.snapshot()
    assert snap["unread"] == 2 and [e["id"] for e in snap["events"]] == [2, 1]   # el más reciente primero
    engine.mark_read(1)
    assert engine.snapshot()["unread"] == 1
    reopened = AlertEngine(tmp_path / "alerts.json", fetch=market)             # tras reiniciar el servidor
    assert len(reopened.snapshot()["alerts"]) == 1 and reopened.snapshot()["unread"] == 1
    reopened.clear_events()
    assert reopened.snapshot()["events"] == [] and reopened.snapshot()["last_event"] == 2


def test_damaged_file_is_kept_aside(tmp_path, market):
    (tmp_path / "alerts.json").write_text("{roto", "utf-8")
    assert AlertEngine(tmp_path / "alerts.json", fetch=market).snapshot()["alerts"] == []
    assert len(list(tmp_path.glob("alerts.json.dañado-*"))) == 1


# ---------------------------------------------------------------------------
# API HTTP y de principio a fin con el TWS simulado
# ---------------------------------------------------------------------------
def test_alerts_api(http):
    assert http.get("/api/alerts")[1]["alerts"] == []
    status, a = http.request("POST", "/api/alerts", body={"symbol": "san.mc", "type": "rsi_below", "value": 25})
    assert status == 201 and a["symbol"] == "SAN.MC" and a["description"] == "RSI(14) cruza por debajo de 25"
    assert a["status"]["text"].startswith("RSI ")                                # ya evaluada
    assert http.request("POST", "/api/alerts", body={"symbol": "AAPL", "type": "x"})[0] == 400
    assert http.request("POST", "/api/alerts", body="{}", headers={"Content-Type": "text/plain"})[0] == 415
    status, snap = http.put(f"/api/alerts/{a['id']}", {"enabled": False})
    assert status == 200 and snap["alerts"][0]["enabled"] is False
    assert http.put("/api/alerts/nope", {"enabled": True})[0] == 404
    assert http.request("DELETE", f"/api/alerts/{a['id']}")[1]["alerts"] == []
    assert http.request("DELETE", "/api/alerts/nope")[0] == 404


def test_end_to_end_with_simulated_tws(http, app, tws):
    level = round(tws.last_close("AAPL") * 1.02, 2)
    http.request("POST", "/api/alerts", body={"symbol": "AAPL", "type": "price_above", "value": level})
    assert app.ALERTS.check() == []
    tws.last_mult = {"AAPL": 1.03}            # la sesión en curso sube un 3 %
    (event,) = app.ALERTS.check()
    assert event["symbol"] == "AAPL" and "superado" in event["message"]
    snap = http.get("/api/alerts")[1]
    assert snap["unread"] == 1 and snap["events"][0]["id"] == snap["last_event"]
    assert http.request("POST", "/api/alerts/read", body={"upto": snap["last_event"]})[1]["unread"] == 0
    assert http.request("DELETE", "/api/alerts/events")[1]["events"] == []


def test_alerts_respect_lan_password(http_remote, app):
    app.PASSWORD = "clave"
    assert http_remote.get("/api/alerts")[0] == 401
    assert http_remote.request("POST", "/api/alerts", body={"symbol": "AAPL", "type": "rsi_below"})[0] == 401
    assert http_remote.get("/api/alerts", password="clave")[0] == 200
