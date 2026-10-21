"""Cliente HTTP con protección SSRF para los orígenes que aceptan URLs arbitrarias.

El bot es público y corre dentro de la red de Render: sin protección, alguien podría pedirle
``http://localhost:10000/api/status``, ``http://169.254.169.254/`` (metadatos de la nube) o un
servicio de la red privada de Render y recibir la respuesta como si fuera un "archivo".

``GuardedTransport`` comprueba, **antes de cada petición** (también las de cada redirección,
porque httpx las envía por el mismo transporte), que el destino resuelve solo a direcciones
públicas. Limitación conocida: la resolución DNS de la comprobación y la de la conexión son
dos consultas distintas, así que un atacante con un DNS malicioso (*DNS rebinding*) podría
intentar colarse entre ambas. Cerrar del todo esa ventana exige fijar la IP en la conexión
(tocando internals de httpcore); aquí se acepta ese riesgo residual a cambio de no depender
de ellos.

Cada operación usa su propio cliente: así no se comparte el almacén de cookies entre usuarios
(lo que un sitio ponga en una cookie para un usuario no llega a las peticiones de otro).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable

import httpx

from ..errors import SourceError

Resolver = Callable[[str, int], Awaitable[list[str]]]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Sin compresión: el tamaño de Content-Length debe ser el de los bytes que se suben
    # (httpx descomprime gzip al vuelo y el tamaño declarado dejaría de coincidir).
    "Accept-Encoding": "identity",
}

# ``is_global`` de Python da True para multicast y para prefijos de traducción IPv4↔IPv6
# que pueden embeber una dirección privada (p. ej. 64:ff9b::7f00:1 → 127.0.0.1).
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # NAT64 local
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
    )
)


def is_public_ip(value: str) -> bool:
    """¿Es una dirección alcanzable por Internet y no de la red interna?"""
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_multicast or not addr.is_global:
        return False
    return not any(addr in net for net in _BLOCKED_NETWORKS if net.version == addr.version)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class GuardedTransport(httpx.AsyncBaseTransport):
    """Transporte que rechaza destinos que no resuelvan solo a direcciones públicas."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=15.0)
        )
        self._resolve = resolver or system_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._check(request.url)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _check(self, url: httpx.URL) -> None:
        host = url.host
        if not host:
            raise SourceError("El enlace no tiene un servidor válido.")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = [str(ipaddress.ip_address(host))]  # IP escrita directamente en la URL
        except ValueError:
            try:
                addresses = await self._resolve(host, port)
            except OSError:
                raise SourceError(
                    "No encuentro ese servidor. Revisa que el enlace esté bien escrito."
                ) from None
        if not addresses or not all(is_public_ip(a) for a in addresses):
            raise SourceError("Ese enlace apunta a una dirección interna o no permitida.")


def guarded_client(
    inner: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None
) -> httpx.AsyncClient:
    """Cliente de usar y tirar (``async with``) con protección SSRF y redirecciones acotadas."""
    return httpx.AsyncClient(
        transport=GuardedTransport(inner, resolver),
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(connect=20.0, read=90.0, write=60.0, pool=30.0),
        headers=BROWSER_HEADERS,
        trust_env=False,
    )
