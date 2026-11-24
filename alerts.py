"""Alertas de MadCow: precio, RSI, bandas de Bollinger y medias móviles.

El servidor las comprueba cada minuto con velas diarias (la última es la sesión en curso,
así que reaccionan durante el día) y guarda alertas y avisos en alerts.json.

Cada alerta salta al CRUZAR su condición, es decir, cuando pasa de no cumplirse a
cumplirse. Si ya se cumple al crearla, no salta hasta que deje de cumplirse y vuelva a
hacerlo. Las de un solo uso se desactivan al saltar; las repetibles se vuelven a armar
cuando la condición deja de cumplirse.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from indicators import bollinger, moving_average, rsi

SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,20}$")
MAX_ALERTS = 200
MAX_EVENTS = 200

TYPES = {
    "price_above": "El precio cruza por encima de",
    "price_below": "El precio cruza por debajo de",
    "rsi_above": "El RSI cruza por encima de",
    "rsi_below": "El RSI cruza por debajo de",
    "bb_upper": "Cierra por encima de la banda superior",
    "bb_lower": "Cierra por debajo de la banda inferior",
    "ma_cross_up": "Cruce alcista de medias",
    "ma_cross_down": "Cruce bajista de medias",
    "price_above_ma": "El precio cruza por encima de la media",
    "price_below_ma": "El precio cruza por debajo de la media",
}


class InsufficientData(Exception):
    pass


def num(v: float, d: int = 2) -> str:
    """Número con formato español: 1.234,56"""
    return f"{v:,.{d}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def pct(v: float) -> str:
    return ("+" if v > 0 else "") + num(v * 100) + " %"


# ---------------------------------------------------------------------------
# Validación y descripción
# ---------------------------------------------------------------------------
def _int(data, key, default, lo, hi):
    try:
        v = int(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"«{key}» debe ser un número entero") from None
    if not lo <= v <= hi:
        raise ValueError(f"«{key}» debe estar entre {lo} y {hi}")
    return v


def normalize(data: dict) -> dict:
    """Valida los datos de una alerta nueva. Lanza ValueError con un mensaje para el usuario."""
    if not isinstance(data, dict):
        raise ValueError("Se esperaba un objeto JSON")
    symbol = str(data.get("symbol", "")).strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError("Símbolo no válido")
    kind = data.get("type")
    if kind not in TYPES:
        raise ValueError("Tipo de alerta no válido")
    a = {"symbol": symbol, "type": kind, "repeat": bool(data.get("repeat", False))}
    if kind.startswith("price_") and not kind.endswith("_ma"):
        try:
            a["value"] = float(data["value"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Indica el precio de la alerta") from None
        if not 0 < a["value"] < 1e9:
            raise ValueError("El precio debe ser mayor que cero")
    elif kind.startswith("rsi_"):
        try:
            a["value"] = float(data.get("value", 70 if kind == "rsi_above" else 30))
        except (TypeError, ValueError):
            raise ValueError("Nivel de RSI no válido") from None
        if not 0 < a["value"] < 100:
            raise ValueError("El nivel de RSI debe estar entre 0 y 100")
        a["rsi_period"] = _int(data, "rsi_period", 14, 2, 100)
    elif kind.startswith("bb_"):
        a["bb_period"] = _int(data, "bb_period", 20, 2, 200)
        try:
            a["bb_mult"] = float(data.get("bb_mult", 2))
        except (TypeError, ValueError):
            raise ValueError("Desviaciones no válidas") from None
        if not 0.5 <= a["bb_mult"] <= 5:
            raise ValueError("Las desviaciones deben estar entre 0,5 y 5")
    if kind.startswith("ma_") or kind.endswith("_ma"):
        a["ma_type"] = "EMA" if data.get("ma_type") == "EMA" else "SMA"
    if kind.startswith("ma_cross"):
        a["ma_fast"] = _int(data, "ma_fast", 50, 2, 500)
        a["ma_slow"] = _int(data, "ma_slow", 200, 2, 500)
        if a["ma_fast"] >= a["ma_slow"]:
            raise ValueError("La media rápida debe tener menos sesiones que la lenta")
    elif kind.endswith("_ma"):
        a["ma_period"] = _int(data, "ma_period", 200, 2, 500)
    return a


def describe(a: dict) -> str:
    kind = a["type"]
    if kind in ("price_above", "price_below"):
        return f"{TYPES[kind]} {num(a['value'])}"
    if kind.startswith("rsi_"):
        return f"RSI({a['rsi_period']}) cruza por {'encima' if kind == 'rsi_above' else 'debajo'} de {num(a['value'], 0 if a['value'] == int(a['value']) else 1)}"
    if kind.startswith("bb_"):
        return f"{TYPES[kind]} de Bollinger ({a['bb_period']}, {num(a['bb_mult'], 1)})"
    if kind.startswith("ma_cross"):
        t = a["ma_type"]
        return f"{TYPES[kind]}: {t} {a['ma_fast']} / {t} {a['ma_slow']}"
    return f"{TYPES[kind]} {a['ma_type']} {a['ma_period']}"


def history_needed(a: dict) -> str:
    """Rango de velas diarias necesario (como mínimo un año, por el suavizado del RSI)."""
    n = max(a.get("ma_slow", 0), a.get("ma_period", 0), a.get("bb_period", 0), a.get("rsi_period", 0) * 4) + 5
    return "1y" if n <= 240 else "2y" if n <= 490 else "5y"


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------
def evaluate(a: dict, closes: list, currency: str = "") -> tuple:
    """Devuelve (se cumple, texto del estado actual, texto del aviso si salta)."""
    if len(closes) < 2:
        raise InsufficientData()
    c = closes[-1]
    cur = f" {currency}" if currency else ""
    kind = a["type"]

    def last(values):
        if values[-1] is None:
            raise InsufficientData()
        return values[-1]

    if kind in ("price_above", "price_below"):
        met = c > a["value"] if kind == "price_above" else c < a["value"]
        word = "superado" if kind == "price_above" else "bajado de"
        return met, f"precio {num(c)}{cur}", f"el precio ha {word} {num(a['value'])}{cur} ({num(c)}{cur})"
    if kind.startswith("rsi_"):
        r = last(rsi(closes, a["rsi_period"]))
        met = r > a["value"] if kind == "rsi_above" else r < a["value"]
        side = "encima" if kind == "rsi_above" else "debajo"
        return met, f"RSI {num(r, 1)}", f"RSI({a['rsi_period']}) por {side} de {num(a['value'], 0)}: {num(r, 1)}"
    if kind.startswith("bb_"):
        _, upper, lower = bollinger(closes, a["bb_period"], a["bb_mult"])
        band = last(upper) if kind == "bb_upper" else last(lower)
        met = c > band if kind == "bb_upper" else c < band
        name = "superior" if kind == "bb_upper" else "inferior"
        return (met, f"precio {num(c)} · banda {name} {num(band)}",
                f"cierra {'por encima' if kind == 'bb_upper' else 'por debajo'} de la banda {name} "
                f"({num(c)}{cur} frente a {num(band)})")
    if kind.startswith("ma_cross"):
        t = a["ma_type"]
        fast = last(moving_average(closes, a["ma_fast"], t))
        slow = last(moving_average(closes, a["ma_slow"], t))
        met = fast > slow if kind == "ma_cross_up" else fast < slow
        rel = fast / slow - 1
        return (met, f"{t} {a['ma_fast']} {'por encima' if rel >= 0 else 'por debajo'} de {t} {a['ma_slow']} ({pct(rel)})",
                f"cruce {'alcista' if kind == 'ma_cross_up' else 'bajista'}: {t} {a['ma_fast']} "
                f"{'sobre' if kind == 'ma_cross_up' else 'bajo'} {t} {a['ma_slow']}")
    t = a["ma_type"]
    m = last(moving_average(closes, a["ma_period"], t))
    met = c > m if kind == "price_above_ma" else c < m
    rel = c / m - 1
    return (met, f"precio {pct(rel)} {'sobre' if rel >= 0 else 'bajo'} {t} {a['ma_period']}",
            f"el precio cruza por {'encima' if kind == 'price_above_ma' else 'debajo'} de la {t} {a['ma_period']} "
            f"({num(c)}{cur} frente a {num(m)})")


# ---------------------------------------------------------------------------
# Motor: guarda las alertas y las comprueba periódicamente
# ---------------------------------------------------------------------------
class AlertEngine:
    def __init__(self, path, fetch, interval: float = 60):
        """`fetch(symbol, rng)` devuelve los datos de get_chart para velas diarias."""
        self.path = Path(path)
        self.fetch = fetch
        self.interval = interval
        self._lock = threading.RLock()
        self._data = self._load()
        self._stop = threading.Event()

    # ---- persistencia ----
    def _load(self) -> dict:
        empty = {"alerts": [], "events": [], "next_event": 1}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return empty
        except (json.JSONDecodeError, UnicodeDecodeError):
            backup = self.path.with_name(f"{self.path.name}.dañado-{int(time.time())}")
            self.path.replace(backup)
            print(f"Aviso: {self.path.name} no era JSON válido; copiado a {backup.name}")
            return empty
        return {**empty, **(data if isinstance(data, dict) else {})}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    # ---- consulta y edición ----
    def snapshot(self) -> dict:
        with self._lock:
            alerts = [{**a, "description": describe(a)} for a in self._data["alerts"]]
            events = list(reversed(self._data["events"]))[:50]
            unread = sum(1 for e in self._data["events"] if not e.get("read"))
            return {"alerts": alerts, "events": events, "unread": unread,
                    "last_event": self._data["next_event"] - 1}

    def create(self, data: dict) -> dict:
        a = normalize(data)
        with self._lock:
            if len(self._data["alerts"]) >= MAX_ALERTS:
                raise ValueError(f"Máximo {MAX_ALERTS} alertas")
            a.update(id=secrets.token_hex(4), enabled=True, armed=None, created=int(time.time()),
                     triggered=0, last_triggered=None, status=None)
            self._data["alerts"].append(a)
            self._save()
        self.check([a["id"]])  # estado inicial (sin saltar)
        return self._get(a["id"])

    def _get(self, alert_id: str) -> dict:
        with self._lock:
            for a in self._data["alerts"]:
                if a["id"] == alert_id:
                    return {**a, "description": describe(a)}
        raise KeyError(alert_id)

    def update(self, alert_id: str, changes: dict) -> dict:
        with self._lock:
            a = next((x for x in self._data["alerts"] if x["id"] == alert_id), None)
            if a is None:
                raise KeyError(alert_id)
            if "enabled" in changes:
                a["enabled"] = bool(changes["enabled"])
                if a["enabled"]:
                    a["armed"] = None  # al reactivarla se toma de nuevo el estado actual
            if "repeat" in changes:
                a["repeat"] = bool(changes["repeat"])
            self._save()
        return self._get(alert_id)

    def delete(self, alert_id: str) -> None:
        with self._lock:
            before = len(self._data["alerts"])
            self._data["alerts"] = [a for a in self._data["alerts"] if a["id"] != alert_id]
            if len(self._data["alerts"]) == before:
                raise KeyError(alert_id)
            self._save()

    def mark_read(self, upto: int) -> None:
        with self._lock:
            for e in self._data["events"]:
                if e["id"] <= upto:
                    e["read"] = True
            self._save()

    def clear_events(self) -> None:
        with self._lock:
            self._data["events"] = []
            self._save()

    # ---- comprobación ----
    def check(self, only: list | None = None) -> list:
        """Evalúa las alertas activas (o solo las indicadas). Devuelve los avisos nuevos."""
        with self._lock:
            todo = [dict(a) for a in self._data["alerts"]
                    if a["enabled"] and (only is None or a["id"] in only)]
        results = {}
        for symbol in {a["symbol"] for a in todo}:
            mine = [a for a in todo if a["symbol"] == symbol]
            rng = max((history_needed(a) for a in mine), key=["1y", "2y", "5y"].index)
            try:
                data = self.fetch(symbol, rng)
            except Exception as e:  # símbolo desconocido, sin conexión…
                for a in mine:
                    results[a["id"]] = ("error", str(e))
                continue
            if data.get("stale") or data.get("demo"):
                for a in mine:
                    results[a["id"]] = ("error", "sin conexión con la fuente de datos")
                continue
            closes = [b["close"] for b in data["bars"]]
            for a in mine:
                try:
                    results[a["id"]] = ("ok", evaluate(a, closes, data.get("currency", "")))
                except InsufficientData:
                    results[a["id"]] = ("error", "pocos datos para calcularla")

        now = int(time.time())
        fired = []
        with self._lock:
            for a in self._data["alerts"]:
                if a["id"] not in results or not a["enabled"]:
                    continue
                kind, payload = results[a["id"]]
                if kind == "error":
                    a["status"] = {"text": payload, "met": None, "checked": now, "error": True}
                    continue
                met, state, message = payload
                a["status"] = {"text": state, "met": met, "checked": now, "error": False}
                if a["armed"] is None:            # primera comprobación: solo toma el estado
                    a["armed"] = not met
                elif met and a["armed"]:          # ha cruzado: salta
                    event = {"id": self._data["next_event"], "alert_id": a["id"], "symbol": a["symbol"],
                             "message": f"{a['symbol']}: {message}", "time": now, "read": False}
                    self._data["next_event"] += 1
                    self._data["events"].append(event)
                    fired.append(event)
                    a["triggered"] += 1
                    a["last_triggered"] = now
                    a["armed"] = False
                    if not a["repeat"]:
                        a["enabled"] = False
                elif not met:
                    a["armed"] = True             # se vuelve a armar
            self._data["events"] = self._data["events"][-MAX_EVENTS:]
            if results:
                self._save()
        for e in fired:
            print(f"[{time.strftime('%H:%M:%S')}] Alerta: {e['message']}", flush=True)
        return fired

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.check()
                except Exception as e:  # nunca debe parar el servidor
                    print(f"Error al comprobar alertas: {e}", flush=True)
        threading.Thread(target=loop, name="alertas", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
