"""Configuração central de logging.

Toda a aplicação usa ``logging.getLogger(__name__)`` e essa função apenas
configura *uma única vez* o root logger (formato, nível, destino).

Evita o uso de ``print(...)`` espalhado pelo código: logs passam a ter
timestamp, nível de severidade e nome do módulo de origem.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Configura o root logger da aplicação.

    Args:
        level: Nível mínimo de log (ex.: ``logging.DEBUG`` para troubleshooting).

    Notas:
        - Usa ``force=True`` para sobrescrever qualquer configuração prévia
          (útil quando o uvicorn já configurou algo antes de subirmos).
        - Escreve em ``stdout`` (em vez de ``stderr``) para que plataformas
          como Docker/Heroku capturem tudo num stream só.
        - Reconfigura ``stdout`` para UTF-8 no Windows (evita
          ``UnicodeEncodeError`` em logs com acentos/emoji no console cp1252).
    """
    # Em Windows, o console default (cp1252) quebra com caracteres não-ASCII.
    # ``reconfigure`` só existe em streams do CPython; protegemos com getattr.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
