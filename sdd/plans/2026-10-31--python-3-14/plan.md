# Plan: migración a Python 3.14.8

## Contexto

Render documenta `3.14.8` como versión por defecto actual. El entorno local de
trabajo ya ejecuta Python `3.14.8`, lo que permite validar los módulos que no
dependen de `aiogram`.

Las versiones mínimas actuales de `curl-cffi`, `cryptg` y `pycryptodome` son
anteriores a las primeras releases con soporte explícito de Python 3.14. Aunque
pip suele resolver la última versión compatible, dejar los mínimos antiguos hace
que el contrato de `requirements.txt` no refleje la versión de Python soportada.

## Archivos afectados

- `render.yaml`: variable `PYTHON_VERSION`.
- `requirements.txt`: mínimos de las dependencias existentes.
- `requirements-dev.txt`: línea de `pytest`.
- `sdd/evidence/2026-10-31--python-3-14/`: evidencia de verificación.

No se modifican módulos de `app/` ni tests.

## Contratos y decisiones

- Se mantiene el estilo de rangos compatibles (`>=mínimo,<siguiente_major`) en
  lugar de fijar versiones exactas.
- Se conservan `telethon` y `cryptg` por exigencia de `AGENTS.md`.
- `pycryptodome` sube a la línea 4.x porque la release 4.2.0 publica ruedas
  `cp39-abi3` compatibles con Python 3.14.
- No se añaden dependencias nuevas ni se elimina ninguna.
- El cambio no altera el flujo de entrega ni la configuración funcional del bot.

## Tareas

1. Actualizar `render.yaml` a `PYTHON_VERSION=3.14.8`.
2. Actualizar `requirements.txt` con los mínimos compatibles con Python 3.14.
3. Actualizar `requirements-dev.txt` a `pytest>=9,<10`.
4. Ejecutar los tests herméticos que no requieren `aiogram`.
5. Guardar la salida de `pytest` y la versión de Python en `sdd/evidence/`.
6. Revisar `git diff` para confirmar que solo cambiaron los archivos previstos
   (más los artefactos SDD).

## Verificación

- `python --version` debe reportar `3.14.8`.
- `pytest -q tests/test_config.py tests/test_archive.py tests/test_folders.py tests/test_direct_gdrive.py tests/test_mediafire.py tests/test_mega.py tests/test_http_flows.py`
  debe pasar.
- No se intentará instalar `aiogram` ni ejecutar la suite completa si falta esa
  dependencia; se dejará constancia en la evidencia.
