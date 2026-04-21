"""Configurações da aplicação.

Centralizar aqui tem três vantagens:

1. Tipagem forte — erros de env viram erros de startup, não de runtime.
2. Sem ``os.getenv(...)`` espalhado pelo código.
3. Fácil de mockar em testes (``get_settings.cache_clear()``).

Em produção na AWS, os segredos (``JWT_SECRET``, ``ENCRYPTION_KEY``,
``INSTAGRAM_APP_SECRET``, ``DATABASE_URL``, ...) ficam no
**AWS Secrets Manager**. Quando a env ``AWS_SECRETS_NAME`` (ou
``AWS_SECRETS_ARN``) está definida, o módulo **antes** de instanciar
``Settings`` busca o JSON do segredo e injeta cada chave em
``os.environ``. Com isso o Pydantic carrega tudo como se fossem envs
normais e ganhamos rotação de segredo gratuitamente.

Em dev (sem essa env), seguimos lendo do ``.env`` na raiz do projeto.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# O .env fica na raiz do projeto (pasta ``ProjetoInstagram``), um nível
# acima deste arquivo (``app/config.py``).
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


# ---------------------------------------------------------------------------
# Bootstrap: carrega segredos do AWS Secrets Manager para os.environ
# ---------------------------------------------------------------------------
def _load_aws_secrets_into_env() -> None:
    """Se houver ``AWS_SECRETS_NAME`` ou ``AWS_SECRETS_ARN``, busca o segredo
    e injeta cada chave no ``os.environ``.

    Como o Pydantic lê ``os.environ`` antes do ``.env`` (por padrão), isso
    tem prioridade automática sobre valores locais. **Não sobrescreve**
    envs já setadas — útil pra permitir overrides em testes.

    Silencioso em erros conhecidos (ex.: rodando localmente sem boto3):
    só loga warning e segue. Isso é de propósito: se o SecretsManager
    estiver indisponível em prod, quem deve falhar é a validação do
    Pydantic (``JWT_SECRET`` ausente, etc.), com mensagem clara.
    """
    secret_id = os.environ.get("AWS_SECRETS_NAME") or os.environ.get(
        "AWS_SECRETS_ARN"
    )
    if not secret_id:
        return

    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        logger.warning(
            "AWS_SECRETS_NAME=%s definido, mas boto3 não está instalado. "
            "Pulando carga de segredos do Secrets Manager.",
            secret_id,
        )
        return

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    try:
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_id)
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "Falha ao buscar segredo '%s' no Secrets Manager: %s",
            secret_id,
            exc,
        )
        return

    raw = response.get("SecretString")
    if not raw:
        logger.warning("Segredo '%s' não tem SecretString (binário?).", secret_id)
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "Segredo '%s' não é um JSON válido. Esperado um objeto com "
            "chaves no formato das envs da aplicação.",
            secret_id,
        )
        return

    if not isinstance(data, dict):
        logger.error("Segredo '%s' deve ser um JSON de objeto.", secret_id)
        return

    injected = 0
    for key, value in data.items():
        if key in os.environ:
            # Respeita overrides explícitos (ex.: testes, debug local).
            continue
        os.environ[key] = str(value)
        injected += 1

    logger.info(
        "Secrets Manager: %d variáveis carregadas do segredo '%s'.",
        injected,
        secret_id,
    )


# Carrega **antes** de o Pydantic instanciar Settings.
_load_aws_secrets_into_env()


class Settings(BaseSettings):
    """Configurações globais (variáveis de ambiente)."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Ignora envs que não usamos (ex.: tokens legados do Facebook Login).
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Ambiente de execução
    # ------------------------------------------------------------------ #
    # ``dev`` ou ``production``. Usado para ajustar comportamentos como:
    # - ``DB_AUTO_CREATE``: só honrado em dev.
    # - Cookies: em prod exigimos ``Secure=true``.
    environment: str = "dev"

    # ------------------------------------------------------------------ #
    # Meta / Instagram — App credentials
    # ------------------------------------------------------------------ #
    # App ID do app Instagram Login (painel developers.facebook.com).
    instagram_app_id: str

    # App Secret. Usado em DOIS lugares distintos:
    #   1. Validar assinatura HMAC dos webhooks (X-Hub-Signature-256).
    #   2. Trocar o ``code`` do OAuth por um ``access_token`` (client_secret).
    # NUNCA deve vazar em logs nem no frontend.
    instagram_app_secret: str

    graph_api_version: str = "v25.0"
    graph_api_base_url: str = "https://graph.instagram.com"

    # ------------------------------------------------------------------ #
    # Verificação do handshake inicial do webhook (GET /webhook)
    # ------------------------------------------------------------------ #
    verify_token: str = "meu_token_de_verificacao"

    # Se true, valida a assinatura HMAC em todo POST /webhook.
    webhook_verify_signature: bool = False

    # ------------------------------------------------------------------ #
    # Banco de dados
    # ------------------------------------------------------------------ #
    database_url: str = "sqlite+aiosqlite:///./projetoinstagram.db"
    db_auto_create: bool = True

    # ------------------------------------------------------------------ #
    # Autenticação do nosso SaaS (JWT)
    # ------------------------------------------------------------------ #
    # Segredo HS256 para assinar os tokens de sessão dos clientes. Obrigatório.
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24  # 24h

    # ------------------------------------------------------------------ #
    # Frontend / CORS / Cookies de sessão
    # ------------------------------------------------------------------ #
    # Origem do frontend permitida pelo CORS. Em dev, ``http://localhost:3000``.
    # Em produção, o domínio do nosso SaaS (ex.: ``https://app.replibo.io``).
    frontend_origin: str = "http://localhost:3000"

    # Nome do cookie HttpOnly onde o JWT de sessão vive.
    session_cookie_name: str = "access_token"

    # ``Secure=true`` só funciona em HTTPS. Deixe ``false`` em dev local
    # (http://localhost) e ``true`` em produção.
    cookie_secure: bool = False

    # ``SameSite``:
    #   - "lax"  (default): permite top-level navigations (basta para nosso
    #     frontend same-origin via rewrites do Next).
    #   - "none": necessário se frontend e backend forem cross-site em prod
    #     (ex.: app.dominio.com vs api.dominio.com) E exige ``Secure=true``.
    cookie_samesite: str = "lax"

    # Domínio do cookie. ``None`` -> cookie apenas para o host atual (bom em
    # dev). Em prod, ex.: "dominio.com" para compartilhar entre subdomains.
    cookie_domain: str | None = None

    # ------------------------------------------------------------------ #
    # Criptografia em repouso (Fernet) — tokens do IG no DB
    # ------------------------------------------------------------------ #
    # Chave Fernet (base64 urlsafe, 32 bytes). Obrigatória.
    encryption_key: str

    # ------------------------------------------------------------------ #
    # OAuth Instagram (Business Login)
    # ------------------------------------------------------------------ #
    oauth_redirect_uri: str
    oauth_scopes: str = (
        "instagram_business_basic,"
        "instagram_business_manage_comments,"
        "instagram_business_manage_messages"
    )
    # Vida útil do ``state`` de OAuth (uso único + expiração).
    oauth_state_expires_minutes: int = 10

    # ------------------------------------------------------------------ #
    # Token refresher (job em background)
    # ------------------------------------------------------------------ #
    token_refresher_interval_minutes: int = 60
    token_refresh_before_days: int = 10

    # ------------------------------------------------------------------ #
    # Computed helpers
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_strong(cls, v: str) -> str:
        # Evita o erro clássico de subir com "changeme" em prod. 32 bytes
        # é o mínimo razoável para HS256.
        if len(v) < 32:
            raise ValueError(
                "JWT_SECRET muito curto. Gere um com "
                '`python -c "import secrets; print(secrets.token_urlsafe(64))"`.'
            )
        return v

    @field_validator("encryption_key")
    @classmethod
    def _fernet_key_valid(cls, v: str) -> str:
        # Validação estrutural: Fernet exige 44 chars base64 urlsafe.
        from cryptography.fernet import Fernet

        try:
            Fernet(v.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "ENCRYPTION_KEY inválida. Gere com "
                '`python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"`.'
            ) from exc
        return v

    @property
    def oauth_scopes_list(self) -> list[str]:
        """Retorna scopes como lista, ignorando espaços."""
        return [s.strip() for s in self.oauth_scopes.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna uma instância singleton de :class:`Settings`."""
    return Settings()  # type: ignore[call-arg]
