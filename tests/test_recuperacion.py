import csv

from migracion_digicorp import recuperacion as RC
from migracion_digicorp.estado import Estado
from migracion_digicorp.descarga import clave_url

from .conftest import CUIT_A, contenido_url, escribir, jpg_bytes, pdf_bytes, url_s1


def test_destino_espejo(tmp_path):
    origen = tmp_path / "C" / "Users" / "x" / "AMX Argentina S.A"
    assert RC.destino_espejo(origen, tmp_path / "E") == tmp_path / "E" / "AMX Argentina S.A"


def test_copiar_carpeta_reanudable(tmp_path):
    origen = tmp_path / "AMX Argentina S.A"
    escribir(origen / "Casos" / "2024" / "00123456_DNI.pdf", pdf_bytes())
    escribir(origen / "otro.txt", b"hola")
    destino = RC.destino_espejo(origen, tmp_path / "E")
    man = tmp_path / "man.csv"
    r = RC.copiar_carpeta(origen, destino, man, 0)
    assert r.copiados == 2 and (destino / "Casos" / "2024" / "00123456_DNI.pdf").read_bytes() == \
        (origen / "Casos" / "2024" / "00123456_DNI.pdf").read_bytes()
    r2 = RC.copiar_carpeta(origen, destino, man, 0)
    assert r2.copiados == 0 and r2.ya_copiados == 2
    assert (origen / "otro.txt").read_bytes() == b"hola"  # origen intacto
    assert not list(destino.rglob("*.part"))


def test_sql_y_casos(tmp_path):
    probs = [RC.SdsProblema(CUIT_A, "29067459SEC", "SDS.url", "POSTERGADO"),
             RC.SdsProblema(CUIT_A, "29067460TAE", "DNI.url", "URL_CORTADA_O_INVALIDA")]
    _, sql = RC.escribir_sds_y_sql(probs, tmp_path, por_consulta=1)
    texto = sql.read_text()
    assert texto.count("SELECT SUBJECT, CASE_ID") == 2 and "UPPER(SUBJECT) LIKE '%29067459SEC%'" in texto
    assert "REGEXP_LIKE" in (tmp_path / "consulta_case_id_unica.sql").read_text()
    casos = tmp_path / "casos.csv"
    casos.write_text("SUBJECT;CASE_ID\nAlta SDS 29067459sec cliente;00123456\nOtra 29067460TAE;5004000000AbCdE\n"
                     "Sin relacion;99999999\n", encoding="utf-8")
    leidos = RC.leer_casos(casos)
    assert ("Alta SDS 29067459sec cliente", "00123456") in leidos

    carpeta = tmp_path / "copia"
    escribir(carpeta / "a" / "Caso 00123456 - DNI.pdf", pdf_bytes())
    escribir(carpeta / "a" / "00123456_SDS.jpg", jpg_bytes())
    escribir(carpeta / "b" / "001234567.pdf", pdf_bytes())                 # otro número: no coincide
    escribir(carpeta / "b" / "5004000000abcde_roto.pdf", pdf_bytes()[:80])  # corrupto
    escribir(carpeta / "b" / "99999999.pdf", pdf_bytes())                  # caso ajeno
    b = RC.buscar_casos(probs, leidos, carpeta)
    assert b.sds_casos == {"29067459SEC": {"00123456"}, "29067460TAE": {"5004000000AbCdE"}}
    assert sorted(p.name for p, _, _ in b.archivos_caso["00123456"]) == ["00123456_SDS.jpg", "Caso 00123456 - DNI.pdf"]
    assert [r for _, r, _ in b.archivos_caso["5004000000AbCdE"]] == ["CORRUPTO"]
    tot = RC.escribir_reporte(probs, b, tmp_path / "r.xlsx")
    assert tot == {"CON_ARCHIVOS_VALIDOS": 1, "SOLO_CORRUPTOS": 1}


