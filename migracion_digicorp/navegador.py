"""Descarga de enlaces públicos de Salesforce (``/sfc/p/#…``) con un navegador automático.

El identificador del documento va después del ``#`` y el servidor nunca lo recibe: la página lo
lee con JavaScript y muestra los botones "Descargar" / "Descargar como PDF". Por eso se abre el
enlace con Playwright (sin ventana), se presiona el botón y se guarda el archivo. No se elude
ningún control de acceso: solo se usan enlaces públicos que no piden inicio de sesión.

Playwright no es seguro entre hilos, así que cada navegador vive en un hilo propio que atiende
pedidos desde una cola.
"""
from __future__ import annotations

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
        fut: Future = Future()
        self._cola.put((url, destino, fut))
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
        elif self.cfg.navegador_canal:
            opciones["channel"] = self.cfg.navegador_canal  # p. ej. "msedge" (Edge ya instalado)
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
                        url, destino, fut = item
                        try:
                            self._descargar_en(navegador, url, destino)
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
            item[2].set_exception(exc)

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
