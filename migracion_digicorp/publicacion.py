"""Fases 4 y 5: validación final del staging y publicación atómica."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from . import reglas as R
from .archivos import eliminar_carpetas_vacias, largo, sha256_archivo
from .modelo import DEST_FACULTATIVA, DEST_FIJA, DEST_MOVIL, Entrada, Plan, TipoAccion
from .preparacion import Preparacion
from .validar_pdf import validar_pdf

log = logging.getLogger(__name__)


class ErrorPublicacion(Exception):
    pass


def _archivos_en(raiz: Path) -> dict[tuple[str, ...], Path]:
    res = {}
    for actual, dirs, files in os.walk(largo(raiz)):
        rel_dir = Path(actual).relative_to(Path(largo(raiz))).parts
        for f in files:
            res[tuple(rel_dir) + (f,)] = Path(actual) / f
    return res


def validar_final(plan: Plan, prep: Preparacion, entradas: list[Entrada], destino_final: Path) -> list[str]:
    """Devuelve la lista de fallos (vacía si el cliente puede publicarse)."""
    fallos: list[str] = []
    if not plan.cuit_valido:
        fallos.append("CUIT inválido")
    if plan.bloqueado:
        fallos.append("El plan tiene incidencias bloqueantes")
    if os.path.lexists(largo(destino_final)):
        fallos.append(f"El destino ya existe: {destino_final}")

    eliminar_carpetas_vacias(prep.staging)
    for actual, dirs, files in os.walk(largo(prep.staging)):
        if not dirs and not files:
            fallos.append(f"Carpeta vacía en el resultado: {actual}")

    fisicos = _archivos_en(prep.staging)
    esperados = {g.destino_rel: g for g in prep.generados}
    if not esperados:
        fallos.append("El resultado no contiene documentos")
    if set(fisicos) != set(esperados):
        fallos.append(f"El staging no coincide con el manifiesto (sobran {len(set(fisicos) - set(esperados))}, "
                      f"faltan {len(set(esperados) - set(fisicos))})")

    for rel, g in esperados.items():
        if len(rel) != 3:
            fallos.append(f"Profundidad inesperada: {'/'.join(rel)}")
            continue
        cat, sub, nombre = rel
        if cat == DEST_MOVIL:
            ok = sub == DEST_FACULTATIVA or R.RE_SDS.fullmatch(sub)
        elif cat == DEST_FIJA:
            ok = sub == DEST_FACULTATIVA or R.RE_CONTRATO.fullmatch(sub)
        else:
            ok = False
        if not ok:
            fallos.append(f"Ubicación no permitida: {'/'.join(rel)}")
        ruta = fisicos.get(rel)
        if ruta is None:
            continue
        if os.path.getsize(ruta) != g.tamano or sha256_archivo(ruta) != g.sha256:
            fallos.append(f"Integridad (tamaño/SHA-256) no coincide: {'/'.join(rel)}")
        if R.extension(nombre) == R.EXT_PDF:
            v = validar_pdf(ruta)
            if not v.ok:
                fallos.append(f"PDF inválido en resultado: {'/'.join(rel)}: {v.motivo}")

    # Correspondencia origen -> resultado: cada archivo de origen está migrado o excluido con motivo.
    origen_archivos = {e.rel for e in entradas if not e.es_dir}
    contabilizados = {g.origen_rel for g in prep.generados} | {o for o, _ in prep.excluidos}
    sin_destino = origen_archivos - contabilizados
    if sin_destino:
        fallos.append(f"{len(sin_destino)} archivo(s) de origen sin destino ni motivo de exclusión, p. ej. "
                      + "/".join(sorted(sin_destino)[0]))
    pendientes = [a for a in plan.acciones if a.tipo == TipoAccion.DESCARGAR
                  and a.origen_rel not in contabilizados]
    if pendientes:
        fallos.append(f"{len(pendientes)} descarga(s) pendiente(s)")
    return fallos


def publicar(prep: Preparacion, destino_final: Path, rename=os.rename) -> None:
    """Publica con un único rename (atómico en el mismo volumen). Nunca sobrescribe.

    Si la verificación posterior falla, retira el resultado del destino.
    """
    if os.path.lexists(largo(destino_final)):
        raise ErrorPublicacion(f"El destino ya existe: {destino_final}")
    os.makedirs(largo(destino_final.parent), exist_ok=True)
    try:
        rename(largo(prep.staging), largo(destino_final))
    except OSError as exc:
        raise ErrorPublicacion(f"No se pudo publicar: {exc}") from exc
    publicados = _archivos_en(destino_final)
    esperados = {g.destino_rel: g for g in prep.generados}
    problema = None
    if set(publicados) != set(esperados):
        problema = "el contenido publicado no coincide con el manifiesto"
    else:
        for rel, g in esperados.items():
            if os.path.getsize(publicados[rel]) != g.tamano:
                problema = f"tamaño distinto en {'/'.join(rel)}"
                break
    if problema:
        retirado = prep.staging.with_name(f"{prep.staging.name}.retirado-{int(time.time())}")
        os.rename(largo(destino_final), largo(retirado))
        raise ErrorPublicacion(f"Verificación posterior fallida ({problema}); resultado retirado a {retirado}")
