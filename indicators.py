"""Indicadores técnicos en Python, para las alertas del servidor.

Son el mismo cálculo que static/indicators.js (el que ve el usuario en el gráfico);
tests/test_alerts.py comprueba que ambos dan exactamente lo mismo. Cada función recibe
una lista de cierres y devuelve una lista de la misma longitud (None donde no hay datos).
"""
from __future__ import annotations

import math


def sma(values: list, period: int) -> list:
    out = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: list, period: int) -> list:
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def moving_average(values: list, period: int, kind: str = "SMA") -> list:
    return ema(values, period) if kind == "EMA" else sma(values, period)


def bollinger(values: list, period: int = 20, mult: float = 2) -> tuple:
    """Media simple ± mult desviaciones típicas poblacionales: (media, superior, inferior)."""
    mid = sma(values, period)
    upper = [None] * len(values)
    lower = [None] * len(values)
    for i in range(period - 1, len(values)):
        sd = math.sqrt(sum((values[j] - mid[i]) ** 2 for j in range(i - period + 1, i + 1)) / period)
        upper[i] = mid[i] + mult * sd
        lower[i] = mid[i] - mult * sd
    return mid, upper, lower


def rsi(values: list, period: int = 14) -> list:
    """RSI de Wilder."""
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    gain /= period
    loss /= period

    def calc():
        return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    out[period] = calc()
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
        out[i] = calc()
    return out
