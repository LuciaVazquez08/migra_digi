"""Operaciones de sistema de archivos seguras (rutas largas, copias verificadas)."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

TAM_BLOQUE = 1024 * 1024


def largo(ruta: Path | str) -> str:
    """En Windows antepone ``\\\\?\\`` para soportar rutas de más de 260 caracteres."""
    s = str(ruta)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(largo(ruta), "rb") as fh:
        for bloque in iter(lambda: fh.read(TAM_BLOQUE), b""):
            h.update(bloque)
    return h.hexdigest()


def copiar_verificado(origen: Path, destino: Path) -> tuple[str, int]:
    """Copia en streaming y verifica que el hash del destino coincida con el del origen.

    Nunca sobrescribe: falla si el destino ya existe.
    """
    os.makedirs(largo(destino.parent), exist_ok=True)
    h = hashlib.sha256()
    total = 0
    with open(largo(origen), "rb") as src, open(largo(destino), "xb") as dst:
        for bloque in iter(lambda: src.read(TAM_BLOQUE), b""):
            h.update(bloque)
            dst.write(bloque)
            total += len(bloque)
        dst.flush()
        os.fsync(dst.fileno())
    try:
        shutil.copystat(largo(origen), largo(destino))
    except OSError:
        pass
    esperado = h.hexdigest()
    obtenido = sha256_archivo(destino)
    if obtenido != esperado:
        raise IOError(f"Hash distinto tras copiar {origen} -> {destino}")
    return esperado, total


def borrar_arbol(ruta: Path) -> None:
    """Borra un árbol de trabajo propio (staging). Nunca usar sobre origen o destino."""
    if os.path.lexists(largo(ruta)):
        shutil.rmtree(largo(ruta))


def eliminar_carpetas_vacias(raiz: Path) -> list[Path]:
    """Elimina carpetas vacías bajo ``raiz`` (de abajo hacia arriba). No borra ``raiz``."""
    eliminadas = []
    for actual, dirs, files in os.walk(largo(raiz), topdown=False):
        if Path(actual) == Path(largo(raiz)):
            continue
        if not os.listdir(actual):
            os.rmdir(actual)
            eliminadas.append(Path(actual))
    return eliminadas


def mismo_volumen(a: Path, b: Path) -> bool:
    """True si ``a`` y ``b`` (o su ancestro existente más cercano) están en el mismo volumen."""
    def existente(p: Path) -> Path:
        p = p.resolve()
        while not p.exists():
            p = p.parent
        return p
    return os.stat(existente(a)).st_dev == os.stat(existente(b)).st_dev
