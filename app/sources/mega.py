"""Origen Mega: descarga y descifra en Python puro (AES-CTR), sin depender de megatools.

Ventajas frente a un binario externo: admite carpetas, reanuda a mitad de archivo
(el contador CTR se posiciona en cualquier bloque) y no requiere Docker.
Limitación conocida: no se verifica el MAC del archivo (integridad extremo a extremo).

Detalles de la API pública (verificados contra ``g.api.mega.co.nz/cs``):

* En un enlace público de **carpeta**, el handle del enlace NO es el handle del nodo raíz:
  la API devuelve el subárbol enraizado en otro nodo (el que cuyo ``p`` no aparece en la
  respuesta). La clave del enlace descifra la clave de ese nodo raíz.
* En un share público, la clave de 16 bytes del enlace descifra la clave de **cada** nodo
  (32 bytes en los archivos, 16 en las carpetas), no solo la de los hijos directos.
* Para descargar un nodo de una carpeta compartida: ``{"a": "g", "g": 1, "n": <nodo>}`` con
  el parámetro ``n``=<handle del enlace> en la URL (el handle de la *raíz real* no vale).
* Los enlaces profundos (``.../folder/<nodo>`` o ``.../file/<nodo>``) seleccionan un
  elemento concreto dentro de la carpeta.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import re
import struct
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from Crypto.Cipher import AES

from ..errors import QuotaExceeded, ResumeNotSupported, SourceError, TransientError
from ..utils import safe_filename
from .archive import zip_stream
from .base import BundleFile, FolderNode, Inspection, RemoteFile, Source, node_to_file

API_URL = "https://g.api.mega.co.nz/cs"
MAX_TREE_NODES = 20_000

log = logging.getLogger(__name__)
READ_CHUNK = 256 * 1024

_FILE_RE = re.compile(r"mega\.(?:nz|co\.nz)/(?:#!|file/)([\w-]{8})[!#]([\w-]{43})")
_FOLDER_RE = re.compile(
    r"mega\.(?:nz|co\.nz)/(?:#F!|folder/)([\w-]{8})[!#]([\w-]{22})"
    r"(?:[!/]+(?:(folder|file)/)?([\w-]{4,}))?"
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


def decrypt_node_keys(k_field: str, master: bytes, expected_len: int | None = None) -> list[bytes]:
    """Todas las claves plausibles de un nodo, descifradas con ``master``.

    El campo "k" de la API puede traer **varias entradas** separadas por "/", cada una
    cifrada con una clave distinta ("usuario:clave" o "handle:clave"). En un enlace
    público la del enlace puede no ser la primera, así que se devuelven todas: luego se
    descarta la que no valida (ver ``_key_is_valid``).
    """
    out: list[bytes] = []
    for entry in (k_field or "").split("/"):
        _, _, enc = entry.partition(":")
        if not enc:
            continue
        try:
            raw = b64d(enc)
        except ValueError:
            continue
        if not raw or len(raw) % 16 or len(raw) not in (16, 32):
            continue
        if expected_len is not None and len(raw) != expected_len:
            continue
        out.append(AES.new(master, AES.MODE_ECB).decrypt(raw))
    return out


def decrypt_node_key(k_field: str, master: bytes, expected_len: int | None = None) -> bytes | None:
    """Descifra la clave de un nodo ("uid:clave[/uid:clave]") con ``master``.

    En un share público la clave del enlace descifra la clave de todos los nodos, así que
    ``master`` es siempre la clave del enlace. La longitud del texto cifrado dice de qué
    tipo es el nodo: 32 bytes = archivo, 16 en las carpetas.
    """
    keys = decrypt_node_keys(k_field, master, expected_len)
    return keys[0] if keys else None


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
    node: str | None = None  # elemento concreto dentro de la carpeta
    node_kind: str | None = None  # "file" | "folder"


def parse_link(url: str) -> MegaLink | None:
    if m := _FOLDER_RE.search(url):
        return MegaLink("folder", m[1], b64d(m[2]), m[4], m[3])
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


# -- recorrido de carpetas -----------------------------------------------------------------


def _node_name(attrs: str | None, key: bytes) -> str | None:
    """Nombre del nodo: los atributos van cifrados con su propia clave."""
    if not attrs:
        return None
    aes_key = file_key_parts(key)[0] if len(key) == 32 else key
    try:
        return decrypt_attrs(attrs, aes_key).get("n")
    except SourceError:
        return None


def _key_is_valid(node: dict[str, Any], key: bytes) -> bool:
    """Comprueba la clave de un nodo descifrando sus atributos.

    AES nunca falla al descifrar: con una clave equivocada devuelve bytes basura. Los
    atributos sí llevan un prefijo "MEGA", así que son los que delatan una clave mala
    (y por tanto un enlace con la clave equivocada o un subtree que no nos pertenece).
    """
    attrs = node.get("a")
    if not attrs:
        return True  # el nodo no trae atributos: no hay nada que comprobar
    aes_key = file_key_parts(key)[0] if len(key) == 32 else key
    try:
        decrypt_attrs(attrs, aes_key)
    except SourceError:
        return False
    return True


def _root_candidates(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nodos que pueden ser la raíz del share (su padre no está en la respuesta)."""
    handles = {str(n.get("h")) for n in nodes if n.get("h")}
    candidates = [n for n in nodes if n.get("h") and str(n.get("p") or "") not in handles]
    if candidates:
        return candidates
    # Respaldo: el nodo cuya clave cifrada lleva su propio handle como prefijo.
    own = [
        n
        for n in nodes
        if n.get("h")
        and any(entry.startswith(f"{n['h']}:") for entry in str(n.get("k") or "").split("/"))
    ]
    if own:
        return own
    # Último respaldo: el nodo con más hijos (la raíz real es la que más contiene).
    children: dict[str, int] = defaultdict(int)
    for node in nodes:
        parent = str(node.get("p") or "")
        if parent:
            children[parent] += 1
    if not children:
        return [n for n in nodes if n.get("h")][:1]
    best = max(children.items(), key=lambda kv: kv[1])[0]
    return [n for n in nodes if str(n.get("h")) == best]


