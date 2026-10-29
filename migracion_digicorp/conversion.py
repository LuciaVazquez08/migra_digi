"""Conversión a PDF de imágenes y documentos Office. Si no es posible, se migra el original."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageSequence

from .archivos import largo
from .reglas import EXT_IMAGEN, EXT_OFFICE, extension
from .validar_pdf import validar_pdf

log = logging.getLogger(__name__)


class ErrorConversion(Exception):
    pass


def es_convertible(nombre: str, soffice: str | None) -> bool:
    ext = extension(nombre)
    return ext in EXT_IMAGEN or (ext in EXT_OFFICE and soffice is not None)


def imagen_a_pdf(origen: Path, destino: Path) -> None:
    """Convierte una imagen (todas sus páginas/frames) a PDF."""
    try:
        with Image.open(largo(origen)) as img:
            paginas = [f.convert("RGB") for f in ImageSequence.Iterator(img)]
    except Exception as exc:
        raise ErrorConversion(f"No se pudo abrir la imagen: {exc}") from exc
    if not paginas:
        raise ErrorConversion("La imagen no tiene contenido")
    with open(largo(destino), "xb") as fh:
        paginas[0].save(fh, format="PDF", save_all=True, append_images=paginas[1:], resolution=200.0)


def office_a_pdf(origen: Path, destino: Path, soffice: str, timeout: float) -> None:
    """Convierte con LibreOffice en modo headless usando un perfil aislado."""
    with tempfile.TemporaryDirectory(prefix="conv_") as tmp:
        tmp_p = Path(tmp)
        perfil = (tmp_p / "perfil").as_uri()
        copia = tmp_p / ("entrada" + extension(origen.name))
        shutil.copyfile(largo(origen), copia)  # evita problemas de rutas/caracteres en soffice
        cmd = [soffice, f"-env:UserInstallation={perfil}", "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", str(tmp_p), str(copia)]
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ErrorConversion(f"LibreOffice falló: {exc}") from exc
        salida = tmp_p / "entrada.pdf"
        if not salida.exists():
            raise ErrorConversion("LibreOffice no generó el PDF")
        with open(salida, "rb") as src, open(largo(destino), "xb") as dst:
            shutil.copyfileobj(src, dst)


def convertir_a_pdf(origen: Path, destino: Path, soffice: str | None, timeout: float) -> None:
    """Convierte y valida. Ante cualquier fallo elimina el destino parcial y lanza ErrorConversion."""
    os.makedirs(largo(destino.parent), exist_ok=True)
    ext = extension(origen.name)
    try:
        if ext in EXT_IMAGEN:
            imagen_a_pdf(origen, destino)
        elif ext in EXT_OFFICE and soffice:
            office_a_pdf(origen, destino, soffice, timeout)
        else:
            raise ErrorConversion(f"Sin conversor para {ext}")
        v = validar_pdf(destino)
        if not v.ok:
            raise ErrorConversion(f"PDF convertido inválido: {v.motivo}")
    except Exception:
        if os.path.exists(largo(destino)):
            os.remove(largo(destino))
        raise
