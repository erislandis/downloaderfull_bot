# BridgeBot

Bot de Telegram que recibe un enlace de **MediaFire** o **Mega**, lo transfiere a tu **Google Drive** por streaming
y te devuelve un enlace directo de Drive para pegarlo en tu bot de descargas.

```
Enlace MediaFire/Mega → BridgeBot (Render) → tu Drive → enlace directo → tu bot de descargas → tu app
```

El archivo **nunca se guarda completo** en Render: pasa en bloques de 8 MiB (RAM baja), con reintentos y reanudación.

## Estructura

| Ruta | Función |
|---|---|
| `app/sources/` | Un módulo por sitio (`mediafire.py`, `mega.py`) con la misma interfaz `Source` |
| `app/transfer.py` | Puente origen → Drive: bloques, reintentos, reanudación |
| `app/drive.py` | Cliente de Drive: subida reanudable, permisos, cuota, limpieza |
| `app/jobs.py` | Cola (una transferencia a la vez) y mensajes de progreso |
| `app/bot.py` | Comandos y botones de Telegram |
| `app/main.py` | Webhook FastAPI para Render (`/webhook`, `/healthz`) |

## Puesta en marcha

### 1. Bot de Telegram
Crea el bot con **@BotFather** (`/newbot`) y guarda el token → `BOT_TOKEN`.

### 2. Google Drive (una sola vez)
1. En [Google Cloud Console](https://console.cloud.google.com/) crea un proyecto y habilita **Google Drive API**.
2. *APIs y servicios → Pantalla de consentimiento OAuth*: tipo **Externo**. Luego pulsa **Publicar aplicación** (estado
   *In production*). Si la dejas en *Testing*, el refresh token **caduca a los 7 días**.
3. *Credenciales → Crear credenciales → ID de cliente OAuth → Aplicación de escritorio*. Copia el ID y el secreto.
4. En tu PC:
   ```bash
   pip install -r requirements-dev.txt
   GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_refresh_token.py
   ```
   Autoriza en el navegador (verás un aviso de "app no verificada": es tu propia app, continúa) y copia
   `GOOGLE_REFRESH_TOKEN`. El permiso es `drive.file`: el bot solo ve lo que él mismo crea.

### 3. Render
1. Sube este proyecto a un repositorio de GitHub (el `.env` está en `.gitignore`).
2. En Render: **New → Blueprint** y elige el repositorio (usa `render.yaml`), o crea un *Web Service* con
   build `pip install -r requirements.txt` y start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. Define las variables de `.env.example`: `BOT_TOKEN`, `GOOGLE_*`, `ALLOWED_USER_IDS`. `WEBHOOK_SECRET` lo genera el
   blueprint; `RENDER_EXTERNAL_URL` lo pone Render.
4. Escribe al bot: si no estás autorizado te mostrará tu **ID de Telegram**; añádelo a `ALLOWED_USER_IDS` y redeploya.

### Prueba local (sin webhook)
```bash
cp .env.example .env   # rellena BOT_TOKEN y GOOGLE_*
set -a; . ./.env; set +a
python -m app.polling
```

## Uso
Envía un enlace. Para archivos sueltos arranca solo; para carpetas de Mega muestra la lista y un botón de confirmación.
Recibirás un mensaje de progreso y, al terminar, el enlace en un mensaje aparte (toca para copiarlo).

`/cancelar` · `/limpiar` (borra todo lo del bot en Drive) · `/espacio`

Los archivos se borran solos de Drive pasadas `TTL_HOURS` (6 por defecto).

## Límites y notas importantes

- **Sin probar contra los sitios reales.** Se desarrolló sin acceso a MediaFire ni a Mega: la lógica está cubierta por
  35 pruebas con servidores simulados, pero la primera prueba real puede revelar ajustes. El extractor de MediaFire
  depende del HTML de su página, que puede cambiar (está aislado en `parse_direct_url`, con test).
- **Mega:** se descifra en Python (AES-CTR). No se verifica el MAC del archivo. Su cuota de transferencia gratuita es
  limitada y depende de la IP; si se agota, el bot informa del tiempo de espera cuando Mega lo indica.
- **Render gratis:** se duerme tras 15 min sin tráfico (el primer mensaje tarda hasta ~1 min en responder). Durante una
  transferencia el bot se hace pings a `/healthz` para evitarlo, pero no es una garantía.
- **Drive:** 15 GB gratis. Los archivos de más de ~100 MB muestran una página de aviso de antivirus en el enlace
  `uc?export=download`; depende de que tu bot de descargas la gestione.
- **Seguridad:** los enlaces son públicos (cualquiera con el enlace) mientras el archivo exista; por eso el TTL corto y
  la lista `ALLOWED_USER_IDS`, que evita que otros usen tu cuota y tu Drive. Nunca subas el `.env`.

## Tests
```bash
pip install -r requirements-dev.txt && pytest
```

## Añadir otro sitio
Crea `app/sources/<sitio>.py` con una clase que herede de `Source` (`matches`, `inspect`, `stream`) y regístrala en
`app/sources/__init__.py`. El resto (Drive, reintentos, cola, bot) no cambia.
