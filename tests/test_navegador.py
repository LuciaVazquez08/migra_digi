"""Prueba real del navegador automático contra una página local que imita la de Salesforce."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from migracion_digicorp.navegador import (ErrorConfiguracionNavegador, ErrorNavegadorPermanente,
                                          ServicioNavegador)
from migracion_digicorp.validar_pdf import detectar_tipo, validar_pdf

from .conftest import pdf_bytes

pytest.importorskip("playwright")

PAGINA = """<html><body><div id="app">Cargando...</div><script>
setTimeout(function () {
  var id = location.hash.slice(1);
  if (id === "vencido") { document.getElementById("app").innerText = "Enlace no disponible"; return; }
  document.getElementById("app").innerHTML =
    '<button type="button" title="Descargar" class="bare downloadbutton uiButton">Descargar</button>' +
    '<button type="button" title="Descargar como PDF" class="bare downloadbutton uiButton">PDF</button>';
  var b = document.querySelectorAll("button.downloadbutton");
  b[0].onclick = function () { location.href = "/original?id=" + id; };
  b[1].onclick = function () { location.href = "/comopdf?id=" + id; };
}, 300);
</script></body></html>"""


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/sfc/p/"):
            cuerpo, tipo, adjunto = PAGINA.encode(), "text/html", None
        elif self.path.startswith("/original"):
            if "docx" in self.path:
                cuerpo, tipo, adjunto = b"PK\x03\x04docx", "application/octet-stream", "doc.docx"
            else:
                cuerpo, tipo, adjunto = pdf_bytes(2), "application/pdf", "doc.pdf"
        elif self.path.startswith("/comopdf"):
            cuerpo, tipo, adjunto = pdf_bytes(1), "application/pdf", "doc.pdf"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        if adjunto:
            self.send_header("Content-Disposition", f'attachment; filename="{adjunto}"')
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


@pytest.fixture(scope="module")
def servidor():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def servicio(entorno):
    entorno.cfg.navegador_ejecutable = os.environ.get("NAVEGADOR_TEST_EJECUTABLE", "")
    entorno.cfg.timeout_navegador = 5
    s = ServicioNavegador(entorno.cfg)
    yield s
    s.cerrar()


def _bajar(servicio, url, destino):
    try:
        servicio.descargar(url, destino)
    except ErrorConfiguracionNavegador as exc:
        pytest.skip(f"Navegador no disponible: {exc}")


def test_descarga_con_boton(servicio, servidor, tmp_path):
    destino = tmp_path / "a.pdf"
    _bajar(servicio, f"{servidor}/sfc/p/#ok", destino)
    v = validar_pdf(destino)
    assert v.ok and v.paginas == 2  # se usó "Descargar" (original)


def test_si_el_original_no_es_pdf_usa_descargar_como_pdf(servicio, servidor, tmp_path):
    destino = tmp_path / "b.pdf"
    _bajar(servicio, f"{servidor}/sfc/p/#docx", destino)
    assert detectar_tipo(destino) == "pdf" and validar_pdf(destino).paginas == 1


def test_pagina_sin_boton_es_error_permanente(servicio, servidor, tmp_path):
    with pytest.raises((ErrorNavegadorPermanente, ErrorConfiguracionNavegador)) as e:
        servicio.descargar(f"{servidor}/sfc/p/#vencido", tmp_path / "c.pdf")
    if isinstance(e.value, ErrorConfiguracionNavegador):
        pytest.skip("Navegador no disponible")
