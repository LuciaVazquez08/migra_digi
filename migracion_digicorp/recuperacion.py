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
    motivo_detencion: str = ""


def _recorrer(origen: Path):
    for actual, dirs, files in os.walk(largo(origen)):
        dirs.sort()
        for f in sorted(files):
            p = Path(actual) / f
            yield p, p.relative_to(Path(largo(origen)))


def medir_carpeta(origen: Path, filtro=None) -> tuple[int, int, int]:
    """(archivos, bytes, bytes solo en la nube) sin descargar nada."""
    n = total = nube = 0
    for i, (p, _) in enumerate(_recorrer(origen), 1):
        if i % 20000 == 0:
            log.info("Medición: %d archivos revisados", i)
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


ERRORES_SEGUIDOS_MAX = 50


def copiar_carpeta(origen: Path, destino: Path, manifiesto: Path, reserva_c: int, detener=lambda: False,
                   filtro=None, errores_seguidos_max: int = ERRORES_SEGUIDOS_MAX) -> ResultadoCopia:
    """Copia ``origen`` a ``destino`` conservando la estructura. Nunca modifica ni borra el origen
    (solo restaura "solo en la nube" en los archivos que ya lo estaban). Reanudable."""
    res = ResultadoCopia()
    seguidos = 0
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
                seguidos = 0
                w.writerow([datetime.now().isoformat(timespec="seconds"), str(rel), tam, sha, "COPIADO", detalle])
            except OSError as exc:
                res.errores += 1
                seguidos += 1
                if os.path.exists(largo(tmp)):
                    os.remove(largo(tmp))
                w.writerow([datetime.now().isoformat(timespec="seconds"), str(rel), tam, "", "ERROR", str(exc)[:300]])
                if seguidos >= errores_seguidos_max:
                    res.detenido = True
                    res.motivo_detencion = (f"{seguidos} errores seguidos; el último: {str(exc)[:300]}. "
                                            "Probablemente OneDrive está detenido, sin conexión o pausado.")
                    log.error("Copia detenida: %s", res.motivo_detencion)
                    break
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
    # Alternativa en una sola consulta (Oracle): trae todos los casos cuyo asunto contiene algo con
    # formato de SDS; el filtro por las SDS problemáticas se hace después, en buscar-casos.
    (carpeta / "consulta_case_id_unica.sql").write_text(
        "-- Oracle: una sola consulta para todas las SDS (el programa filtra después las que interesan)\n"
        "SELECT SUBJECT, CASE_ID\nFROM RACING.CD_CASE_DETAILS_DUMP\n"
        "WHERE REGEXP_LIKE(SUBJECT, '[0-9]{8}[A-Za-z]{3}');\n", encoding="utf-8")
    return ruta_csv, ruta_sql


# ---------------------------------------------------------------------------
# 3. Búsqueda por CASE_ID y validación
# ---------------------------------------------------------------------------

def leer_casos(ruta: Path, quitar_inicio: int = 0) -> list[tuple[str, str]]:
    """Lee el resultado de la consulta (columnas SUBJECT y CASE_ID) desde .csv o .xlsx.

    ``quitar_inicio``: caracteres que se quitan al principio de cada CASE_ID para que coincida con cómo
    aparece en los nombres de archivo (en la base tiene un dígito de más al comienzo)."""
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
        if quitar_inicio:
            if len(caso) <= quitar_inicio:
                continue
            caso = caso[quitar_inicio:]
        res.append((str(f[i_s] or ""), caso))
    return res


_RE_SDS_ASUNTO = re.compile(r"(?=(\d{8}[A-Z]{3}))")
_RE_DIGITOS = re.compile(r"\d+")
_RE_ALFANUM = re.compile(r"[0-9a-z]+")


def _sds_en_asunto(subject: str) -> set[str]:
    """Todas las secuencias con formato de SDS (8 dígitos + 3 letras) del asunto, en mayúsculas."""
    return set(_RE_SDS_ASUNTO.findall(subject.upper()))


