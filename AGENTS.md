# AGENTS.md

## Contexto y fuente de verdad

- BridgeBot descarga enlaces de MediaFire, Mega, Google Drive o directos y entrega los archivos por Telegram.
- **La fuente de verdad para cambios y nuevas funcionalidades es la especificación (spec) acordada**, no el código existente. El código actual sigue siendo la referencia del estado presente, pero **no debe usarse para inferir el comportamiento deseado de una feature nueva**.
- La entrega actual es solo por Bot API: `app/uploader.py` trocea los archivos mayores de `BOT_API_MAX_MB` en documentos `nombre.partNNN.ext`.
- La ruta MTProto descrita en el README (cuenta de usuario, `copyMessage`, archivo único de hasta 2 GB) **no está activa**. `app/mtproto_upload.py`, `app/streams.py` y el puente `BRIDGE_PREFIX`/`resolve_bridge` son legado o compatibilidad sin uso real.
- Varios tests, sobre todo `tests/test_uploader.py`, todavía esperan esa ruta MTProto eliminada y fallarán contra el uploader actual. No los uses para inferir el comportamiento vigente.

## Restricciones operativas del agente

Estas reglas son **prohibiciones de comportamiento**. No son negociables y no las puede justificar una spec.

### No compilar ni empaquetar
- No ejecutes `python -m compileall`, `py_compile`, `pyinstaller`, `python -m build`, `setuptools`, `wheel` ni cualquier otro comando que genere binarios, `.pyc`, wheels, sdists o ejecutables.
- No generes artefactos de distribución ni los dejes en el árbol de trabajo.
- La única validación permitida es `pytest` y, si aplica, arrancar `python run.py` desde `bot/` (ver “Comandos”).
- No añadas `Makefile`, `tox.ini`, `noxfile.py`, `pre-commit` ni configuraciones de CI que no existan ya.

### No descargar recursos
- No descargues archivos, fixtures, modelos, binarios, media ni repositorios externos (`curl`, `wget`, `git clone`, `huggingface-cli`, etc.).
- No instales paquetes ad‑hoc (`pip install X`, `uv add X`). La única instalación permitida es `pip install -r requirements-dev.txt` cuando el usuario lo pida explícitamente.
- No añadas dependencias nuevas a `requirements.txt` o `requirements-dev.txt` sin una spec que lo justifique.
- Los tests deben seguir siendo **herméticos**: `httpx.MockTransport`, sesiones falsas de aiogram, `asyncio.run`. Nunca red real, nunca `respx`, nunca llamadas a servicios externos.

### Archivos de solo lectura
Los siguientes ficheros son **de lectura únicamente**: puedes leerlos, pero **no editarlos, renombrarlos ni moverlos** sin una spec explícita aprobada:

- `.env`, `.env.*` — secretos, nunca tocar.
- `Errores.txt` — log suelto de Render, no es código ni documentación.
- `scripts/get_session.py` — depende de `telethon`/`cryptg`; cualquier cambio requiere spec.
- `app/mtproto_upload.py`, `app/streams.py` — módulos legado; no modificar sin spec que justifique su retirada o reactivación.
- `requirements.txt` — conserva `telethon` y `cryptg` por `scripts/get_session.py` y el módulo MTProto legado.
- `README.md` — está desfasado; no “arreglarlo” como efecto colateral de otra tarea.
- `tests/test_uploader.py` — desfasado respecto al uploader actual; no usarlo como referencia ni corregirlo fuera de una spec que lo cubra.

> Esta lista es **ampliable**. Si durante una tarea detectas otro fichero que debería ser de solo lectura, propón añadirlo aquí en la spec correspondiente; no lo añadas de forma implícita.

### No crear copias ni duplicados
- No crees `*.bak`, `*.old`, `*.orig`, `*_copy.py`, `* copy.py`, `copia de *`, `*.~1~`, `*.rej` ni variantes similares.
- No dupliques archivos “por si acaso” antes de editarlos: usa Git (`git diff`, `git status`) como mecanismo de seguridad.
- No renombres archivos existentes ni los muevas de carpeta como efecto colateral de otra tarea; eso requiere spec.
- No generes archivos “temporales” en el repo (`tmp_*.py`, `scratch.py`, `untitled*`). Si necesitas probar algo, usa el intérprete interactivo o `/tmp`, nunca el árbol de trabajo.

### No ejecutar comandos destructivos ni de producción
- No ejecutes `python app/main.py` (servidor Render; termina a propósito).
- No ejecutes `python app/polling.py` como archivo suelto desde dentro de `app/`.
- No lances `git push`, `git reset --hard`, `git clean -fd`, `git checkout .` ni `git rebase` sin orden explícita del usuario.
- No ejecutes `uvicorn app.main:app` (modo producción) en local.
- No borres ni renombres archivos sin spec que lo respalde.

