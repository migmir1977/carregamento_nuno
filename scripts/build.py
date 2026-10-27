"""Gera index.html a partir de config/*.yml, data/*.csv e template/index.html."""
import collections
import json
import re
import statistics

import pandas as pd

from common import COORDS, REDES, ROOT, load_meta, load_yaml, read_tarifas

TEMPLATE = ROOT / "template" / "index.html"
OUT = ROOT / "index.html"
TARIFA_RE = r"€ ([\d.]+) /(\w+)(?: após ([\d.]+) min)?(?: até ([\d.]+) min)?"


# ---------------------------------------------------------------- tarifas

QUALIDADE = collections.Counter()


def alternatives(g):
    """Número máximo de preços diferentes (não nulos) para a mesma componente variável:
    mesma unidade (kWh ou min) e mesma condição de tempo. 1 = normal; 2 = duas tarifas
    alternativas (ex. EDP, provavelmente por dia/hora, informação que o ficheiro não tem);
    mais de 2 = dados inválidos."""
    var = g[(g.u != "charge") & (g.v > 0)]
    return int(var.groupby(["u", "a", "b"]).v.nunique().max()) if len(var) else 1


def load_sockets(cfg):
    """Devolve [(primeira linha, linhas da tomada)] por tomada, depois de limpar o ficheiro.

    Defesas contra problemas de qualidade dos dados da MOBI.E:
      - linhas exatamente repetidas são removidas (senão seriam somadas);
      - uma tomada é identificada por (posto, UID, tipo de tomada, potência), porque
        alguns operadores reutilizam o mesmo UID em postos e tomadas diferentes;
      - tomadas sem potência são excluídas;
      - com duas tarifas alternativas usa-se a mais cara de cada componente
        (em vez de as somar); com mais de duas, a tomada é excluída.
    """
    regras = cfg["regras"]
    df = read_tarifas()
    n0 = len(df)
    df = df.drop_duplicates()
    QUALIDADE["linhas repetidas removidas"] = n0 - len(df)
    df = df[df.TARIFA.notna() & df.TIPO_TOMADA.isin(["CCS", "MENNEKES"])].copy()
    df = df[df.TIPO_TARIFARIO.fillna("REGULAR").isin(regras.get("tarifarios", ["REGULAR"]))]
    df = df[~df.MUNICIPIO.isin(regras.get("excluir_municipios", []))]
    excl_ops = df.OPERADOR.isin(regras.get("excluir_operadores") or [])
    QUALIDADE["linhas de operadores excluídos na configuração"] = int(excl_ops.sum())
    df = df[~excl_ops]
    df["P"] = df.POTENCIA_TOMADA.str.replace(",", ".").astype(float)
    sem_p = df.P.isna()
    QUALIDADE["linhas sem potência (excluídas)"] = int(sem_p.sum())
    df = df[~sem_p]
    p = df.TARIFA.str.extract(TARIFA_RE)
    df["v"] = p[0].astype(float)
    df["u"] = p[1]
    df["a"] = p[2].astype(float).fillna(0)
    df["b"] = p[3].astype(float).fillna(0)
    nao_lidas = df.v.isna()
    QUALIDADE["tarifas em formato desconhecido (ignoradas)"] = int(nao_lidas.sum())
    df = df[~nao_lidas]
    out, bad_ops, alt_ops = [], collections.Counter(), collections.Counter()
    for _, g in df.groupby(["ID", "UID_TOMADA", "TIPO_TOMADA", "P"]):
        n = alternatives(g)
        if n > 2:
            bad_ops[g.OPERADOR.iloc[0]] += 1
            continue
        if n == 2:
            alt_ops[g.OPERADOR.iloc[0]] += 1
            g = g.sort_values("v").drop_duplicates(["u", "a", "b"], keep="last")
        out.append((g.iloc[0], g))
    QUALIDADE["tomadas com tarifas alternativas (usada a mais cara)"] = dict(alt_ops.most_common())
    QUALIDADE["tomadas com dados inválidos (excluídas)"] = dict(bad_ops.most_common())
    return out


