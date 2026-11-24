"""Caché de datos históricos en disco (histcache.py + server.get_chart)."""
from __future__ import annotations

import datetime as dt

import pytest

from histcache import HistCache


def durations(tws, start):
    """Duraciones pedidas a IBKR desde el índice `start` de su registro."""
    return [r[1] for r in tws.history_requests[start:]]


def test_first_request_downloads_then_serves_from_cache(app, tws):
    first = app.get_chart("AAPL", "1y", "1d")
    assert durations(tws, 0) == ["1 Y"] and len(first["bars"]) == 365
    n = len(tws.history_requests)
    again = app.get_chart("AAPL", "1y", "1d")
    subset = app.get_chart("AAPL", "3mo", "1d")       # contenido en lo guardado
    assert len(tws.history_requests) == n
    assert again["bars"] == first["bars"]
    assert 85 <= len(subset["bars"]) <= 93 and subset["bars"][-1] == first["bars"][-1]


def test_expired_cache_asks_only_for_recent_bars(app, tws, monkeypatch):
    first = app.get_chart("AAPL", "1y", "1d")
    monkeypatch.setattr(app, "CACHE_TTL", 0)
    n = len(tws.history_requests)
    second = app.get_chart("AAPL", "1y", "1d")
    (dur,) = durations(tws, n)
    assert dur.endswith(" D") and int(dur.split()[0]) < 15   # unas pocas sesiones, no el año entero
    assert second["bars"] == first["bars"]                    # sin duplicados ni huecos


def test_new_session_and_live_candle_are_merged(app, tws, monkeypatch):
    first = app.get_chart("AAPL", "1y", "1d")
    monkeypatch.setattr(app, "CACHE_TTL", 0)
    tws.last_mult = {"AAPL": 1.03}       # la vela en curso sube un 3 %
    updated = app.get_chart("AAPL", "1y", "1d")["bars"]
    assert updated[-1]["close"] == pytest.approx(first["bars"][-1]["close"] * 1.03)
    assert updated[:-1] == first["bars"][:-1]
    tws.last_mult, tws.extra_days = {}, 1  # se abre una sesión nueva
    times = [b["time"] for b in app.get_chart("AAPL", "1y", "1d")["bars"]]
    assert times[-1] - first["bars"][-1]["time"] == 86400 and times == sorted(set(times))


def test_larger_range_downloads_full_history_once(app, tws, monkeypatch):
    app.get_chart("AAPL", "1y", "1d")
    n = len(tws.history_requests)
    five = app.get_chart("AAPL", "5y", "1d")
    assert durations(tws, n) == ["5 Y"] and len(five["bars"]) == 1300  # todo lo que tiene el simulador
    monkeypatch.setattr(app, "CACHE_TTL", 0)
    n = len(tws.history_requests)
    app.get_chart("AAPL", "2y", "1d")                                    # ya cubierto: incremental
    assert durations(tws, n)[0].endswith(" D")


def test_tws_closed_serves_saved_data(app, tws, monkeypatch):
    saved = app.get_chart("AAPL", "1y", "1d")
    monkeypatch.setattr(app, "CACHE_TTL", 0)
    tws.offline = True
    stale = app.get_chart("AAPL", "1y", "1d")
    assert stale["stale"] is True and "TWS" in stale["stale_reason"]
    assert stale["bars"] == saved["bars"] and stale["updated_at"] > 0
    with pytest.raises(app.OfflineError):
        app.get_chart("MSFT", "1y", "1d")  # nunca guardado


def test_split_is_detected_and_series_downloaded_again(app, tws, monkeypatch):
    before = app.get_chart("MSFT", "1y", "1d")["bars"]
    monkeypatch.setattr(app, "CACHE_TTL", 0)
    tws.split = {"MSFT": 0.25}  # split 4x1: IBKR ajusta todo el histórico
    n = len(tws.history_requests)
    after = app.get_chart("MSFT", "1y", "1d")["bars"]
    assert durations(tws, n)[-1] == "1 Y"  # tras la descarga incremental, la completa
    assert after[0]["close"] == pytest.approx(before[0]["close"] * 0.25)
    assert after[-10]["close"] == pytest.approx(before[-10]["close"] * 0.25)


def test_extended_hours_series_kept_apart(app):
    ext = app.get_chart("AAPL", "1mo", "1h", rth=False)["bars"]
    rth = app.get_chart("AAPL", "1mo", "1h", rth=True)["bars"]
    assert any(b.get("ext") for b in ext) and not any(b.get("ext") for b in rth)


def test_ttl_override_for_live_updates(app, tws):
    app.get_chart("AAPL", "1y", "1d")
    n = len(tws.history_requests)
    app.get_chart("AAPL", "1y", "1d", ttl=0)  # lo que usa el gráfico en vivo (?live=1)
    assert len(tws.history_requests) == n + 1


def test_cache_survives_restart(app, tmp_path):
    app.get_chart("AAPL", "1y", "1d")
    app.get_chart("SAN.MC", "6mo", "1d")
    reopened = HistCache(tmp_path / "cache.sqlite")
    n_series, n_bars = reopened.stats()
    assert n_series == 2 and n_bars > 500
    key = ("ibkr", "AAPL", "1d", 1)
    assert reopened.series(key)["name"] == "APPLE INC"
    reopened.invalidate(key)
    assert reopened.series(key) is None and reopened.stats()[0] == 1


def test_range_start():
    import server
    now = dt.datetime(2026, 11, 24, 12, tzinfo=dt.timezone.utc).timestamp()
    assert server.range_start("max", now) == 0
    assert server.range_start("ytd", now) == int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    assert server.range_start("1y", now) == int(now - 366 * 86400)
