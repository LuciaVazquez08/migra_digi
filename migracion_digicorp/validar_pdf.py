"""Detección de tipo de contenido y validación estructural de PDF."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .archivos import largo

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class Validacion:
    ok: bool
    motivo: str = ""
    paginas: int = 0


def detectar_tipo(ruta: Path) -> str:
    """Tipo real por firma de bytes: pdf, jpeg, png, tiff, gif, bmp, html, zip, vacio, desconocido."""
    with open(largo(ruta), "rb") as fh:
        cabecera = fh.read(2048)
    if not cabecera:
        return "vacio"
    if b"%PDF-" in cabecera[:1024]:
        return "pdf"
    if cabecera.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if cabecera.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if cabecera[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if cabecera[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if cabecera.startswith(b"BM"):
        return "bmp"
    if cabecera.startswith(b"PK\x03\x04"):
        return "zip"
    texto = cabecera.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if texto.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body", b"<script")) \
            or b"<html" in texto[:512]:
        return "html"
    if texto.startswith((b"{", b"[")):
        return "json"
    return "desconocido"


def validar_pdf(ruta: Path) -> Validacion:
    """Un PDF es válido si tiene firma, marca de fin, se abre, no está cifrado y tiene páginas."""
    try:
        tamano = os.path.getsize(largo(ruta))
    except OSError as exc:
        return Validacion(False, f"No se puede leer el archivo: {exc}")
    if tamano == 0:
        return Validacion(False, "Archivo vacío (0 bytes)")
    tipo = detectar_tipo(ruta)
    if tipo != "pdf":
        return Validacion(False, f"El contenido no es un PDF (detectado: {tipo})")
    with open(largo(ruta), "rb") as fh:
        fh.seek(max(0, tamano - 4096))
        cola = fh.read()
    if b"%%EOF" not in cola:
        return Validacion(False, "PDF incompleto o truncado (falta la marca %%EOF)")
    try:
        with open(largo(ruta), "rb") as fh:
            lector = PdfReader(fh, strict=False)
            if lector.is_encrypted:
                try:
                    if not lector.decrypt(""):
                        return Validacion(False, "PDF protegido con contraseña")
                except Exception:
                    return Validacion(False, "PDF cifrado que no puede abrirse")
            paginas = len(lector.pages)
            if paginas == 0:
                return Validacion(False, "PDF sin páginas")
            for pagina in lector.pages:
                _ = pagina.mediabox  # fuerza la resolución de cada página
            return Validacion(True, "", paginas)
    except Exception as exc:  # pypdf lanza muchos tipos distintos
        return Validacion(False, f"PDF corrupto: {type(exc).__name__}: {exc}")
