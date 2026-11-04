"""Línea de comandos. Uso: ``python -m migracion_digicorp <comando> [opciones]``."""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config import MODO_REAL, cargar_config
from .descarga import Descargador, ErrorEntorno, leer_url, validar_url
from .estado import Estado
from .orquestador import (COMANDO_DIAGNOSTICO, COMANDO_INVENTARIO, COMANDO_MIGRAR, Opciones,
                          configurar_logging, ejecutar)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migracion_digicorp", description=__doc__)
    p.add_argument("--config", type=Path, help="Archivo TOML (por defecto config.local.toml si existe)")
    sub = p.add_subparsers(dest="comando", required=True)

    def filtros(sp):
        sp.add_argument("--cuits", nargs="+", default=[], help="Procesar solo estas carpetas de cliente")
        sp.add_argument("--desde", default="", help="Procesar desde esta carpeta (orden alfabético)")
        sp.add_argument("--limite-clientes", type=int, default=0, help="Evaluar como máximo N clientes")
        sp.add_argument("--sin-plan", action="store_true", help="No incluir la hoja 'Plan de archivos'")

    filtros(sub.add_parser(COMANDO_INVENTARIO, help="Fase 1: inventario de solo lectura + contraste con Excel"))
    d = sub.add_parser(COMANDO_DIAGNOSTICO, help="Fase 2: diagnóstico sin red ni escritura")
    filtros(d)
    d.add_argument("--validar-urls", action="store_true",
                   help="Además descarga a caché y valida los enlaces (no prepara ni publica)")
    d.add_argument("--reintentar-fallidas", action="store_true")
    m = sub.add_parser(COMANDO_MIGRAR, help="Fases 3-6 (simulación salvo modo='real' + confirmación)")
    filtros(m)
    m.add_argument("--confirmar-ejecucion-real", action="store_true",
                   help="Obligatorio junto con modo='real' en la configuración")
    m.add_argument("--reintentar-fallidas", action="store_true",
                   help="Reintentar descargas que fallaron de forma permanente en ejecuciones previas")

    pu = sub.add_parser("probar-url", help="Descarga y valida archivos .url individuales (diagnóstico)")
    pu.add_argument("archivos", nargs="+", type=Path)
    sub.add_parser("estado", help="Resumen del estado persistente")

    cc = sub.add_parser("copiar-carpeta", help="Copia la carpeta de casos (OneDrive) a E: con el mismo nombre")
    cc.add_argument("--origen", type=Path, help="Carpeta a copiar (por defecto carpeta_casos)")
    cc.add_argument("--unidad", type=Path, help="Unidad/carpeta donde crear la copia (por defecto E:\\)")
    cc.add_argument("--solo-medir", action="store_true", help="Solo mostrar cantidad y tamaño, sin copiar")
    cc.add_argument("--muestra", type=int, default=0,
                    help="Con --casos: mostrar los primeros N archivos que se copiarían y terminar (no copia nada)")
    cc.add_argument("--casos", type=Path,
                    help="Copiar solo los archivos de estos CASE_ID (resultado de la consulta, CSV o XLSX); "
                         "requiere haber ejecutado 'sds-problematicas'")
    sub.add_parser("sds-problematicas", help="Lista SDS postergadas/cortadas/fallidas y genera la consulta SQL")
    cs = sub.add_parser("consultar-sds", help="Por cada SDS pendiente: SELECT * FROM CNA.ACT_S1_DETALLE_CASOS "
                                               "WHERE ASUNTO LIKE '%%SDS%%' (reanudable)")
    cs.add_argument("--columna-caso", default="", help="Columna con el número de caso (por defecto se detecta)")
    cs.add_argument("--hilos", type=int, default=2, help="Conexiones simultáneas a Oracle (por defecto 2)")
    cs.add_argument("--muestra", type=int, default=0, help="Consultar solo las primeras N SDS y mostrar el resultado")
    bo = sub.add_parser("buscar-en-original", help="Cuenta en cuántas SDS el número de caso aparece en los "
                                                   "nombres de archivo de la carpeta original (no descarga nada)")
    bo.add_argument("--carpeta", type=Path, help="Por defecto carpeta_casos (la original en C:)")
    bo.add_argument("--muestra", action="store_true",
                    help="Usar el resultado de 'consultar-sds --muestra N' en lugar del de todas las SDS")
    rs = sub.add_parser("recuperar-sds", help="Copia y valida los archivos del caso de cada SDS problemática")
    rs.add_argument("--muestra", action="store_true", help="Usar el resultado de 'consultar-sds --muestra'")
    rs.add_argument("--clientes", type=int, default=0, help="Procesar solo los primeros N clientes candidatos")
    rs.add_argument("--cuits", nargs="+", default=[], help="Procesar solo estos clientes")
    ej = sub.add_parser("ejemplos-sds", help="Muestra N SDS problemáticas con su CASE_ID, para buscarlas a mano")
    ej.add_argument("--casos", type=Path, required=True, help="CSV/XLSX de casos (SUBJECT, CASE_ID)")
    ej.add_argument("--n", type=int, default=3)
    qc = sub.add_parser("consultar-casos", help="Ejecuta la consulta de CASE_ID en Oracle y genera el CSV de casos")
    qc.add_argument("--salida", type=Path, help="Archivo CSV de salida (por defecto trabajo\\recuperacion\\casos.csv)")
    bc = sub.add_parser("buscar-casos", help="Busca en la copia los archivos de cada CASE_ID y los valida")
    bc.add_argument("--casos", type=Path, required=True, help="Resultado de la consulta (CSV o XLSX con SUBJECT y CASE_ID)")
    bc.add_argument("--carpeta", type=Path, help="Carpeta donde buscar (por defecto la copia en E:)")
    return p


