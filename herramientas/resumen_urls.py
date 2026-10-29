"""Resumen de solo lectura de los enlaces .url del origen, sin mostrar CUITs ni tokens.

Uso:  python herramientas\\resumen_urls.py [--origen "E:\\Raw Data"] [--desde 20000000000] [--limite 200]

Muestra:
  * cantidad de enlaces por categoría, esquema (http/https) y dominio;
  * cuántos clientes tienen al menos un enlace de cada dominio;
  * para cada "Copia X.url" de Documentación Facultativa, cómo se compara con el X.url de
    Documentación Móvil del mismo cliente (para entender los casos COPIA_SIN_ORIGINAL).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migracion_digicorp import reglas as R  # noqa: E402
from migracion_digicorp.descarga import leer_url  # noqa: E402
from migracion_digicorp.inventario import escanear_cliente, listar_raiz  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origen", type=Path, default=Path(r"E:\Raw Data"))
    ap.add_argument("--desde", default="")
    ap.add_argument("--limite", type=int, default=0)
    a = ap.parse_args()

    raiz = [r for r in listar_raiz(a.origen) if r[2] and r[0] >= a.desde]
    if a.limite:
        raiz = raiz[:a.limite]

    enlaces = Counter()
    clientes_por_dominio = defaultdict(set)
    copias = Counter()
    salesforce_formato = Counter()
    for n, (nombre, ruta, _) in enumerate(raiz, 1):
        movil_por_nombre: dict[str, list[str | None]] = defaultdict(list)
        movil_rutas: dict[str, str | None] = {}
        copias_fac: list[tuple[str, str | None]] = []
        for e in escanear_cliente(ruta):
            if e.es_dir or R.extension(e.nombre) != R.EXT_URL:
                continue
            cat = R.categoria_cliente(e.rel[0])[0] if len(e.rel) > 1 else "raiz"
            url = leer_url(e.ruta)
            p = urlsplit(url) if url else None
            dominio = (p.hostname or "?") if p else "(sin URL)"
            esquema = p.scheme if p else "-"
            enlaces[(cat, esquema, dominio)] += 1
            clientes_por_dominio[f"{esquema}://{dominio}"].add(nombre)
            if dominio.endswith("salesforce.com") and p:
                salesforce_formato[f"ruta={p.path} | fragmento={'sí' if p.fragment else 'no'}"] += 1
            if cat == "movil":
                movil_por_nombre[R.normalizar(e.nombre)].append(url)
                movil_rutas[url] = e.nombre
            elif cat == "facultativa":
                original = R.nombre_original_de_copia(e.nombre)
                if original:
                    copias_fac.append((original, url))
        todas_movil = {u for lst in movil_por_nombre.values() for u in lst if u}
        for original, url in copias_fac:
            mismos = movil_por_nombre.get(R.normalizar(original), [])
            if url and url in todas_movil:
                copias[("enlace idéntico en Móvil (se excluye)")] += 1
            elif not mismos:
                copias["NO hay un archivo con el nombre original en Móvil"] += 1
            else:
                pc, pm = urlsplit(url or ""), [urlsplit(u or "") for u in mismos]
                if any(pc.hostname != m.hostname for m in pm):
                    copias["original en Móvil con OTRO dominio"] += 1
                elif any(pc.path == m.path and pc.scheme != m.scheme for m in pm):
                    copias["original en Móvil igual salvo http/https"] += 1
                elif any(pc.path == m.path and pc.fragment != m.fragment for m in pm):
                    copias["original en Móvil igual salvo el fragmento (#...)"] += 1
                else:
                    copias["original en Móvil mismo dominio, enlace DISTINTO"] += 1
        if n % 100 == 0:
            print(f"... {n}/{len(raiz)} clientes", file=sys.stderr)

    print(f"Clientes analizados: {len(raiz)}\n")
    print("--- Enlaces por categoría / esquema / dominio ---")
    for (cat, esq, dom), v in enlaces.most_common():
        print(f"{v:7d}  {cat:12s} {esq:6s} {dom}")
    print("\n--- Clientes con al menos un enlace de ---")
    for d, c in sorted(clientes_por_dominio.items(), key=lambda x: -len(x[1])):
        print(f"{len(c):7d}  {d}")
    print("\n--- 'Copia X.url' de Facultativa comparada con Móvil ---")
    for k, v in copias.most_common():
        print(f"{v:7d}  {k}")
    if salesforce_formato:
        print("\n--- Formato de enlaces Salesforce (sin token) ---")
        for k, v in salesforce_formato.most_common(5):
            print(f"{v:7d}  {k}")


if __name__ == "__main__":
    main()
