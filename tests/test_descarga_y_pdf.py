import requests

from migracion_digicorp.descarga import leer_url, validar_url
from migracion_digicorp.estado import Estado
from migracion_digicorp.validar_pdf import detectar_tipo, validar_pdf

from .conftest import RespuestaFalsa, contenido_url, escribir, jpg_bytes, pdf_bytes, url_s1


# --- validación de PDF -------------------------------------------------------

def test_pdf_valido(tmp_path):
    v = validar_pdf(escribir(tmp_path / "a.pdf", pdf_bytes(2)))
    assert v.ok and v.paginas == 2


def test_pdf_truncado(tmp_path):
    datos = pdf_bytes()
    v = validar_pdf(escribir(tmp_path / "a.pdf", datos[: len(datos) // 2]))
    assert not v.ok and "truncado" in v.motivo


def test_pdf_falso_html(tmp_path):
    p = escribir(tmp_path / "login.pdf", b"<!DOCTYPE html><html><body>Iniciar sesion</body></html>")
    assert detectar_tipo(p) == "html"
    assert not validar_pdf(p).ok


def test_pdf_vacio_y_basura(tmp_path):
    assert not validar_pdf(escribir(tmp_path / "v.pdf")).ok
    assert not validar_pdf(escribir(tmp_path / "b.pdf", b"%PDF-1.4\n basura sin estructura %%EOF")).ok


# --- archivos .url ------------------------------------------------------------

def test_leer_url(tmp_path):
    u = url_s1("x")
    assert leer_url(escribir(tmp_path / "a.url", contenido_url(u))) == u
    assert leer_url(escribir(tmp_path / "b.url", b"[InternetShortcut]\r\n")) is None
    assert leer_url(escribir(tmp_path / "c.url", b"[Otra]\r\nURL=https://x\r\n")) is None


def test_validar_url(entorno):
    cfg = entorno.cfg
    u = url_s1("x")
    assert validar_url(u, cfg) == ""
    assert "truncado" in validar_url(u[:-7], cfg)
    assert "Dominio" in validar_url("https://claro.my.salesforce.com/sfc/p/#f4000004kWgH/a/3n0000002gJU/Lnhb", cfg)
    assert "Esquema" in validar_url(u.replace("https", "http"), cfg)
    assert validar_url(None, cfg)
    assert "incompleta" in validar_url("http://cffmslo0cdndmxzmsm5lv21iou5aotnvemviq", cfg)
    assert "incompleta" in validar_url("http://file/get/abc", cfg)


# --- descargador ----------------------------------------------------------------

def _desc(entorno):
    est = Estado(entorno.cfg.archivo_estado)
    return entorno.descargador(est), est


def test_descarga_ok_y_cache(entorno):
    d, _ = _desc(entorno)
    u = entorno.sesion.pdf(url_s1("a"))
    r1 = d.obtener(u)
    r2 = d.obtener(u)
    assert r1.ok and r2.ok and r2.desde_cache
    assert entorno.sesion.llamadas == [u]  # no se repite la descarga


def test_descarga_reintenta_transitorios(entorno):
    d, _ = _desc(entorno)
    u = url_s1("b")
    entorno.sesion.respuestas[u] = [RespuestaFalsa(503, b""), requests.ConnectionError("caida"),
                                    RespuestaFalsa(200, pdf_bytes())]
    assert d.obtener(u).ok
    assert len(entorno.sesion.llamadas) == 3


def test_descarga_incompleta_se_reintenta_y_falla(entorno):
    d, _ = _desc(entorno)
    u = url_s1("c")
    entorno.sesion.respuestas[u] = RespuestaFalsa(200, pdf_bytes()[:50], {"Content-Length": "9999"})
    r = d.obtener(u)
    assert not r.ok and not r.permanente and "incompleta" in r.error


def test_descarga_permanentes(entorno):
    d, _ = _desc(entorno)
    casos = {
        url_s1("403"): (RespuestaFalsa(403, b"x"), "autenticación"),
        url_s1("404"): (RespuestaFalsa(404, b"x"), "no existe"),
        url_s1("html"): (RespuestaFalsa(200, b"<html><body>login</body></html>"), "HTML"),
        url_s1("corrupto"): (RespuestaFalsa(200, b"%PDF-1.7 roto"), "inválido"),
        url_s1("vacio"): (RespuestaFalsa(200, b""), "0 bytes"),
    }
    for u, (resp, texto) in casos.items():
        entorno.sesion.respuestas[u] = resp
        r = d.obtener(u)
        assert not r.ok and r.permanente and texto in r.error, (u, r.error)
    # un error permanente no se vuelve a pedir en la siguiente ejecución
    n = len(entorno.sesion.llamadas)
    d.obtener(url_s1("403"))
    assert len(entorno.sesion.llamadas) == n


def test_descarga_imagen_se_convierte(entorno):
    d, _ = _desc(entorno)
    u = url_s1("img")
    entorno.sesion.respuestas[u] = RespuestaFalsa(200, jpg_bytes(), {"Content-Type": "image/jpeg"})
    r = d.obtener(u)
    assert r.ok and r.convertido_desde == "jpeg" and validar_pdf(r.ruta).ok
