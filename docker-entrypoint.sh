#!/bin/sh
set -e

# Charge les secrets montés (fichier hors-Git, fourni au runtime via un volume).
# Ces valeurs surchargent d'éventuels placeholders présents dans l'environnement.
if [ -f /app/secrets/hermes.env ]; then
  set -a
  . /app/secrets/hermes.env
  set +a
  echo "[entrypoint] secrets chargés depuis /app/secrets/hermes.env"
else
  echo "[entrypoint] /app/secrets/hermes.env absent — utilisation de l'environnement courant"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
