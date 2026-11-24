"""Caché en disco de datos históricos (SQLite, incluido en Python).

Guarda cada serie (fuente, símbolo, intervalo, horario) con sus velas. Así:
  * al volver a abrir un gráfico solo se piden a la fuente las velas nuevas;
  * si TWS / IB Gateway está cerrado, se pueden mostrar los datos guardados.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    source TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL, rth INTEGER NOT NULL,
    name TEXT, currency TEXT, exchange TEXT,
    covered_from INTEGER NOT NULL,  -- el histórico guardado está completo desde aquí (unix)
    updated_at INTEGER NOT NULL,    -- última vez que se consultó a la fuente (unix)
    PRIMARY KEY (source, symbol, interval, rth)
);
CREATE TABLE IF NOT EXISTS bars (
    source TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL, rth INTEGER NOT NULL,
    time INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume INTEGER, ext INTEGER,
    PRIMARY KEY (source, symbol, interval, rth, time)
) WITHOUT ROWID;
"""

Key = tuple  # (source, symbol, interval, rth)


class HistCache:
    def __init__(self, path: Path | str | None):
        """path=None → caché solo en memoria (se pierde al cerrar)."""
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            if self.path:
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def series(self, key: Key) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT name, currency, exchange, covered_from, updated_at FROM series "
                "WHERE source=? AND symbol=? AND interval=? AND rth=?", key).fetchone()
        if not row:
            return None
        return dict(zip(("name", "currency", "exchange", "covered_from", "updated_at"), row))

    def bars(self, key: Key, since: int = 0) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT time, open, high, low, close, volume, ext FROM bars "
                "WHERE source=? AND symbol=? AND interval=? AND rth=? AND time>=? ORDER BY time",
                (*key, since)).fetchall()
        out = []
        for t, o, h, l, c, v, ext in rows:
            bar = {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}
            if ext:
                bar["ext"] = True
            out.append(bar)
        return out

    def last_bars(self, key: Key, n: int) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT time, close FROM bars WHERE source=? AND symbol=? AND interval=? AND rth=? "
                "ORDER BY time DESC LIMIT ?", (*key, n)).fetchall()
        return [{"time": t, "close": c} for t, c in reversed(rows)]

    def save(self, key: Key, data: dict, covered_from: int, now: float | None = None) -> None:
        """Añade/actualiza las velas de `data` (formato de get_chart) y los datos de la serie."""
        now = int(now or time.time())
        rows = [(*key, b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"],
                 1 if b.get("ext") else 0) for b in data["bars"]]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.execute(
                "INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?)",
                (*key, data.get("name"), data.get("currency"), data.get("exchange"), int(covered_from), now))
            self._db.commit()

    def invalidate(self, key: Key) -> None:
        with self._lock:
            self._db.execute("DELETE FROM bars WHERE source=? AND symbol=? AND interval=? AND rth=?", key)
            self._db.execute("DELETE FROM series WHERE source=? AND symbol=? AND interval=? AND rth=?", key)
            self._db.commit()

    def stats(self) -> tuple[int, int]:
        with self._lock:
            n_series = self._db.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            n_bars = self._db.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        return n_series, n_bars