def sorted_counts(s):
    """Valores por frequência decrescente, empates por ordem alfabética."""
    vc = s.value_counts()
    return sorted(vc.index, key=lambda x: (-vc[x], x))


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


CONECTORES = {"iec62196T2": "MENNEKES", "iec62196T2COMBO": "CCS"}
CONTINENTE_BBOX = (36.9, 42.2, -9.6, -6.1)  # lat min/max, lon min/max


def rede_price(o, P):
    """Preço final (€/kWh) de um fornecedor de rede própria para uma tomada de potência P."""
    price = o["preco_final"]
    for e in sorted(o.get("escaloes") or [], key=lambda e: e["acima_de_kw"]):
        if P > e["acima_de_kw"]:
            price = e["preco_final"]
    return price


def load_rede_sockets(forn, known_muns, mobie_points):
    """Tomadas das redes próprias (data/redes.csv), já filtradas e com município atribuído."""
    redes = {f["rede"]: f for f in (forn or {}).get("lista", []) if f.get("rede")}
    if not redes or not REDES.exists():
        return pd.DataFrame()
    df = pd.read_csv(REDES, dtype={"site_id": str, "localidade": str, "morada": str, "codigo_postal": str})
    df = df[df.operador.isin(redes)].copy()
    n0 = len(df)
    lat0, lat1, lon0, lon1 = CONTINENTE_BBOX
    df = df[df.lat.between(lat0, lat1) & df.lon.between(lon0, lon1)]
    QUALIDADE["tomadas de redes próprias fora do continente ou com coordenadas inválidas (excluídas)"] = n0 - len(df)
    df["tomada"] = df.conector.map(CONECTORES)
    df = df[df.tomada.notna() & df.potencia_kw.notna()].copy()
    df["fornecedor"] = df.operador.map(lambda o: redes[o]["id"])
    df["nome"] = df.operador.map(lambda o: redes[o]["nome"])

    # município: a localidade, se for um município conhecido; senão o do posto MOBI.E mais próximo
    cache = {}

    def municipio(r):
        loc = (r.localidade or "").strip() if isinstance(r.localidade, str) else ""
        if loc in known_muns:
            return loc
        key = (r.lat, r.lon)
        if key not in cache:
            best = min(mobie_points, key=lambda p: (p[0] - r.lat) ** 2 + ((p[1] - r.lon) * 0.77) ** 2)
            cache[key] = best[2]
        return cache[key]
    df["mun"] = [municipio(r) for r in df.itertuples()]
    QUALIDADE["tomadas de redes próprias consideradas"] = int(len(df))
    return df


