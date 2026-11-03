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
    cc.add_argument("--casos", type=Path,
                    help="Copiar solo los archivos de estos CASE_ID (resultado de la consulta, CSV o XLSX); "
                         "requiere haber ejecutado 'sds-problematicas'")
    sub.add_parser("sds-problematicas", help="Lista SDS postergadas/cortadas/fallidas y genera la consulta SQL")
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
    if args.comando == "copiar-carpeta":
        origen = args.origen or cfg.carpeta_casos
        destino = RC.destino_espejo(origen, args.unidad or cfg.unidad_copia_casos)
        if not origen.is_dir():
            print(f"No existe la carpeta de origen: {origen}")
            return 1
        filtro = None
        if args.casos:
            problemas = _leer_problemas(carpeta_trabajo)
            if problemas is None:
                return 1
            filtro, n_ids = RC.filtro_por_casos(problemas, RC.leer_casos(args.casos))
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
        print(f"Detalle: {carpeta_trabajo / 'copia_manifiesto.csv'}")
        return 0 if not (r.errores or r.sin_espacio) else 1

    if args.comando == "sds-problematicas":
        problemas = RC.sds_problematicas(cfg)
        ruta_csv, ruta_sql = RC.escribir_sds_y_sql(problemas, carpeta_trabajo)
        from collections import Counter
        print(f"SDS con problemas: {len({(p.cuit, p.sds) for p in problemas})} "
              f"({len(problemas)} archivos) en {len({p.cuit for p in problemas})} clientes")
        for motivo, cant in Counter(p.motivo for p in problemas).most_common():
            print(f"  {cant:7d} archivos  {motivo}")
        print(f"Listado: {ruta_csv}\nConsulta SQL: {ruta_sql}")
        return 0

    # buscar-casos
    problemas = _leer_problemas(carpeta_trabajo)
    if problemas is None:
        return 1
    carpeta = args.carpeta or RC.destino_espejo(cfg.carpeta_casos, cfg.unidad_copia_casos)
    casos = RC.leer_casos(args.casos)
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

    if args.comando in ("copiar-carpeta", "sds-problematicas", "buscar-casos"):
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
