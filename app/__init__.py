"""Aplicação multi-tenant de auto-resposta a comentários do Instagram.

Organização em camadas:

- ``config``         → carrega/valida o .env.
- ``logging_config`` → configuração central de logging.
- ``db``             → modelos ORM e sessão async (SQLAlchemy).
- ``schemas``        → DTOs Pydantic (webhook + domínio).
- ``repositories``   → acesso a dados (TODAS as queries SQL vivem aqui).
- ``services``       → lógica de negócio (cliente IG, engine, orquestrador).
- ``security``       → validação de assinatura HMAC do webhook.
- ``api``            → roteadores FastAPI (camada HTTP, fina).
- ``scripts``        → utilitários (ex.: seed do banco).

Regra de dependência (quem pode importar quem)::

    api  →  services  →  repositories  →  db
                  ↘        ↗
                   schemas
                   security
"""
