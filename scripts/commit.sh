#!/usr/bin/env bash
# Faz commit dos dados e da página gerados pela Action e envia para o GitHub.
# Se entretanto outra execução tiver enviado um commit, reconstrói a página por cima
# da versão mais recente (mantendo os dados descarregados nesta execução) e tenta de novo.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898333+github-actions[bot]@users.noreply.github.com"

FILES="data/tarifas.csv data/coords.csv data/redes.csv data/meta.json index.html"
KEEP=$(mktemp -d)

for attempt in 1 2 3; do
  # meta.json muda a cada verificação; só se faz commit se houver mudanças reais
  # status (e não diff) para apanhar também ficheiros novos, como data/redes.csv na primeira vez
  if [ -z "$(git status --porcelain -- data/tarifas.csv data/coords.csv data/redes.csv index.html)" ]; then
    echo "Sem alterações nos dados nem na página."
    exit 0
  fi
  for f in $FILES; do if [ -e "$f" ]; then git add "$f"; fi; done
  git commit -q -m "Atualização automática ($(date -u +%Y-%m-%d))"
  if git push -q origin HEAD:main; then
    echo "Enviado."
    exit 0
  fi
  echo "O ramo main mudou entretanto; a reconstruir sobre a versão mais recente (tentativa $attempt)."
  for f in data/tarifas.csv data/coords.csv data/redes.csv data/meta.json; do if [ -e "$f" ]; then cp "$f" "$KEEP"/; fi; done
  git fetch -q origin main
  git reset -q --hard origin/main
  cp "$KEEP"/* data/
  python scripts/build.py
done

echo "::error::Não foi possível enviar as alterações após 3 tentativas."
exit 1
