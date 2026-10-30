"""Coordina las fases por cliente, con parada controlada, reanudación y tope de publicaciones."""
from __future__ import annotations

import logging
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .archivos import borrar_arbol, mismo_volumen
from .config import MODO_REAL, Config
from .descarga import Descargador, ErrorEntorno
from .diagnostico import diagnosticar
from .estado import Estado
from .inventario import Referencia, escanear_cliente, leer_referencia, listar_raiz
from .modelo import EstadoCliente, Plan, TipoAccion
from .preparacion import Interrumpido, preparar
from .publicacion import ErrorPublicacion, publicar, validar_final
from .reporte import Reporte

log = logging.getLogger(__name__)

COMANDO_INVENTARIO = "inventario"
COMANDO_DIAGNOSTICO = "diagnostico"
COMANDO_MIGRAR = "migrar"


@dataclass
class Opciones:
    comando: str = COMANDO_DIAGNOSTICO
    confirmar_real: bool = False
    validar_urls: bool = False          # en diagnóstico: descargar y validar enlaces (sin publicar)
    reintentar_fallidas: bool = False
    cuits: list[str] = field(default_factory=list)
    desde: str = ""
    limite_clientes: int = 0            # 0 = sin límite de clientes a evaluar
    incluir_plan: bool = True


@dataclass
class Resultado:
    id_ejecucion: str
    contadores: dict[str, int] = field(default_factory=dict)
    publicados: int = 0
    motivo_fin: str = "COMPLETO"
    reporte: Path | None = None


