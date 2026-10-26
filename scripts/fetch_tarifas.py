"""Descarrega o ficheiro de tarifas da MOBI.E e substitui data/tarifas.csv se for válido.

Validação: tem de ter as colunas esperadas e pelo menos MIN_RATIO das tomadas
da versão anterior. Caso contrário termina com erro e o ficheiro atual mantém-se.
"""
import datetime as dt
import hashlib
import sys
import tempfile

import requests

from common import TARIFAS, load_meta, load_yaml, read_tarifas, save_meta

MIN_RATIO = 0.8
UA = "Mozilla/5.0 (compatible; carregamento-precos/1.0; +https://github.com/)"


def main():
    url = load_yaml("site.yml")["fontes"]["tarifas"]
    print(f"A descarregar {url}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    body = r.content
    if len(body) < 100_000:
        raise SystemExit(f"Resposta demasiado pequena ({len(body)} bytes); provavelmente não é o CSV.")

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(body)
    novo = read_tarifas(tmp.name)
    n_novo = novo["UID_TOMADA"].nunique()

    n_ant = read_tarifas()["UID_TOMADA"].nunique() if TARIFAS.exists() else 0
    print(f"Tomadas: {n_novo} (antes {n_ant})")
    if n_ant and n_novo < MIN_RATIO * n_ant:
        raise SystemExit(f"Ficheiro novo tem muito menos tomadas ({n_novo} vs {n_ant}); não foi usado.")

    sha = hashlib.sha256(body).hexdigest()
    meta = load_meta()
    prev = meta.get("tarifas") or {}
    agora = dt.datetime.now(dt.timezone.utc)
    if TARIFAS.exists() and hashlib.sha256(TARIFAS.read_bytes()).hexdigest() == sha:
        print("Sem alterações nas tarifas.")
        atualizado = prev.get("atualizado_em") or prev.get("data")
    else:
        TARIFAS.write_bytes(body)
        print("Tarifas atualizadas.")
        atualizado = agora.strftime("%d/%m/%Y")
    meta["tarifas"] = {
        "fonte": url,
        "last_modified": r.headers.get("Last-Modified"),
        "atualizado_em": atualizado,
        "verificado_em": agora.strftime("%Y-%m-%d %H:%M UTC"),
        "tomadas": int(n_novo),
        "sha256": sha,
    }
    save_meta(meta)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        sys.exit(f"Falha no download das tarifas: {e}")