def relacionar_sds_casos(sds_objetivo: set[str], casos: list[tuple[str, str]]) -> dict[str, set[str]]:
    """SDS -> CASE_ID cuyo asunto la contiene. Lineal en la cantidad de casos."""
    res: dict[str, set[str]] = {}
    for subject, caso in casos:
        for s in _sds_en_asunto(subject) & sds_objetivo:
            res.setdefault(s, set()).add(caso)
    return res


class _IndiceCasos:
    """Encuentra qué CASE_ID aparecen en un nombre de archivo, como número/código completo.

    Un CASE_ID numérico coincide con una secuencia de dígitos completa (``00123456`` no coincide con
    ``001234567``); uno alfanumérico, con una secuencia alfanumérica completa. Sin distinguir mayúsculas.
    """

    def __init__(self, casos: set[str]):
        self.original = {c.casefold(): c for c in casos}
        self.numericos = {k for k in self.original if k.isdigit()}
        self.alfanum = set(self.original) - self.numericos

    def buscar(self, nombre: str) -> set[str]:
        n = nombre.casefold()
        encontrados = {t for t in _RE_DIGITOS.findall(n) if t in self.numericos}
        if self.alfanum:
            encontrados |= {t for t in _RE_ALFANUM.findall(n) if t in self.alfanum}
        return {self.original[t] for t in encontrados}


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
    ids = {c for cs in relacionar_sds_casos(sds_objetivo, casos).values() for c in cs}
    if not ids:
        return lambda nombre: False, 0
    indice = _IndiceCasos(ids)
    return (lambda nombre: bool(indice.buscar(nombre))), len(ids)


def muestra_coincidencias(origen: Path, problemas: list[SdsProblema], casos: list[tuple[str, str]], n: int):
    """Primeros ``n`` archivos de ``origen`` que se copiarían: [(ruta, {CASE_ID}, {SDS}, [(cuit, sds)])].

    Solo lee nombres: no descarga ni copia nada."""
    sds_objetivo = {p.sds.upper() for p in problemas}
    sds_casos = relacionar_sds_casos(sds_objetivo, casos)
    sds_de_caso: dict[str, set[str]] = defaultdict(set)
    for s_, cs in sds_casos.items():
        for c in cs:
            sds_de_caso[c].add(s_)
    clientes_de_sds: dict[str, set[str]] = defaultdict(set)
    for p in problemas:
        clientes_de_sds[p.sds.upper()].add(p.cuit)
    indice = _IndiceCasos(set(sds_de_caso))
    res = []
    for i, (ruta, _) in enumerate(_recorrer(origen), 1):
        if i % 20000 == 0:
            log.info("Muestra: %d archivos revisados, %d coincidencias", i, len(res))
        encontrados = indice.buscar(ruta.name)
        if encontrados:
            sds = {s_ for c in encontrados for s_ in sds_de_caso[c]}
            res.append((ruta, encontrados, sds, sorted({(cu, s_) for s_ in sds for cu in clientes_de_sds[s_]})))
            if len(res) >= n:
                break
    return res


def ejemplos(problemas: list[SdsProblema], casos_crudos: list[tuple[str, str]], n: int, quitar_inicio: int,
             origen_raw: Path, preferir=("URL_CORTADA_O_INVALIDA", "DESCARGA_FALLIDA", "POSTERGADO")):
    """Hasta ``n`` SDS problemáticas que tienen CASE_ID, priorizando enlaces cortados/fallidos.

    Devuelve [{cuit, sds, motivos, archivos, casos: [(subject, case_id_base, case_id_buscado)]}]."""
    por_sds: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for subject, caso in casos_crudos:
        for s_ in _sds_en_asunto(subject):
            por_sds[s_].append((subject, caso))
    agrupado: dict[tuple[str, str], list[SdsProblema]] = defaultdict(list)
    for p in problemas:
        agrupado[(p.cuit, p.sds.upper())].append(p)
    orden = sorted(agrupado.items(), key=lambda kv: (min(preferir.index(p.motivo) if p.motivo in preferir
                                                          else len(preferir) for p in kv[1]), kv[0]))
    res = []
    for (cuit, sds), lista in orden:
        if sds not in por_sds:
            continue
        res.append({
            "cuit": cuit, "sds": sds,
            "motivos": sorted({p.motivo for p in lista}),
            "archivos": [str(origen_raw / cuit / "Documentación Móvil" / sds / p.archivo) for p in lista],
            "casos": [(sub, c, c[quitar_inicio:] if quitar_inicio else c) for sub, c in por_sds[sds][:5]],
        })
        if len(res) >= n:
            break
    return res


