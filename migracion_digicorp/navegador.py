"""Descarga de enlaces públicos de Salesforce (``/sfc/p/#…``) con un navegador automático.

El identificador del documento va después del ``#`` y el servidor nunca lo recibe: la página lo
lee con JavaScript y muestra los botones "Descargar" / "Descargar como PDF". Por eso se abre el
enlace con Playwright (sin ventana), se presiona el botón y se guarda el archivo. No se elude
ningún control de acceso: solo se usan enlaces públicos que no piden inicio de sesión.

Playwright no es seguro entre hilos, así que cada navegador vive en un hilo propio que atiende
pedidos desde una cola.
"""
from __future__ import annotations

import base64
import logging
import os
import queue
import threading
from concurrent.futures import Future
from pathlib import Path

from .config import Config
from .validar_pdf import detectar_tipo

log = logging.getLogger(__name__)

# Títulos del botón según el idioma de la página
TITULOS_ORIGINAL = ("Descargar", "Download")
TITULOS_PDF = ("Descargar como PDF", "Download as PDF")
_TIPOS_ACEPTADOS = {"pdf", "jpeg", "png", "tiff", "gif", "bmp"}


class ErrorNavegador(Exception):
    """Fallo reintentable (tiempo de espera, descarga interrumpida)."""


class ErrorNavegadorPermanente(Exception):
    """Fallo que no se resuelve reintentando (página sin documento, enlace inválido)."""


class ErrorConfiguracionNavegador(Exception):
    """Playwright o el navegador no están instalados/configurados."""


_JS_FETCH = """async (u) => {
  const r = await fetch(u, {credentials: "include"});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, b64: btoa(s)};
}"""


def _buscar_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            ruta = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if ruta.exists():
                return str(ruta)
    return None


def _selector(titulos: tuple[str, ...]) -> str:
    return ", ".join(f'button.downloadbutton[title="{t}"]' for t in titulos)