def _leer_problemas(carpeta_trabajo: Path):
    from . import recuperacion as RC
    ruta_csv = carpeta_trabajo / "sds_problematicas.csv"
    if not ruta_csv.exists():
        print("Primero ejecutar 'sds-problematicas'.")
        return None
    with open(ruta_csv, encoding="utf-8-sig") as fh:
        return [RC.SdsProblema(f["CUIT"], f["SDS"], f["ARCHIVO"], f["MOTIVO"], f["DETALLE"])
                for f in csv.DictReader(fh, delimiter=";")]


def _recuperacion(cfg, args) -> int:
    from . import recuperacion as RC
    from .orquestador import Parada

    carpeta_trabajo = cfg.trabajo / "recuperacion"
    if getattr(args, "casos", None) and not args.casos.is_file():
        print(f"No existe el archivo de casos: {args.casos}\n"
              "Primero ejecutar la consulta SQL (consulta_case_id_unica.sql) y exportar el resultado "
              "(columnas SUBJECT y CASE_ID) a ese archivo.")
        return 1
    if args.comando == "copiar-carpeta":
        origen = args.origen or cfg.carpeta_casos
        destino = RC.destino_espejo(origen, args.unidad or cfg.unidad_copia_casos)
        if not origen.is_dir():
            print(f"No existe la carpeta de origen: {origen}")
            return 1
        if args.muestra:
            if not args.casos:
                print("--muestra requiere --casos")
                return 1
            problemas = _leer_problemas(carpeta_trabajo)
            if problemas is None:
                return 1
            if cfg.case_id_quitar_inicio:
                print(f"Se quita(n) {cfg.case_id_quitar_inicio} carácter(es) al inicio de cada CASE_ID de la base.")
            print(f"Buscando los primeros {args.muestra} archivos que se copiarían en {origen} "
                  "(solo nombres, no descarga nada)...")
            encontrados = RC.muestra_coincidencias(origen, problemas,
                                                    RC.leer_casos(args.casos, cfg.case_id_quitar_inicio), args.muestra)
            for ruta, casos_f, sds_f, cli in encontrados:
                print(f"\n  Archivo:  {RC._sin_prefijo(ruta)}")
                print(f"  CASE_ID:  {', '.join(sorted(casos_f))}")
                print(f"  SDS:      {', '.join(sorted(sds_f))}")
                print(f"  Clientes: {', '.join(c for c, _ in cli)}")
                print(f"  En la nube: {'sí' if RC.solo_en_la_nube(ruta) else 'no (ya está en el disco)'}")
            print(f"\nEncontrados: {len(encontrados)}" + ("" if encontrados else
                  " — ningún nombre de archivo contiene los CASE_ID de las SDS problemáticas"))
            return 0
        filtro = None
        if args.casos:
            problemas = _leer_problemas(carpeta_trabajo)
            if problemas is None:
                return 1
            filtro, n_ids = RC.filtro_por_casos(problemas, RC.leer_casos(args.casos, cfg.case_id_quitar_inicio))
            print(f"Solo archivos de {n_ids} CASE_ID de SDS problemáticas.")
        print(f"Midiendo {origen} (no descarga nada)...")
        n, total, nube = RC.medir_carpeta(origen, filtro)
        libre_destino = shutil.disk_usage(destino.anchor or str(destino.parent)).free
        libre_origen = shutil.disk_usage(origen.anchor or str(origen)).free
        print(f"  Archivos: {n} | Total: {total / 1e9:.2f} GB | Solo en la nube: {nube / 1e9:.2f} GB")
        print(f"  Copia en: {destino} | Libre en destino: {libre_destino / 1e9:.2f} GB | "
              f"Libre en {origen.anchor or 'origen'}: {libre_origen / 1e9:.2f} GB")
        if args.solo_medir:
            return 0
        if total > libre_destino:
            print("No hay espacio suficiente en el destino. No se copió nada.")
            return 1
        with Parada(cfg) as parar:
            r = RC.copiar_carpeta(origen, destino, carpeta_trabajo / "copia_manifiesto.csv",
                                  int(cfg.reserva_c_gb * 1e9), parar, filtro)
        print(f"\nCopiados: {r.copiados} ({r.bytes_copiados / 1e9:.2f} GB) | Ya estaban: {r.ya_copiados} | "
              f"Sin espacio en C: {r.sin_espacio} | Errores: {r.errores}{' | DETENIDO' if r.detenido else ''}")
        if r.motivo_detencion:
            print(f"DETENIDA: {r.motivo_detencion}\nRevisar OneDrive y volver a ejecutar el mismo comando.")
        print(f"Detalle: {carpeta_trabajo / 'copia_manifiesto.csv'}")
        return 0 if not (r.errores or r.sin_espacio) else 1

    if args.comando == "consultar-sds":
        problemas = _leer_problemas(carpeta_trabajo)
        if problemas is None:
            return 1
        sds = RC.sds_unicas(problemas)
        destino = carpeta_trabajo
        if args.muestra:
            destino = carpeta_trabajo / "muestra_sds"
            shutil.rmtree(destino, ignore_errors=True)
            sds = sds[:args.muestra]
        print(f"SDS a consultar: {len(sds)} | Conectando a {cfg.oracle_dsn} como {cfg.oracle_usuario}...")
        try:
            with Parada(cfg) as parar:
                r = RC.consultar_por_sds(cfg, sds, destino, args.columna_caso, args.hilos, parar)
        except ImportError:
            print("Falta el paquete de Oracle: python -m pip install -r requirements.txt")
            return 1
        except Exception as exc:
            import logging
            import traceback
            logging.getLogger(__name__).debug("Detalle del error:\n%s", traceback.format_exc())
            ultimo = traceback.extract_tb(exc.__traceback__)[-1]
            print(f"Error ({type(exc).__name__}): {str(exc).splitlines()[0][:300] if str(exc) else ''}\n"
                  f"  en {Path(ultimo.filename).name}, línea {ultimo.lineno}: {ultimo.line}")
            return 1
        casos = RC.leer_casos_por_sds(destino / "casos_por_sds.csv")
        con = sum(1 for c in casos.values() if c)
        if args.muestra:
            for s_, cs_ in list(casos.items())[:20]:
                print(f"  {s_}: {', '.join(cs_) if cs_ else '(sin caso)'}")
            if len(casos) > 20:
                print(f"  ... ({len(casos) - 20} más en el archivo de resultado)")
        print(f"\nSDS consultadas en total: {len(casos)} de {len(RC.sds_unicas(problemas))} | "
              f"con número de caso: {con} | sin caso: {len(casos) - con}"
              + (f" | pendientes: {r['pendientes']} (volver a ejecutar para continuar)" if r["pendientes"] else ""))
        print(f"Resultado: {destino / 'casos_por_sds.csv'}\nDetalle (todas las columnas): "
              f"{destino / 'detalle_casos_por_sds.csv'}")
        return 0

    if args.comando == "buscar-en-original":
        ruta = (carpeta_trabajo / "muestra_sds" if args.muestra else carpeta_trabajo) / "casos_por_sds.csv"
        if not ruta.exists():
            print(f"No existe {ruta}. Primero ejecutar 'consultar-sds'" + (" --muestra N." if args.muestra else "."))
            return 1
        carpeta = args.carpeta or cfg.carpeta_casos
        print(f"Buscando números de caso en los nombres de archivo de {carpeta} (no descarga nada)...")
        tot, filas = RC.buscar_en_original(RC.leer_casos_por_sds(ruta), carpeta, cfg.case_id_quitar_inicio)
        salida = carpeta_trabajo / f"casos_en_original_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with open(salida, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["SDS", "CASOS", "ARCHIVOS_NUMERO_COMPLETO", "ARCHIVOS_SIN_PRIMER_DIGITO"])
            for s_, cs_, a, b in filas:
                w.writerow([s_, "|".join(cs_), " | ".join(a), " | ".join(b)])
        print(f"SDS consultadas:                                {tot['sds_consultadas']}")
        print(f"SDS con número de caso:                         {tot['sds_con_caso']}")
        print(f"  ...con archivo en el original (alguna forma): {tot['con_archivo_alguna_forma']}")
        print(f"      número completo:                          {tot['con_archivo_numero_completo']}")
        print(f"      sin el primer dígito:                     {tot['con_archivo_sin_primer_digito']}")
        print(f"Detalle: {salida}")
        return 0

    if args.comando == "recuperar-sds":
        problemas = _leer_problemas(carpeta_trabajo)
        if problemas is None:
            return 1
        ruta = (carpeta_trabajo / "muestra_sds" if args.muestra else carpeta_trabajo) / "casos_por_sds.csv"
        if not ruta.exists():
            print(f"No existe {ruta}. Primero ejecutar 'consultar-sds'.")
            return 1
        casos = RC.leer_casos_por_sds(ruta)
        seleccion = RC.clientes_candidatos(problemas, casos, args.clientes, set(args.cuits) or None)
        if not seleccion:
            print("Ningún cliente tiene número de caso en todas sus SDS con problema.")
            return 1
        print(f"Clientes: {len(seleccion)} | SDS: {sum(len(v) for v in seleccion.values())} | "
              f"buscando en {cfg.carpeta_casos} (solo nombres) y copiando los encontrados...")
        with Parada(cfg) as parar:
            filas = RC.recuperar(seleccion, casos, cfg.carpeta_casos, cfg.origen, carpeta_trabajo / "archivos",
                                 int(cfg.reserva_c_gb * 1e9), parar)
        archivo, resumen = RC.guardar_recuperacion(filas, carpeta_trabajo)
        por_sds = {r["sds"]: r for r in RC.resumir_recuperacion(filas)}
        ok_clientes = []
        for cuit, lista in seleccion.items():
            estados = [por_sds.get(s_, {}).get("estado", "SIN_ARCHIVOS_VALIDOS") for s_ in sorted(lista)]
            todas = all(e == RC.ESTADO_RECUPERADA for e in estados)
            ok_clientes += [cuit] if todas else []
            print(f"\nCliente {cuit}: {'TODAS SUS SDS RECUPERADAS' if todas else 'NO se puede migrar todavía'}")
            for s_ in sorted(lista):
                r = por_sds.get(s_, {})
                print(f"   {s_}: enlaces {r.get('enlaces', '?')} | archivos válidos {r.get('validos', 0)} | "
                      f"otros {r.get('otros', 0)} -> {r.get('estado', 'SIN_ARCHIVOS_VALIDOS')}")
                for f in filas:
                    if f["sds"] == s_ and f["origen"] and f["resultado"] != "VALIDO":
                        print(f"      {f['resultado']}: {Path(f['origen']).name} | {f['detalle'][:160]}")
        print(f"\nClientes con todas sus SDS recuperadas: {len(ok_clientes)} de {len(seleccion)}")
        if ok_clientes:
            print("Para migrarlos: --cuits " + " ".join(ok_clientes))
        print(f"Archivos recuperados en: {carpeta_trabajo / 'archivos'}\nDetalle: {archivo}\nResumen: {resumen}")
        return 0

    if args.comando == "ejemplos-sds":
        problemas = _leer_problemas(carpeta_trabajo)
        if problemas is None:
            return 1
        lista = RC.ejemplos(problemas, RC.leer_casos(args.casos), args.n, cfg.case_id_quitar_inicio, cfg.origen)
        for i, e in enumerate(lista, 1):
            print(f"\n===== Ejemplo {i} =====")
            print(f"CUIT:     {e['cuit']}")
            print(f"SDS:      {e['sds']}")
            print(f"Motivo:   {', '.join(e['motivos'])}")
            print("Archivos con problema:")
            for a in e["archivos"]:
                print(f"   {a}")
            print("Casos en la base:")
            for sub, base, buscado in e["casos"]:
                print(f"   CASE_ID en la base: {base}   | buscado en nombres de archivo: {buscado}")
                print(f"   SUBJECT: {sub[:150]}")
        if not lista:
            print("Ninguna SDS problemática tiene CASE_ID en el archivo de casos.")
        print(f"\nBuscar a mano en: {cfg.carpeta_casos}  (y en la copia {RC.destino_espejo(cfg.carpeta_casos, cfg.unidad_copia_casos)})")
        return 0

    if args.comando == "consultar-casos":
        salida = args.salida or carpeta_trabajo / "casos.csv"
        print(f"Conectando a {cfg.oracle_dsn} como {cfg.oracle_usuario} "
              f"(contraseña de {cfg.oracle_archivo_password})...")
        try:
            filas = RC.consultar_casos(cfg, salida)
        except ImportError:
            print("Falta el paquete de Oracle: python -m pip install -r requirements.txt")
            return 1
        except (FileNotFoundError, ValueError) as exc:
            print(exc)
            return 1
        except Exception as exc:  # errores de conexión/consulta de Oracle (DPY-xxxx / ORA-xxxxx)
            print(f"Error de Oracle: {str(exc).splitlines()[0][:300]}")
            return 1
        print(f"Filas obtenidas: {filas}\nArchivo de casos: {salida}")
        return 0

    if args.comando == "sds-problematicas":
        problemas = RC.sds_problematicas(cfg)
        ruta_csv, ruta_sql = RC.escribir_sds_y_sql(problemas, carpeta_trabajo)
        from collections import Counter
        print(f"SDS con problemas: {len({(p.cuit, p.sds) for p in problemas})} "
              f"({len(problemas)} archivos) en {len({p.cuit for p in problemas})} clientes")
        for motivo, cant in Counter(p.motivo for p in problemas).most_common():
            print(f"  {cant:7d} archivos  {motivo}")
        print(f"Listado: {ruta_csv}\nConsulta SQL por lotes: {ruta_sql}\n"
              f"Consulta SQL única (Oracle, recomendada): {ruta_sql.with_name('consulta_case_id_unica.sql')}")
        return 0

    # buscar-casos
    problemas = _leer_problemas(carpeta_trabajo)
    if problemas is None:
        return 1
    carpeta = args.carpeta or RC.destino_espejo(cfg.carpeta_casos, cfg.unidad_copia_casos)
    casos = RC.leer_casos(args.casos, cfg.case_id_quitar_inicio)
    print(f"Casos leídos: {len(casos)} | Buscando en {carpeta} ...")
    b = RC.buscar_casos(problemas, casos, carpeta)
    reporte = carpeta_trabajo / f"recuperacion_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    totales = RC.escribir_reporte(problemas, b, reporte)
    print(f"SDS con problemas: {sum(totales.values())}")
    for k in ("CON_ARCHIVOS_VALIDOS", "SOLO_CORRUPTOS", "SOLO_NO_VERIFICABLES", "SIN_ARCHIVOS", "SIN_CASE_ID"):
        print(f"  {totales.get(k, 0):7d}  {k}")
    print(f"Reporte: {reporte}")
    return 0


