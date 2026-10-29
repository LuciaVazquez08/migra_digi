"""Fase 3: preparación del cliente en staging (fuera del destino definitivo)."""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import reglas as R
from .archivos import borrar_arbol, copiar_verificado, largo, sha256_archivo
from .config import Config
from .conversion import convertir_a_pdf
from .descarga import Descargador, ResultadoDescarga
from .modelo import Accion, ArchivoGenerado, Plan, TipoAccion
from .validar_pdf import validar_pdf

log = logging.getLogger(__name__)


class Interrumpido(Exception):
    """Se solicitó detener el proceso; el cliente en curso se descarta."""


@dataclass
class Preparacion:
    staging: Path
    generados: list[ArchivoGenerado] = field(default_factory=list)
    excluidos: list[tuple[tuple[str, ...], str]] = field(default_factory=list)  # (origen_rel, motivo)
    descargados: int = 0
    pdfs_validados: int = 0
    convertidos: int = 0

    def guardar_manifiesto(self, ruta: Path, plan: Plan, extra: dict) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        datos = {
            "cuit": plan.cuit,
            "origen": str(plan.ruta_origen),
            **extra,
            "archivos": [asdict(g) for g in self.generados],
            "excluidos": [{"origen_rel": list(o), "motivo": m} for o, m in self.excluidos],
        }
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, ruta)


def _clave_dir(rel: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p.casefold() for p in rel)


def preparar(plan: Plan, cfg: Config, descargador: Descargador, soffice: str | None,
             detener: Callable[[], bool], reintentar_fallidas: bool = False) -> Preparacion | None:
    """Arma la estructura final en ``staging/<cuit>``. Devuelve None si el cliente queda bloqueado.

    Ante bloqueo o interrupción elimina el staging del cliente.
    """
    staging = cfg.dir_staging / plan.cuit
    tmpdir = cfg.dir_staging / f"{plan.cuit}.tmp"
    borrar_arbol(staging)
    borrar_arbol(tmpdir)
    staging.mkdir(parents=True)
    tmpdir.mkdir(parents=True)
    prep = Preparacion(staging)
    try:
        resultados = _descargar_todo(plan, descargador, detener, reintentar_fallidas)
        if plan.bloqueado:
            borrar_arbol(staging)
            return None

        usados: dict[tuple[str, ...], dict[str, str]] = {}   # dir -> {nombre_casefold: sha}
        hashes: dict[tuple[str, ...], dict[str, str]] = {}   # dir -> {sha: nombre}
        materializables = [a for a in plan.acciones if a.destino_rel]
        materializables.sort(key=lambda a: (_clave_dir(a.destino_rel[:-1]), _clave_dir(a.origen_rel)))
        for i, accion in enumerate(materializables):
            if detener():
                raise Interrumpido()
            tmp = tmpdir / f"{i:06d}"
            nombre, detalle = _materializar(plan, accion, tmp, resultados, cfg, soffice, prep)
            if nombre is None:
                continue
            _ubicar(plan, accion, tmp, nombre, detalle, staging, usados, hashes, prep)

        for a in plan.acciones:
            if a.tipo in (TipoAccion.EXCLUIR_DUPLICADO, TipoAccion.IGNORAR):
                prep.excluidos.append((a.origen_rel, f"{a.tipo.value}: {a.detalle}"))
        if plan.bloqueado:
            borrar_arbol(staging)
            return None
        return prep
    except BaseException:
        borrar_arbol(staging)
        raise
    finally:
        borrar_arbol(tmpdir)


def _descargar_todo(plan: Plan, descargador: Descargador, detener: Callable[[], bool],
                    reintentar: bool) -> dict[str, ResultadoDescarga]:
    urls = sorted({a.url for a in plan.acciones if a.tipo == TipoAccion.DESCARGAR and a.url})
    resultados: dict[str, ResultadoDescarga] = {}
    if not urls:
        return resultados
    with ThreadPoolExecutor(max_workers=descargador.cfg.descargas_concurrentes) as ex:
        futuros = {}
        for url in urls:
            if detener():
                break
            futuros[url] = ex.submit(descargador.obtener, url, reintentar)
        for url, fut in futuros.items():
            resultados[url] = fut.result()
    if detener():
        raise Interrumpido()
    for a in plan.acciones:
        if a.tipo != TipoAccion.DESCARGAR:
            continue
        r = resultados[a.url]
        if not r.ok:
            plan.incidir(a.origen, "DESCARGA_FALLIDA", r.error, manual=r.permanente,
                         accion="Descargar y validar", resultado="Fallido")
    return resultados


