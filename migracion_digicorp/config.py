"""Configuración centralizada.

Los valores por defecto son los de producción en Windows. Cualquier valor puede
sobrescribirse con un archivo TOML (por defecto ``config.local.toml``, que no se
versiona) usando los mismos nombres de campo.
"""
from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Tope duro de clientes publicados por ejecución en modo real. La configuración
# puede bajarlo, nunca subirlo.
LIMITE_DURO_PUBLICACIONES = 2000

MODO_SIMULACION = "simulacion"
MODO_REAL = "real"


@dataclass
class Config:
    # --- Rutas -------------------------------------------------------------
    origen: Path = Path(r"E:\Raw Data")
    destino: Path = Path(r"E:\Digicorp")
    # Carpeta de trabajo: staging, caché de descargas, estado, logs y reportes.
    # Debe estar en el MISMO volumen que el destino para publicar con un rename atómico.
    trabajo: Path = Path(r"E:\Migracion Digicorp\trabajo")
    excel_referencia: Path = Path(r"E:\Migracion Digicorp\Scripts\estructura_directorio.xlsx")
    hoja_arbol: str = "Arbol_Carpetas"

    # --- Modo de ejecución ---------------------------------------------------
    modo: str = MODO_SIMULACION
    limite_publicaciones: int = LIMITE_DURO_PUBLICACIONES

    # --- Reglas --------------------------------------------------------------
    validar_digito_verificador: bool = True
    # Si una descarga devuelve una imagen (JPEG/PNG/TIFF) en lugar de un PDF, se
    # convierte a PDF y se valida. Con False, cualquier contenido no PDF bloquea.
    aceptar_imagen_descargada: bool = True

    # --- Descargas -----------------------------------------------------------
    dominios_permitidos: tuple[str, ...] = ("cdnva.s1gateway.com", "claro.my.salesforce.com")
    timeout_conexion: float = 15.0
    timeout_lectura: float = 60.0
    reintentos: int = 3
    espera_reintento: float = 2.0  # backoff exponencial: 2, 4, 8...
    descargas_concurrentes: int = 4
    tamano_maximo_descarga: int = 200 * 1024 * 1024
    user_agent: str = "migracion-digicorp/1.0"

    # --- Navegador automático (enlaces públicos de Salesforce) ---------------
    navegadores_simultaneos: int = 2
    timeout_navegador: float = 60.0
    navegador_visible: bool = False   # True para ver el navegador trabajando (diagnóstico)
    navegador_canal: str = ""         # "msedge" usa el Edge instalado en Windows
    navegador_ejecutable: str = ""    # ruta explícita a un Chrome/Chromium

    # --- Conversión ----------------------------------------------------------
    convertir_office: bool = True
    soffice: str = ""  # ruta a soffice(.exe); vacío = autodetectar
    timeout_conversion: float = 180.0

    # Campos internos (no configurables desde TOML)
    _origen_config: Path | None = field(default=None, repr=False)

    # --- Derivados -----------------------------------------------------------
    @property
    def dir_staging(self) -> Path:
        return self.trabajo / "staging"

    @property
    def dir_cache(self) -> Path:
        return self.trabajo / "cache_descargas"

    @property
    def dir_logs(self) -> Path:
        return self.trabajo / "logs"

    @property
    def dir_reportes(self) -> Path:
        return self.trabajo / "reportes"

    @property
    def dir_manifiestos(self) -> Path:
        return self.trabajo / "manifiestos"

    @property
    def archivo_estado(self) -> Path:
        return self.trabajo / "estado.sqlite"

    @property
    def archivo_detener(self) -> Path:
        """Si este archivo existe, el proceso se detiene al terminar el cliente actual."""
        return self.trabajo / "DETENER"

    @property
    def limite_efectivo(self) -> int:
        return max(0, min(self.limite_publicaciones, LIMITE_DURO_PUBLICACIONES))

    def ruta_soffice(self) -> str | None:
        if not self.convertir_office:
            return None
        if self.soffice:
            return self.soffice if Path(self.soffice).exists() else None
        for candidato in ("soffice", "soffice.exe", "libreoffice"):
            encontrado = shutil.which(candidato)
            if encontrado:
                return encontrado
        for candidato in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(candidato).exists():
                return candidato
        return None

    def validar(self) -> None:
        if self.modo not in (MODO_SIMULACION, MODO_REAL):
            raise ValueError(f"modo inválido: {self.modo!r} (usar 'simulacion' o 'real')")
        if self.reintentos < 0 or self.descargas_concurrentes < 1:
            raise ValueError("reintentos >= 0 y descargas_concurrentes >= 1")
        origen = self.origen.resolve()
        for nombre in ("destino", "trabajo"):
            otra = getattr(self, nombre).resolve()
            if otra == origen or origen in otra.parents or otra in origen.parents:
                raise ValueError(f"'{nombre}' no puede estar dentro del origen ni contenerlo")


def cargar_config(ruta: Path | None = None) -> Config:
    cfg = Config()
    if ruta is None:
        candidata = Path("config.local.toml")
        ruta = candidata if candidata.exists() else None
    if ruta is not None:
        with open(ruta, "rb") as fh:
            datos = tomllib.load(fh)
        nombres = {f.name: f for f in fields(Config) if not f.name.startswith("_")}
        for clave, valor in datos.items():
            if clave not in nombres:
                raise ValueError(f"Clave desconocida en {ruta}: {clave}")
            actual = getattr(cfg, clave)
            if isinstance(actual, Path):
                valor = Path(valor)
            elif isinstance(actual, tuple):
                valor = tuple(valor)
            setattr(cfg, clave, valor)
        cfg._origen_config = Path(ruta)
    cfg.validar()
    return cfg