def build_tree(nodes: list[dict[str, Any]], link_key: bytes, link_handle: str) -> FolderNode | None:
    """Construye el árbol de la carpeta a partir de los nodos que devuelve la API.

    La clave del enlace descifra la clave de todos los nodos del share. La raíz es el nodo
    cuyo padre no aparece en la respuesta (el handle del enlace no es el de la raíz real).
    """
    if len(nodes) > MAX_TREE_NODES:
        raise SourceError(f"La carpeta es demasiado grande (más de {MAX_TREE_NODES} elementos).")

    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        handle, parent = str(node.get("h") or ""), str(node.get("p") or "")
        if handle and parent:
            by_parent[parent].append(node)

    def make(node: dict[str, Any], parent_path: str | None) -> FolderNode | None:
        """Construye un nodo. ``parent_path`` es la ruta de su carpeta; None = la raíz.

        Las rutas son relativas a la raíz del árbol (la carpeta real del share).
        """
        handle = str(node["h"])
        is_dir = node.get("t") in (1, 2)
        # El campo "k" puede traer varias claves (p. ej. "usuario:clave/handle:clave"): se
        # prueban todas y gana la que descifra los atributos del nodo.
        key = None
        for candidate in decrypt_node_keys(str(node.get("k") or ""), link_key):
            if _key_is_valid(node, candidate):
                key = candidate
                break
        if key is None:
            return None
        name = safe_filename(_node_name(node.get("a"), key) or handle)
        own_path = "" if parent_path is None else (f"{parent_path}/{name}" if parent_path else name)
        if is_dir:
            out = FolderNode(name=name, node_id=handle, is_dir=True, path=own_path)
            for child in by_parent.get(handle, ()):
                if str(child.get("h") or "") == handle:
                    continue
                built = make(child, own_path)
                if built is not None:
                    out.children.append(built)
            out.children.sort(key=lambda n: (not n.is_dir, n.name.lower()))
            out.size = sum(c.size or 0 for c in out.children)
            return out
        return FolderNode(
            name=name,
            node_id=handle,
            size=int(node.get("s") or 0),
            is_dir=False,
            ref=f"node:{link_handle}:{handle}:{b64e(key)}",
            path=own_path,
        )

    best: FolderNode | None = None
    for candidate in _root_candidates(nodes):
        root = make(candidate, None)
        if root is None:
            continue
        if best is None or _tree_weight(root) > _tree_weight(best):
            best = root
    return best


