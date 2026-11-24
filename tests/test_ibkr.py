"""Fuente de datos Interactive Brokers (ibkr.py) contra el TWS simulado."""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
from ib_async import Contract

import ibkr

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Traducción de símbolos (sin conexión)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("symbol, sec, sym, exch, cur, primary", [
    ("AAPL", "STK", "AAPL", "SMART", "USD", ""),
    ("BRK-B", "STK", "BRK B", "SMART", "USD", ""),
    ("SAN.MC", "STK", "SAN", "SMART", "EUR", "BM"),
    ("SAP.DE", "STK", "SAP", "SMART", "EUR", "IBIS"),
    ("ASML.AS", "STK", "ASML", "SMART", "EUR", "AEB"),
    ("NESN.SW", "STK", "NESN", "SMART", "CHF", "EBS"),
    ("^IBEX", "IND", "IBEX35", "MEFFRV", "", ""),
    ("^GSPC", "IND", "SPX", "CBOE", "", ""),
    ("^ESTX50", "IND", "ESTX50", "", "", ""),  # índice sin traducción: se busca por su símbolo
])
def test_candidates(symbol, sec, sym, exch, cur, primary):
    c = ibkr.candidates(symbol)[0]
    assert (c.secType, c.symbol, c.exchange, c.currency, c.primaryExchange) == (sec, sym, exch, cur, primary)


def test_candidates_london_tries_trailing_dot():
    assert [c.symbol for c in ibkr.candidates("BP.L")] == ["BP", "BP."]


def test_candidates_unknown_suffix():
    with pytest.raises(ibkr.SymbolError, match="Sufijo de bolsa no soportado"):
        ibkr.candidates("SAN.XX")


@pytest.mark.parametrize("contract, expected", [
    (Contract(secType="STK", symbol="SAN", primaryExchange="BM", currency="EUR"), "SAN.MC"),
    (Contract(secType="STK", symbol="BRK B", primaryExchange="NYSE", currency="USD"), "BRK-B"),
    (Contract(secType="STK", symbol="BP.", primaryExchange="LSE", currency="GBP"), "BP.L"),
    (Contract(secType="STK", symbol="AAPL", primaryExchange="", currency="USD"), "AAPL"),  # sin bolsa: EE. UU.
    (Contract(secType="IND", symbol="IBEX35", exchange="MEFFRV"), "^IBEX35"),
    (Contract(secType="STK", symbol="BHP", primaryExchange="ASX", currency="AUD"), None),  # bolsa no soportada
    (Contract(secType="OPT", symbol="AAPL", currency="USD"), None),
])
def test_app_symbol(contract, expected):
    assert ibkr.app_symbol(contract) == expected


# ---------------------------------------------------------------------------
# Sesiones de EE. UU. y horario de verano (sin depender de tzdata)
# ---------------------------------------------------------------------------
def ts(y, mo, d, h, mi, offset):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=offset))).timestamp())


@pytest.mark.parametrize("when, session", [
    ((2026, 11, 20, 9, 25, -5), "pre"),
    ((2026, 11, 20, 9, 30, -5), "regular"),
    ((2026, 11, 20, 15, 55, -5), "regular"),
    ((2026, 11, 20, 16, 0, -5), "post"),
    ((2026, 11, 20, 19, 55, -5), "post"),
    ((2026, 11, 20, 20, 0, -5), "overnight"),
    ((2026, 11, 21, 10, 0, -5), "overnight"),   # sábado
    ((2026, 7, 15, 4, 0, -4), "pre"),           # verano (EDT)
    ((2026, 3, 6, 9, 30, -5), "regular"),       # viernes antes del cambio de hora
    ((2026, 3, 9, 9, 30, -4), "regular"),       # lunes después del cambio de hora
    ((2026, 10, 30, 15, 59, -4), "regular"),    # aún en horario de verano
    ((2026, 11, 2, 16, 0, -5), "post"),         # lunes tras volver al horario de invierno
])
def test_us_session(when, session):
    assert ibkr.us_session(ts(*when)) == session


def test_since_duration():
    now = time.time()
    assert ibkr.since_duration(int(now - 5 * 86400), "1d") == "7 D"
    assert ibkr.since_duration(int(now - 2 * 86400), "1h") == "4 D"
    assert ibkr.since_duration(int(now - 3 * 86400), "1wk") == "1 Y"
    assert ibkr.since_duration(int(now - 400 * 86400), "1d") is None  # hueco grande: rango completo
    assert ibkr.since_duration(int(now - 90 * 86400), "1h") is None


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def test_connects_read_only_detecting_port_and_free_client_id(source, tws):
    source.chart("AAPL", "1mo", "1d")
    assert source.connected_port == 4001             # 7496 cerrado → prueba el siguiente
    assert source._ib.client_id == 118               # 117 en uso → otro clientId
    assert source._ib.fetch_fields == ibkr_fetch("POSITIONS", "ACCOUNT_UPDATES")