### Idioma y estilo
- Textos de usuario, docstrings y comentarios en **español**. Las specs y los planes en `sdd/` también.
- Mantén el estilo existente del repositorio; no introduzcas formateadores ni convenciones nuevas.

## Flujo Spec‑Driven Development (SDD)

0. **Antes de escribir la spec**, revisa las secciones “Restricciones operativas del agente” y “Limitaciones conocidas del proyecto”. Si la feature choca con alguna, la spec debe mencionarla y proponer la alternativa viable (o justificar por qué se acepta la limitación).
1. **Antes de implementar cualquier feature nueva o cambio de comportamiento**, crea una especificación en `sdd/specs/<YYYY-MM-DD>--<nombre-feature>/spec.md`.
2. La spec describe **el problema, los objetivos y los criterios de aceptación**. Sin detalles de implementación: esos van al plan.
3. Con la spec aprobada, genera un plan de implementación en `sdd/plans/<YYYY-MM-DD>--<nombre-feature>/plan.md`.
4. El plan contiene arquitectura, módulos afectados, contratos y una lista de tareas ordenadas.
5. La implementación sigue el plan y la spec. Si durante la implementación descubres que spec o plan están incompletos, **primero actualizas el artefacto en `sdd/`** y luego continúas.
6. Al terminar, adjunta evidencia de verificación en `sdd/evidence/<YYYY-MM-DD>--<nombre-feature>/` (salida de `pytest`, capturas, logs).

## Estructura del directorio `sdd/`
sdd/
├── specs/                # Especificaciones (qué y por qué)
│   └── 2026-03-01--nombre-feature/
│       ├── spec.md
│       ├── plan.md
│       └── tasks.md
├── plans/                # Planes de implementación (cómo)
│   └── 2026-03-01--nombre-feature/
│       └── plan.md       # Si prefieres separar el plan de la spec
├── templates/            # Plantillas base (spec, plan, tasks)
└── evidence/             # (Opcional) Verificaciones, logs, capturas

- El agente **debe copiar la plantilla correspondiente** de `sdd/templates/` al crear una nueva spec o plan.
- Las carpetas de features usan el formato `YYYY-MM-DD--kebab-case-title` para mantener orden cronológico y legible.

## Limitaciones conocidas del proyecto

Restricciones **técnicas** que condicionan cualquier spec. Si una feature las incumple, la spec debe declararlo y justificar la alternativa.

### Plataforma Telegram
- **Subida solo por Bot API**: no hay MTProto activo; el tamaño máximo por documento está acotado por `BOT_API_MAX_MB`. Los archivos mayores se trocean en `nombre.partNNN.ext` y **no se pueden entregar como archivo único**.
- **`callback_data` limitado a 64 bytes**: los botones de carpetas usan tokens cortos (`fb:<token>:<acción>`). No se pueden pasar payloads largos por botones inline.
- **Secret del webhook**: Telegram solo acepta ciertos caracteres en `X-Telegram-Bot-Api-Secret-Token`; el secret se normaliza y **no debe compararse con el valor crudo**.

### Almacenamiento y estado
- **Estado en memoria volátil**: administradores y bloqueos se pierden al reiniciar; solo `ADMIN_USER_IDS` persiste vía entorno. No hay base de datos.
- **Árboles de carpetas con TTL de 30 minutos** (`app/folders.py`): pasado ese tiempo, los botones dejan de ser válidos.
- **Sin persistencia de jobs**: no hay cola durable; un reinicio pierde los jobs en curso.

### ZIP (`app/sources/archive.py`)
- **Sin ZIP64**: máximo 4 GB por ZIP y **máximo 65535 entradas**.
- `ZIP_LEVEL=1` por defecto; no se puede asumir compresión fuerte.

### Configuración
- **Se exigen `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` y `TELEGRAM_SESSION`** aunque MTProto no se use para subir. No se pueden eliminar sin tocar `scripts/get_session.py` y el módulo legado.
- **Webhook** requiere `WEBHOOK_BASE_URL` o `RENDER_EXTERNAL_URL` **y** `WEBHOOK_SECRET`; sin ellos no arranca en modo Render.

### Red y seguridad
- **Todas las URLs arbitrarias pasan por `app/sources/_guard.py`** (protección SSRF). Cualquier nuevo `Source` debe respetar ese punto.
- **`DirectSource` debe quedar el último en `SOURCES`**, porque acepta cualquier `http(s)`.