def _usar_certificados_del_sistema() -> None:
    """Valida HTTPS con el almacén de certificados de Windows (necesario con inspección SSL
    corporativa). No desactiva ninguna verificación."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = cargar_config(args.config)
    if not cfg.certificado_adicional:
        _usar_certificados_del_sistema()
    cfg.trabajo.mkdir(parents=True, exist_ok=True)
    configurar_logging(cfg, datetime.now().strftime("%Y%m%d_%H%M%S"))

    if args.comando in ("copiar-carpeta", "sds-problematicas", "buscar-casos", "consultar-casos", "ejemplos-sds",
                        "consultar-sds", "buscar-en-original", "recuperar-sds"):
        return _recuperacion(cfg, args)

    if args.comando == "estado":
        est = Estado(cfg.archivo_estado)
        for estado, n in est.resumen_clientes():
            print(f"{estado:25s} {n}")
        est.cerrar()
        return 0

    if args.comando == "probar-url":
        est = Estado(cfg.archivo_estado)
        d = Descargador(cfg, est)
        codigo = 0
        for archivo in args.archivos:
            if not archivo.is_file():
                print(f"[NO EXISTE] {archivo}: revisar la ruta (usar la ruta real de un archivo .url)")
                codigo = 1
                continue
            url = leer_url(archivo)
            motivo = validar_url(url, cfg)
            if motivo:
                print(f"[INVÁLIDO] {archivo}: {motivo}")
                codigo = 1
                continue
            try:
                r = d.obtener(url, reintentar_fallidas=True)
            except ErrorEntorno as exc:
                print(f"[ERROR DE ENTORNO] {archivo}: {exc}")
                codigo = 1
                break
            print(f"[{'OK' if r.ok else 'ERROR'}] {archivo}: {r.ruta if r.ok else r.error}")
            codigo |= 0 if r.ok else 1
        d.cerrar()
        est.cerrar()
        return codigo

    if args.comando == COMANDO_MIGRAR and cfg.modo == MODO_REAL and args.confirmar_ejecucion_real:
        print(f"\n*** MODO REAL ***\nSe publicarán clientes en {cfg.destino} (máximo {cfg.limite_efectivo} "
              "en esta ejecución). El origen no se modifica.")
        if input("Escriba MIGRAR para continuar: ").strip() != "MIGRAR":
            print("Cancelado. No se publicó nada.")
            return 1

    op = Opciones(
        comando=args.comando,
        confirmar_real=getattr(args, "confirmar_ejecucion_real", False),
        validar_urls=getattr(args, "validar_urls", False),
        reintentar_fallidas=getattr(args, "reintentar_fallidas", False),
        cuits=args.cuits, desde=args.desde, limite_clientes=args.limite_clientes,
        incluir_plan=not args.sin_plan,
    )
    res = ejecutar(cfg, op)
    print("\n" + "=" * 60)
    print(f"Ejecución {res.id_ejecucion} finalizada: {res.motivo_fin}")
    for k, v in sorted(res.contadores.items()):
        print(f"  {k:25s} {v}")
    print(f"  {'Publicados':25s} {res.publicados}")
    print(f"Reporte: {res.reporte}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
