from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path

import pytest
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter

from migracion_digicorp.config import Config
from migracion_digicorp.descarga import Descargador
from migracion_digicorp.estado import Estado
from migracion_digicorp.reglas import digito_verificador_cuit


def cuit(base10: str) -> str:
    return base10 + str(digito_verificador_cuit(base10))


CUIT_A = cuit("2012345678")
CUIT_B = cuit("3071234567")
CUIT_C = cuit("2733333333")


def pdf_bytes(paginas: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(paginas):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def jpg_bytes(color=(200, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="JPEG")
    return buf.getvalue()


def url_s1(semilla: str) -> str:
    bloque = hashlib.sha256(semilla.encode()).digest()  # 32 bytes = 2 bloques AES
    token = base64.b64encode(base64.b64encode(bloque)).decode()
    return f"https://cdnva.s1gateway.com/file/get/{token}"


def contenido_url(url: str) -> bytes:
    return f"[InternetShortcut]\r\nURL={url}\r\n".encode()


def escribir(ruta: Path, datos: bytes = b"") -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(datos)
    return ruta


class RespuestaFalsa:
    def __init__(self, status: int, cuerpo: bytes, headers: dict | None = None):
        self.status_code = status
        self._cuerpo = cuerpo
        self.headers = {"Content-Length": str(len(cuerpo))} | (headers or {})

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._cuerpo), chunk_size):
            yield self._cuerpo[i:i + chunk_size]

    def close(self):
        pass


class SesionFalsa:
    """url -> RespuestaFalsa o lista de respuestas (una por intento)."""

    def __init__(self):
        self.respuestas: dict[str, object] = {}
        self.llamadas: list[str] = []

    def pdf(self, url: str, paginas: int = 1) -> str:
        self.respuestas[url] = RespuestaFalsa(200, pdf_bytes(paginas), {"Content-Type": "application/pdf"})
        return url

    def get(self, url, **kw):
        self.llamadas.append(url)
        r = self.respuestas.get(url)
        if r is None:
            return RespuestaFalsa(404, b"no")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r


def escribir_excel(origen: Path, ruta: Path, omitir: set[str] = frozenset(), extra: list[tuple] = ()) -> Path:
    """Genera un Excel de referencia que refleja el origen (opcionalmente alterado)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Arbol_Carpetas"
    ws.append([f"Nivel {i}" for i in range(1, 7)] + ["FLAG"])
    def omitido(rel) -> bool:
        return any("/".join(rel[:i]) in omitir for i in range(1, len(rel) + 1))

    for raiz, dirs, files in os.walk(origen):
        dirs.sort()
        rel = Path(raiz).relative_to(origen).parts
        if rel:
            if not omitido(rel):
                ws.append(list(rel) + [None] * (6 - len(rel)) + [0])
        for f in sorted(files):
            r = rel + (f,)
            if len(r) > 1 and not omitido(r):
                ws.append(list(r) + [None] * (6 - len(r)) + [0])
    for r in extra:
        ws.append(list(r) + [None] * (6 - len(r)) + [0])
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta)
    return ruta


class Entorno:
    def __init__(self, base: Path):
        self.base = base
        self.origen = base / "Raw Data"
        self.destino = base / "Digicorp"
        self.origen.mkdir()
        self.cfg = Config(origen=self.origen, destino=self.destino, trabajo=base / "trabajo",
                          excel_referencia=base / "ref.xlsx", espera_reintento=0, reintentos=2,
                          convertir_office=False, descargas_concurrentes=2)
        self.sesion = SesionFalsa()

    def cliente_normal(self, c: str, sds: str = "29067459SEC", contrato: str = "60204441") -> Path:
        """Cliente válido completo con todos los casos habituales."""
        raiz = self.origen / c
        mov = raiz / "Documentación Móvil"
        u_sds = self.sesion.pdf(url_s1(c + "sds"))
        u_dni = self.sesion.pdf(url_s1(c + "dni"), 2)
        u_poder = self.sesion.pdf(url_s1(c + "poder"))
        escribir(mov / sds / "SDS.url", contenido_url(u_sds))
        escribir(mov / sds / "DNI 1.url", contenido_url(u_dni))
        escribir(mov / sds / "desktop.ini", b"[.ShellClassInfo]")
        (mov / "Numero de SDS").mkdir(parents=True)
        fija = raiz / "Contratos Fija"
        escribir(fija / contrato / "Contrato.pdf", pdf_bytes())
        (fija / "Numero Os" / "Contrato Firmado").mkdir(parents=True)
        fac = raiz / f"Documentación Facultativa {c}"
        escribir(fac / "Estatuto.pdf", pdf_bytes(3))
        escribir(fac / "Copia SDS.url", contenido_url(u_sds))
        escribir(fac / "Poder.url", contenido_url(u_poder))
        escribir(fac / "foto dni.jpg", jpg_bytes())
        escribir(fac / "nota.msg", b"mensaje outlook")
        return raiz

    def excel(self, **kw) -> Path:
        return escribir_excel(self.origen, self.cfg.excel_referencia, **kw)

    def descargador(self, estado: Estado) -> Descargador:
        return Descargador(self.cfg, estado, session=self.sesion)


@pytest.fixture
def entorno(tmp_path) -> Entorno:
    return Entorno(tmp_path)


def huella_arbol(raiz: Path) -> dict[str, str]:
    res = {}
    for r, dirs, files in os.walk(raiz):
        for d in dirs:
            res[str(Path(r, d).relative_to(raiz)) + "/"] = "dir"
        for f in files:
            p = Path(r, f)
            res[str(p.relative_to(raiz))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return res
