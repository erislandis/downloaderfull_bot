# Plan: reutilizar el enlace fresco de MediaFire y probar variantes de página

## Contexto

`MediaFireSource.inspect()` llama a `_resolve()`, obtiene un enlace directo y
devuelve como `ref` la URL de la página. Después, `stream()` vuelve a llamar a
`_resolve(file.ref)` para obtener otro enlace directo. En una IP de datacenter esa
segunda petición puede recibir el reto de Cloudflare, aunque el enlace obtenido
segundos antes seguía funcionando.

La página del archivo grande sí entrega un enlace `downloadNNN.mediafire.com`, y
la API pública devuelve el tamaño. El problema es la repetición innecesaria de la
petición HTML y la falta de alternativas de URL cuando Cloudflare bloquea una ruta.

## Archivos afectados

- `app/sources/mediafire.py`: caché efímera de enlaces frescos, variantes de página
  y uso del enlace cacheado en `stream()`.
- `tests/test_mediafire.py`: pruebas de `page_candidates()` y de la variante
  exacta bloqueada.
- `tests/test_http_flows.py`: prueba de regresión de `inspect()` + `stream()` sin
  segunda petición de página.
- `sdd/evidence/2026-10-31--mediafire-descarga-enlace-grande/`: salida de `pytest`.

## Contratos y decisiones

- La caché se indexa por `quickkey` y guarda `(expiración, direct_url, post_data)`.
  TTL de 5 minutos y límite de 128 entradas. Es local al proceso y se pierde al
  reiniciar, igual que los jobs.
- `stream()` intenta primero el enlace cacheado. Si MediaFire pide reparación o
  rechaza el enlace, lo olvida y resuelve una página nueva.
- `_resolve()` prueba varias URLs en orden:
  1. La página exacta recibida, conservando el nombre y `?action=check`, pero
     eliminando `dkey` y `r`.
  2. La misma página exacta sin query, por si `?action=check` es lo bloqueado.
  3. La página canónica `/file/{quickkey}/file`.
  4. `/download/{quickkey}`.
  5. `/file/{quickkey}/file?action=download`.
  6. `/?{quickkey}`.
- Primero se prueban todas con `httpx`; solo si todas reciben el reto se intenta
  `curl_cffi` sobre la página canónica. Así se evita repetir los perfiles de Chrome
  para cada variante.
- Las redirecciones de la página se siguen manualmente. En el salto se envía
  `Referer` de la página anterior y `Sec-Fetch-Site: same-origin`, como haría un
  navegador. `httpx` no añade `Referer` al seguir un 302 y Cloudflare puede
  rechazar `?action=check` por esa diferencia.
- `curl_cffi` también recibe ese `Referer` cuando se reintenta el último salto.
- `post_data` se conserva junto al enlace directo para los archivos con aviso de
  malware; sin `pass` el GET acaba en `download_repair.php`.
- No se cambia `RemoteFile` ni el contrato de `Source`; la caché queda encapsulada
  en `MediaFireSource`.

## Tareas

1. Añadir `page_candidates(url)` como función pura y probarla.
2. Añadir la caché efímera y los métodos `_remember_link`, `_fresh_link` y
   `_forget_link`.
3. Cambiar `stream()` para usar el enlace cacheado y volver a resolver solo si
   caduca o pide reparación.
4. Cambiar `_resolve()` para probar las variantes de página y usar `curl_cffi` solo
   como último recurso.
5. Añadir la prueba de regresión: la segunda petición de página devuelve Cloudflare;
   `inspect()` y `stream()` deben completar usando el enlace fresco.
6. Ajustar la prueba de `curl_cffi` al nuevo número de intentos.
7. Ejecutar los tests herméticos y guardar la evidencia.

## Verificación

- `pytest -q tests/test_mediafire.py tests/test_http_flows.py`
- `pytest -q tests/test_config.py tests/test_archive.py tests/test_folders.py tests/test_direct_gdrive.py tests/test_mediafire.py tests/test_mega.py tests/test_http_flows.py`
- Revisar `git diff` para confirmar que solo cambiaron los archivos previstos y
  los artefactos SDD.
- No se ejecutará la suite completa si falta `aiogram`.