def test_sds_problematicas(entorno):
    raiz = entorno.cliente_normal(CUIT_A)  # SDS 29067459SEC con enlaces s1gateway
    escribir(raiz / "Documentación Móvil" / "29067459SEC" / "SPN.url", contenido_url(url_s1("x")[:-10]))
    entorno.excel()
    entorno.cfg.dominios_postergados = ("cdnva.s1gateway.com",)
    est = Estado(entorno.cfg.archivo_estado)
    est.registrar_cliente(CUIT_A, "REVISION_MANUAL", "x")
    est.cerrar()
    probs = RC.sds_problematicas(entorno.cfg)
    motivos = {(p.sds, p.archivo, p.motivo) for p in probs}
    assert ("29067459SEC", "SDS.url", "POSTERGADO") in motivos
    assert ("29067459SEC", "SPN.url", "URL_CORTADA_O_INVALIDA") in motivos


def test_copiar_solo_casos(tmp_path):
    origen = tmp_path / "AMX Argentina S.A"
    escribir(origen / "x" / "Caso 00123456.pdf", pdf_bytes())
    escribir(origen / "x" / "Caso 99999999.pdf", pdf_bytes())
    probs = [RC.SdsProblema(CUIT_A, "29067459SEC", "SDS.url", "POSTERGADO")]
    filtro, n = RC.filtro_por_casos(probs, [("SDS 29067459sec", "00123456"), ("otra", "99999999")])
    assert n == 1
    destino = RC.destino_espejo(origen, tmp_path / "E")
    r = RC.copiar_carpeta(origen, destino, tmp_path / "m.csv", 0, filtro=filtro)
    assert r.copiados == 1 and [p.name for p in destino.rglob("*.pdf")] == ["Caso 00123456.pdf"]


def test_consultar_casos_con_conexion_falsa(entorno, tmp_path):
    class Cursor:
        arraysize = 0
        def execute(self, sql):
            assert "REGEXP_LIKE" in sql
            self.filas = [("SDS 29067459sec\nalta", 123), (None, "5004"), ("x", None)]
        def fetchmany(self, n):
            lote, self.filas = self.filas[:2], self.filas[2:]
            return lote

    class Conexion:
        cerrada = False
        def cursor(self):
            return Cursor()
        def close(self):
            Conexion.cerrada = True

    salida = tmp_path / "casos.csv"
    n = RC.consultar_casos(entorno.cfg, salida, conectar=Conexion, por_lote=2)
    assert n == 3 and Conexion.cerrada
    assert RC.leer_casos(salida)[:2] == [("SDS 29067459sec alta", "123"), ("", "5004")]


def test_leer_password(tmp_path):
    import pytest
    p = tmp_path / "Pswd.txt"
    p.write_text("secreto\notra linea\n", encoding="utf-8")
    assert RC.leer_password(p) == "secreto"
    with pytest.raises(FileNotFoundError):
        RC.leer_password(tmp_path / "no.txt")


def test_muestra_coincidencias(tmp_path):
    origen = tmp_path / "AMX Argentina S.A"
    for i in range(5):
        escribir(origen / "x" / f"Caso 0012345{i}.pdf", pdf_bytes())
    probs = [RC.SdsProblema(CUIT_A, "29067459SEC", "SDS.url", "POSTERGADO")]
    casos = [("SDS 29067459sec", f"0012345{i}") for i in range(5)]
    res = RC.muestra_coincidencias(origen, probs, casos, 2)
    assert len(res) == 2 and res[0][2] == {"29067459SEC"} and res[0][3] == [(CUIT_A, "29067459SEC")]


def test_case_id_sin_primer_digito(tmp_path):
    casos = tmp_path / "casos.csv"
    casos.write_text("SUBJECT;CASE_ID\nSDS 29067459SEC;500123456\nx;7\n", encoding="utf-8")
    assert RC.leer_casos(casos, quitar_inicio=1) == [("SDS 29067459SEC", "00123456")]
    assert RC.leer_casos(casos)[0][1] == "500123456"


def test_copia_se_detiene_ante_errores_seguidos(tmp_path, monkeypatch):
    origen = tmp_path / "AMX Argentina S.A"
    for i in range(10):
        escribir(origen / f"{i}.txt", b"x")
    def falla(a, b):
        raise OSError(362, "El proveedor de archivos en la nube no se está ejecutando")
    monkeypatch.setattr(RC, "copiar_verificado", falla)
    r = RC.copiar_carpeta(origen, RC.destino_espejo(origen, tmp_path / "E"), tmp_path / "m.csv", 0,
                          errores_seguidos_max=3)
    assert r.detenido and r.errores == 3 and "nube" in r.motivo_detencion


