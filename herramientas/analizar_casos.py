"""Compara las SDS problemáticas con las SDS que aparecen en los asuntos de casos.csv.

Uso:  python herramientas\\analizar_casos.py [--config config.real.toml] [--casos ruta\\casos.csv]

No muestra CUITs ni asuntos completos: solo cantidades, series y formatos (con dígitos enmascarados).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migracion_digicorp import recuperacion as RC  # noqa: E402
from migracion_digicorp.config import cargar_config  # noqa: E402


def serie(sds: str) -> str:
    """Primeros 2 dígitos + sufijo: 29020778SEC -> 29xxxxxxSEC."""
    return sds[:2] + "xxxxxx" + sds[8:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path)
    ap.add_argument("--casos", type=Path)
    a = ap.parse_args()
    cfg = cargar_config(a.config)
    carpeta = cfg.trabajo / "recuperacion"
    casos_csv = a.casos or carpeta / "casos.csv"
    with open(carpeta / "sds_problematicas.csv", encoding="utf-8-sig") as fh:
        problemas = {f["SDS"].upper() for f in csv.DictReader(fh, delimiter=";")}
    casos = RC.leer_casos(casos_csv)
    en_casos: set[str] = set()
    digitos_en_asuntos: set[str] = set()
    formatos = Counter()
    for subject, _ in casos:
        en_casos |= RC._sds_en_asunto(subject)
        digitos_en_asuntos |= set(re.findall(r"(?<!\d)\d{8}(?!\d)", subject))
        formatos[re.sub(r"[a-záéíóúñ]+", "a", re.sub(r"\d", "9", subject.lower()))[:60]] += 1

    comun = problemas & en_casos
    solo_numero = {s for s in problemas - en_casos if s[:8] in digitos_en_asuntos}
    print(f"Casos leídos:                         {len(casos)}")
    print(f"SDS distintas en los asuntos:         {len(en_casos)}")
    print(f"SDS problemáticas:                    {len(problemas)}")
    print(f"  ...que aparecen en los asuntos:     {len(comun)}")
    print(f"  ...cuyo número aparece pero sin el sufijo pegado (otro formato): {len(solo_numero)}")

    print("\n--- Series de SDS problemáticas (top 10) ---")
    for k, v in Counter(serie(s) for s in problemas).most_common(10):
        print(f"{v:8d}  {k}")
    print("\n--- Series de SDS en los asuntos de casos.csv (top 10) ---")
    for k, v in Counter(serie(s) for s in en_casos).most_common(10):
        print(f"{v:8d}  {k}")
    print("\n--- Formatos de asunto más frecuentes (letras=a, dígitos=9) ---")
    for k, v in formatos.most_common(10):
        print(f"{v:8d}  {k}")


if __name__ == "__main__":
    main()
