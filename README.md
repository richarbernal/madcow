# MadCow Charts

Un TradingView sencillo para acciones: velas, **Bandas de Bollinger**, **RSI**,
**medias móviles por sesiones** (SMA/EMA configurables) y **comparación de dos acciones**.

![](https://img.shields.io/badge/python-3.8%2B-blue) Sin dependencias: solo Python estándar.

## Arrancar

```bash
python3 server.py
```

Abre <http://localhost:8000>. Para otro puerto: `python3 server.py --port 9000`.

Los datos se obtienen de Yahoo Finance a través del propio servidor (el navegador
no puede llamarlo directamente por CORS). Si no hay conexión, la app muestra
**datos simulados** con un aviso bien visible. También puedes activar "Demo" a mano.

## Funciones

- **Gráfico de precio**: velas o línea, volumen, rangos 1M → Máx, intervalos 1h / diario / semanal / mensual.
- **Bandas de Bollinger**: periodo, nº de desviaciones y color configurables.
- **Medias móviles**: tantas como quieras, SMA o EMA, periodo en sesiones (por defecto 20, 50 y 200).
- **RSI** (Wilder) en panel propio, sincronizado con el precio, con niveles de sobrecompra/sobreventa configurables.
- **Lectura actual**: RSI, %B de Bollinger, ancho de bandas, distancia a cada media y último cruce entre la media más corta y la más larga.
- **Comparar dos acciones** (campo "vs"):
  - Rendimiento % desde el inicio del periodo, ratio A/B (fuerza relativa) o RSI de ambas.
  - Tabla: rentabilidad, volatilidad anualizada, máx. drawdown, Sharpe (rf=0), RSI, %B,
    distancia a medias, correlación y beta.
- Buscador con sugerencias, tema claro/oscuro, ajustes guardados en el navegador
  y URL compartible (`#s=AAPL&c=MSFT&r=1y&i=1d`).

Símbolos de Yahoo: `AAPL`, `MSFT`, `SAN.MC` (Mercado Continuo), `ITX.MC`, `^IBEX`, `^GSPC`, `BTC-USD`…

## Estructura

```
server.py              servidor HTTP + proxy de Yahoo Finance (stdlib)
static/index.html      interfaz
static/app.js          lógica de gráficos, comparación y controles
static/indicators.js   SMA, EMA, Bollinger, RSI y estadísticas
static/vendor/         TradingView Lightweight Charts 4.1.3 (Apache 2.0)
```

> Datos con posible retraso. Herramienta de análisis, no asesoramiento financiero.
