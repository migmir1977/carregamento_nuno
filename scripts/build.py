"""Gera index.html a partir de config/*.yml, data/*.csv e template/index.html."""
import collections
import json
import re
import statistics

import pandas as pd

from common import COORDS, ROOT, load_meta, load_yaml, read_tarifas

TEMPLATE = ROOT / "template" / "index.html"
OUT = ROOT / "index.html"
TARIFA_RE = r"€ ([\d.]+) /(\w+)(?: após (\d+) min)?(?: até (\d+) min)?"


# ---------------------------------------------------------------- tarifas

def load_sockets(cfg):
    regras = cfg["regras"]
    df = read_tarifas()
    df = df[df.TARIFA.notna() & df.TIPO_TOMADA.isin(["CCS", "MENNEKES"])].copy()
    df = df[df.TIPO_TARIFARIO.fillna("REGULAR").isin(regras.get("tarifarios", ["REGULAR"]))]
    df = df[~df.MUNICIPIO.isin(regras.get("excluir_municipios", []))]
    df["P"] = df.POTENCIA_TOMADA.str.replace(",", ".").astype(float)
    p = df.TARIFA.str.extract(TARIFA_RE)
    df["v"] = p[0].astype(float)
    df["u"] = p[1]
    df["a"] = p[2].fillna(0).astype(int)
    df["b"] = p[3].fillna(0).astype(int)
    df = df[df.v.notna()]
    return [(g.iloc[0], g) for _, g in df.groupby("UID_TOMADA")]


def session_cost(g, kwh, minutes, cut30, include_flat, flat_limit, iva):
    """Custo do posto (com IVA) para uma sessão de `kwh` em `minutes`."""
    t = 0.0
    for v, u, a, b in zip(g.v, g.u, g.a, g.b):
        if cut30:
            if a >= 30:
                continue
            if b >= 30:
                b = 0
        if u == "charge":
            t += v if (include_flat or v > flat_limit) else 0
            continue
        end = b if b else 10**6
        overlap = max(0, min(end, minutes) - a)
        t += v * kwh * overlap / minutes if u == "kWh" else v * overlap
    return round(t * (1 + iva), 2)


def matches(s, tomada, P):
    if tomada != s["tomada"]:
        return False
    if "pot_min" in s and P < s["pot_min"]:
        return False
    if "pot_max" in s and P > s["pot_max"]:
        return False
    if "pot_acima_de" in s and P <= s["pot_acima_de"]:
        return False
    return True


def session_minutes(s, P):
    if "minutos" in s:
        return s["minutos"]
    return s["kwh"] / (s["velocidade_pct"] / 100 * P) * 60


def coords_lookup():
    c = pd.read_csv(COORDS, dtype={"site_id": str, "ids": str}).sort_values("site_id")
    m = {}
    # Ordem determinística: identificadores secundários pela ordem dos locais (o primeiro ganha),
    # e o próprio site_id tem prioridade sobre qualquer outro.
    for r in c.itertuples():
        for t in sorted(set(str(r.ids).split("|"))):
            m.setdefault(t, (round(r.lat, 6), round(r.lon, 6)))
    for r in c.itertuples():
        m[r.site_id] = (round(r.lat, 6), round(r.lon, 6))

    def look(i):
        for k in (i, i.replace("MOBI-", "", 1)):
            if k in m:
                return m[k]
        return (None, None)
    return look


