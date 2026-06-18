#!/bin/sh
set -e

# --- Injection runtime des secrets via Infisical (exclus du build) ---
# Le fichier bootstrap (monté, hors-Git) ne contient QUE l'identité machine
# Infisical (client-id/secret read-only) ; les vrais secrets sont récupérés
# au démarrage depuis le coffre, jamais présents dans l'image ni dans Git.
if [ -f /app/secrets/infisical.env ]; then
  set -a
  . /app/secrets/infisical.env
  set +a
  echo "[entrypoint] Infisical: login machine identity + run (projet=$INFISICAL_PROJECT_ID env=$INFISICAL_ENV path=$INFISICAL_PATH)"
  INFISICAL_TOKEN="$(infisical login --method=universal-auth \
      --client-id="$INFISICAL_CLIENT_ID" --client-secret="$INFISICAL_CLIENT_SECRET" \
      --domain="$INFISICAL_API_URL" --plain --silent)"
  export INFISICAL_TOKEN
  exec infisical run --projectId="$INFISICAL_PROJECT_ID" --env="$INFISICAL_ENV" \
      --path="$INFISICAL_PATH" --domain="$INFISICAL_API_URL" \
      -- uvicorn app.main:app --host 0.0.0.0 --port 8000
fi

# --- Repli (legacy) : fichier de secrets monté en clair ---
if [ -f /app/secrets/hermes.env ]; then
  set -a
  . /app/secrets/hermes.env
  set +a
  echo "[entrypoint] secrets chargés depuis /app/secrets/hermes.env (legacy)"
else
  echo "[entrypoint] aucun secret monté — utilisation de l'environnement courant"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
