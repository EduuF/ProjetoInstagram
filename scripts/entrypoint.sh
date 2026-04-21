#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Entrypoint de produção.
#
# Roda ``alembic upgrade head`` antes de ceder controle para o uvicorn.
# Se ``RUN_MIGRATIONS=false`` estiver setado, pula (útil quando múltiplos
# containers sobem em paralelo em ECS — basta marcar uma task primária).
# ---------------------------------------------------------------------------
set -euo pipefail

if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
    echo "[entrypoint] Rodando alembic upgrade head..."
    alembic upgrade head
else
    echo "[entrypoint] RUN_MIGRATIONS=false -> pulando Alembic."
fi

echo "[entrypoint] Iniciando uvicorn: $*"
exec "$@"