def build_scenarios(cfg):
    regras = cfg["regras"]
    iva = regras["iva"]
    flat_lim = regras["taxa_fixa_ignorada_ate"]
    min_min = regras["minimo_por_minuto"]
    min_min_kw = regras["minimo_por_minuto_acima_de_kw"]
    m_abs, m_rel = regras["margem_perfil"]["abs"], regras["margem_perfil"]["rel"]
    sockets = load_sockets(cfg)
    look = coords_lookup()
    out, pts, report = {}, collections.defaultdict(dict), {}

    for s in cfg["cenarios"]:
        key, cut = s["id"], s.get("ignorar_apos_30", False)
        rows, drop = [], collections.Counter()
        for r, g in sockets:
            if not matches(s, r.TIPO_TOMADA, r.P):
                continue
            if not (g.u.isin(["kWh", "min"]) & (g.v > 0)).any():
                drop["sem parte variável"] += 1
                continue
            if r.P > min_min_kw and ((g.u == "min") & (g.v > 0) & (g.v < min_min)).any():
                drop[f"< {min_min} €/min"] += 1
                continue
            T = session_minutes(s, r.P)
            x = dict(id=r.ID, op=r.OPERADOR, mun=r.MUNICIPIO, mor=r.MORADA, tipo=r.TIPO_POSTO, P=r.P,
                     tar=" + ".join(sorted(set(g.TARIFA))),
                     k=session_cost(g, s["kwh"], T, cut, False, flat_lim, iva),
                     c=session_cost(g, s["kwh"], T, cut, True, flat_lim, iva))
            if "extra" in s:
                e = s["extra"]
                x["c2"] = session_cost(g, e["kwh"], e["minutos"], cut, True, flat_lim, iva)
            rows.append(x)
        if not rows:
            raise SystemExit(f"Cenário {key}: nenhuma tomada encontrada.")
        g = pd.DataFrame(rows)

        # perfis por proximidade ao valor de referência (o mais frequente primeiro)
        L = []
        for v, _ in g.k.value_counts().items():
            for p in L:
                if abs(v - p["ref"]) <= max(m_abs, m_rel * p["ref"]):
                    p["vals"].append(v)
                    break
            else:
                L.append({"ref": v, "vals": [v]})
        L.sort(key=lambda p: p["ref"])
        g["perf"] = g.k.map({v: i + 1 for i, p in enumerate(L) for v in p["vals"]})
        prof = []
        for i in range(len(L)):
            q = g[g.perf == i + 1]
            prof.append(dict(p=i + 1, ref=round(float(q.c.median()), 2), mn=float(q.c.min()), mx=float(q.c.max()),
                             tom=int(len(q)), post=int(q.id.nunique()), ops=q.op.value_counts().index.tolist()))

        keys = ["id", "op", "mun", "mor", "tipo", "perf", "c", "tar"] + (["c2"] if "extra" in s else [])
        agg = (g.groupby(keys).agg(n=("P", "size"),
                                   P=("P", lambda x: "/".join(str(int(v)) if v == int(v) else str(v) for v in sorted(set(x)))))
               .reset_index().sort_values(["c", "mun"]))
        ll = agg.id.map(look)
        agg["lat"] = [a for a, _ in ll]
        agg["lon"] = [b for _, b in ll]
        for r in agg.itertuples():
            if r.lat is not None:
                pts[r.mun][r.id] = (r.lat, r.lon)
        cols = ["c", "perf", "op", "mun", "mor", "id", "P", "n", "tar", "tipo", "lat", "lon"] + (["c2"] if "extra" in s else [])
        recs = [[None if (isinstance(v, float) and v != v) else (v.item() if hasattr(v, "item") else v) for v in row]
                for row in agg[cols].itertuples(index=False)]
        out[key] = {"profiles": prof, "cols": cols, "rows": recs}
        report[key] = dict(perfis=len(prof), postos=int(agg.id.nunique()), sem_coords=int(agg.lat.isna().sum()),
                           excluidas=dict(drop))

    munc = {k: [round(statistics.median(p[0] for p in v.values()), 5),
                round(statistics.median(p[1] for p in v.values()), 5)] for k, v in pts.items()}
    return out, munc, report


# ------------------------------------------------------------ fornecedores

def parse_ranges(lst):
    res = []
    for item in lst:
        a, b = item.split("-")
        ha, ma = map(int, a.split(":"))
        hb, mb = map(int, b.split(":"))
        s, e = ha * 60 + ma, hb * 60 + mb
        if e <= s:  # passa a meia-noite
            res += [[s, 1440], [0, e]]
        else:
            res.append([s, e])
    return res