def buscar_casos(problemas: list[SdsProblema], casos: list[tuple[str, str]], carpeta: Path) -> Busqueda:
    sds_objetivo = {p.sds.upper() for p in problemas}
    b = Busqueda()
    b.sds_casos = relacionar_sds_casos(sds_objetivo, casos)
    todos = {c for cs in b.sds_casos.values() for c in cs}
    if not todos:
        return b
    indice = _IndiceCasos(todos)
    for n, (p, _) in enumerate(_recorrer(carpeta), 1):
        if n % 20000 == 0:
            log.info("Búsqueda: %d archivos revisados", n)
        if p.name.endswith(".part"):
            continue
        encontrados = indice.buscar(p.name)
        if encontrados:
            resultado, detalle = validar_archivo(p)
            for caso in encontrados:
                b.archivos_caso.setdefault(caso, []).append((p, resultado, detalle))
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


# ---------------------------------------------------------------------------
# 2b. Consulta Oracle directa
# ---------------------------------------------------------------------------

CONSULTA_CASOS = ("SELECT SUBJECT, CASE_ID FROM RACING.CD_CASE_DETAILS_DUMP "
                  "WHERE REGEXP_LIKE(SUBJECT, '[0-9]{8}[A-Za-z]{3}')")


def leer_password(ruta: Path) -> str:
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe el archivo de contraseña: {ruta}")
    primer_renglon = ruta.read_text(encoding="utf-8-sig").splitlines()[0].strip() if ruta.stat().st_size else ""
    if not primer_renglon:
        raise ValueError(f"El archivo de contraseña está vacío: {ruta}")
    return primer_renglon


def consultar_casos(cfg: Config, destino: Path, conectar=None, por_lote: int = 10_000) -> int:
    """Ejecuta la consulta en Oracle y guarda SUBJECT;CASE_ID en ``destino`` (CSV) en streaming.

    ``conectar`` permite inyectar la conexión en pruebas. Devuelve la cantidad de filas.
    """
    if conectar is None:
        import oracledb
        oracledb.defaults.fetch_lobs = False  # SUBJECT como texto aunque sea CLOB

        def conectar():
            return oracledb.connect(user=cfg.oracle_usuario, password=leer_password(cfg.oracle_archivo_password),
                                    dsn=cfg.oracle_dsn, expire_time=cfg.oracle_expire_time)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".part")
    filas = 0
    con = conectar()
    try:
        cur = con.cursor()
        cur.arraysize = por_lote
        cur.execute(CONSULTA_CASOS)
        with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["SUBJECT", "CASE_ID"])
            while True:
                lote = cur.fetchmany(por_lote)
                if not lote:
                    break
                for subject, caso in lote:
                    w.writerow(["" if subject is None else str(subject).replace("\r", " ").replace("\n", " "),
                                "" if caso is None else str(caso)])
                filas += len(lote)
                log.info("Consulta Oracle: %d filas", filas)
        os.replace(tmp, destino)
        return filas
    finally:
        if tmp.exists():
            tmp.unlink()
        con.close()


# ---------------------------------------------------------------------------
# 2c. Número de caso por SDS en CNA.ACT_S1_DETALLE_CASOS (una consulta por SDS)
# ---------------------------------------------------------------------------

