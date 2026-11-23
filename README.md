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

### Si no carga datos reales

El aviso amarillo indica la causa. Para un diagnóstico completo:

```bash
python3 server.py --check
```

- **Certificados SSL** (habitual en Mac): ejecuta «Install Certificates.command» de la
  carpeta de Python en Aplicaciones, o `pip3 install certifi`.
- **429 Too Many Requests**: Yahoo Finance no tiene API oficial y está bloqueando
  las peticiones automáticas desde tu conexión.

## Funciones

- **Favoritos (watchlist)** en la barra lateral: último precio, variación diaria, RSI y
  minigráfico de 30 sesiones, con refresco automático cada minuto.
  - Añade símbolos desde el campo de la lista o con la ☆ junto al nombre de la acción.
  - Clic para abrir, **vs** para compararla con la actual, ✕ para quitarla,
    arrastra para reordenar y usa ↑/↓ para recorrer la lista.
  - Se guarda en el archivo `watchlist.json` (junto a `server.py`), así que se conserva
    entre sesiones, navegadores y aunque borres los datos del navegador. Puedes
    cambiar la ubicación con `python3 server.py --watchlist ~/favoritos.json`
    (por ejemplo, a una carpeta sincronizada en la nube). No se sube a git.

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
server.py              servidor HTTP, proxy de Yahoo Finance y watchlist (stdlib)
watchlist.json         tus favoritos (se crea al añadir el primero; ignorado por git)
static/index.html      interfaz
static/app.js          lógica de gráficos, comparación y controles
static/indicators.js   SMA, EMA, Bollinger, RSI y estadísticas
static/vendor/         TradingView Lightweight Charts 4.1.3 (Apache 2.0)
```

> Datos con posible retraso. Herramienta de análisis, no asesoramiento financiero.
