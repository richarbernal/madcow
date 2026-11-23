# MadCow Charts

Un TradingView sencillo para acciones: velas, **Bandas de Bollinger**, **RSI**,
**medias móviles por sesiones** (SMA/EMA configurables) y **comparación de dos acciones**.

Requiere Python 3.10+ y una cuenta de Interactive Brokers (TWS o IB Gateway).

## Arrancar

Los datos vienen de **Interactive Brokers**, a través de TWS o IB Gateway abierto en
tu ordenador. La conexión es de **solo lectura**: la app no puede enviar órdenes.

1. Instala la librería de la API (solo la primera vez):

   ```bash
   pip install -r requirements.txt
   ```

2. Abre **TWS** (o IB Gateway) e inicia sesión.
3. Activa la API (solo la primera vez). En TWS ve a *Archivo → Configuración global → API → Ajustes*
   (en IB Gateway: *Configure → Settings → API → Settings*):
   - marca **«Habilitar clientes ActiveX y Socket»**,
   - deja **«API de solo lectura»** marcada,
   - anota el puerto: 7496 (TWS real), 7497 (TWS simulada), 4001/4002 (Gateway).
4. Arranca la app y abre <http://localhost:8000>:

   ```bash
   python server.py
   ```

   El puerto se detecta solo (prueba 7496, 4001, 7497 y 4002). Para fijarlo: `--ib-port 7497`.
   La terminal indica si se ha conectado.

### Permisos de datos de mercado

IBKR solo entrega históricos de las bolsas para las que tu cuenta tiene **suscripción
de datos de mercado** (se gestionan en el Portal del Cliente → Configuración →
Suscripciones de datos de mercado). Si falta alguna, la app lo indica con
«⚠ Sin permisos de datos» en ese valor; el resto sigue funcionando.

### Cartera (posiciones de IBKR)

La pestaña **Cartera** muestra tus posiciones con la valoración que calcula IBKR
(precio, valor de mercado, coste medio y P&G latente), y un resumen de la cuenta:
valor de las posiciones, P&G total, valor liquidativo y efectivo, en tu divisa base.

- Clic en una posición para abrir su gráfico; ahí verás tu **coste medio** como línea
  horizontal y tu posición junto a la cotización.
- TWS envía la valoración cada ~3 minutos. Se incluyen opciones y otros productos,
  aunque solo las acciones abren gráfico.
- Son datos privados: no se guardan en disco y, desde otros dispositivos de la red,
  solo se muestran si la app está protegida con `--password`.
- `--no-positions` desactiva por completo la lectura de la cartera.

### Acceso desde otros dispositivos (móvil, tablet, otro PC)

Por defecto la app solo se abre desde el propio PC. Para usarla desde la red local:

```bash
python server.py --lan --password una-clave
```

- La terminal muestra la dirección que hay que abrir en el otro dispositivo
  (p. ej. `http://192.168.1.34:8000`, o `http://nombre-del-pc:8000`).
- Los demás dispositivos piden la contraseña una vez (el usuario da igual); el propio
  PC no la pide. También se puede indicar con la variable de entorno `MADCOW_PASSWORD`.
- **Windows** preguntará si permite a Python comunicarse en la red: acepta para
  **redes privadas**. Si tu Wi-Fi está marcada como «pública», cámbiala a «privada».
- TWS sigue funcionando solo en el PC: los demás dispositivos ven los datos a través de la app.
- Solo se aceptan peticiones dirigidas a la IP o al nombre del PC; si usas otro nombre
  (p. ej. un DNS propio), añádelo con `--allow-host nombre`.

No la expongas a Internet (abriendo puertos en el router): no está pensada para ello.

### Caché de datos históricos

Las velas se guardan en `cache.sqlite` (junto a `server.py`, no se sube a git):

- La primera vez que abres un valor se descarga el rango completo; después solo se
  piden a IBKR las sesiones nuevas, así que los gráficos cargan antes.
- Si TWS está cerrado, la app muestra los **datos guardados** con un aviso de cuándo
  se actualizaron (en favoritos, con la marca «CACHÉ»). Solo los valores que nunca
  se han abierto pasan a datos simulados.
- Si un split o un ajuste cambia el histórico, se detecta y la serie se descarga de nuevo.
- `python server.py --clear-cache` la borra; `--no-cache` trabaja sin ella y
  `--cache RUTA` la guarda en otro sitio.

### Si algo no funciona

```bash
python server.py --check
```

Comprueba la conexión con TWS/IB Gateway y prueba AAPL, SAN.MC, SAP.DE y el IBEX.
Si TWS no está abierto, la app muestra **datos simulados** con un aviso bien visible.

### Símbolos

| Mercado | Ejemplos |
|---|---|
| EE. UU. | `AAPL`, `MSFT`, `BRK-B` |
| Madrid (BME) | `SAN.MC`, `ITX.MC`, `IBE.MC` |
| Xetra / Fráncfort | `SAP.DE`, `SIE.DE` / `.F` |
| París, Ámsterdam, Milán | `MC.PA`, `ASML.AS`, `ENI.MI` |
| Londres, Suiza | `BP.L`, `NESN.SW` |
| Estocolmo, Helsinki, Oslo | `VOLV-B.ST`, `NOKIA.HE`, `EQNR.OL` |
| Índices | `^IBEX`, `^GSPC`, `^NDX`, `^GDAXI`, `^STOXX50E`, `^VIX`, o `^` + símbolo de IBKR |

El buscador de símbolos usa el de IBKR.

### Alternativa: Yahoo Finance

`python server.py --source yahoo` usa Yahoo Finance sin dependencias, pero Yahoo bloquea
a menudo las peticiones automáticas (error 429).

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

- **Pre/post-mercado** (acciones de EE. UU.): precio fuera de horario junto a la cotización
  y en favoritos (☀ pre-mercado, ☾ post-mercado), con su variación sobre el último cierre.
  En el intervalo de 1 hora se añaden las velas extendidas, atenuadas. Se activa/desactiva
  en la pestaña Indicadores. Las bolsas europeas no tienen sesión extendida.
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
server.py              servidor HTTP, fuentes de datos y watchlist
ibkr.py                fuente de datos Interactive Brokers (TWS / IB Gateway, solo lectura)
histcache.py           caché de datos históricos en disco (SQLite)
cache.sqlite           la caché (se crea sola; ignorada por git)
watchlist.json         tus favoritos (se crea al añadir el primero; ignorado por git)
static/index.html      interfaz
static/app.js          lógica de gráficos, comparación y controles
static/indicators.js   SMA, EMA, Bollinger, RSI y estadísticas
static/vendor/         TradingView Lightweight Charts 4.1.3 (Apache 2.0)
```

> Datos con posible retraso. Herramienta de análisis, no asesoramiento financiero.