CONSULTA_POR_SDS = "SELECT * FROM CNA.ACT_S1_DETALLE_CASOS WHERE ASUNTO LIKE :patron"
_COLUMNAS_CASO_PREFERIDAS = ("NUMERO_CASO", "NRO_CASO", "NUM_CASO", "CASE_NUMBER", "CASENUMBER", "ID_CASO",
                             "CASO", "CASE_ID")


def elegir_columna_caso(columnas: list[str], forzada: str = "") -> str:
    cols = [c.upper() for c in columnas]
    if forzada:
        if forzada.upper() not in cols:
            raise ValueError(f"La columna '{forzada}' no existe. Columnas: {', '.join(cols)}")
        return forzada.upper()
    for c in _COLUMNAS_CASO_PREFERIDAS:
        if c in cols:
            return c
    candidatas = [c for c in cols if ("CASO" in c or "CASE" in c) and c != "ASUNTO"]
    if len(candidatas) == 1:
        return candidatas[0]
    raise ValueError("No se pudo determinar la columna del número de caso; indicarla con --columna-caso. "
                     f"Columnas: {', '.join(cols)}")


def sds_unicas(problemas: list[SdsProblema]) -> list[str]:
    return sorted({p.sds.upper() for p in problemas})


def _conexion_oracle(cfg: Config):
    import oracledb
    oracledb.defaults.fetch_lobs = False
    return oracledb.connect(user=cfg.oracle_usuario, password=leer_password(cfg.oracle_archivo_password),
                            dsn=cfg.oracle_dsn, expire_time=cfg.oracle_expire_time)


