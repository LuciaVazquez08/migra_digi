"""Resumen del estado acumulado de la migración (solo lectura, sin CUITs ni enlaces).

Uso:  python herramientas\\resumen_estado.py [--config config.real.toml]

Muestra: clientes por estado, descargas por dominio y resultado, los errores de descarga más
frecuentes por dominio, y los motivos de bloqueo más frecuentes entre los clientes no migrados.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migracion_digicorp.config import cargar_config  # noqa: E402


def _normalizar(texto: str) -> str:
    """Quita números y rutas para agrupar mensajes equivalentes."""
    texto = re.sub(r"[A-Z]:\\\\[^:;]*", "<ruta>", texto or "")
    texto = re.sub(r"https?://\S+", "<url>", texto)
    texto = re.sub(r"\d+", "#", texto)
    return texto.strip()[:150]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path)
    a = ap.parse_args()
    cfg = cargar_config(a.config)
    con = sqlite3.connect(cfg.archivo_estado)

    print(f"Estado: {cfg.archivo_estado}\n")
    print("--- Clientes por estado ---")
    for estado, n in con.execute("SELECT estado, COUNT(*) FROM clientes GROUP BY estado ORDER BY 2 DESC"):
        print(f"{n:8d}  {estado}")

    descargas = con.execute("SELECT url, estado, permanente, error FROM descargas").fetchall()
    por_dominio = Counter()
    errores = Counter()
    for url, estado, permanente, error in descargas:
        dominio = urlsplit(url).hostname or "?"
        tipo = "OK" if estado == "OK" else ("ERROR definitivo" if permanente else "ERROR transitorio")
        por_dominio[(dominio, tipo)] += 1
        if estado != "OK":
            errores[(dominio, _normalizar(error))] += 1
    print("\n--- Descargas por dominio ---")
    for (dominio, tipo), n in sorted(por_dominio.items()):
        print(f"{n:8d}  {dominio:28s} {tipo}")
    print("\n--- Errores de descarga más frecuentes ---")
    for (dominio, error), n in errores.most_common(20):
        print(f"{n:8d}  {dominio:28s} {error}")

    print("\n--- Motivos más frecuentes de clientes no migrados (REVISION_MANUAL / NO_APTO) ---")
    motivos = Counter()
    for (motivo,) in con.execute("SELECT motivo FROM clientes WHERE estado IN ('REVISION_MANUAL','NO_APTO')"):
        for parte in {_normalizar(p).split(":")[0] + ": " + _normalizar(p).split(":", 1)[-1][:80]
                      for p in (motivo or "").split("; ") if p}:
            motivos[parte] += 1
    for m, n in motivos.most_common(25):
        print(f"{n:8d}  {m}")
    con.close()


if __name__ == "__main__":
    main()
