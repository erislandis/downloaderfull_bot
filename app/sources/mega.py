"""Origen Mega: descarga y descifra en Python puro (AES-CTR), sin depender de megatools.

Ventajas frente a un binario externo: admite carpetas, reanuda a mitad de archivo
(el contador CTR se posiciona en cualquier bloque) y no requiere Docker.
Limitación conocida: no se verifica el MAC del archivo (integridad extremo a extremo).
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
import struct
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from Crypto.Cipher import AES

from ..errors import QuotaExceeded, ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from .base import RemoteFile, Source

API_URL = "https://g.api.mega.co.nz/cs"
MAX_FOLDER_FILES = 100
READ_CHUNK = 256 * 1024

_FILE_RE = re.compile(r"mega\.(?:nz|co\.nz)/(?:#!|file/)([\w-]{8})[!#]([\w-]{43})")
_FOLDER_RE = re.compile(
    r"mega\.(?:nz|co\.nz)/(?:#F!|folder/)([\w-]{8})[!#]([\w-]{22})(?:[!/]+(?:file/)?([\w-]{8}))?"
)


# -- criptografía y utilidades (funciones puras, cubiertas por tests) -------------------


def b64d(s: str) -> bytes:
    s = s.replace(",", "").replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def file_key_parts(key: bytes) -> tuple[bytes, bytes]:
    """De la clave de 32 bytes de un archivo saca (clave AES de 16 bytes, nonce de 8)."""
    k = struct.unpack(">8I", key)
    aes_key = struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
    return aes_key, struct.pack(">2I", k[4], k[5])


def decrypt_attrs(at: str, aes_key: bytes) -> dict[str, Any]:
    try:
        plain = AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).decrypt(b64d(at)).rstrip(b"\0")
        if not plain.startswith(b"MEGA"):
            raise ValueError("prefijo inválido")
        return json.loads(plain[4:])
    except ValueError as exc:  # incluye JSONDecodeError
        raise SourceError("No pude descifrar los datos del archivo: la clave del enlace parece incorrecta.") from exc


def decrypt_node_key(k_field: str, master: bytes, expected_len: int | None = None) -> bytes | None:
    """Descifra la clave de un nodo ("uid:clave[/uid:clave]") con ``master``.

    ``expected_len`` filtra por longitud (32 para archivos, 16 para carpetas): como
    AES-ECB no cambia el tamaño, la clave cifrada ya indica para qué sirve la entrada.
    """
    for entry in k_field.split("/"):
        _, _, enc = entry.partition(":")
        if not enc:
            continue
        try:
            raw = b64d(enc)
        except ValueError:
            continue
        if not raw or len(raw) % 16:
            continue
        if expected_len is not None and len(raw) != expected_len:
            continue
        return AES.new(master, AES.MODE_ECB).decrypt(raw)
    return None


class MegaDecryptor:
    """Descifrado en flujo de AES-CTR con soporte para empezar en cualquier desplazamiento."""

    def __init__(self, file_key: bytes, offset: int = 0) -> None:
        aes_key, nonce = file_key_parts(file_key)
        self.aligned_start = offset - offset % 16
        self._skip = offset - self.aligned_start
        self._cipher = AES.new(aes_key, AES.MODE_CTR, nonce=nonce, initial_value=self.aligned_start // 16)

    def feed(self, data: bytes) -> bytes:
        out = self._cipher.decrypt(data)
        if self._skip:
            drop = min(self._skip, len(out))
            out, self._skip = out[drop:], self._skip - drop
        return out


@dataclass(frozen=True)
class MegaLink:
    kind: str  # "file" | "folder"
    handle: str
    key: bytes
    node: str | None = None  # archivo concreto dentro de una carpeta


def parse_link(url: str) -> MegaLink | None:
    if m := _FOLDER_RE.search(url):
        return MegaLink("folder", m[1], b64d(m[2]), m[3])
    if m := _FILE_RE.search(url):
        return MegaLink("file", m[1], b64d(m[2]))
    return None


def _api_error(code: int) -> SourceError:
    known = {
        -8: "El enlace de Mega expiró.",
        -9: "El archivo de Mega no existe o fue eliminado.",
        -11: "No tienes acceso a este contenido de Mega.",
        -14: "La clave del enlace de Mega es inválida.",
        -16: "Mega bloqueó este contenido (infracción de sus términos).",
    }
    if code == -17:
        return QuotaExceeded("Se agotó la cuota de transferencia de Mega.")
    return SourceError(known.get(code, f"Mega devolvió el error {code}."))


def walk_folder(
    nodes: list[dict[str, Any]], root_handle: str, root_key: bytes, only_node: str | None = None
) -> list[tuple[str, str, int, bytes]]:
    """Recorre una carpeta de Mega (con subcarpetas anidadas) resolviendo la cadena de claves.

    Devuelve ``(ruta_relativa, handle, tamaño, clave_archivo32)`` por cada archivo.
    Una carpeta cuya clave no se pueda descifrar se omite junto con todo su contenido;
    ``only_node`` limita el resultado a un archivo concreto (enlace profundo /file/...).
    """
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node.get("h") and node.get("p"):
            by_parent.setdefault(str(node["p"]), []).append(node)

    key_of: dict[str, bytes] = {root_handle: root_key}
    visited: set[str] = {root_handle}
    found: list[tuple[str, str, int, bytes]] = []
    queue: deque[tuple[str, str]] = deque([(root_handle, "")])
    while queue:
        parent, path = queue.popleft()
        master = key_of[parent]
        for node in by_parent.get(parent, ()):
            handle = str(node["h"])
            k_field, attrs, kind = node.get("k"), node.get("a"), node.get("t")
            if not k_field or not attrs or handle in visited:
                continue
            visited.add(handle)
            if kind == 0:  # archivo: su clave (32 B) está cifrada con la clave de la carpeta padre
                raw = decrypt_node_key(k_field, master, expected_len=32)
                if raw is None:
                    continue
                try:
                    name = decrypt_attrs(attrs, file_key_parts(raw)[0]).get("n") or handle
                except SourceError:
                    continue
                if only_node is not None and handle != only_node:
                    continue
                found.append((f"{path}{name}", handle, int(node.get("s") or 0), raw))
            elif kind in (1, 2):  # carpeta (o raíz): clave AES de 16 B para sus hijos
                folder_key = decrypt_node_key(k_field, master, expected_len=16)
                if folder_key is None:
                    continue
                key_of[handle] = folder_key
                try:
                    sub = decrypt_attrs(attrs, folder_key).get("n") or handle
                except SourceError:
                    sub = handle
                queue.append((handle, f"{path}{sub}/"))
    return found


# -- origen ------------------------------------------------------------------------------


class MegaSource(Source):
    key = "mega"
    label = "Mega"
    _seq = itertools.count(1)

    @classmethod
    def matches(cls, url: str) -> bool:
        return parse_link(url) is not None

    async def inspect(self, url: str) -> list[RemoteFile]:
        link = parse_link(url)
        if link is None:
            raise SourceError("Enlace de Mega no válido.")
        expected = 32 if link.kind == "file" else 16
        if len(link.key) != expected:
            raise SourceError("La clave del enlace de Mega está incompleta o dañada.")
        if link.kind == "file":
            return [await self._inspect_file(link)]
        return await self._inspect_folder(link)

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        kind, *parts = file.ref.split(":")
        if kind == "file":
            handle, key_b64 = parts
            payload, folder = {"a": "g", "g": 1, "p": handle}, None
        else:
            folder, node, key_b64 = parts
            payload = {"a": "g", "g": 1, "n": node}
        info = await self._api(payload, folder)
        url = info.get("g")
        if not isinstance(url, str):
            raise SourceError("Mega no entregó un enlace de descarga.")

        decryptor = MegaDecryptor(b64d(key_b64), offset)
        headers = {"Range": f"bytes={decryptor.aligned_start}-"} if decryptor.aligned_start else {}
        async with self._http.stream("GET", url, headers=headers) as r:
            if r.status_code == 509:
                wait = r.headers.get("X-MEGA-Time-Left", "")
                raise QuotaExceeded(
                    "Mega limitó tu cuota de transferencia.", int(wait) if wait.isdigit() else None
                )
            if decryptor.aligned_start and r.status_code != 206:
                raise ResumeNotSupported("Mega no permitió reanudar la descarga.")
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError("Mega está fallando; reintentando.")
            if r.status_code >= 400:
                raise SourceError(f"Mega respondió con el error HTTP {r.status_code}.")
            async for chunk in r.aiter_bytes(READ_CHUNK):
                out = decryptor.feed(chunk)
                if out:
                    yield out

    # -- internos ---------------------------------------------------------------------

    async def _api(self, payload: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": next(self._seq)}
        if folder:
            params["n"] = folder
        for attempt in range(5):
            try:
                r = await self._http.post(API_URL, params=params, json=[payload], timeout=30)
            except httpx.HTTPError as exc:
                raise TransientError("No pude conectar con Mega.") from exc
            if r.status_code >= 500:
                raise TransientError("La API de Mega está fallando.")
            data = r.json()
            item = data if isinstance(data, int) else data[0]
            if isinstance(item, int):
                if item in (-3, -4):  # EAGAIN / límite de ritmo: reintento con espera
                    await asyncio.sleep(1.5**attempt)
                    continue
                raise _api_error(item)
            code = item.get("e")
            if isinstance(code, int) and code < 0 and "g" not in item and "s" not in item:
                err = _api_error(code)
                if isinstance(err, QuotaExceeded) and isinstance(item.get("tl"), int):
                    err.retry_after = item["tl"]
                raise err
            return item
        raise TransientError("Mega no responde; intenta de nuevo en un momento.")

    async def _inspect_file(self, link: MegaLink) -> RemoteFile:
        info = await self._api({"a": "g", "p": link.handle})
        size, at = info.get("s"), info.get("at")
        if not isinstance(size, int) or not at:
            raise SourceError("Mega no devolvió los datos del archivo.")
        aes_key, _ = file_key_parts(link.key)
        name = decrypt_attrs(at, aes_key).get("n") or link.handle
        return RemoteFile(safe_filename(name), size, f"file:{link.handle}:{b64e(link.key)}")

    async def _inspect_folder(self, link: MegaLink) -> list[RemoteFile]:
        res = await self._api({"a": "f", "c": 1, "r": 1}, folder=link.handle)
        found = walk_folder(res.get("f") or [], link.handle, link.key, link.node)

        if not found:
            raise SourceError("La carpeta de Mega está vacía o no se pudo leer.")
        if len(found) > MAX_FOLDER_FILES:
            raise SourceError(f"La carpeta tiene más de {MAX_FOLDER_FILES} archivos; usa un enlace más específico.")
        return [
            RemoteFile(safe_filename(path), size, f"node:{link.handle}:{h}:{b64e(raw)}")
            for path, h, size, raw in found
        ]
