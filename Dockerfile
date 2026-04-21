# syntax=docker/dockerfile:1.6
# ---------------------------------------------------------------------------
# Backend FastAPI — imagem única multi-stage para dev e prod
# ---------------------------------------------------------------------------
# Em dev, montamos o código via bind-mount (docker-compose); em prod, o
# código vai COPY-ado na imagem final. A mesma imagem serve para ECS/Fargate.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Pacotes de sistema:
#   - build-essential: necessário para bcrypt/asyncpg eventualmente.
#   - libpq-dev: client Postgres (útil se um dia adicionarmos psycopg).
#   - curl: para healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências primeiro, aproveitando cache de layer.
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage: dev — usa bind-mount do docker-compose e roda uvicorn com reload.
# ---------------------------------------------------------------------------
FROM base AS dev
ENV ENVIRONMENT=dev
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
# Stage: prod — copia o código, roda sem reload, como usuário não-root.
# ---------------------------------------------------------------------------
FROM base AS prod
ENV ENVIRONMENT=production

# Cria usuário não-root (hardening básico — e requisito do Fargate/ECS).
RUN groupadd --system app && useradd --system --gid app --home /app appuser
COPY . /app

# Entrypoint roda alembic upgrade head antes do uvicorn. Executável.
RUN chmod +x /app/scripts/entrypoint.sh \
 && chown -R appuser:app /app

USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
# Em prod rodamos com mais workers (ajuste conforme o tamanho do container).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
