# BridgeBot

Bot de Telegram que recibe un enlace de **MediaFire**, **Mega**, **Google Drive** o cualquier
**enlace de descarga directa** y sube el archivo **directamente a Telegram**, devolviéndotelo como un documento (un solo archivo, hasta 2 GB).

```
Enlace MediaFire/Mega/Drive/directo → BridgeBot (Render) → Telegram → tu chat
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

### Por qué la subida es rápida (plan gratuito)

Telethon sube los archivos **trozo a trozo esperando la respuesta de Telegram** antes de enviar el
siguiente: en un archivo grande eso son miles de ida y vuelta y lo que limita no es el ancho de banda
sino la latencia. BridgeBot usa `app/mtproto_upload.py`, que lanza varios trozos de 512 KB **en
paralelo** (6 por defecto, `MT_UPLOAD_PARALLEL`) y reutiliza el `InputFileBig` resultante, así que
`send_file()` solo publica el documento y no lo vuelve a subir.

Además:

- **`cryptg` es imprescindible en el plan gratuito.** Telethon cifra cada trozo con AES-IGE; sin
  `cryptg` lo hace con libssl/Python, a ~6 MB/s *por núcleo completo* (medido: 84 ms por trozo de
  512 KB). Con `cryptg` son ~0,9 ms. En 0,1 CPU la diferencia es la que hay entre un tope de
  ~0,6 MB/s y uno de decenas de MB/s, y el paralelismo de arriba no ayuda si la CPU es el cuello de
  botella. Está en `requirements.txt`; al arrancar, el log dice `cryptg activo` (o avisa si falta).
- La descarga se **reanuda** desde el byte exacto si la CDN corta la conexión (Mega permite descifrar
  desde cualquier bloque), sin volver a empezar el archivo.
- Los ZIP se comprimen con `ZIP_LEVEL=1` por defecto: en 0,1 CPU, bajar de nivel 6 a 1 multiplica la
  velocidad y apenas cambia el tamaño de vídeos o imágenes. Con `ZIP_LEVEL=0` se empaqueta sin comprimir.
- El buffer del puente descarga/subida es de 16 MB (`BUFFER_MB`): menos bloqueos y más margen para la red.

## Estructura

| Ruta | Función |
|---|---|
| `app/sources/` | Un módulo por sitio (`mediafire.py`, `mega.py`, `gdrive.py`, `direct.py`) con la interfaz `Source` |
| `app/sources/direct.py` | Enlaces de descarga directa (cualquier http/https): nombre, tamaño, reanudación |
| `app/sources/gdrive.py` | Google Drive: archivos, documentos nativos (docx/xlsx/pptx) y carpetas (con `GOOGLE_API_KEY`) |
| `app/sources/_guard.py` | Cliente HTTP con protección SSRF para los orígenes con URLs arbitrarias |
| `app/sources/base.py` | Modelo común: `RemoteFile`, `BundleFile`, `FolderNode`, `Inspection` y utilidades del árbol |
| `app/sources/archive.py` | Generador de ZIP en streaming (deflate + data descriptor), sin cargar el ZIP en memoria |
| `app/folders.py` | Navegador de carpetas: árbol en memoria con TTL y botones (Telegram limita `callback_data` a 64 B) |
| `app/streams.py` | Puente sync/async: cola acotada que alimenta la subida MTProto sin cargar el archivo en RAM |
| `app/mtproto_upload.py` | Subida MTProto con varios trozos (512 KB) en vuelo a la vez |
| `app/uploader.py` | Entrega híbrida: Bot API (≤50 MB) o MTProto + reenvío (>50 MB), con reanudación de la descarga |
| `app/jobs.py` | Cola (un archivo a la vez), pausa, reintentos, estadísticas y mensajes de progreso |
| `app/admin.py` | Permisos de administración, bloqueos (`BanRegistry`) e informes |
| `app/runtime.py` | Ensamblado de dependencias, menú de comandos, registro en memoria y reinicio |
| `app/bot.py` | Comandos y handlers de Telegram (públicos + comandos de administración) |
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

### Enlaces directos y Google Drive

**Enlace directo:** cualquier URL `http(s)` que empiece a descargar al abrirla. El bot averigua
nombre y tamaño sin bajar el archivo, rechaza las páginas web con un mensaje claro y reanuda si el
servidor admite `Range`. Si el servidor no declara el tamaño, el archivo pasa por disco (con tope de
`MAX_FILE_MB`) antes de subirse.

**Google Drive:** el archivo (o carpeta) debe estar compartido como *«Cualquier persona con el
enlace»*. Formatos admitidos: `/file/d/<id>`, `open?id=`, `uc?id=`, `drive.usercontent…/download?id=`,
carpetas `/drive/folders/<id>` y documentos de Google (`docs.google.com/document|spreadsheets|presentation`,
que se exportan a docx / xlsx / pptx).

| | Sin `GOOGLE_API_KEY` | Con `GOOGLE_API_KEY` |
|---|---|---|
| Archivo suelto | Sí (salta el aviso de virus de los archivos grandes) | Sí, por la API v3 (más fiable) |
| Carpetas (navegar, ZIP) | No | Sí |
| Documentos de Google | Sí | Sí |

La ruta sin clave lee páginas de Drive que no son una API: Google puede cambiarlas y, desde IPs de
centros de datos como las de Render, a veces limita archivos muy populares. La clave es gratuita:
Google Cloud Console → *APIs y servicios* → habilitar **Google Drive API** → *Credenciales* →
*Crear clave de API* (restríngela a «Google Drive API») → pégala en `GOOGLE_API_KEY` en Render.
Drive limita las descargas diarias de un mismo archivo público; el bot lo avisa con un mensaje
específico.

### Página de estado y API

Render exige que el servicio sirva tráfico web, así que el bot expone:

- `GET /` — página web con el estado del bot (en línea, @usuario, tiempo en funcionamiento, cola, sitios soportados).
- `GET /api/status` — lo mismo en JSON, por ejemplo:

  ```json
  {"ok": true, "status": "online", "bot": "tu_bot", "uptime_seconds": 3600,
   "queue": {"pending_jobs": 0, "current_file": null, "paused": false},
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

Para que `/reiniciar` reinicie de verdad en Render, crea un *Deploy Hook* (Settings → Deploy Hook)
y pégalo en `RENDER_DEPLOY_HOOK_URL`. Sin él, el bot termina su proceso y Render lo vuelve a
levantar automáticamente (es igual de efectivo, pero menos elegante).

### 3. Render
1. Sube el proyecto a GitHub (el `.env` está en `.gitignore`).
2. En Render: **New → Blueprint** y elige el repositorio (usa `render.yaml`), o crea un *Web Service*
   con build `pip install -r requirements.txt` y start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. Define las variables: `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`,
   `ADMIN_USER_IDS`. `WEBHOOK_SECRET` lo genera el blueprint; `RENDER_EXTERNAL_URL` lo pone Render.
   Opcionales: `MT_UPLOAD_PARALLEL` (trozos en paralelo, 6 por defecto), `ZIP_LEVEL` (0–9, 1 por
   defecto), `GOOGLE_API_KEY` (carpetas de Drive), `BUFFER_MB` (16 por defecto) y `RENDER_DEPLOY_HOOK_URL` (para que `/reiniciar` haga un
   redeploy limpio; se obtiene en *Settings → Deploy Hook* del servicio en Render).
4. El bot lo puede usar cualquiera; escribe al bot para ver tu **ID de Telegram** y añádelo a
   `ADMIN_USER_IDS` si quieres poder usar los comandos de administración. Después, redeploya.

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

Al pulsar **Start** (o `/ayuda`), cada usuario ve lo que puede hacer: los administradores
reciben además el panel de estado y la lista completa de comandos de administración.

/ayuda (o /help) · /estado · /cancelar

### Comandos de administración
Solo los responde el bot a los **administradores** (los ID de Telegram de `ADMIN_USER_IDS`).
Para el resto de usuarios, esos comandos no existen y el bot se comporta con normalidad:

| Comando | Qué hace |
| --- | --- |
| `/admin` | Panel: estado del bot, cola, usuarios y cuota. |
| `/reiniciar` (o `/restart`) | Reinicia el servicio tras confirmar. Usa el *deploy hook* de Render si está configurado; si no, termina el proceso y Render lo relanza. |
| `/version` | Versión, Python, sistema, tiempo en marcha y estado de MTProto. |
| `/ping` | Comprueba que el bot responde y mide la latencia de la Bot API. |
| `/stats` | Envíos ok/fallidos, bytes, velocidad media, memoria, disco, pausa y paralelismo. |
| `/logs [n]` | Últimas líneas del registro en memoria (1–100; por defecto 20). |
| `/test_mtproto` | Comprueba (y reconecta) la sesión de las subidas grandes. |
| `/cola` | Lista las transferencias en curso y en espera. |
| `/usuarios` | Usuarios que han usado el bot y sus transferencias. |
| `/pausar` · `/reanudar` | Detiene o reanuda la cola (el trabajo en curso termina). |
| `/cancelar_todo` | Cancela todos los trabajos en cola. |
| `/cancelar_usuario <id>` | Cancela las transferencias de un usuario. |
| `/limpiar [horas]` | Borra del almacén del bot los archivos ya subidos (todos, o los más antiguos que `horas`). |
| `/broadcast <texto>` | Envía un mensaje a todos los usuarios que han usado el bot. |
| `/permitir <id>` · `/revocar <id>` · `/admins` | Gestionan y listan los administradores (en memoria: se pierden al reiniciar). |
| `/ban <id> [motivo]` · `/unban <id>` · `/baneados` | Bloquean, desbloquean y listan usuarios (en memoria). Un usuario bloqueado no puede usar el bot. |

Para que un permiso sea permanente hay que añadir el ID a `ADMIN_USER_IDS` y reiniciar el servicio.

**Si el bot no te reconoce como administrador:**
1. Envía `/start`: el bot te dice tu ID numérico. Debe ser ese número, no tu `@usuario`.
2. En Render → **Environment**, comprueba que está en `ADMIN_USER_IDS` (separados por comas si hay varios).
3. Tras cambiar la variable, haz **Manual Deploy → Deploy latest commit** (o reinicia el servicio).
4. `GET /api/status` muestra `"admins"`: si vale `0`, la variable no llegó al proceso. Los logs del
   arranque también indican los administradores configurados o avisan de que la lista está vacía.

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
- **Carpetas:** las de Mega (y las de Google Drive, con `GOOGLE_API_KEY`) se listan con botones para
  navegar y elegir archivo, ZIP de archivo o ZIP de la carpeta; MediaFire solo acepta archivos sueltos.
  Una carpeta de Drive admite hasta 2000 elementos y 8 niveles.
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
- **Arranque:** el servidor web abre el puerto antes de cargar Telethon (que es pesado) para que
  Render no tarde en detectarlo. Si al arrancar no se puede registrar el webhook en Telegram
  (la red del plan gratuito se cae a menudo), el servicio **no se cae**: lo reintenta cada
  minuto durante media hora.
- **Seguridad (enlaces directos):** el bot descarga URLs que escribe cualquiera, así que bloquea los
  destinos que no resuelven solo a direcciones públicas (`localhost`, redes privadas, metadatos de la
  nube `169.254.169.254`, multicast, NAT64…), también en cada redirección, y limita las redirecciones a 5.
  Límite conocido: la comprobación DNS y la conexión son dos consultas distintas (*DNS rebinding*).
  Además, un servidor que envíe más datos de los declarados se corta (tope de RAM y de disco).
- **Seguridad:** el bot es público, así que cada usuario puede mandar como mucho 3 transferencias
  en cola a la vez y los comandos de administración (`ADMIN_USER_IDS`) están restringidos al
  administrador; nunca subas el `.env`.

## Tests
```bash
pip install -r requirements-dev.txt && pytest
```

## Añadir otro sitio
Crea `app/sources/<sitio>.py` con una clase que herede de `Source` (`matches`, `inspect`, `stream`) y
regístrala en `app/sources/__init__.py`. El resto (subida, cola, bot) no cambia.
