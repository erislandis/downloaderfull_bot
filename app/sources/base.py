"""Contrato común de los sitios de origen (MediaFire, Mega, ...)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, ClassVar

import httpx


@dataclass(frozen=True)
class RemoteFile:
    """Archivo descargable. `ref` es opaco: solo lo interpreta su Source."""

    name: str
    size: int | None
    ref: str
    path: str = ""  # ruta relativa dentro de la carpeta de origen ("" = mismo que `name`)


@dataclass(frozen=True)
class BundleFile(RemoteFile):
    """Varios archivos que se entregan juntos en un único archivo comprimido.

    `size` es la suma de los tamaños sin comprimir (el ZIP final suele ser más pequeño);
    sirve para elegir la vía de subida y para avisar de los límites.
    """

    members: tuple[RemoteFile, ...] = ()


@dataclass
class FolderNode:
    """Nodo de un árbol de carpetas (para navegar antes de descargar)."""

    name: str
    node_id: str
    size: int | None = None
    is_dir: bool = False
    ref: str = ""  # cómo obtener el contenido (solo si `is_dir` es False)
    path: str = ""  # ruta relativa a la raíz del árbol
    children: list["FolderNode"] = field(default_factory=list)


@dataclass
class Inspection:
    """Resultado de analizar un enlace: archivos sueltos y/o un árbol de carpetas."""

    files: list[RemoteFile] = field(default_factory=list)
    tree: FolderNode | None = None


class Source(ABC):
    key: ClassVar[str]
    label: ClassVar[str]

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        """¿Este enlace pertenece a este sitio?"""

    @abstractmethod
    async def inspect(self, url: str) -> Inspection:
        """Analiza el enlace: archivos sueltos (`files`) y/o carpeta navegable (`tree`)."""

    @abstractmethod
    def stream(self, file: RemoteFile, offset: int = 0) -> AsyncIterator[bytes]:
        """Entrega el contenido (ya descifrado) desde `offset`.

        Si el origen no puede reanudar, debe lanzar ResumeNotSupported.
        """


# -- utilidades del árbol ----------------------------------------------------------------


def node_to_file(node: FolderNode) -> RemoteFile:
    """Convierte un nodo de archivo en el `RemoteFile` que lo descarga."""
    return RemoteFile(name=node.name, size=node.size, ref=node.ref, path=node.path)


def collect_files(node: FolderNode) -> list[FolderNode]:
    """Todos los archivos del nodo y sus subcarpetas, en orden."""
    out: list[FolderNode] = []
    for child in node.children:
        if child.is_dir:
            out.extend(collect_files(child))
        else:
            out.append(child)
    return out


def relative_path(root: FolderNode, path: str) -> str:
    """Ruta `path` expresada respecto a la carpeta `root`."""
    prefix = root.path
    if prefix and path.startswith(prefix + "/"):
        return path[len(prefix) + 1:]
    if prefix and path == prefix:
        return ""
    return path


def build_bundle(node: FolderNode, zip_name: str) -> BundleFile:
    """Empaqueta una carpeta (con sus subcarpetas) en un único archivo comprimido.

    Las rutas internas conservan la estructura: la raíz del ZIP es la carpeta elegida.
    """
    members: list[RemoteFile] = []
    for file_node in collect_files(node):
        rel = relative_path(node, file_node.path)
        path = f"{node.name}/{rel}" if rel else node.name
        members.append(RemoteFile(file_node.name, file_node.size, file_node.ref, path=path))
    total = sum(m.size or 0 for m in members)
    return BundleFile(name=zip_name, size=total, ref=f"bundle:{node.node_id}", members=tuple(members))


def parent_of(root: FolderNode, node: FolderNode) -> FolderNode | None:
    """Carpeta que contiene a ``node`` (``None`` si ``node`` es la raíz o no está)."""
    if root.node_id == node.node_id:
        return None
    for child in root.children:
        if child.node_id == node.node_id:
            return root
        if child.is_dir:
            found = parent_of(child, node)
            if found is not None:
                return found
    return None


def tree_from_files(files: list[RemoteFile]) -> FolderNode:
    """Árbol sintético con un solo nivel, para orígenes que no devuelven árbol."""
    root = FolderNode(name="Archivos", node_id="", is_dir=True)
    for i, remote in enumerate(files):
        root.children.append(
            FolderNode(
                name=remote.name,
                node_id=f"f{i}",
                size=remote.size,
                is_dir=False,
                ref=remote.ref,
                path=remote.path or remote.name,
            )
        )
    root.size = sum(f.size or 0 for f in root.children)
    return root