def _materializar(plan: Plan, a: Accion, tmp: Path, resultados: dict[str, ResultadoDescarga],
                  cfg: Config, soffice: str | None, prep: Preparacion) -> tuple[str | None, str]:
    """Escribe el archivo final en ``tmp``. Devuelve (nombre_deseado, detalle) o (None, '') si falla."""
    nombre = a.destino_rel[-1]
    if a.tipo == TipoAccion.DESCARGAR:
        r = resultados[a.url]
        copiar_verificado(r.ruta, tmp)
        prep.descargados += 1
        prep.pdfs_validados += 1
        detalle = "Descargado" + (" (desde caché)" if r.desde_cache else "")
        if r.convertido_desde:
            detalle += f"; el servidor devolvió {r.convertido_desde}, convertido a PDF"
        return nombre, detalle
    if a.tipo == TipoAccion.COPIAR:
        if R.extension(a.origen.name) == R.EXT_PDF:
            v = validar_pdf(a.origen)
            if not v.ok:
                plan.incidir(a.origen, "PDF_CORRUPTO", v.motivo, accion="Validar PDF de origen", resultado="Inválido")
                return None, ""
            prep.pdfs_validados += 1
        copiar_verificado(a.origen, tmp)
        return nombre, a.detalle or "Copiado"
    if a.tipo == TipoAccion.CONVERTIR:
        try:
            convertir_a_pdf(a.origen, tmp, soffice, cfg.timeout_conversion)
            prep.convertidos += 1
            prep.pdfs_validados += 1
            return nombre, f"Convertido desde {R.extension(a.origen.name)}"
        except Exception as exc:
            if os.path.exists(largo(tmp)):
                os.remove(largo(tmp))
            plan.incidir(a.origen, "CONVERSION_NO_POSIBLE", str(exc), manual=False, bloqueante=False,
                         accion="Convertir a PDF", resultado="Se migra el archivo original")
            copiar_verificado(a.origen, tmp)
            return a.origen.name, "Conversión no posible; se migra el original"
    raise ValueError(f"Acción no materializable: {a.tipo}")


def _ubicar(plan: Plan, a: Accion, tmp: Path, nombre: str, detalle: str, staging: Path,
            usados: dict, hashes: dict, prep: Preparacion) -> None:
    """Aplica deduplicación por contenido y resolución de colisiones; mueve a su lugar final."""
    dir_rel = a.destino_rel[:-1]
    clave = _clave_dir(dir_rel)
    sha = sha256_archivo(tmp)
    hashes.setdefault(clave, {})
    usados.setdefault(clave, {})
    if sha in hashes[clave]:
        conservado = hashes[clave][sha]
        os.remove(largo(tmp))
        motivo = f"Contenido idéntico (SHA-256) a '{conservado}' en la misma carpeta"
        prep.excluidos.append((a.origen_rel, f"DUPLICADO_CONTENIDO: {motivo}"))
        plan.incidir(a.origen, "DUPLICADO_EXCLUIDO", motivo, manual=False, bloqueante=False,
                     accion="Excluir duplicado", resultado=f"Se conserva '{conservado}'")
        return
    final = nombre
    n = 2
    while final.casefold() in usados[clave]:
        final = R.nombre_con_sufijo(nombre, n)
        n += 1
    if final != nombre:
        plan.incidir(a.origen, "RENOMBRADO_POR_COLISION", f"'{nombre}' ya existe en la carpeta; se guarda como '{final}'",
                     manual=False, bloqueante=False, accion="Renombrar", resultado=final)
        detalle += f"; renombrado por colisión desde '{nombre}'"
    destino = staging.joinpath(*dir_rel, final)
    os.makedirs(largo(destino.parent), exist_ok=True)
    os.replace(largo(tmp), largo(destino))
    usados[clave][final.casefold()] = sha
    hashes[clave][sha] = final
    prep.generados.append(ArchivoGenerado(dir_rel + (final,), sha, destino.stat().st_size, a.origen_rel,
                                          a.tipo.value, detalle))
