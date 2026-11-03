"""Recuperación de documentos de SDS problemáticas a partir de archivos locales identificados por CASE_ID.

Flujo (nada de esto migra; solo copia, identifica y valida):

1. ``copiar_carpeta``: copia una carpeta (p. ej. la biblioteca de OneDrive/SharePoint en C:) a E:,
   verificando cada archivo por SHA-256. Es reanudable. Los archivos que estaban "solo en la nube"
   se descargan a C: para copiarlos y luego se devuelven a "solo en la nube", para no llenar C:.
2. ``sds_problematicas``: lista las SDS cuyos enlaces están postergados, cortados o no se pudieron
   descargar, y genera la consulta SQL para obtener sus CASE_ID.
3. ``buscar_casos``: con el resultado de la consulta (SUBJECT, CASE_ID) busca en la copia los archivos
   cuyo nombre contiene cada CASE_ID, valida que abran y resume cuántas SDS quedan cubiertas.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import subprocess
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import Workbook, load_workbook
from PIL import Image

from . import reglas as R
from .archivos import copiar_verificado, largo
from .config import Config
from .descarga import clave_url, leer_url
from .diagnostico import diagnosticar
from .estado import Estado
from .inventario import escanear_cliente, leer_referencia
from .modelo import TipoAccion
from .validar_pdf import validar_pdf

log = logging.getLogger(__name__)

# Atributos de Windows de archivos "solo en la nube" (OneDrive Files On-Demand)
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_PINNED = 0x80000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# ---------------------------------------------------------------------------
# 1. Copia de la carpeta a E:
# ---------------------------------------------------------------------------

def _sin_prefijo(ruta) -> str:
    """Quita el prefijo de rutas largas de Windows (\\\\?\\) para mostrar o pasar a otros programas."""
    s = str(ruta)
    return s[4:] if s.startswith("\\\\?\\") else s


def solo_en_la_nube(ruta: Path) -> bool:
    atributos = getattr(os.stat(largo(ruta)), "st_file_attributes", 0)
    return bool(atributos & (_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                             | _FILE_ATTRIBUTE_OFFLINE)) and not atributos & _FILE_ATTRIBUTE_PINNED


def devolver_a_la_nube(ruta: Path) -> bool:
    """Marca el archivo como "liberar espacio" (solo en la nube). No borra nada de OneDrive."""
    if os.name != "nt":
        return False
    r = subprocess.run(["attrib", "+U", "-P", _sin_prefijo(ruta)], capture_output=True, text=True)
    return r.returncode == 0


@dataclass
class ResultadoCopia:
    copiados: int = 0
    ya_copiados: int = 0
    sin_espacio: int = 0
    errores: int = 0
    bytes_copiados: int = 0
    detenido: bool = False


def _recorrer(origen: Path):
    for actual, dirs, files in os.walk(largo(origen)):
        dirs.sort()
        for f in sorted(files):
            p = Path(actual) / f
            yield p, p.relative_to(Path(largo(origen)))


def medir_carpeta(origen: Path, filtro=None) -> tuple[int, int, int]:
    """(archivos, bytes, bytes solo en la nube) sin descargar nada."""
    n = total = nube = 0
    for p, _ in _recorrer(origen):
        if filtro is not None and not filtro(p.name):
            continue
        st = os.stat(p)
        n += 1
        total += st.st_size
        if solo_en_la_nube(p):
            nube += st.st_size
    return n, total, nube


def destino_espejo(origen: Path, unidad: Path) -> Path:
    """Carpeta con el mismo nombre que el origen en otra unidad: C:\\...\\AMX Argentina S.A -> E:\\AMX Argentina S.A."""
    return unidad / origen.name


def copiar_carpeta(origen: Path, destino: Path, manifiesto: Path, reserva_c: int, detener=lambda: False,
                   filtro=None) -> ResultadoCopia:
    """Copia ``origen`` a ``destino`` conservando la estructura. Nunca modifica ni borra el origen
    (solo restaura "solo en la nube" en los archivos que ya lo estaban). Reanudable."""
    res = ResultadoCopia()
    manifiesto.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not manifiesto.exists()
    with open(manifiesto, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        if nuevo:
            w.writerow(["fecha", "ruta_relativa", "bytes", "sha256", "resultado", "detalle"])
        for p, rel in _recorrer(origen):
            if detener():
                res.detenido = True
                break
            if filtro is not None and not filtro(p.name):
                continue
            dst = destino / rel
            tam = os.stat(p).st_size
            if dst.exists() and dst.stat().st_size == tam:
                res.ya_copiados += 1
                continue
            nube = solo_en_la_nube(p)
            if nube and shutil.disk_usage(origen.anchor or str(origen)).free < tam + reserva_c:
                res.sin_espacio += 1
                w.writerow([datetime.now().isoformat(timespec="seconds"), str(rel), tam, "", "SIN_ESPACIO_EN_C",
                            "No hay espacio en C: para traer el archivo de la nube"])
                continue
            tmp = dst.with_name(dst.name + ".part")
            try:
                os.makedirs(largo(dst.parent), exist_ok=True)
                if os.path.exists(largo(tmp)):
                    os.remove(largo(tmp))
                sha, _ = copiar_verificado(p, tmp)
                os.replace(largo(tmp), largo(dst))
                detalle = ""
                if nube:
                    detalle = "devuelto a solo en la nube" if devolver_a_la_nube(p) else \
                        "no se pudo devolver a solo en la nube"
                res.copiados += 1
                res.bytes_copiados += tam
                w.writerow([datetime.now().isoformat(timespec="seconds"), str(rel), tam, sha, "COPIADO", detalle])
            except OSError as exc:
                res.errores += 1
                if os.path.exists(largo(tmp)):
                    os.remove(largo(tmp))
                w.writerow([datetime.now().isoformat(timespec="seconds"), str(rel), tam, "", "ERROR", str(exc)[:300]])
            if (res.copiados + res.errores + res.sin_espacio) % 200 == 0:
                fh.flush()
                log.info("Copia: %d copiados, %d ya estaban, %d sin espacio, %d errores", res.copiados,
                         res.ya_copiados, res.sin_espacio, res.errores)
    return res


# ---------------------------------------------------------------------------
# 2. SDS problemáticas
# ---------------------------------------------------------------------------

@dataclass
class SdsProblema:
    cuit: str
    sds: str
    archivo: str
    motivo: str
    detalle: str = ""


def sds_problematicas(cfg: Config, estados=("POSTERGADO", "REVISION_MANUAL", "NO_APTO")) -> list[SdsProblema]:
    """SDS de clientes no migrados con enlaces postergados, cortados/dañados o con descarga fallida."""
    est = Estado(cfg.archivo_estado)
    try:
        cuits = [c for (c,) in est._exec(
            f"SELECT cuit FROM clientes WHERE estado IN ({','.join('?' * len(estados))}) ORDER BY cuit", estados)]
        ref = leer_referencia(cfg.excel_referencia, cfg.hoja_arbol)
        res: list[SdsProblema] = []
        for n, cuit in enumerate(cuits, 1):
            ruta = cfg.origen / cuit
            if not ruta.is_dir():
                continue
            entradas = escanear_cliente(ruta)
            plan = diagnosticar(cuit, ruta, entradas, ref, cfg, None)
            vistos = set()
            for a in plan.acciones:
                if a.tipo != TipoAccion.DESCARGAR or not _es_sds(a.origen_rel):
                    continue
                host = (urlsplit(a.url).hostname or "").lower()
                if host in cfg.dominios_postergados:
                    res.append(SdsProblema(cuit, a.origen_rel[1], a.origen_rel[-1], "POSTERGADO", host))
                    vistos.add(a.origen_rel)
                    continue
                d = est.descarga(clave_url(a.url))
                if d and d["estado"] == "ERROR":
                    res.append(SdsProblema(cuit, a.origen_rel[1], a.origen_rel[-1], "DESCARGA_FALLIDA",
                                           (d["error"] or "")[:200]))
                    vistos.add(a.origen_rel)
            for i in plan.incidencias:
                if i.tipo != "URL_INVALIDA":
                    continue
                rel = Path(i.ruta).relative_to(ruta).parts if Path(i.ruta).is_relative_to(ruta) else ()
                if _es_sds(rel) and rel not in vistos:
                    res.append(SdsProblema(cuit, rel[1], rel[-1], "URL_CORTADA_O_INVALIDA", i.descripcion[:200]))
            if n % 200 == 0:
                log.info("SDS problemáticas: %d/%d clientes revisados", n, len(cuits))
        return res
    finally:
        est.cerrar()


def _es_sds(rel: tuple[str, ...]) -> bool:
    return len(rel) == 3 and R.categoria_cliente(rel[0])[0] == "movil" and bool(R.RE_SDS.fullmatch(rel[1]))


def escribir_sds_y_sql(problemas: list[SdsProblema], carpeta: Path, por_consulta: int = 300) -> tuple[Path, Path]:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_csv = carpeta / "sds_problematicas.csv"
    with open(ruta_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["CUIT", "SDS", "ARCHIVO", "MOTIVO", "DETALLE"])
        for p in problemas:
            w.writerow([p.cuit, p.sds, p.archivo, p.motivo, p.detalle])
    sds = sorted({p.sds.upper() for p in problemas})
    ruta_sql = carpeta / "consulta_case_id.sql"
    partes = []
    for i in range(0, len(sds), por_consulta):
        lote = sds[i:i + por_consulta]
        condiciones = "\n   OR ".join(f"UPPER(SUBJECT) LIKE '%{s}%'" for s in lote)
        partes.append(f"-- Lote {i // por_consulta + 1} ({len(lote)} SDS)\n"
                      f"SELECT SUBJECT, CASE_ID\nFROM RACING.CD_CASE_DETAILS_DUMP\nWHERE {condiciones};\n")
    ruta_sql.write_text("\n".join(partes), encoding="utf-8")
    return ruta_csv, ruta_sql


# ---------------------------------------------------------------------------
# 3. Búsqueda por CASE_ID y validación
# ---------------------------------------------------------------------------

def leer_casos(ruta: Path) -> list[tuple[str, str]]:
    """Lee el resultado de la consulta (columnas SUBJECT y CASE_ID) desde .csv o .xlsx."""
    filas: list[list] = []
    if ruta.suffix.lower() in (".xlsx", ".xlsm"):
        wb = load_workbook(ruta, read_only=True, data_only=True)
        filas = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
    else:
        crudo = ruta.read_bytes()
        for cod in ("utf-8-sig", "cp1252"):
            try:
                texto = crudo.decode(cod)
                break
            except UnicodeDecodeError:
                continue
        dialecto = csv.Sniffer().sniff(texto[:4096], delimiters=",;\t|")
        filas = list(csv.reader(texto.splitlines(), dialecto))
    if not filas:
        return []
    cab = [str(c or "").strip().upper() for c in filas[0]]
    if "SUBJECT" not in cab or "CASE_ID" not in cab:
        raise ValueError(f"{ruta} debe tener las columnas SUBJECT y CASE_ID (tiene: {cab})")
    i_s, i_c = cab.index("SUBJECT"), cab.index("CASE_ID")
    res = []
    for f in filas[1:]:
        if len(f) <= max(i_s, i_c) or f[i_c] in (None, ""):
            continue
        caso = str(f[i_c]).strip()
        if caso.endswith(".0") and caso[:-2].isdigit():  # Excel numérico
            caso = caso[:-2]
        res.append((str(f[i_s] or ""), caso))
    return res


def _patron_casos(casos: set[str]) -> re.Pattern:
    def pieza(c: str) -> str:
        borde = r"0-9" if c.isdigit() else r"0-9a-z"
        return rf"(?<![{borde}]){re.escape(c.casefold())}(?![{borde}])"
    return re.compile("|".join(pieza(c) for c in sorted(casos, key=len, reverse=True)))


def validar_archivo(ruta: Path) -> tuple[str, str]:
    """(resultado, detalle). resultado: VALIDO, CORRUPTO, ENLACE, NO_VERIFICABLE."""
    ext = R.extension(ruta.name)
    try:
        if os.path.getsize(largo(ruta)) == 0:
            return "CORRUPTO", "Archivo vacío"
        if ext == R.EXT_PDF:
            v = validar_pdf(ruta)
            return ("VALIDO", f"{v.paginas} páginas") if v.ok else ("CORRUPTO", v.motivo)
        if ext in R.EXT_IMAGEN:
            with Image.open(largo(ruta)) as im:
                im.verify()
            with Image.open(largo(ruta)) as im:
                im.load()
            return "VALIDO", "Imagen legible"
        if ext in (".docx", ".xlsx", ".pptx", ".zip", ".odt", ".ods"):
            with zipfile.ZipFile(largo(ruta)) as z:
                malo = z.testzip()
            return ("CORRUPTO", f"Entrada dañada: {malo}") if malo else ("VALIDO", "Contenedor íntegro")
        if ext in (".msg", ".doc", ".xls", ".ppt"):
            with open(largo(ruta), "rb") as fh:
                ok = fh.read(8) == _OLE
            return ("VALIDO", "Estructura OLE correcta") if ok else ("CORRUPTO", "Cabecera OLE inválida")
        if ext == R.EXT_URL:
            u = leer_url(ruta)
            return "ENLACE", f"Acceso directo a {urlsplit(u).hostname if u else '(sin URL)'}: no es un documento"
        return "NO_VERIFICABLE", f"Formato {ext or '(sin extensión)'} sin verificación automática"
    except Exception as exc:
        return "CORRUPTO", f"{type(exc).__name__}: {str(exc)[:150]}"


@dataclass
class Busqueda:
    sds_casos: dict[str, set[str]] = field(default_factory=dict)          # SDS -> CASE_ID
    archivos_caso: dict[str, list[tuple[Path, str, str]]] = field(default_factory=dict)  # caso -> [(ruta, res, det)]


def filtro_por_casos(problemas: list[SdsProblema], casos: list[tuple[str, str]]):
    """Función nombre_de_archivo -> bool que acepta solo archivos de los CASE_ID de las SDS problemáticas."""
    sds_objetivo = {p.sds.upper() for p in problemas}
    ids = {c for subject, c in casos for s in sds_objetivo if s in subject.upper()}
    if not ids:
        return lambda nombre: False, 0
    patron = _patron_casos(ids)
    return (lambda nombre: patron.search(nombre.casefold()) is not None), len(ids)


def buscar_casos(problemas: list[SdsProblema], casos: list[tuple[str, str]], carpeta: Path) -> Busqueda:
    sds_objetivo = {p.sds.upper() for p in problemas}
    b = Busqueda()
    for subject, caso in casos:
        asunto = subject.upper()
        for s in sds_objetivo:
            if s in asunto:
                b.sds_casos.setdefault(s, set()).add(caso)
    todos = {c for cs in b.sds_casos.values() for c in cs}
    if not todos:
        return b
    patron = _patron_casos(todos)
    por_casefold = {c.casefold(): c for c in todos}
    for p, _ in _recorrer(carpeta):
        if p.name.endswith(".part"):
            continue
        encontrados = {m.group(0) for m in patron.finditer(p.name.casefold())}
        if encontrados:
            resultado, detalle = validar_archivo(p)
            for e in encontrados:
                b.archivos_caso.setdefault(por_casefold[e], []).append((p, resultado, detalle))
    return b


def escribir_reporte(problemas: list[SdsProblema], b: Busqueda, ruta: Path) -> dict[str, int]:
    por_sds: dict[tuple[str, str], list[SdsProblema]] = defaultdict(list)
    for p in problemas:
        por_sds[(p.cuit, p.sds.upper())].append(p)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Resumen por SDS")
    ws.append(["CUIT", "SDS", "Motivos", "Archivos con problema", "CASE_ID", "Archivos encontrados",
               "Válidos", "Corruptos", "Otros", "Resultado"])
    totales: dict[str, int] = defaultdict(int)
    for (cuit, sds), lista in sorted(por_sds.items()):
        casos = sorted(b.sds_casos.get(sds, set()))
        archivos = [a for c in casos for a in b.archivos_caso.get(c, [])]
        validos = sum(1 for _, r, _ in archivos if r == "VALIDO")
        corruptos = sum(1 for _, r, _ in archivos if r == "CORRUPTO")
        if not casos:
            resultado = "SIN_CASE_ID"
        elif not archivos:
            resultado = "SIN_ARCHIVOS"
        elif validos:
            resultado = "CON_ARCHIVOS_VALIDOS"
        elif corruptos:
            resultado = "SOLO_CORRUPTOS"
        else:
            resultado = "SOLO_NO_VERIFICABLES"
        totales[resultado] += 1
        ws.append([cuit, sds, ", ".join(sorted({p.motivo for p in lista})), ", ".join(p.archivo for p in lista),
                   ", ".join(casos), len(archivos), validos, corruptos, len(archivos) - validos - corruptos,
                   resultado])
    wa = wb.create_sheet("Archivos encontrados")
    wa.append(["CASE_ID", "SDS", "Archivo", "Resultado", "Detalle"])
    sds_de_caso = defaultdict(set)
    for s, cs in b.sds_casos.items():
        for c in cs:
            sds_de_caso[c].add(s)
    for caso, lista in sorted(b.archivos_caso.items()):
        for p, r, d in lista:
            wa.append([caso, ", ".join(sorted(sds_de_caso[caso])), _sin_prefijo(p), r, d])
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta)
    return dict(totales)