def consultar_por_sds(cfg: Config, sds: list[str], carpeta: Path, columna_caso: str = "", hilos: int = 1,
                      detener=lambda: False, conectar=None, tambien_minusculas: bool = True) -> dict[str, int]:
    """Ejecuta la consulta por cada SDS y guarda, en forma reanudable:

    * ``casos_por_sds.csv``: SDS;CASOS (separados por |);FILAS
    * ``detalle_casos_por_sds.csv``: SDS + todas las columnas devueltas (auditoría)

    Si no hay filas con la SDS en mayúsculas, se repite con las letras en minúscula.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from queue import Empty, Queue

    conectar = conectar or (lambda: _conexion_oracle(cfg))
    carpeta.mkdir(parents=True, exist_ok=True)
    resumen = carpeta / "casos_por_sds.csv"
    detalle = carpeta / "detalle_casos_por_sds.csv"
    hechas: set[str] = set()
    if resumen.exists():
        with open(resumen, encoding="utf-8-sig") as fh:
            hechas = {f["SDS"] for f in csv.DictReader(fh, delimiter=";")}
    pendientes: Queue = Queue()
    for s_ in sds:
        if s_ not in hechas:
            pendientes.put(s_)
    lock = threading.Lock()
    estado = {"columnas": None, "col_caso": None, "hechas": 0, "con_caso": 0}
    nuevo_resumen = not resumen.exists()
    fr = open(resumen, "a", newline="", encoding="utf-8-sig")
    fd = open(detalle, "a", newline="", encoding="utf-8-sig")
    wr, wd = csv.writer(fr, delimiter=";"), csv.writer(fd, delimiter=";")
    if nuevo_resumen:
        wr.writerow(["SDS", "CASOS", "FILAS"])
    escribir_cabecera_detalle = detalle.stat().st_size == 0

    def trabajar():
        nonlocal escribir_cabecera_detalle
        con = conectar()
        try:
            cur = con.cursor()
            while not detener():
                try:
                    s_ = pendientes.get_nowait()
                except Empty:
                    return
                filas = []
                for variante in ((s_, s_.lower()) if tambien_minusculas else (s_,)):
                    cur.execute(CONSULTA_POR_SDS, patron=f"%{variante}%")
                    filas = cur.fetchall()
                    if filas:
                        break
                columnas = [d[0].upper() for d in cur.description]
                with lock:
                    if estado["columnas"] is None:
                        estado["columnas"] = columnas
                        estado["col_caso"] = elegir_columna_caso(columnas, columna_caso)
                        log.info("Columnas: %s | número de caso: %s", ", ".join(columnas), estado["col_caso"])
                    if escribir_cabecera_detalle:
                        wd.writerow(["SDS_BUSCADA"] + columnas)
                        escribir_cabecera_detalle = False
                    i = estado["columnas"].index(estado["col_caso"])
                    casos = sorted({str(f[i]).strip() for f in filas if f[i] not in (None, "")})
                    for f in filas:
                        wd.writerow([s_] + ["" if v is None else str(v).replace("\r", " ").replace("\n", " ")
                                            for v in f])
                    wr.writerow([s_, "|".join(casos), len(filas)])
                    estado["hechas"] += 1
                    estado["con_caso"] += bool(casos)
                    if estado["hechas"] % 200 == 0:
                        fr.flush()
                        fd.flush()
                        log.info("Consulta por SDS: %d hechas en esta ejecución, %d con caso (pendientes: %d)",
                                 estado["hechas"], estado["con_caso"], pendientes.qsize())
        finally:
            con.close()

    try:
        with ThreadPoolExecutor(max_workers=max(1, hilos)) as ex:
            for f in [ex.submit(trabajar) for _ in range(max(1, hilos))]:
                f.result()
    finally:
        fr.close()
        fd.close()
    return {"consultadas_ahora": estado["hechas"], "con_caso_ahora": estado["con_caso"],
            "ya_consultadas": len(hechas), "pendientes": pendientes.qsize()}


def leer_casos_por_sds(ruta: Path) -> dict[str, list[str]]:
    with open(ruta, encoding="utf-8-sig") as fh:
        return {f["SDS"]: [c for c in f["CASOS"].split("|") if c] for f in csv.DictReader(fh, delimiter=";")}


def buscar_en_original(casos_por_sds: dict[str, list[str]], carpeta: Path, quitar_inicio: int = 1):
    """Para cada SDS con caso, si el número de caso aparece en algún nombre de archivo de ``carpeta``.

    Solo lee nombres (no descarga archivos de la nube). Cuenta por separado la coincidencia con el número
    completo y con el número sin sus primeros ``quitar_inicio`` caracteres.
    Devuelve (totales, filas) con filas = [(sds, casos, archivos_completo, archivos_recortado)].
    """
    completos = {c for cs in casos_por_sds.values() for c in cs}
    recortados = {c[quitar_inicio:] for c in completos if len(c) > quitar_inicio} if quitar_inicio else set()
    idx = _IndiceCasos(completos | recortados)
    hallados: dict[str, list[str]] = defaultdict(list)
    for n, (p, _) in enumerate(_recorrer(carpeta), 1):
        if n % 20000 == 0:
            log.info("Búsqueda en original: %d archivos revisados", n)
        for c in idx.buscar(p.name):
            if len(hallados[c]) < 20:
                hallados[c].append(_sin_prefijo(p))
    filas = []
    tot = {"sds_consultadas": len(casos_por_sds), "sds_con_caso": 0, "con_archivo_numero_completo": 0,
           "con_archivo_sin_primer_digito": 0, "con_archivo_alguna_forma": 0}
    for s_, casos in sorted(casos_por_sds.items()):
        if not casos:
            continue
        tot["sds_con_caso"] += 1
        a_comp = [a for c in casos for a in hallados.get(c, [])]
        a_rec = [a for c in casos if quitar_inicio and len(c) > quitar_inicio
                 for a in hallados.get(c[quitar_inicio:], [])]
        tot["con_archivo_numero_completo"] += bool(a_comp)
        tot["con_archivo_sin_primer_digito"] += bool(a_rec)
        tot["con_archivo_alguna_forma"] += bool(a_comp or a_rec)
        filas.append((s_, casos, a_comp, a_rec))
    return tot, filas
