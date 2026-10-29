"""Reglas documentales puras (sin acceso a disco). Ver docs/REGLAS.md."""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

RE_CUIT = re.compile(r"\d{11}")
RE_SDS = re.compile(r"\d{8}[A-Z]{3}")
RE_CONTRATO = re.compile(r"\d{8}")
RE_SDS_EN_TEXTO = re.compile(r"(?<![0-9A-Za-z])(\d{8}[A-Z]{3})(?![0-9A-Za-z])")
RE_CONTRATO_ARCHIVO = re.compile(r"(?i)(?:os\s*)?(\d{8})")
RE_FACULTATIVA = re.compile(r"documentacion facultativa(?: (\S+))?")

CAT_MOVIL = "documentacion movil"
CAT_CONTRATOS = "contratos fija"

PLACEHOLDERS_MOVIL = frozenset({"numero de sds"})
PLACEHOLDERS_CONTRATOS = frozenset({"numero os"})

ARCHIVOS_IGNORADOS = frozenset({"desktop.ini", "thumbs.db"})
EXT_BLOQUEANTES = frozenset({".crdownload", ".partial", ".lnk", ".tmp", ".part"})
EXT_IMAGEN = frozenset({".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".gif"})
EXT_OFFICE = frozenset({".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".ods"})
# Formatos que se migran tal cual (no existe conversión a PDF sin pérdida)
EXT_COPIA_DIRECTA = frozenset({".msg", ".eml", ".mht", ".mhtml", ".zip", ".rar", ".7z", ".txt", ".csv"})
EXT_URL = ".url"
EXT_PDF = ".pdf"
EXT_PERMITIDAS = EXT_IMAGEN | EXT_OFFICE | EXT_COPIA_DIRECTA | {EXT_URL, EXT_PDF}

_PESOS_CUIT = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con espacios colapsados (solo para comparar)."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.casefold().split())


def digito_verificador_cuit(primeros10: str) -> int:
    suma = sum(int(d) * p for d, p in zip(primeros10, _PESOS_CUIT))
    resto = 11 - suma % 11
    if resto == 11:
        return 0
    if resto == 10:
        return 9
    return resto


def validar_cuit(nombre: str, validar_dv: bool = True) -> tuple[bool, str]:
    if not RE_CUIT.fullmatch(nombre):
        digitos = sum(c.isdigit() for c in nombre)
        return False, f"El nombre '{nombre}' no son exactamente 11 dígitos ({digitos} dígitos, {len(nombre)} caracteres)"
    if validar_dv and digito_verificador_cuit(nombre[:10]) != int(nombre[10]):
        return False, f"Dígito verificador inválido para el CUIT '{nombre}'"
    return True, ""


def extension(nombre: str) -> str:
    return PurePath(nombre).suffix.lower()


def es_ignorado(nombre: str) -> bool:
    return nombre.casefold() in ARCHIVOS_IGNORADOS


_RE_COPIA_PREFIJO = re.compile(r"(?i)^copia(?:\s+de)?\s+(.+)$")
_RE_COPIA_SUFIJO = re.compile(r"(?i)^(.+?)\s+-\s+copia(?:\s+\(\d+\))?(\.[^.]+)$")


def nombre_original_de_copia(nombre: str) -> str | None:
    """Devuelve el nombre del original si ``nombre`` sigue una convención de copia.

    Convenciones reconocidas: ``Copia X``, ``Copia de X`` y ``X - copia[ (n)].ext``
    (las que genera Windows en español). ``X (1)`` NO se considera copia.
    """
    m = _RE_COPIA_PREFIJO.match(nombre.strip())
    if m:
        return m.group(1).strip()
    m = _RE_COPIA_SUFIJO.match(nombre.strip())
    if m:
        return m.group(1).strip() + m.group(2)
    return None


def inferir_sds(nombre_archivo: str) -> str | None:
    """SDS contenida en el nombre del archivo, solo si hay exactamente una distinta."""
    encontrados = set(RE_SDS_EN_TEXTO.findall(PurePath(nombre_archivo).stem))
    return encontrados.pop() if len(encontrados) == 1 else None


def inferir_contrato(nombre_archivo: str) -> str | None:
    """Número de contrato si el nombre es exactamente ``<8 dígitos>`` u ``OS <8 dígitos>``."""
    m = RE_CONTRATO_ARCHIVO.fullmatch(PurePath(nombre_archivo).stem.strip())
    return m.group(1) if m else None


def categoria_cliente(nombre_carpeta: str) -> tuple[str, str | None]:
    """Clasifica una carpeta de nivel 2. Devuelve (categoria, sufijo_facultativa)."""
    n = normalizar(nombre_carpeta)
    if n == CAT_MOVIL:
        return "movil", None
    if n == CAT_CONTRATOS:
        return "contratos", None
    m = RE_FACULTATIVA.fullmatch(n)
    if m:
        return "facultativa", m.group(1)
    return "desconocida", None


def nombre_pdf(nombre_archivo: str) -> str:
    """``SDS FIRMADA.url`` -> ``SDS FIRMADA.pdf``; conserva el resto del nombre."""
    return PurePath(nombre_archivo).stem + EXT_PDF


def nombre_con_sufijo(nombre: str, n: int) -> str:
    """Regla de colisión: ``doc.pdf`` -> ``doc (n).pdf``."""
    p = PurePath(nombre)
    return f"{p.stem} ({n}){p.suffix}"
