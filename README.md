# BridgeBot

Bot de Telegram que recibe un enlace de **MediaFire** o **Mega** y sube el archivo
**directamente a Telegram**, devolviéndotelo como un documento (un solo archivo, hasta 2 GB).

```
Enlace MediaFire/Mega → BridgeBot (Render) → Telegram → tu chat
```

El archivo **nunca se guarda en disco**: se descarga y se sube en bloques con memoria constante
(apto para el plan gratuito de Render). El código vive en GitHub; Render solo lo ejecuta.

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
| `app/streams.py` | Puente sync/async: cola acotada que alimenta la subida MTProto sin cargar el archivo en RAM |
| `app/uploader.py` | Entrega híbrida: Bot API (≤50 MB) o MTProto + reenvío (>50 MB) |
| `app/jobs.py` | Cola (un archivo a la vez), reintentos ante fallos transitorios y mensajes de progreso |
| `app/bot.py` | Comandos y handlers de Telegram |
| `app/main.py` | Webhook FastAPI para Render (`/webhook`, `/healthz`) más página de estado `/` y API `/api/status` |

### Página de estado y API

Render exige que el servicio sirva tráfico web, así que el bot expone:

- `GET /` — página web con el estado del bot (en línea, @usuario, tiempo en funcionamiento, cola, sitios soportados).
- `GET /api/status` — lo mismo en JSON, por ejemplo:

  ```json
  {"ok": true, "status": "online", "bot": "tu_bot", "uptime_seconds": 3600,
   "queue": {"pending_jobs": 0, "current_file": null},
   "sources": ["MediaFire", "Mega"], "max_file_mb": 2048}
  ```

- `GET /healthz` — sonda de vida para Render (`healthCheckPath`).

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

`/cancelar` · `/limpiar` (borra del almacén del bot los archivos subidos)

Los archivos que pasan por la cuenta de usuario se borran de su chat con el bot pasadas `TTL_HOURS`
(6 por defecto); la copia que recibes tú permanece.

## Límites y notas importantes

- **Máximo 2 GB por archivo** (tope de MTProto; con Telegram Premium sería 4 GB). Se corta antes de empezar.
- **Carpetas:** las de Mega se listan con botones para elegir archivo o descargar todo; MediaFire solo
  acepta archivos sueltos.
- **Reintentos automáticos:** los fallos transitorios (red, 5xx, límite de peticiones de MediaFire/Mega)
  se reintentan hasta 3 veces con espera creciente antes de dar por perdido el archivo.
- **Sin disco y sin estado:** el archivo se descarga y se sube en streaming. El consumo de memoria es constante
  (unos pocos MB), pensado para el plan gratuito de Render (512 MB).
- **El archivo sí atraviesa Render** (RAM y red) porque ahí corre el bot; lo que no se hace es almacenarlo.
- **Render gratis:** se duerme a los 15 min sin tráfico (el primer mensaje puede tardar ~1 min). Durante
  una transferencia el bot se hace pings a `/healthz`, pero un deploy reinicia el servicio y aborta la subida.
- **Mega:** su cuota de transferencia gratuita depende de la IP; las IPs de datacenter (como Render) se
  agotan pronto. El bot informa del tiempo de espera cuando Mega lo indica.
- **GitHub solo aloja el código**, no los archivos (límite de 100 MB por archivo).
- **Seguridad:** `ALLOWED_USER_IDS` evita que otros gasten tu cuota y tu cuenta; nunca subas el `.env`.

## Tests
```bash
pip install -r requirements-dev.txt && pytest
```

## Añadir otro sitio
Crea `app/sources/<sitio>.py` con una clase que herede de `Source` (`matches`, `inspect`, `stream`) y
regístrala en `app/sources/__init__.py`. El resto (subida, cola, bot) no cambia.
