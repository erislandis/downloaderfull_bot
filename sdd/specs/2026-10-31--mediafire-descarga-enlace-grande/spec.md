# Descargar el enlace grande de MediaFire sin repetir la página bloqueada

## Problema

El enlace `https://www.mediafire.com/file/rdle9nv3j93z9y9/UNLOCKTOOL+OFFLINE.rar/file?action=check`
falla con el mensaje de que MediaFire está bloqueando la IP del servidor por
Cloudflare. El enlace pequeño `https://www.mediafire.com/file/10zchzczswkf95f/BFTSetup1.8.zip/file`
sí funciona.

El diagnóstico local muestra que la página del archivo grande sí entrega un enlace
directo válido y que la API pública devuelve nombre y tamaño. El problema aparece
al volver a pedir la página durante la descarga: `inspect()` obtiene un enlace
directo fresco, pero `stream()` lo descarta y resuelve la página otra vez. En la IP
de Render esa segunda petición puede recibir el reto de Cloudflare y el job falla
aunque el enlace directo recién obtenido seguía siendo usable.

## Objetivos

- Reutilizar durante la descarga el enlace directo fresco obtenido en `inspect()`,
  sin volver a pedir la página inmediatamente.
- Mantener el reintento con una página nueva si el enlace directo caduca o pide
  reparación.
- Probar varias rutas públicas de la página de MediaFire antes de declarar que la
  IP está bloqueada.
- No cambiar el flujo general de descarga, el troceado por Bot API ni la entrega
  por Telegram.
- Añadir pruebas herméticas que reproduzcan el fallo y la recuperación.

## Fuera de alcance

- Resolver Cloudflare con proxies, servicios externos o credenciales.
- Cambiar `curl-cffi`, añadir dependencias o modificar `requirements.txt`.
- Soportar carpetas de MediaFire.
- Cambiar el mensaje de bloqueo cuando todas las alternativas fallen.
- Modificar el uploader, MTProto o el README.

## Restricciones

- No se pueden hacer peticiones reales en los tests: deben usar
  `httpx.MockTransport`, sesiones falsas y `asyncio.run`.
- No se instalan dependencias ni se descargan recursos.
- El cambio afecta al flujo de entrega, por lo que requiere esta spec y su plan.
- No se modifican archivos de solo lectura de `AGENTS.md`.
- La caché de enlaces es efímera y local al proceso; no sustituye una cola durable.

## Criterios de aceptación

1. Tras `inspect()`, `stream()` usa el enlace directo fresco sin pedir otra vez la
   página del archivo.
2. Si el enlace directo cacheado caduca, pide una página nueva y continúa.
3. Si la página exacta está bloqueada, prueba al menos la página canónica y la ruta
   `/download/{quickkey}` antes de lanzar el error de Cloudflare.
4. El enlace directo de un archivo con aviso de malware conserva su `post_data`
   (`pass`) al reutilizarse.
5. Los tests existentes de MediaFire y los flujos HTTP siguen pasando.
6. Se añade una prueba de regresión que falla con el código anterior y pasa con el
   nuevo.
