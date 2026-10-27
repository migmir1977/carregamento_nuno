"""Descarrega o feed DATEX II da MOBI.E (Ponto de Acesso Nacional) e extrai:

- data/coords.csv: para todos os locais, site_id, ids (identificadores do local e
  dos postos, separados por '|'), lat, lon;
- data/redes.csv: para os operadores com rede própria indicados em
  config/fornecedores.yml (campo "rede", ex. CNT = Continente), uma linha por
  tomada com tipo, potência, morada e coordenadas. Estes postos não estão no
  ficheiro de tarifas da MOBI.E.

O feed tem ~170 MB, por isso é descarregado para um ficheiro temporário e lido em streaming.
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

from common import COORDS, REDES, load_meta, load_yaml, save_meta

MIN_RATIO = 0.8
UA = "Mozilla/5.0 (compatible; carregamento-precos/1.0; +https://github.com/)"
REDES_COLS = ["operador", "site_id", "tomada_id", "conector", "potencia_kw",
              "morada", "localidade", "codigo_postal", "lat", "lon"]


def local(tag):
    return tag.rsplit("}", 1)[-1]


def first_text(el, path):
    """Texto do primeiro <value> dentro do caminho de tags locais `path` (ex. ['city'])."""
    node = el
    for name in path:
        node = next((c for c in node.iter() if local(c.tag) == name and c is not node), None)
        if node is None:
            return ""
    for c in node.iter():
        if local(c.tag) == "value" and c.text:
            return c.text.strip()
    return (node.text or "").strip()


def child_text(el, name):
    c = next((c for c in el.iter() if local(c.tag) == name), None)
    return (c.text or "").strip() if c is not None else ""


def extract(xml_path, redes):
    rows, rede_rows = [], []
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
            lat, lon = round(float(lat), 6), round(float(lon), 6)
            rows.append((site, "|".join(sorted(ids)), lat, lon))
            op = site.split("-", 1)[0]
            if op in redes:
                morada = first_text(el, ["addressLine", "text"])
                cidade = first_text(el, ["address", "city"])
                cp = child_text(el, "postcode")
                for rp in (c for c in el.iter() if local(c.tag) == "refillPoint"):
                    for con in (c for c in rp.iter() if local(c.tag) == "connector"):
                        tipo = child_text(con, "connectorType")
                        pw = child_text(con, "maxPowerAtSocket")
                        kw = round(float(pw) / 1000, 1) if pw else ""
                        rede_rows.append((op, site, rp.get("id") or "", tipo, kw, morada, cidade, cp, lat, lon))
        el.clear()
        root.clear()  # liberta memória dos elementos já processados
    rows.sort()
    rede_rows.sort(key=lambda r: tuple(str(x) for x in r))
    return rows, rede_rows


def write_if_changed(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    new = buf.getvalue()
    if path.exists() and path.read_text(encoding="utf-8") == new:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def main():
    url = load_yaml("site.yml")["fontes"]["coordenadas"]
    forn = load_yaml("fornecedores.yml", required=False) or {}
    redes = {f["rede"] for f in forn.get("fornecedores", []) if f.get("rede")}
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

    rows, rede_rows = extract(path, redes)
    os.remove(path)
    n_ant = sum(1 for _ in open(COORDS, encoding="utf-8")) - 1 if COORDS.exists() else 0
    print(f"Locais com coordenadas: {len(rows)} (antes {n_ant})")
    if n_ant and len(rows) < MIN_RATIO * n_ant:
        raise SystemExit(f"Feed novo tem muito menos locais ({len(rows)} vs {n_ant}); não foi usado.")

    meta = load_meta()
    prev = meta.get("coordenadas") or {}
    agora = dt.datetime.now(dt.timezone.utc)
    changed = write_if_changed(COORDS, ["site_id", "ids", "lat", "lon"], rows)
    if redes:
        n_rede_ant = sum(1 for _ in open(REDES, encoding="utf-8")) - 1 if REDES.exists() else 0
        print(f"Tomadas de redes próprias ({', '.join(sorted(redes))}): {len(rede_rows)} (antes {n_rede_ant})")
        if n_rede_ant and len(rede_rows) < MIN_RATIO * n_rede_ant:
            print("Aviso: muito menos tomadas de redes próprias; mantém-se o ficheiro anterior.")
        else:
            changed |= write_if_changed(REDES, REDES_COLS, rede_rows)
    print("Coordenadas atualizadas." if changed else "Sem alterações nas coordenadas.")
    atualizado = agora.strftime("%d/%m/%Y") if changed else (prev.get("atualizado_em") or prev.get("data"))

    meta["coordenadas"] = {
        "fonte": url,
        "last_modified": lm,
        "atualizado_em": atualizado,
        "verificado_em": agora.strftime("%Y-%m-%d %H:%M UTC"),
        "locais": len(rows),
        "tomadas_redes_proprias": len(rede_rows),
    }
    save_meta(meta)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Falha no download das coordenadas: {e}")
