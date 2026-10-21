# BridgeBot

Bot de Telegram que recibe un enlace de **MediaFire** o **Mega** y sube el archivo
**directamente a Telegram**, devolviéndotelo como un documento (un solo archivo, hasta 2 GB).

```
Enlace MediaFire/Mega → BridgeBot (Render) → Telegram → tu chat
```

El archivo **casi nunca se guarda en disco**: se descarga y se sube en bloques con memoria constante
(apto para el plan gratuito de Render). Solo los ZIP que no caben en la Bot API (>50 MB) se
comprimen antes en un fichero temporal, porque MTProto necesita conocer el tamaño final y leer
de un fichero real. El código vive en GitHub; Render solo lo ejecuta.

## Cómo reparte las subidas

| Tamaño del archivo | Ruta | Límite |
|---|---|---|
| ≤ 50 MB | La sube el propio bot por la Bot API | 50 MB (tope de Telegram) |
| > 50 MB | La sube una **cuenta de usuario** por MTProto y el bot la reenvía | 2 GB |

La Bot API de Telegram solo admite 50 MB por documento. Para archivos grandes se usa una cuenta
de usuario (Teléfono/Telegram) que sube el documento al chat privado del bot y este lo **reenvía**
al usuario: reenviar no vuelve a subir el archivo, así se esquiva el límite de 50 MB.

## Estructura

| Ruta | Función |
|---|---|
| `app/sources/` | Un módulo por sitio (`mediafire.py`, `mega.py`) con la interfaz `Source` |
| `app/sources/base.py` | Modelo común: `RemoteFile`, `BundleFile`, `FolderNode`, `Inspection` y utilidades del árbol |
| `app/sources/archive.py` | Generador de ZIP en streaming (deflate + data descriptor), sin cargar el ZIP en memoria |
| `app/folders.py` | Navegador de carpetas: árbol en memoria con TTL y botones (Telegram limita `callback_data` a 64 B) |
| `app/streams.py` | Puente sync/async: cola acotada que alimenta la subida MTProto sin cargar el archivo en RAM |
| `app/uploader.py` | Entrega híbrida: Bot API (≤50 MB) o MTProto + reenvío (>50 MB) |
| `app/jobs.py` | Cola (un archivo a la vez), reintentos ante fallos transitorios y mensajes de progreso |
| `app/bot.py` | Comandos y handlers de Telegram |
| `app/main.py` | Webhook FastAPI para Render (`/webhook`, `/healthz`) más página de estado `/` y API `/api/status` |

### Carpetas de Mega y ZIP

Un enlace de carpeta de Mega (o un enlace profundo `.../folder/<id>` / `.../file/<id>`) abre un
**listado navegable**: subcarpetas y archivos con botones, paginado. Desde ahí se puede:

- **⬇️ Descargar** un archivo suelto tal cual.
- **🗜 Comprimir en ZIP** la carpeta actual (con sus subcarpetas) o un único archivo, y subirlo como
  un solo documento. El ZIP se genera en streaming, así que comprimir 10 GB no ocupa 10 GB de RAM.

| Contenido | Cómo se sube |
|---|---|
| Archivo suelto ≤ 50 MB | Bot API (en RAM) |
| Archivo suelto > 50 MB | MTProto + reenvío (en RAM, en bloques) |
| ZIP ≤ 50 MB | Bot API (en RAM) |
| ZIP > 50 MB | Se comprime en un fichero temporal y se sube por MTProto + reenvío |

El ZIP conserva la estructura de carpetas dentro del archivo (la raíz del ZIP es la carpeta elegida).
Si el servidor no tiene espacio libre suficiente, el bot lo dice antes de empezar.

### Página de estado y API

Render exige que el servicio sirva tráfico web, así que el bot expone:

- `GET /` — página web con el estado del bot (en línea, @usuario, tiempo en funcionamiento, cola, sitios soportados).
- `GET /api/status` — lo mismo en JSON, por ejemplo:

  ```json
  {"ok": true, "status": "online", "bot": "tu_bot", "uptime_seconds": 3600,
   "queue": {"pending_jobs": 0, "current_file": null},
   "sources": ["MediaFire", "Mega"], "max_file_mb": 2048,
   "mtproto": {"ok": true, "error": ""}}
  ```

- `GET /healthz` — sonda de vida para Render (`healthCheckPath`).

Si la sesión MTProto está mal generada, el bot **arranca igual** (sigue subiendo archivos de hasta
50 MB por la Bot API) y la página de estado lo indica con un aviso ⚠.

## Puesta en marcha

### 1. Bot de Telegram
Crea el bot con **@BotFather** (`/newbot`) y guarda el token → `BOT_TOKEN`. El bot necesita tener
un **@username** (el que le da BotFather).