### Tooling y calidad
- **No hay lint, formateador, typecheck, CI ni lockfile**. No se pueden añadir comandos de verificación que no existan.
- **Tests herméticos sin `pytest-asyncio`**: se usa `asyncio.run` y `httpx.MockTransport`. Los nuevos tests deben seguir ese patrón.
- **Parte de la suite está desfasada** (sobre todo `tests/test_uploader.py` respecto a MTProto). Una spec que toque el uploader debe incluir la actualización de esos tests como tarea explícita.

## Comandos

- Instalar dependencias de desarrollo: `pip install -r requirements-dev.txt`
- Ejecutar toda la suite: `pytest` (desde la raíz del repo; `pytest.ini` fija `pythonpath = .` y `testpaths = tests`)
- Ejecutar un test concreto: `pytest tests/test_jobs.py::test_nombre`
- Arranque local, desde `bot/`: `python run.py` (equivale a `python -m app.polling`)
- No ejecutes `python app/main.py`: es el servidor de Render y termina a propósito.
- Tampoco ejecutes `python app/polling.py` como archivo suelto desde dentro de `app/`.
- Producción/Render: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- No hay lint, formateador, typecheck, CI ni lockfile configurados; no inventes comandos.

## Arquitectura

- `run.py` → `app.polling` → `app.runtime.Runtime` para local; `app.main:app` → `Runtime` + webhook para Render.
- `Runtime` (`app/runtime.py`) construye el bot de aiogram, el uploader, `JobManager`, `FolderBrowser`, administradores, bloqueos y tareas de fondo.
- Flujo de un enlace: `app/bot.py` → `resolve_source()` → `Source.inspect()` → `Job` → `JobManager` → `TelegramUploader` → `Source.stream()`.
- Los orígenes viven en `app/sources/` y se registran en `SOURCES` (`app/sources/__init__.py`). El orden importa: `DirectSource` acepta cualquier `http(s)` y debe quedar el último.
- Para añadir un sitio: implementa `matches`, `inspect` y `stream` heredando de `Source` (`app/sources/base.py`) y regístralo en `SOURCES`.
- Las URLs arbitrarias deben seguir pasando por `app/sources/_guard.py` (protección SSRF). `direct` y `gdrive` crean un `guarded_client()` por operación; Mega y MediaFire usan el cliente HTTP compartido del `Runtime`.
- Carpetas: `app/folders.py` guarda los árboles en memoria con TTL de 30 minutos. Los botones usan `fb:<token>:<acción>` porque Telegram limita `callback_data` a 64 bytes.
- ZIP: `app/sources/archive.py` genera el ZIP en streaming, sin ZIP64 y con máximo de 65535 entradas. `ZIP_LEVEL=1` por defecto.
- Configuración: `Settings.from_env()` carga `.env` de la raíz del repo, pero las variables de entorno tienen prioridad. **Sigue exigiendo** `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` y `TELEGRAM_SESSION` aunque MTProto no se use para subir.
- Webhook: requiere `WEBHOOK_BASE_URL` o `RENDER_EXTERNAL_URL` y `WEBHOOK_SECRET`. El secret se normaliza a caracteres válidos de Telegram (`X-Telegram-Bot-Api-Secret-Token`); no lo compare con el valor crudo.
- Administradores y bloqueos en memoria se pierden al reiniciar; solo `ADMIN_USER_IDS` persiste vía entorno.

## Convenciones y trampas

- Textos de usuario, docstrings y comentarios están en español; mantén ese estilo.
- Los mensajes usan HTML de Telegram: escapa siempre texto dinámico (`escape`/`html.escape`).
- Tests herméticos: sin red, con `httpx.MockTransport`, sesiones falsas de aiogram y `asyncio.run`. No hay `pytest-asyncio`; sigue el patrón existente.
- `Settings` es un dataclass congelado que los tests construyen directamente. Si añades campos obligatorios, actualiza los fixtures.
- `direct` y `gdrive` se parchean en tests mediante `tests/test_direct_gdrive.py::patch_clients`; respeta ese punto de inyección.
- `requirements.txt` conserva `telethon` y `cryptg` por el script `scripts/get_session.py` y el módulo MTProto legado; no los elimines sin revisar esos usos.
- Nunca subas `.env` ni secretos. `Errores.txt` es un log suelto de Render, no documentación ni código.

## Estado del repo

- El árbol de trabajo está sucio y contiene módulos nuevos sin seguimiento; no asumas que `git HEAD` representa el código actual.
- README y parte de los tests están desfasados respecto al uploader. Contrasta con `app/uploader.py`, `app/runtime.py` y `app/bot.py` antes de cambiar el flujo de entrega.
- **Cualquier cambio que afecte al flujo de entrega debe ir acompañado de una spec y un plan en `sdd/` que justifiquen el nuevo comportamiento.**