class Parada:
    """Parada controlada: Ctrl+C (1ra vez) o archivo DETENER. El 2do Ctrl+C aborta."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.solicitada = False
        self._previo = None

    def __enter__(self):
        def manejador(signum, frame):
            if self.solicitada:
                raise KeyboardInterrupt
            self.solicitada = True
            log.warning("Parada solicitada: se termina el cliente en curso y se cierra de forma ordenada. "
                        "Presione Ctrl+C otra vez para abortar.")
        try:
            self._previo = signal.signal(signal.SIGINT, manejador)
        except ValueError:  # fuera del hilo principal (p. ej. en tests)
            self._previo = None
        return self

    def __exit__(self, *exc):
        if self._previo is not None:
            signal.signal(signal.SIGINT, self._previo)

    def __call__(self) -> bool:
        if not self.solicitada and self.cfg.archivo_detener.exists():
            log.warning("Archivo %s encontrado: deteniendo.", self.cfg.archivo_detener)
            self.solicitada = True
        return self.solicitada


def configurar_logging(cfg: Config, id_ejecucion: str) -> Path:
    cfg.dir_logs.mkdir(parents=True, exist_ok=True)
    ruta = cfg.dir_logs / f"migracion_{id_ejecucion}.log"
    raiz = logging.getLogger()
    raiz.setLevel(logging.DEBUG)
    for h in list(raiz.handlers):
        if getattr(h, "_migracion", False):
            raiz.removeHandler(h)
    archivo = logging.FileHandler(ruta, encoding="utf-8")
    archivo.setLevel(logging.DEBUG)
    archivo.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
    consola = logging.StreamHandler()
    consola.setLevel(logging.INFO)
    consola.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    for h in (archivo, consola):
        h._migracion = True
        raiz.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return ruta


def ejecutar(cfg: Config, op: Opciones, *, referencia: Referencia | None = None,
             descargador: Descargador | None = None, estado: Estado | None = None,
             rename=None) -> Resultado:
    id_ejecucion = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    real = op.comando == COMANDO_MIGRAR and cfg.modo == MODO_REAL
    if op.comando == COMANDO_MIGRAR and cfg.modo == MODO_REAL and not op.confirmar_real:
        raise SystemExit("modo='real' requiere además el parámetro --confirmar-ejecucion-real")
    if op.confirmar_real and cfg.modo != MODO_REAL:
        raise SystemExit("--confirmar-ejecucion-real solo es válido con modo='real' en la configuración")

    for d in (cfg.trabajo, cfg.dir_staging, cfg.dir_cache, cfg.dir_reportes, cfg.dir_manifiestos):
        d.mkdir(parents=True, exist_ok=True)
    if real:
        if not mismo_volumen(cfg.dir_staging, cfg.destino):
            raise SystemExit("El staging y el destino deben estar en el mismo volumen para publicar de forma atómica")
        cfg.destino.mkdir(parents=True, exist_ok=True)
    if not cfg.origen.is_dir():
        raise SystemExit(f"No existe el origen: {cfg.origen}")

    propio_estado = estado is None
    estado = estado or Estado(cfg.archivo_estado)
    estado.iniciar_ejecucion(id_ejecucion, op.comando, cfg.modo if op.comando == COMANDO_MIGRAR else "lectura")
    res = Resultado(id_ejecucion)
    reporte = Reporte(incluir_plan=op.incluir_plan)
    soffice = cfg.ruta_soffice()
    log.info("Ejecución %s | comando=%s | modo=%s | origen=%s | destino=%s",
             id_ejecucion, op.comando, "REAL" if real else "SIMULACIÓN/LECTURA", cfg.origen, cfg.destino)
    if real:
        log.warning("MODO REAL: se publicarán como máximo %d clientes en esta ejecución", cfg.limite_efectivo)
    if soffice is None:
        log.info("LibreOffice no disponible: los documentos Office se migrarán sin convertir")

    referencia = referencia or leer_referencia(cfg.excel_referencia, cfg.hoja_arbol)
    necesita_red = op.comando == COMANDO_MIGRAR or op.validar_urls
    propio_descargador = necesita_red and descargador is None
    if propio_descargador:
        descargador = Descargador(cfg, estado)

    raiz = listar_raiz(cfg.origen)
    if op.cuits:
        pedidos = set(op.cuits)
        raiz = [r for r in raiz if r[0] in pedidos]
    if op.desde:
        raiz = [r for r in raiz if r[0] >= op.desde]
    if op.limite_clientes:
        raiz = raiz[:op.limite_clientes]

    def contar(clave: str) -> None:
        res.contadores[clave] = res.contadores.get(clave, 0) + 1

    inicio = time.monotonic()
    try:
        with Parada(cfg) as parar:
            for n, (nombre, ruta, es_dir) in enumerate(raiz, start=1):
                if parar():
                    res.motivo_fin = "DETENIDO_MANUALMENTE"
                    break
                if real and res.publicados >= cfg.limite_efectivo:
                    res.motivo_fin = f"TOPE_DE_SEGURIDAD_{cfg.limite_efectivo}_CLIENTES"
                    log.warning("Failsafe: se alcanzó el tope de %d clientes publicados. Proceso detenido.",
                                cfg.limite_efectivo)
                    break
                if not es_dir:
                    reporte.agregar_simple(nombre, ruta, EstadoCliente.REVISION_MANUAL.value,
                                           "Archivo suelto en la raíz del origen")
                    contar(EstadoCliente.REVISION_MANUAL.value)
                    continue
                previo = estado.estado_cliente(nombre)
                if previo == EstadoCliente.PUBLICADO.value:
                    reporte.agregar_simple(nombre, ruta, EstadoCliente.OMITIDO_YA_PUBLICADO.value,
                                           "Publicado en una ejecución anterior")
                    contar(EstadoCliente.OMITIDO_YA_PUBLICADO.value)
                    continue
                try:
                    final = _procesar_cliente(cfg, op, nombre, ruta, referencia, descargador, estado, soffice,
                                              parar, reporte, id_ejecucion, real, rename)
                except ErrorEntorno as exc:
                    res.motivo_fin = "ERROR_DE_ENTORNO"
                    log.error("Ejecución detenida por un problema del equipo o la red (no de los clientes): %s",
                              exc)
                    break
                contar(final)
                if final == EstadoCliente.PUBLICADO.value:
                    res.publicados += 1
                if final == EstadoCliente.INTERRUMPIDO.value:
                    res.motivo_fin = "DETENIDO_MANUALMENTE"
                    break
                if n % 50 == 0 or n == len(raiz):
                    log.info("Avance: %d/%d clientes (%.0fs) %s", n, len(raiz), time.monotonic() - inicio,
                             res.contadores)
    except KeyboardInterrupt:
        res.motivo_fin = "ABORTADO"
        log.error("Ejecución abortada por el usuario")
    finally:
        res.reporte = reporte.guardar(cfg.dir_reportes / f"reporte_{op.comando}_{id_ejecucion}.xlsx")
        estado.finalizar_ejecucion(id_ejecucion, res.publicados, res.motivo_fin)
        log.info("Fin (%s). Resultados: %s. Publicados: %d. Reporte: %s",
                 res.motivo_fin, res.contadores, res.publicados, res.reporte)
        if propio_descargador:
            descargador.cerrar()
        if propio_estado:
            estado.cerrar()
    return res


def _procesar_cliente(cfg: Config, op: Opciones, nombre: str, ruta: Path, ref: Referencia,
                      descargador: Descargador | None, estado: Estado, soffice: str | None, parar: Parada,
                      reporte: Reporte, id_ejecucion: str, real: bool, rename) -> str:
    destino = cfg.destino / nombre
    try:
        entradas = escanear_cliente(ruta)
    except OSError as exc:
        plan = Plan(nombre, ruta)
        plan.incidir(ruta, "ERROR_LECTURA", f"No se pudo recorrer la carpeta: {exc}", manual=False)
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion)

    plan = diagnosticar(nombre, ruta, entradas, ref, cfg, soffice)

    if op.comando == COMANDO_MIGRAR and plan.cuit_valido and destino.exists():
        plan.incidir(destino, "DESTINO_EXISTENTE",
                     "La carpeta de destino ya existe y no fue publicada por este proceso: no se sobrescribe")

    if op.comando == COMANDO_INVENTARIO:
        reporte.agregar_inventario(nombre, entradas, ref.arbol.get(nombre))
    if op.comando == COMANDO_INVENTARIO or (op.comando == COMANDO_DIAGNOSTICO and not op.validar_urls):
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion, registrar=False)

    if plan.bloqueado or plan.postergado_por:
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion)

    if op.comando == COMANDO_DIAGNOSTICO:  # --validar-urls: descarga y valida, no prepara
        from .preparacion import _descargar_todo
        try:
            _descargar_todo(plan, descargador, parar, op.reintentar_fallidas)
        except Interrumpido:
            return EstadoCliente.INTERRUMPIDO.value
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion, registrar=False)

    try:
        prep = preparar(plan, cfg, descargador, soffice, parar, op.reintentar_fallidas)
    except Interrumpido:
        log.warning("%s: interrumpido durante la preparación; se reprocesará en la próxima ejecución", nombre)
        return EstadoCliente.INTERRUMPIDO.value
    except ErrorEntorno:
        raise
    except Exception as exc:
        log.exception("%s: error inesperado en la preparación", nombre)
        plan.incidir(ruta, "ERROR_PREPARACION", f"{type(exc).__name__}: {exc}", manual=False)
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion)
    if prep is None:
        return _cerrar(plan, plan.estado.value, destino, None, reporte, estado, id_ejecucion)

    fallos = validar_final(plan, prep, entradas, destino)
    if fallos:
        for f in fallos:
            plan.incidir(prep.staging, "VALIDACION_FINAL", f, manual=False)
        borrar_arbol(prep.staging)
        return _cerrar(plan, plan.estado.value, destino, prep, reporte, estado, id_ejecucion)

    if not real:
        prep.guardar_manifiesto(cfg.dir_manifiestos / "simulacion" / f"{nombre}.json", plan,
                                {"ejecucion": id_ejecucion, "estado": "SIMULADO_OK", "destino": str(destino)})
        borrar_arbol(prep.staging)
        return _cerrar(plan, EstadoCliente.SIMULADO_OK.value, destino, prep, reporte, estado, id_ejecucion)

    try:
        publicar(prep, destino, rename=rename) if rename else publicar(prep, destino)
    except ErrorPublicacion as exc:
        plan.incidir(destino, "ERROR_PUBLICACION", str(exc), manual=False)
        borrar_arbol(prep.staging)
        return _cerrar(plan, plan.estado.value, destino, prep, reporte, estado, id_ejecucion)
    prep.guardar_manifiesto(cfg.dir_manifiestos / f"{nombre}.json", plan,
                            {"ejecucion": id_ejecucion, "estado": "PUBLICADO", "destino": str(destino)})
    log.info("%s: PUBLICADO (%d archivos)", nombre, len(prep.generados))
    return _cerrar(plan, EstadoCliente.PUBLICADO.value, destino, prep, reporte, estado, id_ejecucion)


def _cerrar(plan: Plan, final: str, destino: Path, prep, reporte: Reporte, estado: Estado,
            id_ejecucion: str, registrar: bool = True) -> str:
    reporte.agregar(plan, final, destino, prep)
    if registrar:
        motivo = "; ".join(f"{i.tipo}: {i.descripcion}" for i in plan.incidencias if i.bloqueante)[:4000]
        estado.registrar_cliente(plan.cuit, final, id_ejecucion, str(destino),
                                 len(prep.generados) if prep else 0, motivo)
    log.debug("%s -> %s (%d incidencias)", plan.cuit, final, len(plan.incidencias))
    return final