def load_fornecedores(cfg):
    f = load_yaml("fornecedores.yml", required=False)
    if not f:
        return None
    ciclos = {}
    for cid, c in f["ciclos"].items():
        vz = c.get("vazio")
        if vz is None:
            ciclos[cid] = {"nome": c["nome"], "vazio": None, "texto": c.get("vazio")}
            continue
        est = {}
        for estacao in ("verao", "inverno"):
            src = vz.get(estacao, vz)
            est[estacao] = {d: parse_ranges(src.get(d, src.get("todos", []))) for d in ("uteis", "sabado", "domingo")}
        ciclos[cid] = {"nome": c["nome"], "vazio": est, "texto": vz}
    tensoes = {s["tensao"] for s in cfg["cenarios"]}
    for t in tensoes:
        if t not in f["tar"]:
            raise SystemExit(f"Falta a TAR para a tensão {t} em fornecedores.yml")
    ids = []
    for fo in f["fornecedores"]:
        ids.append(fo["id"])
        if fo["ciclo"] not in ciclos:
            raise SystemExit(f"Fornecedor {fo['id']}: ciclo '{fo['ciclo']}' não existe")
        for tipo in ("AC", "DC"):
            o = fo.get(tipo)
            if o is None:
                raise SystemExit(f"Fornecedor {fo['id']}: falta o preço {tipo}")
            if "preco_final" not in o and "energia" not in o:
                raise SystemExit(f"Fornecedor {fo['id']} {tipo}: indicar 'energia' ou 'preco_final'")
            if (fo["ciclo"] == "simples" and "energia" in o and not o.get("inclui_tar")
                    and any("simples" not in f["tar"][t] for t in tensoes)):
                raise SystemExit(f"Fornecedor {fo['id']}: ciclo simples sem TAR incluída exige tar.<tensão>.simples")
    if f.get("defeito") not in ids:
        raise SystemExit("fornecedores.yml: 'defeito' tem de ser o id de um fornecedor")
    return {"defeito": f["defeito"], "tar": f["tar"], "ciclos": ciclos, "lista": f["fornecedores"]}


# ------------------------------------------------------------------ main

def fmt_date(meta, key):
    """Data em que os dados mudaram pela última vez (estável entre execuções sem mudanças)."""
    m = meta.get(key) or {}
    return m.get("atualizado_em") or m.get("data") or "desconhecida"


def main():
    cfg = load_yaml("site.yml")
    forn = load_fornecedores(cfg)
    data, munc, report = build_scenarios(cfg)
    meta = load_meta()
    page_cfg = {
        "iva": cfg["regras"]["iva"],
        "cenarios": {s["id"]: {
            "separador": s["separador"], "titulo": s["titulo"], "curto": s["curto"],
            "kwh": s["kwh"], "tipo": "DC" if s["tomada"] == "CCS" else "AC", "tensao": s["tensao"],
            "extra": s.get("extra"),
        } for s in cfg["cenarios"]},
        "fornecedores": forn,
        # sem data de geração: a página só muda quando os dados ou a configuração mudam
        "dados": {"tarifas": fmt_date(meta, "tarifas"), "coordenadas": fmt_date(meta, "coordenadas")},
    }
    t = TEMPLATE.read_text(encoding="utf-8")
    repl = {
        "__TITLE__": cfg["titulo"],
        "__H1__": cfg["h1"],
        "__NSESS__": cfg["sessoes"],
        "__PNOTE_JSON__": json.dumps((cfg.get("nota_perfis") or "").strip(), ensure_ascii=False),
        "__CFG__": json.dumps(page_cfg, ensure_ascii=False),
        "__DATA__": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        "__MUNC__": json.dumps(munc, ensure_ascii=False, separators=(",", ":")),
    }
    for k, v in repl.items():
        if k not in t:
            raise SystemExit(f"Template sem o marcador {k}")
        t = t.replace(k, v)
    left = re.findall(r"__[A-Z_]+__", t)
    if left:
        raise SystemExit(f"Marcadores por substituir: {left}")
    OUT.write_text(t, encoding="utf-8")
    for k, v in report.items():
        print(k, v)
    print(f"Gerado {OUT} ({OUT.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
