"""Estado persistente (SQLite) para reanudar ejecuciones sin repetir trabajo."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    cuit TEXT PRIMARY KEY,
    estado TEXT NOT NULL,
    ejecucion TEXT,
    destino TEXT,
    archivos INTEGER,
    motivo TEXT,
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS descargas (
    clave TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    estado TEXT NOT NULL,          -- OK | ERROR
    permanente INTEGER DEFAULT 0,  -- 1 = error que no se reintenta (4xx, URL inválida...)
    ruta TEXT,
    sha256 TEXT,
    bytes INTEGER,
    tipo TEXT,
    error TEXT,
    intentos INTEGER DEFAULT 0,
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS ejecuciones (
    id TEXT PRIMARY KEY,
    comando TEXT,
    modo TEXT,
    inicio TEXT,
    fin TEXT,
    publicados INTEGER DEFAULT 0,
    resultado TEXT
);
"""


def ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Estado:
    def __init__(self, ruta: Path):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(ruta), check_same_thread=False, isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_ESQUEMA)

    def cerrar(self) -> None:
        self._con.close()

    def _exec(self, sql: str, params=()):
        with self._lock:
            return self._con.execute(sql, params).fetchall()

    # --- clientes ---------------------------------------------------------
    def estado_cliente(self, cuit: str) -> str | None:
        filas = self._exec("SELECT estado FROM clientes WHERE cuit=?", (cuit,))
        return filas[0][0] if filas else None

    def registrar_cliente(self, cuit: str, estado: str, ejecucion: str, destino: str = "",
                          archivos: int = 0, motivo: str = "") -> None:
        # Un cliente PUBLICADO nunca vuelve a otro estado.
        self._exec(
            """INSERT INTO clientes(cuit, estado, ejecucion, destino, archivos, motivo, actualizado)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(cuit) DO UPDATE SET estado=excluded.estado, ejecucion=excluded.ejecucion,
                 destino=excluded.destino, archivos=excluded.archivos, motivo=excluded.motivo,
                 actualizado=excluded.actualizado
               WHERE clientes.estado <> 'PUBLICADO'""",
            (cuit, estado, ejecucion, destino, archivos, motivo[:4000], ahora()),
        )

    def resumen_clientes(self) -> list[tuple[str, int]]:
        return self._exec("SELECT estado, COUNT(*) FROM clientes GROUP BY estado ORDER BY estado")

    # --- descargas --------------------------------------------------------
    def descarga(self, clave: str) -> dict | None:
        with self._lock:
            cur = self._con.execute("SELECT * FROM descargas WHERE clave=?", (clave,))
            fila = cur.fetchone()
            if fila is None:
                return None
            return dict(zip([c[0] for c in cur.description], fila))

    def registrar_descarga(self, clave: str, url: str, estado: str, *, permanente: bool = False,
                           ruta: str = "", sha256: str = "", bytes_: int = 0, tipo: str = "",
                           error: str = "", intentos: int = 0) -> None:
        self._exec(
            """INSERT OR REPLACE INTO descargas
               (clave, url, estado, permanente, ruta, sha256, bytes, tipo, error, intentos, actualizado)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (clave, url, estado, int(permanente), ruta, sha256, bytes_, tipo, error[:2000], intentos, ahora()),
        )

    # --- ejecuciones ------------------------------------------------------
    def iniciar_ejecucion(self, id_: str, comando: str, modo: str) -> None:
        self._exec("INSERT INTO ejecuciones(id, comando, modo, inicio) VALUES(?,?,?,?)",
                   (id_, comando, modo, ahora()))

    def finalizar_ejecucion(self, id_: str, publicados: int, resultado: str) -> None:
        self._exec("UPDATE ejecuciones SET fin=?, publicados=?, resultado=? WHERE id=?",
                   (ahora(), publicados, resultado, id_))
