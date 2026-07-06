"""
Regras de classificacao do status de BOLETO lido na planilha do ADM.

Toda comparacao e feita sobre uma versao NORMALIZADA do status:
  - upper case
  - sem acentos      (NÃO  -> NAO)
  - sem espacos extras (colapsa multiplos espacos em 1)
  - sem espacos no inicio/fim

Isso permite que o usuario digite na planilha em qualquer variacao:
  'Não Baixado', 'NAO BAIXADO', 'nao  baixado', '  NÃO BAIXADO  '
e tudo seja reconhecido como o mesmo status.
"""

from entrada.utils.texto_utils import normalizar_status


# Constantes ja estao em formato NORMALIZADO (upper + sem acento +
# espacos colapsados). Nao adicione variantes com acento aqui — a
# normalizacao do input cuida disso.
BLOQUEADOS = {
    "DDA",
    "CC",
    "CANCELADO",
    "NAO PROCESSAR",
    "DEBITO",   # variante sem acento de DÉBITO
    "CREDITO",  # variante sem acento de CRÉDITO
    "CARTAO",   # variante sem acento de CARTÃO
}


def status_boleto(texto: str) -> str:
    """Retorna o status normalizado pronto pra comparacao."""
    return normalizar_status(texto)


def deve_bloquear(status: str) -> bool:
    return status in BLOQUEADOS


def esta_nao_baixado(status: str) -> bool:
    return status == "NAO BAIXADO"
