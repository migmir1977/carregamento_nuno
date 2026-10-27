"""Funções partilhadas pelos scripts de download e de build."""
import json
import pathlib

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
TARIFAS = DATA / "tarifas.csv"
COORDS = DATA / "coords.csv"
REDES = DATA / "redes.csv"  # postos de redes próprias (ex. Continente), fora do ficheiro de tarifas
META = DATA / "meta.json"

TARIFAS_COLUNAS = {
    "ID", "UID_TOMADA", "TIPO_POSTO", "MUNICIPIO", "MORADA", "OPERADOR",
    "TIPO_TARIFARIO", "TIPO_TARIFA", "TARIFA", "TIPO_TOMADA", "POTENCIA_TOMADA",
}


def load_yaml(name, required=True):
    path = CONFIG / name
    if not path.exists():
        if required:
            raise SystemExit(f"Falta o ficheiro de configuração {path}")
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_tarifas(path=TARIFAS):
    """Lê o CSV de tarifas da MOBI.E (separador ';', UTF-8 com BOM)."""
    df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    falta = TARIFAS_COLUNAS - set(df.columns)
    if falta:
        raise ValueError(f"Colunas em falta no ficheiro de tarifas: {sorted(falta)}")
    return df


def load_meta():
    if META.exists():
        return json.loads(META.read_text(encoding="utf-8"))
    return {}


def save_meta(meta):
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