### 2. Cuenta de Telegram para las subidas grandes
1. Entra en https://my.telegram.org → *API development tools* y crea una app.
   Guarda `api_id` y `api_hash` → `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
2. Con esa cuenta, **abre el chat del bot y pulsa Start** (para que la cuenta pueda escribirle).
3. Pon `TELEGRAM_API_ID` y `TELEGRAM_API_HASH` en tu `.env` y genera la sesión (una sola vez):
   ```powershell
   pip install -r requirements.txt
   python scripts/get_session.py
   ```
   Autoriza con teléfono/código/2FA. El script **guarda solo** el `TELEGRAM_SESSION` en el `.env`.

> Esta cuenta es la que sube los archivos >50 MB. Úsala poco para no arriesgarte a limitaciones.

### 3. Render
1. Sube el proyecto a GitHub (el `.env` está en `.gitignore`).
2. En Render: **New → Blueprint** y elige el repositorio (usa `render.yaml`), o crea un *Web Service*
   con build `pip install -r requirements.txt` y start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. Define las variables: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`,
   `ALLOWED_USER_IDS`. `WEBHOOK_SECRET` lo genera el blueprint; `RENDER_EXTERNAL_URL` lo pone Render.
4. Escribe al bot: si no estás autorizado te mostrará tu **ID de Telegram**; añádelo a `ALLOWED_USER_IDS`
   y redeploya.

### Prueba local (sin webhook)
1. Crea tu `.env` a partir del ejemplo y rellénalo:
   ```powershell
   Copy-Item .env.example .env
   # edita .env con BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION
   ```
2. **Sitúate en la carpeta `bot/`** (donde está `run.py`) y arranca:
   ```powershell
   pip install -r requirements.txt
   python run.py
   ```
   Equivale a `python -m app.polling`. No ejecutes `python app\polling.py` como archivo suelto
   desde dentro de `app\`, ni `python main.py`: eso es solo para Render.

El `.env` se carga automáticamente (gracias a `python-dotenv`); en Render manda el panel de
variables de entorno, que tiene prioridad sobre cualquier `.env`.

## Uso
Envía un enlace de archivo suelto. El bot te avisa del progreso y, al terminar, tendrás el archivo
como documento en el propio chat.

Si envías el enlace de una **carpeta de Mega**, el bot abre el listado de su contenido para que
elijas: un archivo concreto, un archivo comprimido en ZIP o toda la carpeta en un ZIP.

`/estado` · `/cancelar` · `/limpiar` (borra del almacén del bot los archivos subidos)

### Si te desconectas (puedes cerrar Telegram)

La descarga y la subida ocurren **en el servidor**, no en tu conexión: puedes cerrar Telegram o
apagar el móvil y, cuando vuelvas, el archivo te estará esperando en el chat.

- El enlace se analiza y el trabajo entra en la cola del servidor (la respuesta a Telegram es
  inmediata, así que no hay timeouts).
- Telegram guarda los mensajes mientras no estás conectado y te los entrega al volver.
- `/estado` te dice qué transfers hay en marcha o qué se entregó ya.
- Mientras haya trabajos, el bot se hace pings a sí mismo para que Render no lo duerma.
- Lo único que **interrumpe** una transferencia es un **reinicio del servicio** (un deploy): el
  trabajo se pierde y hay que reenviar el enlace.

Los archivos que pasan por la cuenta de usuario se borran de su chat con el bot pasadas `TTL_HOURS`
(6 por defecto); la copia que recibes tú permanece.

## Límites y notas importantes

- **Máximo 2 GB por archivo** (tope de MTProto; con Telegram Premium sería 4 GB). Se corta antes de empezar.
  El límite se aplica a cada archivo y también al tamaño total de un ZIP.
- **Carpetas:** las de Mega se listan con botones para navegar y elegir archivo, ZIP de archivo o
  ZIP de la carpeta; MediaFire solo acepta archivos sueltos.
- **Reintentos automáticos:** los fallos transitorios (red, 5xx, límite de peticiones de MediaFire/Mega)
  se reintentan hasta 3 veces con espera creciente antes de dar por perdido el archivo.
- **Memoria constante:** el contenido se descarga y se sube en streaming (unos pocos MB), pensado para el
  plan gratuito de Render (512 MB). La excepción es el ZIP >50 MB, que se escribe en un fichero temporal
  del disco efímero de Render y se borra al terminar.
- **El archivo sí atraviesa Render** (RAM y red) porque ahí corre el bot; lo que no se hace es almacenarlo
  (salvo el ZIP grande, que es temporal).
- **Render gratis:** se duerme a los 15 min sin tráfico (el primer mensaje puede tardar ~1 min). Durante
  una transferencia el bot se hace pings a `/healthz`, pero un deploy reinicia el servicio y aborta la subida.
- **Mega:** su cuota de transferencia gratuita depende de la IP; las IPs de datacenter (como Render) se
  agotan pronto. El bot informa del tiempo de espera cuando Mega lo indica. Además, las carpetas de Mega
  se montan encriptadas con la clave del enlace: si la clave no es correcta, el bot lo avisa.
- **GitHub solo aloja el código**, no los archivos (límite de 100 MB por archivo).
- **Seguridad:** `ALLOWED_USER_IDS` evita que otros gasten tu cuota y tu cuenta; nunca subas el `.env`.

## Tests
```bash
pip install -r requirements-dev.txt && pytest
```

## Añadir otro sitio
Crea `app/sources/<sitio>.py` con una clase que herede de `Source` (`matches`, `inspect`, `stream`) y
regístrala en `app/sources/__init__.py`. El resto (subida, cola, bot) no cambia.
