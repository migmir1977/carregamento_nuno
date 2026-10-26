"""Descarrega o feed DATEX II da MOBI.E (Ponto de Acesso Nacional) e extrai as coordenadas.

Gera data/coords.csv com: site_id, ids (todos os identificadores do local e dos
postos, separados por '|'), lat, lon. O feed tem ~170 MB, por isso é descarregado
para um ficheiro temporário e lido em streaming.

Validação: pelo menos MIN_RATIO dos locais da versão anterior.
"""
import csv
import datetime as dt
import io
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

import requests

from common import COORDS, load_meta, load_yaml, save_meta

MIN_RATIO = 0.8
UA = "Mozilla/5.0 (compatible; carregamento-precos/1.0; +https://github.com/)"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def extract(xml_path):
    rows = []
    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)
    for ev, el in context:
        if ev != "end" or local(el.tag) != "energyInfrastructureSite":
            continue
        lat = lon = None
        ids = set()
        for sub in el.iter():
            t = local(sub.tag)
            if t == "latitude" and lat is None:
                lat = sub.text
            elif t == "longitude" and lon is None:
                lon = sub.text
            if sub.get("id"):
                ids.add(sub.get("id"))
            if t.lower().endswith("identifier") and sub.text and sub.text.strip():
                ids.add(sub.text.strip())
        site = el.get("id")
        if site and lat and lon:
            rows.append((site, "|".join(sorted(ids)), round(float(lat), 6), round(float(lon), 6)))
        el.clear()
        root.clear()  # liberta memória dos elementos já processados
    rows.sort()
    return rows


def main():
    url = load_yaml("site.yml")["fontes"]["coordenadas"]
    print(f"A descarregar {url}")
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    with requests.get(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"}, stream=True, timeout=600) as r:
        r.raise_for_status()
        lm = r.headers.get("Last-Modified")
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    print(f"Descarregados {os.path.getsize(path) / 1e6:.0f} MB")

    rows = extract(path)
    os.remove(path)
    n_ant = sum(1 for _ in open(COORDS, encoding="utf-8")) - 1 if COORDS.exists() else 0
    print(f"Locais com coordenadas: {len(rows)} (antes {n_ant})")
    if n_ant and len(rows) < MIN_RATIO * n_ant:
        raise SystemExit(f"Feed novo tem muito menos locais ({len(rows)} vs {n_ant}); não foi usado.")

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["site_id", "ids", "lat", "lon"])
    w.writerows(rows)
    new = buf.getvalue()
    meta = load_meta()
    prev = meta.get("coordenadas") or {}
    agora = dt.datetime.now(dt.timezone.utc)
    if COORDS.exists() and COORDS.read_text(encoding="utf-8") == new:
        print("Sem alterações nas coordenadas.")
        atualizado = prev.get("atualizado_em") or prev.get("data")
    else:
        COORDS.write_text(new, encoding="utf-8")
        print("Coordenadas atualizadas.")
        atualizado = agora.strftime("%d/%m/%Y")

    meta["coordenadas"] = {
        "fonte": url,
        "last_modified": lm,
        "atualizado_em": atualizado,
        "verificado_em": agora.strftime("%Y-%m-%d %H:%M UTC"),
        "locais": len(rows),
    }
    save_meta(meta)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Falha no download das coordenadas: {e}")
