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
    assert leer_url(escribir(tmp_path / "d.url", b"[InternetSortcut]\r\nURL=https://claroaup.sharepoint.com/x\r\n")) \
        == "https://claroaup.sharepoint.com/x"


def test_validar_url(entorno):
    cfg = entorno.cfg
    u = url_s1("x")
    assert validar_url(u, cfg) == ""
    assert "truncado" in validar_url(u[:-7], cfg)
    sf = "https://claro.my.salesforce.com/sfc/p/#f4000004kWgH/a/3n0000002gJU/LnhbDjA0a9fCRWkM8x1X6kVrgO6aADXj1xhx2mVrLxM"
    assert validar_url(sf, cfg) == ""
    assert "truncado" in validar_url(sf[:-40], cfg)
    assert "truncado" in validar_url("https://claro.my.salesforce.com/sfc/p/", cfg)
    assert "Dominio" in validar_url("https://otro.my.salesforce.com/sfc/p/#f4000004kWgH/a/3n0000002gJU/x", cfg)
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


# --- Salesforce (navegador automático) -------------------------------------------

SF = "https://claro.my.salesforce.com/sfc/p/#f4000004kWgH/a/3n0000002gJU/LnhbDjA0a9fCRWkM8x1X6kVrgO6aADXj1xhx2mVrLxM"


class NavegadorFalso:
    def __init__(self, efectos):
        self.efectos = list(efectos)
        self.llamadas = 0

    def descargar(self, url, destino):
        self.llamadas += 1
        efecto = self.efectos.pop(0) if len(self.efectos) > 1 else self.efectos[0]
        if isinstance(efecto, Exception):
            raise efecto
        destino.write_bytes(efecto)

    def cerrar(self):
        pass


def _desc_sf(entorno, efectos):
    from migracion_digicorp.descarga import Descargador
    nav = NavegadorFalso(efectos)
    return Descargador(entorno.cfg, Estado(entorno.cfg.archivo_estado), session=entorno.sesion, navegador=nav), nav


def test_salesforce_ok_y_cache(entorno):
    d, nav = _desc_sf(entorno, [pdf_bytes(2)])
    assert d.obtener(SF).ok
    assert d.obtener(SF).desde_cache and nav.llamadas == 1
    assert entorno.sesion.llamadas == []  # no usa requests


def test_salesforce_reintenta_y_errores(entorno):
    from migracion_digicorp.navegador import ErrorNavegador, ErrorNavegadorPermanente
    d, nav = _desc_sf(entorno, [ErrorNavegador("lento"), pdf_bytes()])
    assert d.obtener(SF).ok and nav.llamadas == 2
    d, nav = _desc_sf(entorno, [ErrorNavegadorPermanente("sin botón")])
    r = d.obtener(SF.replace("Lnhb", "Xnhb"))
    assert not r.ok and r.permanente and "sin botón" in r.error
    d, _ = _desc_sf(entorno, [b"<html>error</html>"])
    r = d.obtener(SF.replace("Lnhb", "Ynhb"))
    assert not r.ok and "HTML" in r.error


def test_problemas_de_entorno_detienen_sin_reintentar(entorno):
    import pytest
    from migracion_digicorp.descarga import ErrorEntorno
    from migracion_digicorp.navegador import ErrorConfiguracionNavegador
    d, nav = _desc_sf(entorno, [ErrorConfiguracionNavegador("Playwright no está instalado")])
    with pytest.raises(ErrorEntorno):
        d.obtener(SF)
    assert nav.llamadas == 1
    d, _ = _desc(entorno)
    u = url_s1("ssl")
    entorno.sesion.respuestas[u] = requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    with pytest.raises(ErrorEntorno, match="certificado"):
        d.obtener(u)
    assert entorno.sesion.llamadas.count(u) == 1
    u2 = url_s1("ssl2")
    entorno.sesion.respuestas[u2] = requests.exceptions.SSLError(
        "SSLCertVerificationError('A certificate chain processed, but terminated in a root certificate which "
        "is not trusted by the trust provider.')")
    with pytest.raises(ErrorEntorno):
        d.obtener(u2)
    assert entorno.sesion.llamadas.count(u2) == 1


def test_logs_sin_token():
    from migracion_digicorp.descarga import _sin_token, url_corta
    u = url_s1("secreto")
    assert u.split("/")[-1] not in url_corta(u)
    assert u.split("/")[-1] not in _sin_token(f"error en {u} y /file/get/x", u)


def test_certificado_adicional(entorno, tmp_path):
    import pytest
    from migracion_digicorp.descarga import ErrorEntorno, armar_bundle_certificados
    entorno.cfg.certificado_adicional = str(tmp_path / "no.pem")
    with pytest.raises(ErrorEntorno, match="No existe"):
        armar_bundle_certificados(entorno.cfg)
    pem = escribir(tmp_path / "empresa.pem", b"-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n")
    entorno.cfg.certificado_adicional = str(pem)
    bundle = armar_bundle_certificados(entorno.cfg)
    assert bundle.read_bytes().endswith(pem.read_bytes()) and bundle.stat().st_size > 10_000
