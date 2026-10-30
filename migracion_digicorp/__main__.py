"""Línea de comandos. Uso: ``python -m migracion_digicorp <comando> [opciones]``."""
from __future__ import annotations

import argparse
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
    return p


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
