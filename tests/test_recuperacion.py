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