def build_scenarios(cfg, forn=None):
    regras = cfg["regras"]
    iva = regras["iva"]
    flat_lim = regras["taxa_fixa_ignorada_ate"]
    min_min = regras["minimo_por_minuto"]
    min_min_kw = regras["minimo_por_minuto_acima_de_kw"]
    m_abs, m_rel = regras["margem_perfil"]["abs"], regras["margem_perfil"]["rel"]
    sockets = load_sockets(cfg)
    look = coords_lookup()
    known_muns = {r.MUNICIPIO for r, _ in sockets}
    mobie_points = sorted({(*look(r.ID), r.MUNICIPIO) for r, _ in sockets if look(r.ID)[0] is not None})
    redes_df = load_rede_sockets(forn, known_muns, mobie_points)
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
        # mais frequente primeiro; empates pelo valor, para o resultado não depender da versão do pandas
        vc = g.k.value_counts()
        for v in sorted(vc.index, key=lambda x: (-vc[x], x)):
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
                             tom=int(len(q)), post=int(q.id.nunique()), ops=sorted_counts(q.op)))

        keys = ["id", "op", "mun", "mor", "tipo", "perf", "c", "tar"] + (["c2"] if "extra" in s else [])
        agg = (g.groupby(keys).agg(n=("P", "size"),
                                   P=("P", lambda x: "/".join(str(int(v)) if v == int(v) else str(v) for v in sorted(set(x)))))
               .reset_index().sort_values(["c", "mun"]))
        ll = agg.id.map(look)
        agg["lat"] = [a for a, _ in ll]
        agg["lon"] = [b for _, b in ll]
        agg["rede"] = None
        agg["cf"] = None
        if "extra" in s:
            agg["cf2"] = None

        # postos de redes próprias (ex. Continente): preço final do fornecedor, sem preço de posto
        rede_info = {}
        if len(redes_df):
            # os postos de rede própria podem ter um intervalo de potência próprio (ex. o Continente
            # cobra só por kWh, por isso um posto de 7,4 kW custa o mesmo que um de 22 kW)
            sr = {**{k: v for k, v in s.items() if not k.startswith("pot_")}, **s.get("redes_potencia", {})} \
                if "redes_potencia" in s else s
            q = redes_df[[matches(sr, tm, P) for tm, P in zip(redes_df.tomada, redes_df.potencia_kw)]]
            if len(q):
                o_tipo = "DC" if s["tomada"] == "CCS" else "AC"
                fo = {f["id"]: f for f in forn["lista"]}
                rr = (q.groupby(["fornecedor", "nome", "operador", "site_id", "mun", "morada", "potencia_kw", "lat", "lon"])
                      .size().reset_index(name="n"))
                rr["price"] = [rede_price(fo[f][o_tipo], P) for f, P in zip(rr.fornecedor, rr.potencia_kw)]
                rows_r = pd.DataFrame({
                    "id": rr.site_id, "op": rr.operador, "mun": rr["mun"], "mor": rr.morada.fillna(""),
                    "tipo": rr.nome, "perf": None, "c": None,
                    "tar": [f"{nm}: {pr:.2f} €/kWh, preço final".replace(".", ",") for nm, pr in zip(rr.nome, rr.price)],
                    "n": rr.n, "P": [str(int(v)) if v == int(v) else str(v) for v in rr.potencia_kw],
                    "lat": rr.lat, "lon": rr.lon, "rede": rr.fornecedor,
                    "cf": [round(pr * s["kwh"], 2) for pr in rr.price],
                })
                if "extra" in s:
                    rows_r["c2"] = None
                    rows_r["cf2"] = [round(pr * s["extra"]["kwh"], 2) for pr in rr.price]
                for f_id, gq in rows_r.groupby("rede"):
                    rede_info[f_id] = dict(post=int(gq.id.nunique()), tom=int(gq.n.sum()),
                                           mn=float(gq.cf.min()), mx=float(gq.cf.max()))
                agg = pd.concat([agg, rows_r[agg.columns]], ignore_index=True)
        for r in agg.itertuples():
            if pd.notna(r.lat) and pd.notna(r.lon):
                pts[r.mun][r.id] = (r.lat, r.lon)
        cols = (["c", "perf", "op", "mun", "mor", "id", "P", "n", "tar", "tipo", "lat", "lon", "rede", "cf"]
                + (["c2", "cf2"] if "extra" in s else []))
        recs = [[None if (v is None or (isinstance(v, float) and v != v)) else (v.item() if hasattr(v, "item") else v)
                 for v in row]
                for row in agg[cols].itertuples(index=False)]
        out[key] = {"profiles": prof, "cols": cols, "rows": recs, "redes": rede_info}
        report[key] = dict(perfis=len(prof), postos=int(agg[agg.rede.isna()].id.nunique()),
                           sem_coords=int(agg.lat.isna().sum()), excluidas=dict(drop),
                           **({"redes_proprias": rede_info} if rede_info else {}))

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
            if fo.get("rede") and "preco_final" not in o:
                raise SystemExit(f"Fornecedor {fo['id']} {tipo}: fornecedor com rede própria exige 'preco_final'")
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
    data, munc, report = build_scenarios(cfg, forn)
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
    print("Qualidade dos dados:", dict(QUALIDADE))
    for k, v in report.items():
        print(k, v)
    print(f"Gerado {OUT} ({OUT.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
