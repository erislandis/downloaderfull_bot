# Migrar el proyecto a Python 3.14.8 en Render

## Problema

`render.yaml` fija `PYTHON_VERSION=3.12.8`. Render ya usa Python **3.14.8** como
versión por defecto para servicios creados desde el 12 de octubre de 2026, y el
proyecto debe alinearse con esa versión. Las dependencias con extensiones nativas
(`curl-cffi`, `cryptg`, `pycryptodome`) deben permitir versiones con soporte para
Python 3.14; de lo contrario el despliegue puede intentar compilar versiones
antiguas o fallar.

## Objetivos

- Fijar Render en Python `3.14.8`.
- Actualizar `requirements.txt` para que las versiones mínimas declaradas tengan
  soporte de Python 3.14 y sigan usando las versiones actuales del ecosistema.
- Mantener `telethon` y `cryptg`, requeridos por `scripts/get_session.py` y el
  módulo MTProto legado.
- Actualizar `requirements-dev.txt` a la línea actual de `pytest` si es compatible.
- No cambiar el comportamiento del bot ni añadir dependencias nuevas.

## Fuera de alcance

- Reescribir el README desfasado.
- Reactivar o retirar MTProto.
- Añadir `pyproject.toml`, lockfile, CI, lint o formateadores.
- Pinear versiones exactas o crear un lockfile.

## Restricciones

- `requirements.txt` figura como solo lectura en `AGENTS.md`; esta spec es la
  autorización explícita para modificarlo únicamente en las versiones de sus
  dependencias existentes.
- No se descargan paquetes ni se instalan dependencias durante la tarea.
- Los tests deben seguir siendo herméticos. La suite completa puede no ejecutarse
  si faltan dependencias locales; se ejecutarán los tests que no requieran
  `aiogram`.
- No se tocan `.env`, `Errores.txt`, `README.md`, `scripts/get_session.py`,
  `app/mtproto_upload.py` ni `app/streams.py`.

## Criterios de aceptación

1. `render.yaml` declara `PYTHON_VERSION` con el valor `3.14.8`.
2. `requirements.txt` conserva todas las dependencias existentes y declara mínimos
   compatibles con Python 3.14:
   - `aiogram>=3.31,<4`
   - `fastapi>=0.162,<1`
   - `uvicorn[standard]>=0.54,<1`
   - `httpx>=0.28,<1`
   - `curl-cffi>=0.17,<1`
   - `pycryptodome>=4.2,<5`
   - `telethon>=1.45,<2`
   - `python-dotenv>=1.2,<2`
   - `cryptg>=0.6,<1`
3. `requirements-dev.txt` usa `pytest>=9,<10`.
4. No se añaden ni eliminan dependencias.
5. Los tests herméticos disponibles pasan en Python 3.14.8.