def _tree_weight(node: FolderNode) -> int:
    return len(node.children) + sum(_tree_weight(c) for c in node.children)


def find_node(root: FolderNode, node_id: str) -> FolderNode | None:
    """Busca un nodo por su handle dentro del árbol."""
    if root.node_id == node_id:
        return root
    for child in root.children:
        found = find_node(child, node_id)
        if found is not None:
            return found
    return None


# -- origen ------------------------------------------------------------------------------


class MegaSource(Source):
    key = "mega"
    label = "Mega"
    _seq = itertools.count(1)

    @classmethod
    def matches(cls, url: str) -> bool:
        return parse_link(url) is not None

    async def inspect(self, url: str) -> Inspection:
        link = parse_link(url)
        if link is None:
            raise SourceError("Enlace de Mega no válido.")
        expected = 32 if link.kind == "file" else 16
        if len(link.key) != expected:
            raise SourceError("La clave del enlace de Mega está incompleta o dañada.")
        if link.kind == "file":
            return Inspection(files=[await self._inspect_file(link)])

        root = await self._inspect_folder(link)
        if link.node is None:
            return Inspection(tree=root)
        target = find_node(root, link.node)
        if target is None:
            raise SourceError("El enlace apunta a un elemento que no está dentro de esa carpeta.")
        if target.is_dir:
            return Inspection(tree=target)
        return Inspection(files=[node_to_file(target)])

    async def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        if isinstance(file, BundleFile):
            if offset:
                raise ResumeNotSupported("Un archivo comprimido no se puede reanudar.")
            async for chunk in zip_stream(file.members, self._open_member):
                yield chunk
            return

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
            try:
                async for chunk in r.aiter_bytes(READ_CHUNK):
                    out = decryptor.feed(chunk)
                    if out:
                        yield out
            except httpx.HTTPError as exc:
                # La CDN de Mega corta conexiones largas: se reintenta desde el principio.
                raise TransientError("Se cortó la conexión con Mega; reintentando.") from exc

    # -- internos ---------------------------------------------------------------------

    async def _open_member(self, remote: RemoteFile) -> AsyncIterator[bytes]:
        async for chunk in self.stream(remote):
            yield chunk

    async def _api(self, payload: dict[str, Any], folder: str | None = None) -> dict[str, Any]:
        for attempt in range(5):
            params: dict[str, Any] = {"id": next(self._seq)}
            if folder:
                params["n"] = folder
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
        info = await self._file_info(link)
        size, at = info.get("s"), info.get("at")
        if not isinstance(size, int) or not at:
            raise SourceError("Mega no devolvió los datos del archivo.")
        aes_key, _ = file_key_parts(link.key)
        name = decrypt_attrs(at, aes_key).get("n") or link.handle
        return RemoteFile(safe_filename(name), size, f"file:{link.handle}:{b64e(link.key)}")

    async def _file_info(self, link: MegaLink) -> dict[str, Any]:
        """Datos de un archivo suelto. Se prueban varias formas de petición por si la
        API no reconoce el handle como público."""
        last: SourceError | None = None
        for payload in (
            {"a": "g", "g": 1, "p": link.handle},
            {"a": "g", "p": link.handle, "ssm": 1},
            {"a": "g", "g": 1, "n": link.handle},
        ):
            try:
                info = await self._api(payload)
            except SourceError as exc:
                last = exc
                continue
            if isinstance(info, dict) and ("at" in info or "s" in info):
                return info
        raise last or SourceError("Mega no devolvió los datos del archivo.")

    async def _inspect_folder(self, link: MegaLink) -> FolderNode:
        res = await self._api({"a": "f", "c": 1, "r": 1}, folder=link.handle)
        nodes = res.get("f") or []
        root = build_tree(nodes, link.key, link.handle)
        if root is None:
            if not nodes:
                raise SourceError(
                    "Mega devolvió la carpeta vacía: el enlace puede haber caducado o "
                    "haber sido eliminado."
                )
            log.warning(
                "Mega: %d nodo(s) recibidos pero ninguno descifrable con la clave del "
                "enlace (handle=%s)",
                len(nodes),
                link.handle,
            )
            raise SourceError(
                "La carpeta de Mega está vacía o no se pudo leer (¿la clave del enlace es correcta?)."
            )
        return root
