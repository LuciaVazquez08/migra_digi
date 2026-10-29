"""Estructuras de datos compartidas entre fases."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class EstadoCliente(str, Enum):
    APTO = "APTO"                          # diagnóstico sin bloqueos
    NO_APTO = "NO_APTO"                    # error técnico (descarga, conversión, publicación)
    REVISION_MANUAL = "REVISION_MANUAL"    # requiere intervención humana
    SIMULADO_OK = "SIMULADO_OK"            # pasó todas las validaciones en simulación
    PUBLICADO = "PUBLICADO"
    OMITIDO_YA_PUBLICADO = "OMITIDO_YA_PUBLICADO"
    INTERRUMPIDO = "INTERRUMPIDO"


class TipoAccion(str, Enum):
    COPIAR = "COPIAR"
    DESCARGAR = "DESCARGAR"
    CONVERTIR = "CONVERTIR"
    EXCLUIR_DUPLICADO = "EXCLUIR_DUPLICADO"
    IGNORAR = "IGNORAR"


# Nombres de las carpetas del destino
DEST_FIJA = "Documentacion Corpo Fija"
DEST_MOVIL = "Documentacion Movil"
DEST_FACULTATIVA = "Documentacion Facultativa"


@dataclass(frozen=True)
class Entrada:
    """Un elemento físico del origen, relativo a la carpeta del cliente."""
    rel: tuple[str, ...]
    es_dir: bool
    tamano: int
    ruta: Path

    @property
    def nombre(self) -> str:
        return self.rel[-1]


@dataclass
class Incidencia:
    cuit: str
    ruta: str
    tipo: str
    descripcion: str
    accion: str = ""
    resultado: str = ""
    requiere_manual: bool = True
    bloqueante: bool = True


@dataclass
class Accion:
    tipo: TipoAccion
    origen: Path
    origen_rel: tuple[str, ...]
    destino_rel: tuple[str, ...] | None = None  # relativo a la carpeta del cliente en destino
    url: str | None = None
    detalle: str = ""


@dataclass
class Plan:
    cuit: str  # nombre de la carpeta de origen (puede no ser un CUIT válido)
    ruta_origen: Path
    cuit_valido: bool = False
    acciones: list[Accion] = field(default_factory=list)
    incidencias: list[Incidencia] = field(default_factory=list)
    carpetas: int = 0
    archivos: int = 0

    def incidir(self, ruta, tipo: str, descripcion: str, *, manual: bool = True,
                bloqueante: bool = True, accion: str = "", resultado: str = "") -> None:
        self.incidencias.append(Incidencia(
            cuit=self.cuit, ruta=str(ruta), tipo=tipo, descripcion=descripcion,
            accion=accion, resultado=resultado, requiere_manual=manual, bloqueante=bloqueante,
        ))

    @property
    def bloqueado(self) -> bool:
        return any(i.bloqueante for i in self.incidencias)

    @property
    def estado(self) -> EstadoCliente:
        bloqueantes = [i for i in self.incidencias if i.bloqueante]
        if not bloqueantes:
            return EstadoCliente.APTO
        if any(i.requiere_manual for i in bloqueantes):
            return EstadoCliente.REVISION_MANUAL
        return EstadoCliente.NO_APTO

    def contar(self, tipo: TipoAccion) -> int:
        return sum(1 for a in self.acciones if a.tipo == tipo)


@dataclass
class ArchivoGenerado:
    """Un archivo materializado en staging (manifiesto)."""
    destino_rel: tuple[str, ...]
    sha256: str
    tamano: int
    origen_rel: tuple[str, ...]
    accion: str
    detalle: str = ""
