"""Fase 2: diagnóstico. Compara la estructura real con la esperada y arma el plan del cliente.

No accede a la red ni modifica nada. Cualquier situación no resoluble con reglas
deterministas genera una incidencia bloqueante (ver docs/REGLAS.md).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from . import reglas as R
from .config import Config
from .conversion import es_convertible
from .descarga import leer_url, validar_url
from .inventario import Referencia, comparar_con_referencia
from .modelo import (DEST_FACULTATIVA, DEST_FIJA, DEST_MOVIL, Accion, Entrada, Plan,
                     TipoAccion)

MAX_DIFERENCIAS_EXCEL = 20


class _Arbol:
    """Acceso por carpeta a las entradas del cliente."""

    def __init__(self, entradas: list[Entrada]):
        self.hijos: dict[tuple[str, ...], list[Entrada]] = defaultdict(list)
        for e in entradas:
            self.hijos[e.rel[:-1]].append(e)

    def de(self, rel: tuple[str, ...]) -> list[Entrada]:
        return self.hijos.get(rel, [])

    def archivos_bajo(self, rel: tuple[str, ...]) -> list[Entrada]:
        res = []
        for e in self.de(rel):
            if e.es_dir:
                res.extend(self.archivos_bajo(e.rel))
            else:
                res.append(e)
        return res


def diagnosticar(cuit: str, ruta: Path, entradas: list[Entrada], ref: Referencia, cfg: Config,
                 soffice: str | None) -> Plan:
    plan = Plan(cuit=cuit, ruta_origen=ruta)
    plan.carpetas = sum(1 for e in entradas if e.es_dir)
    plan.archivos = sum(1 for e in entradas if not e.es_dir)
    arbol = _Arbol(entradas)

    # 1. CUIT
    ok, motivo = R.validar_cuit(cuit, cfg.validar_digito_verificador)
    plan.cuit_valido = ok
    if not ok:
        plan.incidir(ruta, "CUIT_INVALIDO", motivo)

    # 2. Contraste con el Excel
    esperado = ref.arbol.get(cuit)
    if esperado is None:
        plan.incidir(ruta, "CLIENTE_NO_EN_EXCEL", "La carpeta del cliente no figura en el Excel de referencia")
    else:
        solo_disco, solo_excel = comparar_con_referencia(entradas, esperado)
        for rel in solo_disco[:MAX_DIFERENCIAS_EXCEL]:
            plan.incidir(ruta.joinpath(*rel), "DIFERENCIA_EXCEL", "Existe en disco pero no en el Excel")
        for rel in solo_excel[:MAX_DIFERENCIAS_EXCEL]:
            plan.incidir(ruta.joinpath(*rel), "DIFERENCIA_EXCEL", "Figura en el Excel pero no existe en disco")
        resto = len(solo_disco) + len(solo_excel) - 2 * MAX_DIFERENCIAS_EXCEL
        if resto > 0:
            plan.incidir(ruta, "DIFERENCIA_EXCEL", f"... y {resto} diferencias más con el Excel")

    # 3. Enlaces simbólicos: no son documentación verificable
    for e in entradas:
        if e.tamano < 0:
            plan.incidir(e.ruta, "ENLACE_SIMBOLICO", "Enlace simbólico o junction dentro del cliente")

    # 4. Categorías de nivel 2
    categorias: dict[str, list[Entrada]] = defaultdict(list)
    for e in arbol.de(()):
        if not e.es_dir:
            if R.es_ignorado(e.nombre):
                _ignorar(plan, e)
            else:
                _archivo_suelto_raiz(plan, e)
            continue
        cat, sufijo = R.categoria_cliente(e.nombre)
        if cat == "desconocida":
            plan.incidir(e.ruta, "CARPETA_NO_RECONOCIDA",
                         f"Carpeta '{e.nombre}' no corresponde a ninguna categoría documental")
            continue
        if cat == "facultativa" and sufijo != cuit:
            if sufijo is None:
                plan.incidir(e.ruta, "FACULTATIVA_SIN_CUIT",
                             "La carpeta de Documentación Facultativa no indica el CUIT")
            else:
                plan.incidir(e.ruta, "FACULTATIVA_CUIT_DISTINTO",
                             f"La carpeta indica '{sufijo}', distinto del CUIT del cliente")
        categorias[cat].append(e)
    for cat, lista in categorias.items():
        if len(lista) > 1:
            plan.incidir(ruta, "CATEGORIA_DUPLICADA",
                         f"Hay {len(lista)} carpetas de la categoría '{cat}': "
                         + ", ".join(x.nombre for x in lista))

    movil = categorias.get("movil", [])
    contratos = categorias.get("contratos", [])
    facultativa = categorias.get("facultativa", [])
    for e in movil:
        _procesar_contenedor(plan, arbol, e, cfg, soffice, ref, tipo="movil")
    for e in contratos:
        _procesar_contenedor(plan, arbol, e, cfg, soffice, ref, tipo="contratos")
    urls_movil = _urls_de(arbol, movil)
    for e in facultativa:
        _procesar_facultativa(plan, arbol, e, cfg, soffice, urls_movil)

    # Solo se informa cuando la falta de documentos no se explica por otro bloqueo (p. ej. URL inválidas).
    hay_archivos = any(not e.es_dir and not R.es_ignorado(e.nombre) for e in entradas)
    migrables = any(a.tipo in (TipoAccion.COPIAR, TipoAccion.DESCARGAR, TipoAccion.CONVERTIR) for a in plan.acciones)
    if not hay_archivos:
        plan.incidir(ruta, "CLIENTE_SIN_DOCUMENTACION", "El cliente no tiene ningún archivo")
    elif not migrables and not plan.bloqueado:
        plan.incidir(ruta, "CLIENTE_SIN_DOCUMENTACION", "El cliente no tiene ningún documento migrable")
    _detectar_colisiones(plan)
    return plan


# ---------------------------------------------------------------------------
# Archivos individuales
# ---------------------------------------------------------------------------

def _ignorar(plan: Plan, e: Entrada) -> None:
    plan.acciones.append(Accion(TipoAccion.IGNORAR, e.ruta, e.rel, detalle="Archivo de sistema"))
    plan.incidir(e.ruta, "ARCHIVO_SISTEMA_IGNORADO", f"'{e.nombre}' no se migra",
                 manual=False, bloqueante=False, accion="Ignorar", resultado="Excluido")


def _planificar_archivo(plan: Plan, e: Entrada, destino_dir: tuple[str, ...], cfg: Config,
                        soffice: str | None) -> None:
    """Decide qué hacer con un archivo que ya tiene carpeta de destino asignada."""
    if R.es_ignorado(e.nombre):
        _ignorar(plan, e)
        return
    ext = R.extension(e.nombre)
    if ext == "":
        plan.incidir(e.ruta, "ARCHIVO_SIN_EXTENSION", "Archivo sin extensión: no se puede determinar su tipo")
        return
    if ext in R.EXT_EXCEL:
        plan.incidir(e.ruta, "PLANILLA_EXCEL", f"Planilla '{e.nombre}': las planillas (Excel/CSV) no son documentación y no se migran")
        return
    if ext in R.EXT_BLOQUEANTES:
        plan.incidir(e.ruta, "ARCHIVO_INCOMPLETO_O_ACCESO_DIRECTO",
                     f"Extensión '{ext}' (descarga parcial o acceso directo)")
        return
    if ext not in R.EXT_PERMITIDAS:
        plan.incidir(e.ruta, "EXTENSION_NO_RECONOCIDA", f"Extensión '{ext}' no contemplada en las reglas")
        return
    if e.tamano == 0:
        plan.incidir(e.ruta, "ARCHIVO_VACIO", "Archivo de 0 bytes")
        return
    if ext == R.EXT_URL:
        url = leer_url(e.ruta)
        motivo = validar_url(url, cfg)
        if motivo:
            plan.incidir(e.ruta, "URL_INVALIDA", motivo, accion="Validar enlace", resultado="Rechazado")
            return
        plan.acciones.append(Accion(TipoAccion.DESCARGAR, e.ruta, e.rel, destino_dir + (R.nombre_pdf(e.nombre),),
                                    url=url))
    elif ext == R.EXT_PDF:
        plan.acciones.append(Accion(TipoAccion.COPIAR, e.ruta, e.rel, destino_dir + (e.nombre,)))
    elif es_convertible(e.nombre, soffice):
        plan.acciones.append(Accion(TipoAccion.CONVERTIR, e.ruta, e.rel, destino_dir + (R.nombre_pdf(e.nombre),),
                                    detalle=f"Convertir {ext} a PDF; si falla, copiar original"))
    else:
        plan.acciones.append(Accion(TipoAccion.COPIAR, e.ruta, e.rel, destino_dir + (e.nombre,),
                                    detalle=f"Formato {ext} migrado sin conversión"))


def _archivo_suelto_raiz(plan: Plan, e: Entrada) -> None:
    # En la raíz del cliente no hay evidencia suficiente para decidir categoría.
    plan.incidir(e.ruta, "ARCHIVO_SUELTO", "Archivo suelto en la raíz del cliente")


# ---------------------------------------------------------------------------
# Documentación Móvil / Contratos Fija
# ---------------------------------------------------------------------------

def _procesar_contenedor(plan: Plan, arbol: _Arbol, cont: Entrada, cfg: Config, soffice: str | None,
                         ref: Referencia, tipo: str) -> None:
    if tipo == "movil":
        patron, placeholders, base = R.RE_SDS, R.PLACEHOLDERS_MOVIL, (DEST_MOVIL,)
        inferir, etiqueta = R.inferir_sds, "SDS"
        otros = ref.clientes_de_sds
    else:
        patron, placeholders, base = R.RE_CONTRATO, R.PLACEHOLDERS_CONTRATOS, (DEST_FIJA,)
        inferir, etiqueta = R.inferir_contrato, "contrato"
        otros = ref.clientes_de_contrato

    for e in arbol.de(cont.rel):
        if not e.es_dir:
            if R.es_ignorado(e.nombre):
                _ignorar(plan, e)
                continue
            ident = inferir(e.nombre)
            if ident is None:
                plan.incidir(e.ruta, "ARCHIVO_SUELTO",
                             f"Archivo suelto en '{cont.nombre}' sin {etiqueta} inferible por su nombre")
                continue
            plan.incidir(e.ruta, "ARCHIVO_SUELTO_REUBICADO",
                         f"Se ubica en la carpeta de {etiqueta} '{ident}' inferida del nombre del archivo",
                         manual=False, bloqueante=False, accion=f"Crear/usar carpeta {ident}", resultado="Planificado")
            _verificar_unicidad(plan, e, ident, otros, etiqueta)
            _planificar_archivo(plan, e, base + (ident,), cfg, soffice)
            continue

        if R.normalizar(e.nombre) in placeholders:
            con_contenido = [a for a in arbol.archivos_bajo(e.rel) if not R.es_ignorado(a.nombre)]
            if con_contenido:
                plan.incidir(e.ruta, "PLACEHOLDER_CON_CONTENIDO",
                             f"La carpeta modelo '{e.nombre}' contiene {len(con_contenido)} archivo(s); "
                             "debe renombrarse manualmente")
            else:
                plan.incidir(e.ruta, "PLACEHOLDER_VACIO", f"Carpeta modelo '{e.nombre}' vacía: no se migra",
                             manual=False, bloqueante=False, accion="Omitir", resultado="Excluida")
            continue

        if not patron.fullmatch(e.nombre):
            plan.incidir(e.ruta, f"NOMBRE_{etiqueta.upper()}_INVALIDO",
                         f"'{e.nombre}' no cumple el formato de {etiqueta}")
            continue

        _verificar_unicidad(plan, e, e.nombre, otros, etiqueta)
        contenido = arbol.de(e.rel)
        subcarpetas = [x for x in contenido if x.es_dir]
        for s in subcarpetas:
            plan.incidir(s.ruta, "SUBCARPETA_INESPERADA",
                         f"Subcarpeta dentro de la carpeta de {etiqueta} '{e.nombre}': pertenencia ambigua")
        documentos = [x for x in contenido if not x.es_dir and not R.es_ignorado(x.nombre)]
        if not documentos and not subcarpetas:
            plan.incidir(e.ruta, f"{etiqueta.upper()}_VACIA", f"La carpeta de {etiqueta} '{e.nombre}' no tiene documentos")
        for x in contenido:
            if not x.es_dir:
                _planificar_archivo(plan, x, base + (e.nombre,), cfg, soffice)


def _verificar_unicidad(plan: Plan, e: Entrada, ident: str, otros, etiqueta: str) -> None:
    ajenos = sorted(otros(ident) - {plan.cuit})
    if ajenos:
        plan.incidir(e.ruta, f"{etiqueta.upper()}_EN_VARIOS_CLIENTES",
                     f"El {etiqueta} '{ident}' también aparece en: {', '.join(ajenos[:5])}")


# ---------------------------------------------------------------------------
# Documentación Facultativa
# ---------------------------------------------------------------------------

def _urls_de(arbol: _Arbol, contenedores: list[Entrada]) -> dict[str, list[tuple[Entrada, str | None]]]:
    """Archivos .url de Móvil indexados por nombre normalizado -> [(entrada, url)]."""
    res: dict[str, list[tuple[Entrada, str | None]]] = defaultdict(list)
    for c in contenedores:
        for a in arbol.archivos_bajo(c.rel):
            if R.extension(a.nombre) == R.EXT_URL:
                res[R.normalizar(a.nombre)].append((a, leer_url(a.ruta)))
    return res


def _procesar_facultativa(plan: Plan, arbol: _Arbol, cont: Entrada, cfg: Config, soffice: str | None,
                          urls_movil: dict[str, list[tuple[Entrada, str | None]]]) -> None:
    todas_las_urls_movil = {u for lst in urls_movil.values() for _, u in lst if u}
    for e in arbol.de(cont.rel):
        if e.es_dir:
            plan.incidir(e.ruta, "SUBCARPETA_INESPERADA",
                         f"Subcarpeta '{e.nombre}' dentro de Documentación Facultativa")
            continue
        if R.extension(e.nombre) != R.EXT_URL:
            _planificar_archivo(plan, e, (DEST_FIJA, DEST_FACULTATIVA), cfg, soffice)
            continue

        original = R.nombre_original_de_copia(e.nombre)
        if original is None:
            _planificar_archivo(plan, e, (DEST_MOVIL, DEST_FACULTATIVA), cfg, soffice)
            continue

        # Regla confirmada: "Copia X" se genera a partir de X cargado en Documentación Móvil, por lo
        # que es un duplicado aunque apunte a otro destino (p. ej. SharePoint) o no tenga URL legible.
        # Se excluye si el original X existe en Móvil o si su enlace es idéntico a uno de Móvil.
        url = leer_url(e.ruta)
        candidatos = urls_movil.get(R.normalizar(original), [])
        if candidatos:
            ref = next((c for c, u in candidatos if url and u == url), candidatos[0][0])
            criterio = "mismo nombre y mismo enlace" if url and any(u == url for _, u in candidatos) \
                else f"mismo nombre (enlace de la copia: {urlsplit(url).hostname if url else 'ilegible'})"
        elif url and url in todas_las_urls_movil:
            ref = next(c for lst in urls_movil.values() for c, u in lst if u == url)
            criterio = "mismo enlace"
        else:
            plan.incidir(e.ruta, "COPIA_SIN_ORIGINAL",
                         f"Copia de '{original}', pero no existe ese archivo ni el mismo enlace en Documentación "
                         "Móvil del cliente: no puede confirmarse que el documento esté representado")
            continue
        plan.acciones.append(Accion(TipoAccion.EXCLUIR_DUPLICADO, e.ruta, e.rel, url=url,
                                    detalle=f"Copia de {ref.ruta} ({criterio})"))
        plan.incidir(e.ruta, "DUPLICADO_EXCLUIDO", f"Copia de '{ref.ruta}' ({criterio})", manual=False,
                     bloqueante=False, accion="Excluir copia",
                     resultado="Se conserva el original en Documentación Móvil")


# ---------------------------------------------------------------------------
# Colisiones de nombres
# ---------------------------------------------------------------------------

def _detectar_colisiones(plan: Plan) -> None:
    """Las colisiones se resuelven en la preparación con sufijo ' (n)'; aquí solo se informan."""
    destinos: dict[tuple[str, ...], list[Accion]] = defaultdict(list)
    for a in plan.acciones:
        if a.destino_rel:
            clave = tuple(p.casefold() for p in a.destino_rel)
            destinos[clave].append(a)
    for acciones in destinos.values():
        if len(acciones) > 1:
            nombres = ", ".join("/".join(a.origen_rel) for a in acciones)
            plan.incidir(plan.ruta_origen.joinpath(*acciones[0].destino_rel[:-1]), "COLISION_NOMBRE",
                         f"Varios archivos generan '{acciones[0].destino_rel[-1]}': {nombres}",
                         manual=False, bloqueante=False,
                         accion="Renombrar con sufijo ' (n)' en orden alfabético del origen (o excluir si "
                                "el contenido es idéntico)",
                         resultado="Planificado")
