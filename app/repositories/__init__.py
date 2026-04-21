"""Camada de acesso a dados.

Convenção: **toda query SQL vive aqui**. Serviços não emitem SQL direto;
eles compõem chamadas de repositório. Isso centraliza o schema de acesso
e facilita testes (podemos mockar os repositórios).
"""
