"""Lectura de archivos .url, validación de enlaces y descarga a caché con validación."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .archivos import largo, sha256_archivo
from .config import Config
from .conversion import ErrorConversion, imagen_a_pdf
from .estado import Estado
from .navegador import (ErrorConfiguracionNavegador, ErrorNavegador, ErrorNavegadorPermanente,
                        ServicioNavegador)
from .validar_pdf import detectar_tipo, validar_pdf

log = logging.getLogger(__name__)

_RE_FRAGMENTO_SF = re.compile(r"[A-Za-z0-9]{12}/a/[A-Za-z0-9]{12}/[A-Za-z0-9_\-]{20,}")
_RE_TOKEN_S1 = re.compile(r"/file/get/([A-Za-z0-9+/]+={0,2})")
_TIPOS_IMAGEN = {"jpeg", "png", "tiff", "gif", "bmp"}


# "[InternetSortcut]" (sin la h) aparece en copias generadas por un proceso automático.
_SECCIONES_URL = ("[internetshortcut]", "[internetsortcut]")


def leer_url(ruta: Path) -> str | None:
    """Extrae ``URL=`` de la sección ``[InternetShortcut]`` de un archivo .url."""
    with open(largo(ruta), "rb") as fh:
        crudo = fh.read(64 * 1024)
    for codificacion in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            texto = crudo.decode(codificacion)
            if "URL=" in texto.upper():
                break
        except UnicodeDecodeError:
            continue
    else:
        return None
    en_seccion = False
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea.startswith("["):
            en_seccion = linea.lower() in _SECCIONES_URL
            continue
        if en_seccion and linea.upper().startswith("URL="):
            valor = linea[4:].strip()
            return valor or None
    return None


def _validar_s1gateway(partes) -> str:
    m = _RE_TOKEN_S1.fullmatch(partes.path)
    if not m or partes.query or partes.fragment:
        return "Ruta de s1gateway inesperada (se esperaba /file/get/<token>)"
    token = m.group(1)
    # El token es base64(base64(bloque cifrado)). Si está truncado, alguna capa no decodifica
    # o el bloque interno no es múltiplo de 16 bytes.
    try:
        interno = base64.b64decode(token, validate=True)
        bloque = base64.b64decode(interno, validate=True)
    except (binascii.Error, ValueError):
        return "Token del enlace truncado o mal formado (base64 inválido)"
    if len(bloque) == 0 or len(bloque) % 16 != 0:
        return f"Token del enlace truncado (bloque de {len(bloque)} bytes, no múltiplo de 16)"
    return ""


def _validar_salesforce(partes) -> str:
    if partes.path != "/sfc/p/" or partes.query:
        return "Ruta de Salesforce inesperada (se esperaba /sfc/p/#<org>/a/<id>/<token>)"
    if not _RE_FRAGMENTO_SF.fullmatch(partes.fragment or ""):
        return "Enlace de Salesforce truncado o mal formado (la parte después de '#' no tiene el formato esperado)"
    return ""


def es_salesforce(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower().endswith(".salesforce.com")


_VALIDADORES = {"cdnva.s1gateway.com": _validar_s1gateway, "claro.my.salesforce.com": _validar_salesforce}


def validar_url(url: str | None, cfg: Config) -> str:
    """Devuelve '' si el enlace es estructuralmente válido; si no, el motivo."""
    if not url:
        return "El archivo .url no contiene una dirección (URL=)"
    if any(c.isspace() for c in url):
        return "La URL contiene espacios"
    try:
        partes = urlsplit(url)
    except ValueError as exc:
        return f"URL mal formada: {exc}"
    host = (partes.hostname or "").lower()
    if partes.scheme in ("http", "https") and "." not in host:
        return (f"URL incompleta o dañada: '{host[:20]}…' no es un dominio válido "
                "(falta el servidor; no se reconstruye)")
    if partes.scheme != "https":
        return f"Esquema no permitido: {partes.scheme or '(ninguno)'}"
    if host not in cfg.dominios_permitidos:
        return f"Dominio no soportado para descarga automática: {host or '(vacío)'}"
    validador = _VALIDADORES.get(host)
    return validador(partes) if validador else ""


def clave_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


@dataclass
class ResultadoDescarga:
    ok: bool
    ruta: Path | None = None
    sha256: str = ""
    tipo: str = ""
    error: str = ""
    desde_cache: bool = False
    convertido_desde: str = ""
    permanente: bool = False  # True = error no transitorio: requiere revisión manual


class ErrorPermanente(Exception):
    """Error que no tiene sentido reintentar."""


class Descargador:
    """Descarga a ``cache/<sha256(url)>.pdf`` y valida. Reutiliza descargas previas válidas."""

    def __init__(self, cfg: Config, estado: Estado, session: requests.Session | None = None,
                 navegador: ServicioNavegador | None = None):
        self.cfg = cfg
        self.estado = estado
        self.session = session or self._crear_sesion()
        self._navegador = navegador
        self._lock_navegador = threading.Lock()
        self.cfg.dir_cache.mkdir(parents=True, exist_ok=True)
        (self.cfg.dir_cache / "tmp").mkdir(exist_ok=True)

    def _crear_sesion(self) -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = self.cfg.user_agent
        s.max_redirects = 10
        return s

    def navegador(self) -> ServicioNavegador:
        with self._lock_navegador:
            if self._navegador is None:
                self._navegador = ServicioNavegador(self.cfg)
            return self._navegador

    def cerrar(self) -> None:
        if self._navegador is not None:
            self._navegador.cerrar()
            self._navegador = None

    def ruta_cache(self, url: str) -> Path:
        return self.cfg.dir_cache / f"{clave_url(url)}.pdf"

    def obtener(self, url: str, reintentar_fallidas: bool = False) -> ResultadoDescarga:
        clave = clave_url(url)
        previa = self.estado.descarga(clave)
        destino = self.ruta_cache(url)
        if previa and previa["estado"] == "OK" and destino.exists():
            if sha256_archivo(destino) == previa["sha256"]:
                return ResultadoDescarga(True, destino, previa["sha256"], previa["tipo"], desde_cache=True)
            log.warning("Caché alterada para %s; se descarga de nuevo", url)
            destino.unlink()
        if previa and previa["estado"] == "ERROR" and previa["permanente"] and not reintentar_fallidas:
            return ResultadoDescarga(False, error=f"(error previo) {previa['error']}", desde_cache=True,
                                     permanente=True)

        motivo = validar_url(url, self.cfg)
        if motivo:
            self.estado.registrar_descarga(clave, url, "ERROR", permanente=True, error=motivo)
            return ResultadoDescarga(False, error=motivo, permanente=True)

        intentos = 0
        ultimo_error = ""
        tmp = self.cfg.dir_cache / "tmp" / f"{clave}.part"
        while intentos <= self.cfg.reintentos:
            intentos += 1
            try:
                tipo_original = self._bajar(url, tmp)
                resultado = self._validar_y_guardar(tmp, destino, tipo_original)
                self.estado.registrar_descarga(clave, url, "OK", ruta=str(destino), sha256=resultado.sha256,
                                               bytes_=destino.stat().st_size, tipo=resultado.tipo,
                                               intentos=intentos)
                return resultado
            except ErrorConfiguracionNavegador as exc:
                ultimo_error = str(exc)
                self.estado.registrar_descarga(clave, url, "ERROR", permanente=False, error=ultimo_error,
                                               intentos=intentos)
                return ResultadoDescarga(False, error=ultimo_error)
            except ErrorPermanente as exc:
                ultimo_error = str(exc)
                self.estado.registrar_descarga(clave, url, "ERROR", permanente=True, error=ultimo_error,
                                               intentos=intentos)
                return ResultadoDescarga(False, error=ultimo_error, permanente=True)
            except (requests.RequestException, IOError) as exc:
                ultimo_error = f"{type(exc).__name__}: {exc}"
                log.warning("Intento %d/%d fallido para %s: %s", intentos, self.cfg.reintentos + 1,
                            url, ultimo_error)
                if intentos <= self.cfg.reintentos:
                    time.sleep(self.cfg.espera_reintento * 2 ** (intentos - 1))
            finally:
                if tmp.exists():
                    tmp.unlink()
        self.estado.registrar_descarga(clave, url, "ERROR", permanente=False, error=ultimo_error,
                                       intentos=intentos)
        return ResultadoDescarga(False, error=f"Descarga fallida tras {intentos} intentos: {ultimo_error}")

    def _bajar(self, url: str, tmp: Path) -> str:
        """Descarga en streaming a ``tmp``. Lanza ErrorPermanente o excepciones reintentables."""
        if es_salesforce(url):
            return self._bajar_salesforce(url, tmp)
        resp = self.session.get(url, stream=True, allow_redirects=True,
                                timeout=(self.cfg.timeout_conexion, self.cfg.timeout_lectura))
        try:
            codigo = resp.status_code
            if codigo in (401, 403):
                raise ErrorPermanente(f"HTTP {codigo}: el recurso requiere autenticación o el acceso fue denegado")
            if codigo in (404, 410):
                raise ErrorPermanente(f"HTTP {codigo}: el documento no existe en el servidor")
            if codigo == 429 or codigo >= 500:
                raise IOError(f"HTTP {codigo} (transitorio)")
            if codigo != 200:
                raise ErrorPermanente(f"HTTP {codigo} inesperado")
            esperado = resp.headers.get("Content-Length")
            total = 0
            with open(tmp, "wb") as fh:
                for bloque in resp.iter_content(chunk_size=256 * 1024):
                    if not bloque:
                        continue
                    total += len(bloque)
                    if total > self.cfg.tamano_maximo_descarga:
                        raise ErrorPermanente("La descarga supera el tamaño máximo configurado")
                    fh.write(bloque)
                fh.flush()
                os.fsync(fh.fileno())
            if esperado is not None and esperado.isdigit() and int(esperado) != total:
                raise IOError(f"Descarga incompleta: {total} de {esperado} bytes")
            if total == 0:
                raise ErrorPermanente("El enlace no devolvió ningún contenido (0 bytes)")
            return resp.headers.get("Content-Type", "")
        finally:
            resp.close()

    def _bajar_salesforce(self, url: str, tmp: Path) -> str:
        try:
            self.navegador().descargar(url, tmp)
        except ErrorNavegadorPermanente as exc:
            raise ErrorPermanente(str(exc)) from exc
        except ErrorNavegador as exc:
            raise IOError(str(exc)) from exc
        tamano = tmp.stat().st_size if tmp.exists() else 0
        if tamano == 0:
            raise ErrorPermanente("La descarga de Salesforce no devolvió contenido (0 bytes)")
        if tamano > self.cfg.tamano_maximo_descarga:
            raise ErrorPermanente("La descarga supera el tamaño máximo configurado")
        return ""

    def _validar_y_guardar(self, tmp: Path, destino: Path, content_type: str) -> ResultadoDescarga:
        tipo = detectar_tipo(tmp)
        convertido = ""
        if tipo == "pdf":
            v = validar_pdf(tmp)
            if not v.ok:
                raise ErrorPermanente(f"PDF descargado inválido: {v.motivo}")
            os.replace(tmp, destino)
        elif tipo in _TIPOS_IMAGEN and self.cfg.aceptar_imagen_descargada:
            tmp_pdf = tmp.with_suffix(".conv.pdf")
            try:
                imagen_a_pdf(tmp, tmp_pdf)
                v = validar_pdf(tmp_pdf)
                if not v.ok:
                    raise ErrorPermanente(f"PDF generado desde imagen inválido: {v.motivo}")
                os.replace(tmp_pdf, destino)
            except ErrorConversion as exc:
                raise ErrorPermanente(f"Imagen descargada ilegible: {exc}") from exc
            finally:
                if tmp_pdf.exists():
                    tmp_pdf.unlink()
            convertido = tipo
        elif tipo == "html":
            raise ErrorPermanente("El enlace devolvió una página HTML (posible login, error o enlace vencido), "
                                  "no un documento")
        else:
            raise ErrorPermanente(f"El contenido descargado no es un PDF ni una imagen (tipo: {tipo}, "
                                  f"Content-Type: {content_type or '-'})")
        return ResultadoDescarga(True, destino, sha256_archivo(destino), "pdf", convertido_desde=convertido)