class ServicioNavegador:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cola: queue.Queue = queue.Queue()
        self._hilos: list[threading.Thread] = []
        self._arranque = threading.Event()
        self._error_arranque: Exception | None = None
        for i in range(max(1, cfg.navegadores_simultaneos)):
            h = threading.Thread(target=self._trabajar, name=f"navegador-{i}", daemon=True)
            h.start()
            self._hilos.append(h)

    # --- API ---------------------------------------------------------------
    def descargar(self, url: str, destino: Path) -> None:
        """Página pública de Salesforce: presiona el botón de descarga."""
        self._encolar("boton", url, destino)

    def descargar_archivo(self, url: str, destino: Path) -> None:
        """Enlace directo a un archivo, descargado con la red y los certificados del navegador."""
        self._encolar("archivo", url, destino)

    def _encolar(self, modo: str, url: str, destino: Path) -> None:
        fut: Future = Future()
        self._cola.put((modo, url, destino, fut))
        fut.result()  # propaga la excepción del hilo del navegador

    def cerrar(self) -> None:
        for _ in self._hilos:
            self._cola.put(None)
        for h in self._hilos:
            h.join(timeout=30)

    # --- hilo del navegador -------------------------------------------------
    def _lanzar(self, p):
        opciones = {"headless": not self.cfg.navegador_visible}
        if self.cfg.navegador_ejecutable:
            opciones["executable_path"] = self.cfg.navegador_ejecutable
        elif self.cfg.navegador_canal == "msedge" and (edge := _buscar_edge()):
            opciones["executable_path"] = edge  # Edge ya instalado en Windows
        elif self.cfg.navegador_canal:
            opciones["channel"] = self.cfg.navegador_canal
        return p.chromium.launch(**opciones)

    def _trabajar(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._fallar_todo(ErrorConfiguracionNavegador(
                "Playwright no está instalado: ejecutar 'pip install playwright' y "
                "'python -m playwright install chromium' (o configurar navegador_canal = \"msedge\")"))
            return
        try:
            with sync_playwright() as p:
                try:
                    navegador = self._lanzar(p)
                except Exception as exc:
                    self._fallar_todo(ErrorConfiguracionNavegador(f"No se pudo iniciar el navegador: {exc}"))
                    return
                try:
                    while True:
                        item = self._cola.get()
                        if item is None:
                            return
                        modo, url, destino, fut = item
                        try:
                            if modo == "boton":
                                self._descargar_en(navegador, url, destino)
                            else:
                                self._descargar_archivo_en(navegador, url, destino)
                            fut.set_result(None)
                        except Exception as exc:
                            fut.set_exception(exc)
                finally:
                    navegador.close()
        except Exception as exc:  # error inesperado de Playwright
            self._fallar_todo(ErrorConfiguracionNavegador(f"Error del navegador: {exc}"))

    def _fallar_todo(self, exc: Exception) -> None:
        log.error("%s", exc)
        while True:
            item = self._cola.get()
            if item is None:
                return
            item[-1].set_exception(exc)

    def _descargar_en(self, navegador, url: str, destino: Path) -> None:
        from playwright.sync_api import TimeoutError as PwTimeout

        espera = int(self.cfg.timeout_navegador * 1000)
        contexto = navegador.new_context(accept_downloads=True, locale="es-AR")
        try:
            pagina = contexto.new_page()
            try:
                pagina.goto(url, wait_until="domcontentloaded", timeout=espera)
            except PwTimeout as exc:
                raise ErrorNavegador("La página de Salesforce no respondió a tiempo") from exc
            boton = pagina.locator(_selector(TITULOS_ORIGINAL)).first
            try:
                boton.wait_for(state="visible", timeout=espera)
            except PwTimeout as exc:
                raise ErrorNavegadorPermanente(
                    "La página de Salesforce no mostró el botón 'Descargar' (enlace vencido, inválido o "
                    "sin documento)") from exc
            self._click_y_guardar(pagina, boton, destino, espera)
            tipo = detectar_tipo(destino)
            if tipo not in _TIPOS_ACEPTADOS:
                alternativo = pagina.locator(_selector(TITULOS_PDF)).first
                if alternativo.count():
                    log.info("Salesforce: 'Descargar' devolvió %s; se usa 'Descargar como PDF'", tipo)
                    os.remove(destino)
                    self._click_y_guardar(pagina, alternativo, destino, espera)
        finally:
            contexto.close()

    def _descargar_archivo_en(self, navegador, url: str, destino: Path) -> None:
        """Navega al archivo. Si el navegador lo descarga, se guarda la descarga; si lo muestra en línea
        (p. ej. un PDF), se guarda el cuerpo de la respuesta."""
        from playwright.sync_api import Error as PwError
        from playwright.sync_api import TimeoutError as PwTimeout

        espera = int(self.cfg.timeout_navegador * 1000)
        contexto = navegador.new_context(accept_downloads=True)
        try:
            pagina = contexto.new_page()
            descargas = []
            estados: list[int] = []
            pagina.on("download", lambda d: descargas.append(d))
            pagina.on("response", lambda r: estados.append(r.status)
                      if r.request.is_navigation_request() and r.request.frame == pagina.main_frame else None)
            try:
                pagina.goto(url, wait_until="commit", timeout=espera)
            except PwTimeout as exc:
                raise ErrorNavegador("El servidor no respondió a tiempo") from exc
            except PwError as exc:
                texto = str(exc)
                if any(m in texto for m in ("ERR_CERT", "ERR_SSL")):
                    raise ErrorConfiguracionNavegador(
                        "El navegador tampoco confía en el certificado del servidor "
                        f"({texto.split(' at ')[0][:80]}). Ver 'Problemas de red' en el README.") from exc
                if not any(m in texto for m in ("Download is starting", "ERR_ABORTED",
                                                "ERR_HTTP_RESPONSE_CODE_FAILURE")):
                    raise ErrorNavegador(f"Error de navegación: {texto[:200]}") from exc
            estado = estados[-1] if estados else None
            if estado in (401, 403):
                raise ErrorNavegadorPermanente(
                    f"HTTP {estado}: el recurso requiere autenticación o el acceso fue denegado")
            if estado in (404, 410):
                raise ErrorNavegadorPermanente(f"HTTP {estado}: el documento no existe en el servidor")
            if estado is not None and (estado >= 500 or estado == 429):
                raise ErrorNavegador(f"HTTP {estado} (transitorio)")
            for _ in range(20):  # hasta ~5 s para que se dispare una descarga
                if descargas:
                    break
                pagina.wait_for_timeout(250)
            if descargas:
                d = descargas[0]
                if d.path() is None or d.failure():  # path() espera a que termine
                    raise ErrorNavegador(f"La descarga falló en el navegador: {d.failure()}")
                d.save_as(str(destino))
                return
            if estado is None:
                raise ErrorNavegador("El navegador no obtuvo respuesta ni descarga")
            # Se muestra en línea (p. ej. visor de PDF): se pide el archivo desde la propia página, con
            # la red y los certificados del navegador, y se obtienen los bytes exactos.
            try:
                datos = pagina.evaluate(_JS_FETCH, url)
            except PwError as exc:
                raise ErrorNavegador(f"No se pudo leer el archivo: {str(exc)[:160]}") from exc
            if datos["status"] in (401, 403, 404, 410):
                raise ErrorNavegadorPermanente(f"HTTP {datos['status']} al leer el archivo")
            if datos["status"] != 200:
                raise ErrorNavegador(f"HTTP {datos['status']} al leer el archivo")
            destino.write_bytes(base64.b64decode(datos["b64"]))
        finally:
            contexto.close()

    def _click_y_guardar(self, pagina, boton, destino: Path, espera: int) -> None:
        from playwright.sync_api import TimeoutError as PwTimeout

        try:
            with pagina.expect_download(timeout=espera) as info:
                boton.click()
            descarga = info.value
            fallo = descarga.failure()
            if fallo:
                raise ErrorNavegador(f"La descarga falló en el navegador: {fallo}")
            descarga.save_as(str(destino))
        except PwTimeout as exc:
            raise ErrorNavegador("El botón de descarga no inició ninguna descarga a tiempo") from exc