def test_ejemplos_prioriza_cortados(tmp_path):
    probs = [RC.SdsProblema(CUIT_A, "29000001SEC", "SDS.url", "POSTERGADO"),
             RC.SdsProblema(CUIT_A, "29000002SEC", "DNI.url", "URL_CORTADA_O_INVALIDA"),
             RC.SdsProblema(CUIT_A, "29000003SEC", "DNI.url", "URL_CORTADA_O_INVALIDA")]
    casos = [("Alta 29000001sec", "512345678"), ("Alta 29000002SEC", "587654321")]
    ej = RC.ejemplos(probs, casos, 2, 1, tmp_path / "Raw Data")
    assert [e["sds"] for e in ej] == ["29000002SEC", "29000001SEC"]  # 29000003SEC no tiene caso
    assert ej[0]["casos"][0][1:] == ("587654321", "87654321")
    assert ej[0]["archivos"][0].endswith("29000002SEC" + __import__("os").sep + "DNI.url")


class _CursorSds:
    description = [("NUMERO_CASO",), ("ASUNTO",)]
    datos = {"%29000001SEC%": [("8597420", "Alta 29000001SEC")], "%29000002sec%": [("1234567", "x 29000002sec")]}

    def execute(self, sql, params):
        assert sql == RC.CONSULTA_POR_SDS
        self.filas = self.datos.get(params[0], [])

    def fetchall(self):
        return self.filas


class _ConSds:
    def cursor(self):
        return _CursorSds()

    def close(self):
        pass


def test_consultar_por_sds_reanudable_y_busqueda_en_original(entorno, tmp_path):
    carpeta = tmp_path / "rec"
    sds = ["29000001SEC", "29000002SEC", "29000003SEC"]
    r = RC.consultar_por_sds(entorno.cfg, sds[:2], carpeta, conectar=_ConSds)
    assert r["consultadas_ahora"] == 2 and r["con_caso_ahora"] == 2
    r = RC.consultar_por_sds(entorno.cfg, sds, carpeta, conectar=_ConSds, hilos=2)
    assert r["consultadas_ahora"] == 1 and r["ya_consultadas"] == 2  # reanuda sin repetir
    casos = RC.leer_casos_por_sds(carpeta / "casos_por_sds.csv")
    assert casos == {"29000001SEC": ["8597420"], "29000002SEC": ["1234567"], "29000003SEC": []}

    original = tmp_path / "AMX Argentina S.A"
    escribir(original / "a" / "Caso 597420 DNI.pdf", pdf_bytes())     # sin el primer dígito
    escribir(original / "b" / "1234567-SDS.pdf", pdf_bytes())         # número completo
    escribir(original / "b" / "85974201.pdf", pdf_bytes())            # otro número: no cuenta
    tot, filas = RC.buscar_en_original(casos, original, 1)
    assert tot == {"sds_consultadas": 3, "sds_con_caso": 2, "con_archivo_numero_completo": 1,
                   "con_archivo_sin_primer_digito": 1, "con_archivo_alguna_forma": 2}


def test_elegir_columna_caso():
    import pytest
    assert RC.elegir_columna_caso(["ID", "Numero_Caso", "ASUNTO"]) == "NUMERO_CASO"
    assert RC.elegir_columna_caso(["ID", "NRO_DE_CASO_S1", "ASUNTO"]) == "NRO_DE_CASO_S1"
    assert RC.elegir_columna_caso(["CASO_ID", "FECHA", "ASUNTO", "ESTADO_CASO", "CASO_SALIENTE"]) == "CASO_ID"
    with pytest.raises(ValueError, match="--columna-caso"):
        RC.elegir_columna_caso(["ID", "ASUNTO", "FECHA"])


def test_columna_ambigua_no_deja_hilos_con_columna_vacia(entorno, tmp_path):
    import pytest

    class Cur(_CursorSds):
        description = [("ID",), ("ESTADO_CASO",), ("CASO_SALIENTE",), ("ASUNTO",)]
        datos = {}

    class Con(_ConSds):
        def cursor(self):
            return Cur()
    with pytest.raises(ValueError, match="--columna-caso"):
        RC.consultar_por_sds(entorno.cfg, ["29000001SEC", "29000002SEC", "29000003SEC"], tmp_path, conectar=Con,
                             hilos=2)


