"""TWS simulado: sustituye a `ib_async.IB` para probar la app sin Interactive Brokers.

Imita lo que la app usa de ib_async (conexión, detalles de contrato, velas históricas
diarias e intradía con pre/post-mercado, búsqueda de símbolos, posiciones y valoración
de la cuenta) y reproduce los avisos y errores reales de IBKR: puerto cerrado, clientId
en uso, símbolo desconocido (200), falta de permisos de datos (162) y avisos
informativos (2104).

Cada test controla el simulador a través de `FakeTWS` (ver el fixture `tws` en conftest.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import random
import zlib

from ib_async import (AccountValue, BarData, Contract, ContractDescription, ContractDetails,
                      PortfolioItem, Position)

UTC = dt.timezone.utc

# (secType, símbolo IBKR, bolsa con la que la app lo pide o None) → (divisa, nombre, bolsa principal)
CONTRACTS = {
    ("STK", "AAPL", None): ("USD", "APPLE INC", "NASDAQ"),
    ("STK", "MSFT", None): ("USD", "MICROSOFT CORP", "NASDAQ"),
    ("STK", "BRK B", None): ("USD", "BERKSHIRE HATHAWAY INC-CL B", "NYSE"),
    ("STK", "SAN", "BM"): ("EUR", "BANCO SANTANDER SA", "BM"),
    ("STK", "ITX", "BM"): ("EUR", "INDUSTRIA DE DISENO TEXTIL", "BM"),
    ("STK", "SAP", "IBIS"): ("EUR", "SAP SE", "IBIS"),
    ("STK", "BP.", "LSE"): ("GBP", "BP PLC", "LSE"),
    ("IND", "IBEX35", "MEFFRV"): ("EUR", "IBEX 35 Index", "MEFFRV"),
    ("IND", "SPX", "CBOE"): ("USD", "S&P 500 Stock Index", "CBOE"),
}
OPTION = Contract(secType="OPT", conId=99999, symbol="AAPL", localSymbol="AAPL  261218C00260000",
                  multiplier="100", currency="USD", exchange="SMART")
ACCOUNT = "U1234567"


def last_friday_after_hours() -> dt.datetime:
    """El último viernes (anterior a hoy) a las 23:30 UTC: post-mercado en Nueva York,
    tanto en horario de invierno como de verano. Mantiene los tests válidos con el tiempo."""
    today = dt.datetime.now(UTC).date()
    friday = today - dt.timedelta(days=(today.weekday() - 4) % 7 or 7)
    return dt.datetime.combine(friday, dt.time(23, 30), UTC)


def con_id(key) -> int:
    return zlib.crc32(repr(key).encode()) % 10**8


class FakeTWS:
    """Estado del TWS simulado. Los tests cambian sus atributos para crear cada situación."""

    def __init__(self):
        self.now = last_friday_after_hours()  # «ahora» para las velas intradía

        self.open_port = 4001                # puerto en el que «escucha» (IB Gateway real)
        self.busy_client_ids = {117}         # clientId ya en uso por otra aplicación
        self.offline = False                 # TWS cerrado
        self.no_permissions = {"SAP"}        # sin suscripción de datos de mercado
        self.extra_days = 0                  # sesiones diarias nuevas desde el inicio del test
        self.last_mult = {}                  # símbolo → factor sobre la vela en curso
        self.split = {}                      # símbolo → factor aplicado a todo el histórico
        self.ext_jump = 1.0                  # salto de precio fuera de horario (acciones de EE. UU.)
        self.holdings = [("AAPL", 50, 180.0), ("SAN", 120, 4.0), ("SAP", 20, 150.0), ("MSFT", -10, 400.0)]
        self.unvalued = {"SAP"}              # posiciones que TWS aún no ha valorado
        self.connect_attempts = 0
        self.history_requests = []           # (símbolo, duración, tamaño de vela, useRTH)
        self.search_requests = []

    # ---- series de precios deterministas ----
    def daily_series(self, symbol: str) -> list:
        """Velas diarias (una por día natural) que terminan hoy. Los precios dependen solo de
        la posición en la serie, así que las velas ya devueltas no cambian durante un test."""
        rnd = random.Random(symbol)
        price = rnd.uniform(20, 300)
        n = 1300 + self.extra_days
        end = dt.datetime.now(UTC).date() + dt.timedelta(days=self.extra_days)
        out = []
        for i in range(n):
            o = price
            price *= math.exp(rnd.gauss(0.0003, 0.015))
            vol = rnd.randint(10**5, 10**7)
            c = price * (self.last_mult.get(symbol, 1.0) if i == n - 1 else 1.0)
            out.append((end - dt.timedelta(days=n - 1 - i), o, max(o, c) * 1.005, min(o, c) * 0.995, c, vol))
        k = self.split.get(symbol, 1.0)
        return [(d, o * k, h * k, l * k, c * k, v) for d, o, h, l, c, v in out]

    def last_close(self, symbol: str) -> float:
        return self.daily_series(symbol)[-1][4]


STATE = FakeTWS()


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class _Client:
    @staticmethod
    def serverVersion():
        return 187


class FakeIB:
    """Lo que la app usa de `ib_async.IB`."""

    def __init__(self):
        self.errorEvent = _Event()
        self.client = _Client()
        self._connected = False
        self.port = self.client_id = self.fetch_fields = None

    # ---- conexión ----
    def isConnected(self):
        return self._connected and not STATE.offline

    def disconnect(self):
        self._connected = False

    async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
        assert readonly is True, "la app debe conectarse siempre en solo lectura"
        STATE.connect_attempts += 1
        await asyncio.sleep(0)
        if STATE.offline or port != STATE.open_port:
            raise ConnectionRefusedError(111, f"Connect call failed ('{host}', {port})")
        if clientId in STATE.busy_client_ids:
            raise ConnectionError(f"clientId {clientId} already in use")
        self._connected, self.port, self.client_id, self.fetch_fields = True, port, clientId, fetchFields

    def managedAccounts(self):
        return [ACCOUNT]

    # ---- contratos ----
    async def reqContractDetailsAsync(self, c):
        await asyncio.sleep(0)
        if c.conId and not c.symbol:  # por conId (posiciones)
            if c.conId == OPTION.conId:
                return [ContractDetails(contract=OPTION, longName="APPLE INC")]
            for key, (cur, name, exch) in CONTRACTS.items():
                if con_id(key) == c.conId:
                    full = Contract(secType=key[0], conId=c.conId, symbol=key[1], exchange="SMART",
                                    primaryExchange=exch, currency=cur)
                    return [ContractDetails(contract=full, longName=name)]
            return []
        exch = (c.exchange or None) if c.secType == "IND" else (c.primaryExchange or None)
        key = (c.secType, c.symbol, exch)
        if key not in CONTRACTS:
            self.errorEvent.emit(5, 200, "No security definition has been found for the request", c)
            return []
        cur, name, primary = CONTRACTS[key]
        full = Contract(secType=c.secType, conId=con_id(key), symbol=c.symbol, exchange=c.exchange or primary,
                        primaryExchange=primary, currency=cur)
        return [ContractDetails(contract=full, longName=name)]

    # ---- históricos ----
    async def reqHistoricalDataAsync(self, contract, endDateTime, durationStr, barSizeSetting,
                                     whatToShow, useRTH, formatDate, timeout):
        STATE.history_requests.append((contract.symbol, durationStr, barSizeSetting, useRTH))
        await asyncio.sleep(0)
        self.errorEvent.emit(-1, 2104, "Market data farm connection is OK:usfarm", None)  # aviso informativo
        if contract.symbol in STATE.no_permissions:
            self.errorEvent.emit(7, 162, "Historical Market Data Service error message:No market data "
                                         f"permissions for {contract.primaryExchange} STK", contract)
            return []
        if barSizeSetting in ("1 hour", "5 mins"):
            return self._intraday(contract, durationStr, barSizeSetting, useRTH)
        num, unit = durationStr.split()
        days = int(num) * {"D": 1, "W": 7, "M": 30, "Y": 365}[unit]
        step = {"1 day": 1, "1 week": 7, "1 month": 30}[barSizeSetting]
        series = STATE.daily_series(contract.symbol)[::-1][::step][::-1]
        cutoff = series[-1][0] - dt.timedelta(days=days)
        return [BarData(date=d, open=o, high=h, low=l, close=c,
                        volume=-1 if contract.secType == "IND" else v, average=c, barCount=1)
                for d, o, h, l, c, v in series if d > cutoff]

    def _intraday(self, contract, durationStr, barSizeSetting, useRTH):
        import ibkr  # la clasificación de sesiones de la app (probada aparte)
        step = 3600 if barSizeSetting == "1 hour" else 300
        num, unit = durationStr.split()
        days = int(num) * {"D": 1, "W": 7, "M": 30, "Y": 365}[unit]
        us = contract.primaryExchange in ibkr.US_EXCHANGES
        now = int(STATE.now.timestamp()) // step * step
        stamps = []
        for t in range(now, now - days * 86400, -step):
            if us:
                session = ibkr.us_session(t)
                keep = session == "regular" or (not useRTH and session in ("pre", "post"))
            else:
                u = dt.datetime.fromtimestamp(t, UTC)
                keep = u.weekday() < 5 and 8 <= u.hour < 16
            if keep:
                stamps.append(t)
        stamps.reverse()
        rnd = random.Random(contract.symbol + "/intradía")
        moves = [rnd.gauss(0, 0.003) for _ in stamps]
        price = STATE.last_close(contract.symbol) * math.exp(-sum(moves))  # termina en el cierre diario
        out = []
        for t, m in zip(stamps, moves):
            o, price = price, price * math.exp(m)
            k = STATE.ext_jump if us and ibkr.us_session(t) in ("pre", "post") else 1.0
            out.append(BarData(date=dt.datetime.fromtimestamp(t, UTC), open=o * k, high=max(o, price) * k * 1.001,
                               low=min(o, price) * k * 0.999, close=price * k, volume=1000, average=price, barCount=1))
        return out

    # ---- búsqueda ----
    async def reqMatchingSymbolsAsync(self, pattern):
        STATE.search_requests.append(pattern)

        def desc(sec, sym, exch, cur, text):
            return ContractDescription(contract=Contract(secType=sec, symbol=sym, primaryExchange=exch,
                                                         exchange=exch, currency=cur, description=text))
        if pattern.lower().startswith("san"):
            return [desc("STK", "SAN", "BM", "EUR", "BANCO SANTANDER SA"),
                    desc("STK", "SAN", "NYSE", "USD", "BANCO SANTANDER SA-SPON ADR"),
                    desc("STK", "SAGALIO", "ASX", "AUD", "SAGALIO ENERGY"),  # bolsa no soportada
                    desc("IND", "IBEX35", "MEFFRV", "EUR", "IBEX 35 Index"),
                    desc("STK", "BRK B", "NYSE", "USD", "BERKSHIRE HATHAWAY B")]
        return []

    # ---- cartera ----
    def _stock(self, symbol):
        key = next(k for k in CONTRACTS if k[0] == "STK" and k[1] == symbol)
        return Contract(secType="STK", conId=con_id(key), symbol=symbol, exchange="", currency=CONTRACTS[key][0])

    def positions(self, account=""):
        out = [Position(ACCOUNT, self._stock(sym), qty, avg) for sym, qty, avg in STATE.holdings]
        return out + [Position(ACCOUNT, OPTION, 2, 1520.0)]

    def portfolio(self, account=""):
        items = []
        for p in self.positions():
            if p.contract.symbol in STATE.unvalued and p.contract.secType == "STK":
                continue
            price, mult = (18.4, 100) if p.contract.secType == "OPT" else (STATE.last_close(p.contract.symbol), 1)
            value = price * p.position * mult
            items.append(PortfolioItem(p.contract, p.position, price, value, p.avgCost,
                                       value - p.avgCost * p.position, 0.0, p.account))
        return items

    def accountValues(self, account=""):
        return [AccountValue(ACCOUNT, "NetLiquidation", "85432.10", "EUR", ""),
                AccountValue(ACCOUNT, "TotalCashValue", "12050.00", "EUR", ""),
                AccountValue(ACCOUNT, "TotalCashValue", "12050.00", "BASE", ""),
                AccountValue(ACCOUNT, "ExchangeRate", "0.92", "USD", ""),
                AccountValue(ACCOUNT, "ExchangeRate", "1.00", "EUR", ""),
                AccountValue(ACCOUNT, "ExchangeRate", "1.00", "BASE", "")]

    async def reqAccountUpdatesAsync(self, account):
        return None