def test_no_positions_flag_skips_account_data(tws):
    src = ibkr.IBKRSource(positions=False)
    try:
        src.chart("AAPL", "1mo", "1d")
        assert src._ib.fetch_fields == ibkr_fetch()
        with pytest.raises(ibkr.Unavailable, match="--no-positions"):
            src.positions()
    finally:
        src.close()


def ibkr_fetch(*names):
    import ib_async
    flags = ib_async.ib.StartupFetch(0)
    for n in names:
        flags |= getattr(ib_async.ib.StartupFetch, n)
    return flags


def test_tws_closed_explains_how_to_enable_api(source, tws):
    tws.offline = True
    with pytest.raises(ibkr.Unavailable, match="7496, 4001, 7497, 4002.*Habilitar clientes ActiveX y Socket"):
        source.chart("AAPL", "1mo", "1d")


def test_failed_connection_is_not_retried_on_every_request(source, tws, monkeypatch):
    monkeypatch.setattr(ibkr, "RETRY_AFTER", 60)
    tws.offline = True
    for _ in range(5):
        with pytest.raises(ibkr.Unavailable):
            source.chart("AAPL", "1mo", "1d")
    assert tws.connect_attempts == 4  # un intento por puerto, solo la primera vez


def test_reconnects_when_tws_comes_back(source, tws):
    source.chart("AAPL", "1mo", "1d")
    tws.offline = True
    with pytest.raises(ibkr.Unavailable):
        source.chart("MSFT", "1mo", "1d")
    tws.offline = False
    assert source.chart("MSFT", "1mo", "1d")["bars"]


# ---------------------------------------------------------------------------
# Velas
# ---------------------------------------------------------------------------
def test_daily_chart(source, tws):
    d = source.chart("SAN.MC", "1y", "1d")
    assert (d["name"], d["currency"], d["exchange"], d["demo"]) == ("BANCO SANTANDER SA", "EUR", "BM", False)
    assert len(d["bars"]) == 365
    times = [b["time"] for b in d["bars"]]
    assert times == sorted(times)
    assert d["bars"][-1]["close"] == pytest.approx(tws.last_close("SAN"))
    assert set(d["bars"][0]) == {"time", "open", "high", "low", "close", "volume"}


def test_london_symbol_with_trailing_dot_and_index(source):
    assert source.chart("BP.L", "1mo", "1d")["name"] == "BP PLC"
    ibex = source.chart("^IBEX", "1mo", "1d")
    assert ibex["name"] == "IBEX 35 Index"
    assert all(b["volume"] == 0 for b in ibex["bars"])  # IBKR da volumen -1 en índices


def test_unknown_symbol(source):
    with pytest.raises(ibkr.SymbolError, match="no reconoce el símbolo ZZZZ"):
        source.chart("ZZZZ", "1y", "1d")


def test_missing_market_data_permissions(source):
    with pytest.raises(ibkr.SymbolError, match="no tiene permisos de datos de mercado para SAP.DE"):
        source.chart("SAP.DE", "1y", "1d")


def test_since_requests_only_recent_bars(source, tws):
    source.chart("AAPL", "1y", "1d", since=int(time.time() - 3 * 86400))
    assert tws.history_requests[-1][1:3] == ("5 D", "1 day")


def test_extended_hours_marked_only_for_us_intraday(source, tws):
    ext = source.chart("AAPL", "1mo", "1h", rth=False)["bars"]
    rth = source.chart("AAPL", "1mo", "1h", rth=True)["bars"]
    assert any(b.get("ext") for b in ext) and not any(b.get("ext") for b in rth)
    assert len(ext) > len(rth)
    assert all(ibkr.us_session(b["time"]) != "regular" for b in ext if b.get("ext"))
    assert not any(b.get("ext") for b in source.chart("SAN.MC", "1mo", "1h", rth=False)["bars"])
    source.chart("AAPL", "1y", "1d", rth=False)  # en diario no se pide horario extendido
    assert tws.history_requests[-1][3] is True