def _cliente_con_sds_rota(entorno):
    """Cliente con una SDS de 2 enlaces: uno truncado y otro de s1gateway (postergado)."""
    raiz = entorno.cliente_normal(CUIT_A, sds="29387442SEC")
    sds = raiz / "Documentación Móvil" / "29387442SEC"
    escribir(sds / "SPN.url", contenido_url(url_s1("t")[:-10]))  # cortado
    for f in ("DNI 1.url",):
        (sds / f).unlink()  # queda SDS.url (s1gateway) + SPN.url (cortado) + desktop.ini
    # Un enlace propio de Facultativa (no es de una SDS) seguiría postergando al cliente: se quita.
    (raiz / f"Documentación Facultativa {CUIT_A}" / "Poder.url").unlink()
    entorno.excel()
    entorno.cfg.dominios_postergados = ("cdnva.s1gateway.com",)
    return raiz


def test_recuperar_y_migrar_con_documentos_recuperados(entorno, tmp_path):
    from .test_migracion import archivos_destino, correr
    _cliente_con_sds_rota(entorno)
    original = tmp_path / "AMX Argentina S.A"
    escribir(original / "Casos" / "8597420 - SDS firmada.pdf", pdf_bytes(2))
    escribir(original / "Casos" / "8597420 - DNI.jpg", jpg_bytes())
    escribir(original / "Casos" / "8597420 planilla.xlsx", b"PK\x03\x04")       # no es documentación
    escribir(original / "Otros" / "85974201.pdf", pdf_bytes())                   # otro número
    casos = {"29387442SEC": ["8597420"]}
    probs = [RC.SdsProblema(CUIT_A, "29387442SEC", "SPN.url", "URL_CORTADA_O_INVALIDA"),
             RC.SdsProblema(CUIT_A, "29387442SEC", "SDS.url", "POSTERGADO")]
    sel = RC.clientes_candidatos(probs, casos, 4)
    assert sel == {CUIT_A: {"29387442SEC"}}
    carpeta = entorno.cfg.trabajo / "recuperacion"
    filas = RC.recuperar(sel, casos, original, entorno.origen, carpeta / "archivos", 0)
    resumen = RC.resumir_recuperacion(filas)
    assert resumen[0]["enlaces"] == 2 and resumen[0]["validos"] == 2 and resumen[0]["estado"] == "RECUPERADA"
    archivo, _ = RC.guardar_recuperacion(filas, carpeta)

    entorno.cfg.archivo_recuperados = str(archivo)
    res = correr(entorno)
    assert res.publicados == 1, res.contadores
    archivos = archivos_destino(entorno, CUIT_A)
    assert "Documentacion Movil/29387442SEC/8597420 - SDS firmada.pdf" in archivos
    assert "Documentacion Movil/29387442SEC/8597420 - DNI.pdf" in archivos      # jpg convertido
    assert not any(a.endswith("SDS.pdf") or a.endswith("SPN.pdf") for a in archivos)
    assert entorno.sesion.llamadas == []  # no se descargó ningún enlace de esa SDS


def test_recuperacion_insuficiente_no_migra(entorno, tmp_path):
    from .test_migracion import correr, estado_de
    _cliente_con_sds_rota(entorno)
    original = tmp_path / "AMX Argentina S.A"
    escribir(original / "8597420 - unico.pdf", pdf_bytes())  # 1 archivo para 2 enlaces
    casos = {"29387442SEC": ["8597420"]}
    probs = [RC.SdsProblema(CUIT_A, "29387442SEC", "SPN.url", "URL_CORTADA_O_INVALIDA")]
    carpeta = entorno.cfg.trabajo / "recuperacion"
    filas = RC.recuperar(RC.clientes_candidatos(probs, casos), casos, original, entorno.origen, carpeta / "archivos", 0)
    assert RC.resumir_recuperacion(filas)[0]["estado"] == "INSUFICIENTE"
    archivo, _ = RC.guardar_recuperacion(filas, carpeta)
    entorno.cfg.archivo_recuperados = str(archivo)
    RC._CACHE_RECUPERADOS.clear()
    assert correr(entorno).publicados == 0
    assert estado_de(entorno, CUIT_A) == "REVISION_MANUAL"
