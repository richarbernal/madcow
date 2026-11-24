"""Fuente de datos Interactive Brokers para MadCow (TWS o IB Gateway).

Usa la API oficial de TWS a través de la librería `ib_async` (pip install ib_async).
La conexión es de SOLO LECTURA: la app no puede enviar órdenes. Si las posiciones están
activadas, también se leen las posiciones y su valoración (nunca órdenes ni ejecuciones).

Los símbolos de la app siguen el formato habitual (estilo Yahoo) y aquí se
traducen al contrato de IBKR:

    AAPL, BRK-B         → acciones de EE. UU. (SMART, USD)
    SAN.MC              → Bolsa de Madrid (BM)
    SAP.DE / SAP.F      → Xetra (IBIS) / Fráncfort (FWB)
    MC.PA, ASML.AS      → Euronext París (SBF) / Ámsterdam (AEB)
    ENI.MI, BP.L, NESN.SW, VOLV-B.ST, NOKIA.HE, EQNR.OL
    ^IBEX, ^GSPC, ^GDAXI, ^STOXX50E, ^NDX, ^VIX, ^FCHI   → índices
    ^LOQUESEA           → cualquier índice de IBKR por su símbolo (p. ej. ^IBEX35)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import logging
import math
import threading
import time

# ib_async escribe en inglés cada intento fallido; los errores ya se explican desde aquí.
logging.getLogger("ib_async").setLevel(logging.CRITICAL)


class Unavailable(Exception):
    """TWS / IB Gateway no está accesible (o falta ib_async)."""


class SymbolError(ValueError):
    """El símbolo no existe en IBKR o no hay permisos de datos para él."""


# Sufijo estilo Yahoo → (bolsa principal en IBKR, divisa)
SUFFIXES = {
    "MC": ("BM", "EUR"),      # Bolsa de Madrid
    "DE": ("IBIS", "EUR"),    # Xetra
    "F": ("FWB", "EUR"),      # Fráncfort
    "PA": ("SBF", "EUR"),     # Euronext París
    "AS": ("AEB", "EUR"),     # Euronext Ámsterdam
    "MI": ("BVME", "EUR"),    # Borsa Italiana
    "L": ("LSE", "GBP"),      # Londres
    "SW": ("EBS", "CHF"),     # SIX Suiza
    "ST": ("SFB", "SEK"),     # Estocolmo
    "HE": ("HEX", "EUR"),     # Helsinki
    "OL": ("OSE", "NOK"),     # Oslo
}
EXCHANGE_TO_SUFFIX = {exch: suf for suf, (exch, _) in SUFFIXES.items()}
US_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "IEX", "ISLAND", "PINK", "NYSENAT"}

# Índices con nombre Yahoo → (símbolo IBKR, bolsa)
INDICES = {
    "^GSPC": ("SPX", "CBOE"),
    "^SPX": ("SPX", "CBOE"),
    "^NDX": ("NDX", "NASDAQ"),
    "^VIX": ("VIX", "CBOE"),
    "^IBEX": ("IBEX35", "MEFFRV"),
    "^GDAXI": ("DAX", "EUREX"),
    "^STOXX50E": ("ESTX50", "EUREX"),
    "^FCHI": ("CAC40", None),
    "^DJI": ("INDU", None),
    "^RUT": ("RUT", None),
}

DURATIONS = {"1mo": "1 M", "3mo": "3 M", "6mo": "6 M", "1y": "1 Y", "2y": "2 Y",
             "5y": "5 Y", "10y": "10 Y", "max": "30 Y"}
BAR_SIZES = {"1h": "1 hour", "1d": "1 day", "1wk": "1 week", "1mo": "1 month"}
DEFAULT_PORTS = (7496, 4001, 7497, 4002)
RETRY_AFTER = 15  # segundos sin reintentar la conexión tras un fallo  # TWS real, Gateway real, TWS simulada, Gateway simulada
PORT_NAMES = {7496: "TWS (real)", 7497: "TWS (simulada)", 4001: "IB Gateway (real)", 4002: "IB Gateway (simulada)"}

# Códigos de IBKR que son solo avisos informativos
def _is_info(code: int) -> bool:
    return 2100 <= code < 2200 or code in (10167, 10197, 300)


def candidates(symbol: str):
    """Contratos de IBKR a probar para un símbolo de la app (en orden)."""
    from ib_async import Index, Stock

    if symbol.startswith("^"):
        sym, exch = INDICES.get(symbol, (symbol[1:], None))
        return [Index(sym, exch or "")]
    base, _, suffix = symbol.rpartition(".")
    if base and suffix in SUFFIXES:
        exch, cur = SUFFIXES[suffix]
        base = base.replace("-", " ")
        out = [Stock(base, "SMART", cur, primaryExchange=exch)]
        if exch == "LSE":  # IBKR escribe algunos valores de Londres con punto final (BP.)
            out.append(Stock(base + ".", "SMART", cur, primaryExchange=exch))
        return out
    if "." in symbol:
        raise SymbolError(f"Sufijo de bolsa no soportado: {symbol}. "
                          f"Usa uno de: {', '.join('.' + s for s in SUFFIXES)}")
    return [Stock(symbol.replace("-", " "), "SMART", "USD")]


def app_symbol(contract) -> str | None:
    """Traduce un contrato de IBKR al formato de símbolo de la app (o None si no se soporta)."""
    if contract.secType == "IND":
        return "^" + contract.symbol
    if contract.secType != "STK":
        return None
    exch = contract.primaryExchange or contract.exchange
    if exch in US_EXCHANGES or (exch in ("", "SMART") and contract.currency == "USD"):
        return contract.symbol.replace(" ", "-")
    suffix = EXCHANGE_TO_SUFFIX.get(exch)
    if not suffix:
        return None
    return contract.symbol.rstrip(".").replace(" ", "-") + "." + suffix


def _timestamp(d) -> int:
    if isinstance(d, dt.datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp())
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def _ny_offset(t: dt.datetime) -> dt.timedelta:
    """Desfase UTC de Nueva York (horario de verano de EE. UU.), sin depender de tzdata."""
    y = t.year
    mar1, nov1 = dt.date(y, 3, 1), dt.date(y, 11, 1)
    dst_start = mar1 + dt.timedelta(days=(6 - mar1.weekday()) % 7 + 7)   # 2.º domingo de marzo
    dst_end = nov1 + dt.timedelta(days=(6 - nov1.weekday()) % 7)         # 1.er domingo de noviembre
    start = dt.datetime.combine(dst_start, dt.time(7), dt.timezone.utc)  # 2:00 EST
    end = dt.datetime.combine(dst_end, dt.time(6), dt.timezone.utc)      # 2:00 EDT
    return dt.timedelta(hours=-4 if start <= t < end else -5)


def us_session(ts: int) -> str:
    """Sesión de EE. UU. de una vela según su hora de inicio: pre, regular, post u overnight."""
    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    ny = t + _ny_offset(t)
    m = ny.hour * 60 + ny.minute
    if ny.weekday() < 5:
        if 4 * 60 <= m < 9 * 60 + 30:
            return "pre"
        if 9 * 60 + 30 <= m < 16 * 60:
            return "regular"
        if 16 * 60 <= m < 20 * 60:
            return "post"
    return "overnight"


def is_us_stock(contract) -> bool:
    return contract.secType == "STK" and (contract.primaryExchange or contract.exchange) in US_EXCHANGES


def since_duration(since: int, interval: str) -> str | None:
    """Duración IBKR para pedir solo las velas desde `since`; None si el hueco es demasiado
    grande para una actualización incremental (entonces se pide el rango completo)."""
    days = int((time.time() - since) // 86400) + 2
    if interval == "1h":
        return f"{days} D" if days <= 60 else None
    if interval == "1d":
        return f"{days} D" if days <= 365 else None
    return "1 Y" if days <= 365 else None  # semanal / mensual: un año son pocas velas


def _duration(rng: str, interval: str) -> str:
    if rng == "ytd":
        days = (dt.date.today() - dt.date(dt.date.today().year, 1, 1)).days + 1
        return f"{max(days, 2)} D"
    if interval == "1h" and rng not in ("1mo", "3mo", "6mo"):
        return "1 Y"  # IBKR limita el histórico intradía; un año de velas horarias es suficiente
    return DURATIONS[rng]


class IBKRSource:
    name = "Interactive Brokers"

    def __init__(self, host: str = "127.0.0.1", port: int | None = None, client_id: int = 117,
                 positions: bool = True):
        self.host = host
        self.ports = (port,) if port else DEFAULT_PORTS
        self.client_id = client_id
        self.positions_enabled = positions
        self._by_conid: dict = {}     # conId → (contrato completo, nombre)
        self.connected_port: int | None = None
        self._ib = None
        self._errors: list = []
        self._details: dict = {}      # símbolo → (contrato, nombre)
        self._last_search = 0.0
        self._down_until = 0.0  # tras un fallo de conexión, no reintentar hasta entonces
        self._down_msg = ""
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="ibkr", daemon=True).start()
        self._lock = asyncio.run_coroutine_threadsafe(self._make_lock(), self._loop).result()

    @staticmethod
    async def _make_lock():
        return asyncio.Lock()

    # ---- puente entre los hilos del servidor HTTP y el bucle asyncio de ib_async ----
    def _run(self, coro, timeout: float = 120):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise Unavailable("IBKR no respondió a tiempo") from None

    def _on_error(self, req_id, code, text, contract):
        if not _is_info(code):
            self._errors.append((code, text))

    async def _connect(self):
        try:
            import ib_async
        except ImportError:
            raise Unavailable("falta la librería ib_async. Instálala con: pip install ib_async") from None
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        if time.monotonic() < self._down_until:
            raise Unavailable(self._down_msg)  # no reintentar en cada petición (en Windows cada intento tarda)
        if self._ib is None:
            self._ib = ib_async.IB()
            self._ib.errorEvent += self._on_error
        sf = ib_async.ib.StartupFetch
        # Nunca se piden órdenes ni ejecuciones. Con posiciones activadas se suscribe a la
        # valoración de la cartera (TWS la actualiza cada ~3 minutos).
        fetch = sf.POSITIONS | sf.ACCOUNT_UPDATES if self.positions_enabled else sf(0)
        tried = []
        for port in self.ports:
            for cid in (self.client_id, self.client_id + 1, self.client_id + 2):
                try:
                    await self._ib.connectAsync(self.host, port, clientId=cid, timeout=10,
                                                readonly=True, fetchFields=fetch)
                    self.connected_port = port
                    accounts = self._ib.managedAccounts()
                    if self.positions_enabled and len(accounts) > 1:
                        # con varias cuentas, IBKR solo valora una a la vez: la principal
                        await self._ib.reqAccountUpdatesAsync(accounts[0])
                    return self._ib
                except (ConnectionRefusedError, asyncio.TimeoutError, TimeoutError):
                    self._ib.disconnect()
                    break  # nadie escucha (o no responde) en este puerto: probar el siguiente
                except Exception:  # p. ej. clientId en uso (error 326): probar con otro
                    self._ib.disconnect()
            tried.append(str(port))
        self._down_msg = (
            f"no se pudo conectar con TWS / IB Gateway en {self.host} (puertos {', '.join(tried)}). "
            "Ábrelo, inicia sesión y activa la API en Configuración global → API → Ajustes → "
            "«Habilitar clientes ActiveX y Socket»")
        self._down_until = time.monotonic() + RETRY_AFTER
        raise Unavailable(self._down_msg)

    def _error_text(self) -> str:
        return "; ".join(f"[{c}] {t}" for c, t in self._errors[-3:])

    async def _resolve(self, ib, symbol: str):
        if symbol in self._details:
            return self._details[symbol]
        for c in candidates(symbol):
            self._errors.clear()
            details = await ib.reqContractDetailsAsync(c)
            if details:
                d = details[0]
                if c.secType == "IND" and not c.exchange:  # índice sin bolsa conocida: preferir la de su país
                    d = sorted(details, key=lambda x: x.contract.exchange in ("CBOE", "EUREX", "MEFFRV", "MONEP", "CME"),
                               reverse=True)[0]
                hit = (d.contract, d.longName or symbol)
                self._details[symbol] = hit
                return hit
        raise SymbolError(f"IBKR no reconoce el símbolo {symbol}")

    async def _bars(self, symbol: str, duration: str, bar_size: str, rth: bool):
        async with self._lock:
            ib = await self._connect()
            contract, name = await self._resolve(ib, symbol)
            self._errors.clear()
            bars = await ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
                whatToShow="TRADES", useRTH=rth, formatDate=2, timeout=45)
            errors = self._error_text()
            if not ib.isConnected():
                raise Unavailable("se perdió la conexión con TWS / IB Gateway")
        if not bars:
            if "permission" in errors.lower() or "subscription" in errors.lower():
                raise SymbolError(f"tu cuenta de IBKR no tiene permisos de datos de mercado para {symbol} ({errors})")
            raise SymbolError(f"IBKR no devolvió datos para {symbol}" + (f": {errors}" if errors else ""))
        return contract, name, [b for b in bars if b.close is not None and b.close > 0]

    async def _chart(self, symbol: str, rng: str, interval: str, rth: bool = True, since: int | None = None) -> dict:
        rth = rth or interval != "1h"  # el horario extendido solo tiene sentido en velas intradía
        duration = (since_duration(since, interval) if since else None) or _duration(rng, interval)
        contract, name, bars = await self._bars(symbol, duration, BAR_SIZES[interval], rth)
        mark = not rth and is_us_stock(contract)
        out = []
        for b in bars:
            bar = {"time": _timestamp(b.date), "open": b.open, "high": b.high, "low": b.low,
                   "close": b.close, "volume": max(0, int(b.volume or 0))}
            if mark and us_session(bar["time"]) != "regular":
                bar["ext"] = True
            out.append(bar)
        return {
            "symbol": symbol,
            "name": name,
            "currency": contract.currency,
            "exchange": contract.primaryExchange or contract.exchange,
            "bars": out,
            "demo": False,
        }

    async def _extended(self, symbol: str) -> dict | None:
        """Último precio de pre/post-mercado (solo acciones de EE. UU.), o None si no aplica."""
        async with self._lock:
            ib = await self._connect()
            contract, _ = await self._resolve(ib, symbol)
        if not is_us_stock(contract):
            return None
        _, _, bars = await self._bars(symbol, "3 D", "5 mins", rth=False)
        if not bars:
            return None
        last = bars[-1]
        ts = _timestamp(last.date)
        return {"session": us_session(ts), "price": last.close, "time": ts}

    async def _search(self, query: str) -> list:
        async with self._lock:
            ib = await self._connect()
            wait = 1.05 - (time.monotonic() - self._last_search)  # IBKR: máx. 1 búsqueda por segundo
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_search = time.monotonic()
            found = await ib.reqMatchingSymbolsAsync(query) or []
        results = []
        for d in found:
            sym = app_symbol(d.contract)
            if sym:
                results.append({"symbol": sym, "name": d.contract.description or "",
                                "exchange": d.contract.primaryExchange or d.contract.exchange,
                                "type": d.contract.secType})
        return results[:10]

    # ---- interfaz pública (llamada desde los hilos del servidor) ----
    def chart(self, symbol: str, rng: str, interval: str, rth: bool = True, since: int | None = None) -> dict:
        """Velas del rango pedido; con `since`, solo las posteriores a esa fecha (aprox.)."""
        return self._run(self._chart(symbol, rng, interval, rth, since))

    async def _positions(self) -> dict:
        from ib_async import Contract

        if not self.positions_enabled:
            raise Unavailable("las posiciones están desactivadas (se arrancó con --no-positions)")
        async with self._lock:
            ib = await self._connect()
            positions = [p for p in ib.positions() if p.position]
            for p in positions:  # nombre y bolsa principal (una sola vez por contrato)
                cid = p.contract.conId
                if cid not in self._by_conid:
                    details = await ib.reqContractDetailsAsync(Contract(conId=cid))
                    self._by_conid[cid] = ((details[0].contract, details[0].longName) if details
                                           else (p.contract, ""))
            portfolio = {(i.account, i.contract.conId): i for i in ib.portfolio()}
            values = ib.accountValues()
            accounts = ib.managedAccounts()

        def num(v):
            return v if isinstance(v, (int, float)) and math.isfinite(v) else None

        rates, base, net_liq, cash = {}, None, None, None
        for v in values:
            try:
                val = float(v.value)
            except (TypeError, ValueError):
                continue
            if v.tag == "ExchangeRate" and v.currency != "BASE":
                rates[v.currency] = val
            elif v.tag == "NetLiquidation":
                base, net_liq = v.currency, val
            elif v.tag == "TotalCashValue" and v.currency != "BASE":
                cash = val
        if base:
            rates.setdefault(base, 1.0)

        rows = []
        for p in positions:
            contract, name = self._by_conid[p.contract.conId]
            item = portfolio.get((p.account, p.contract.conId))
            try:
                mult = float(contract.multiplier) if contract.multiplier else 1.0
            except ValueError:
                mult = 1.0
            cost = p.position * p.avgCost
            price = num(item.marketPrice) if item else None
            value = num(item.marketValue) if item else None
            pnl = num(item.unrealizedPNL) if item else None
            rate = rates.get(contract.currency)
            rows.append({
                "symbol": app_symbol(contract),
                "label": contract.localSymbol or contract.symbol,
                "name": name,
                "type": contract.secType,
                "currency": contract.currency,
                "qty": p.position,
                "avg_price": p.avgCost / mult,     # por acción (o por unidad del subyacente)
                "price": price if price and price > 0 else None,
                "cost": cost,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl / abs(cost) if pnl is not None and cost else None,
                "value_base": value * rate if value is not None and rate else None,
                "pnl_base": pnl * rate if pnl is not None and rate else None,
                "cost_base": cost * rate if rate else None,
                "account": ("…" + p.account[-4:]) if len(accounts) > 1 else None,
            })
        rows.sort(key=lambda r: abs(r["value_base"] or 0), reverse=True)
        valued = [r for r in rows if r["value_base"] is not None and r["cost_base"] is not None]
        total_value = sum(r["value_base"] for r in valued)
        total_pnl = sum(r["pnl_base"] or 0 for r in valued)
        total_cost = sum(r["cost_base"] for r in valued)
        return {
            "available": True,
            "base": base,
            "net_liquidation": net_liq,
            "cash": cash,
            "total_value": total_value if valued else None,
            "total_pnl": total_pnl if valued else None,
            "total_pnl_pct": total_pnl / abs(total_cost) if valued and total_cost else None,
            "unvalued": len(rows) - len(valued),
            "multi_account": len(accounts) > 1,
            "positions": rows,
            "time": int(time.time()),
        }

    def positions(self) -> dict:
        return self._run(self._positions(), timeout=60)

    def extended(self, symbol: str) -> dict | None:
        return self._run(self._extended(symbol))

    def search(self, query: str) -> list:
        return self._run(self._search(query), timeout=30)

    def check(self) -> bool:
        """Diagnóstico para `python3 server.py --check`."""
        try:
            import ib_async
            print(f"ib_async ...... {ib_async.__version__}")
        except ImportError:
            print("ib_async ...... NO INSTALADO → ejecuta: pip install ib_async")
            return False
        print(f"Conexión ...... probando {self.host}, puertos {', '.join(map(str, self.ports))} … ", end="", flush=True)
        try:
            ib = self._run(self._connect(), timeout=60)
        except Unavailable as e:
            print(f"FALLO\n    → {e}")
            return False
        print(f"OK, {PORT_NAMES.get(self.connected_port, 'puerto')} {self.connected_port} "
              f"(versión de servidor {ib.client.serverVersion()})\n")
        ok = True
        for sym in ("AAPL", "SAN.MC", "SAP.DE", "^IBEX"):
            print(f"Probando {sym:<8} … ", end="", flush=True)
            try:
                d = self.chart(sym, "1mo", "1d")
                last = d["bars"][-1]
                print(f"OK ({len(d['bars'])} sesiones, último cierre {last['close']:.2f} {d['currency']}) · {d['name']}")
            except (SymbolError, Unavailable) as e:
                print(f"FALLO\n    → {e}")
                ok = False
        if self.positions_enabled:
            print("Posiciones .... ", end="", flush=True)
            try:
                d = self.positions()
                print(f"OK ({len(d['positions'])} posiciones"
                      + (f", {d['unvalued']} sin valorar" if d["unvalued"] else "") + ")")
            except Unavailable as e:
                print(f"FALLO\n    → {e}")
                ok = False
        return ok
