import pytest

from migracion_digicorp import reglas as R

from .conftest import CUIT_A


def test_cuit_valido():
    assert R.validar_cuit(CUIT_A) == (True, "")


@pytest.mark.parametrize("nombre", ["2012345678", "201234567890", "23324234233(1)", "30537926240-Docu",
                                    "29998793SEC", "Contratos Fija"])
def test_cuit_formato_invalido(nombre):
    assert R.validar_cuit(nombre)[0] is False


def test_cuit_digito_verificador():
    malo = CUIT_A[:10] + str((int(CUIT_A[10]) + 1) % 10)
    assert R.validar_cuit(malo)[0] is False
    assert R.validar_cuit(malo, validar_dv=False)[0] is True
    assert R.validar_cuit("11111111111")[0] is False


@pytest.mark.parametrize("nombre,original", [
    ("Copia SDS.url", "SDS.url"),
    ("Copia de DNI frente.url", "DNI frente.url"),
    ("copia sds firmada .url", "sds firmada .url"),
    ("Estatuto - copia.url", "Estatuto.url"),
    ("Estatuto - copia (2).url", "Estatuto.url"),
    ("SDS.url", None),
    ("ESTATUTO (1).pdf", None),
    ("Copiadora.url", None),
])
def test_copias(nombre, original):
    assert R.nombre_original_de_copia(nombre) == original


def test_categorias():
    assert R.categoria_cliente("Documentación Móvil") == ("movil", None)
    assert R.categoria_cliente("Documentacion  movil") == ("movil", None)
    assert R.categoria_cliente("Contratos Fija") == ("contratos", None)
    assert R.categoria_cliente(f"Documentación Facultativa {CUIT_A}") == ("facultativa", CUIT_A)
    assert R.categoria_cliente("Documentación Facultativa") == ("facultativa", None)
    assert R.categoria_cliente("Excepciones 2023")[0] == "desconocida"


def test_inferencias():
    assert R.inferir_sds("SDS 29067459SEC.url") == "29067459SEC"
    assert R.inferir_sds("SDS.url") is None
    assert R.inferir_sds("29067459SEC y 29067460SEC.pdf") is None
    assert R.inferir_contrato("60374809.pdf") == "60374809"
    assert R.inferir_contrato("OS 60204441.pdf") == "60204441"
    assert R.inferir_contrato("Summary 60211834.pdf") is None


def test_nombres():
    assert R.nombre_pdf("SDS FIRMADA.url") == "SDS FIRMADA.pdf"
    assert R.nombre_con_sufijo("DNI.pdf", 2) == "DNI (2).pdf"
    assert R.extension("DNI") == ""
    assert R.es_ignorado("Desktop.ini")