def test_concurrent_requests(source):
    results, errors = [], []

    def worker(sym):
        try:
            results.append(source.chart(sym, "3mo", "1d")["symbol"])
        except Exception as e:  # pragma: no cover - se informa abajo
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(s,)) for s in ["AAPL", "MSFT", "SAN.MC", "ITX.MC", "^IBEX"] * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(results) == sorted(["AAPL", "MSFT", "SAN.MC", "ITX.MC", "^IBEX"] * 2)


# ---------------------------------------------------------------------------
# Búsqueda, pre/post-mercado y cartera
# ---------------------------------------------------------------------------
def test_search_translates_and_filters(source):
    found = [(r["symbol"], r["exchange"]) for r in source.search("san")]
    assert found == [("SAN.MC", "BM"), ("SAN", "NYSE"), ("^IBEX35", "MEFFRV"), ("BRK-B", "NYSE")]


@pytest.mark.parametrize("now, session", [
    (dt.datetime(2026, 11, 20, 23, 30, tzinfo=UTC), "post"),     # 18:30 en Nueva York
    (dt.datetime(2026, 11, 20, 13, 0, tzinfo=UTC), "pre"),       # 8:00
    (dt.datetime(2026, 11, 20, 17, 0, tzinfo=UTC), "regular"),   # 12:00
    (dt.datetime(2026, 11, 22, 12, 0, tzinfo=UTC), "post"),      # domingo: último post-mercado del viernes
])
def test_extended_price(source, tws, now, session):
    tws.now = now
    e = source.extended("AAPL")
    assert e["session"] == session
    assert source.extended("SAN.MC") is None and source.extended("^IBEX") is None


@pytest.mark.parametrize("data_until, clock, expected", [
    # Datos con 15 min de retraso: a las 9:40 la última vela aún es de las 9:20 (pre-mercado)
    (dt.datetime(2026, 11, 20, 14, 20, tzinfo=UTC), dt.datetime(2026, 11, 20, 14, 40, tzinfo=UTC), "regular"),
    # Pre-mercado sin operaciones todavía: el último precio es el post-mercado de ayer
    (dt.datetime(2026, 11, 19, 23, 30, tzinfo=UTC), dt.datetime(2026, 11, 20, 9, 30, tzinfo=UTC), "post"),
    # Datos con retraso justo tras el cierre: la última vela aún es de la sesión regular
    (dt.datetime(2026, 11, 20, 20, 50, tzinfo=UTC), dt.datetime(2026, 11, 20, 21, 5, tzinfo=UTC), "regular"),
])
def test_extended_session_follows_the_clock(source, tws, monkeypatch, data_until, clock, expected):
    tws.now = data_until                                     # hasta dónde llegan las velas
    monkeypatch.setattr(ibkr, "now", lambda: clock.timestamp())
    n = len(tws.history_requests)
    assert source.extended("AAPL")["session"] == expected
    if ibkr.us_session(int(clock.timestamp())) == "regular":
        assert len(tws.history_requests) == n               # en sesión regular ni se pide a IBKR


def test_positions(source, tws):
    for sym in ("AAPL", "MSFT", "SAN.MC"):
        source.chart(sym, "1mo", "1d")
    d = source.positions()
    rows = {r["label"]: r for r in d["positions"]}
    assert (d["base"], d["net_liquidation"], d["cash"], d["unvalued"], d["multi_account"]) == ("EUR", 85432.10, 12050.0, 1, False)

    aapl = rows["AAPL"]
    price = tws.last_close("AAPL")
    assert aapl["symbol"] == "AAPL" and aapl["qty"] == 50 and aapl["price"] == pytest.approx(price)
    assert aapl["pnl"] == pytest.approx((price - 180.0) * 50)
    assert aapl["value_base"] == pytest.approx(price * 50 * 0.92)  # USD → EUR con el tipo de IBKR

    msft = rows["MSFT"]  # posición corta: gana si baja
    assert msft["qty"] == -10 and msft["pnl"] == pytest.approx((400.0 - tws.last_close("MSFT")) * 10)

    opt = rows["AAPL  261218C00260000"]  # opción: multiplicador 100, sin gráfico
    assert opt["symbol"] is None and opt["type"] == "OPT"
    assert opt["avg_price"] == pytest.approx(15.20) and opt["value"] == pytest.approx(3680.0)

    assert rows["SAP"]["price"] is None and rows["SAP"]["value_base"] is None  # aún sin valorar
    assert rows["SAN"]["symbol"] == "SAN.MC"
    values = [abs(r["value_base"] or 0) for r in d["positions"]]
    assert values == sorted(values, reverse=True)
    valued = [r for r in d["positions"] if r["value_base"] is not None]
    assert d["total_value"] == pytest.approx(sum(r["value_base"] for r in valued))
