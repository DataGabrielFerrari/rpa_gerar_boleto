"""
Configuracoes por modalidade do RPA Gerar Boleto.
Centraliza todas as regras que mudam entre MOTORS e IMOVEL.
"""

from typing import Set


# Status da coluna BOLETO da planilha que devem ser ignorados pelo robo.
# Sao iguais para MOTORS e IMOVEL.
#
# IMPORTANTE: as constantes ja estao em formato NORMALIZADO (UPPER,
# sem acentos, sem espacos extras). Nao adicione variantes com acento
# aqui (ex.: 'NÃO PROCESSAR') — a funcao normalizar_status do leitor
# faz a normalizacao do input antes da comparacao, entao 'Não Processar',
# 'NAO PROCESSAR', 'nao  processar' caem todos na mesma chave.
BLOQUEADOS: Set[str] = {
    "DDA",
    "CC",
    "CANCELADO",
    "CANCELADOS",
    "BLOQUEADO",
    "BLOQUEADOS",
    "DEBITO",
    "DEBITOS",
    "CREDITO",
    "CREDITOS",
    "NAO PROCESSAR",
}

# Dia base de vencimento do boleto por modalidade.
# Sera ajustado para o proximo dia util se cair em FDS ou feriado nacional.
DIA_VENCIMENTO = {
    "MOTORS": 7,
    "IMOVEL": 15,
}

# Rotulo usado no assunto e corpo do email.
LABEL_EMAIL = {
    "MOTORS": "Boletos Motors",
    "IMOVEL": "Boletos Imóvel",
}

# Modalidades validas
MODALIDADES_VALIDAS = ("MOTORS", "IMOVEL")


def validar_modalidade(modalidade: str) -> str:
    """
    Normaliza e valida a modalidade. Retorna em maiusculas.
    """
    mod = (modalidade or "").strip().upper()
    if mod not in MODALIDADES_VALIDAS:
        raise ValueError(
            f"Modalidade inválida: '{modalidade}'. "
            f"Use uma de: {', '.join(MODALIDADES_VALIDAS)}"
        )
    return mod


def dia_vencimento(modalidade: str) -> int:
    """
    Dia base de vencimento conforme modalidade.
    """
    return DIA_VENCIMENTO[validar_modalidade(modalidade)]


def label_email(modalidade: str) -> str:
    """
    Rotulo da modalidade para uso em emails.
    """
    return LABEL_EMAIL[validar_modalidade(modalidade)]


def deve_pular_dia(dia: int, modalidade: str) -> bool:
    """
    Regra de filtro de parcelas no worker:
      - MOTORS: pula parcelas com dia >= 15
      - IMOVEL: pula parcelas com dia <  15
    """
    mod = validar_modalidade(modalidade)
    if mod == "MOTORS":
        return dia >= 15
    return dia < 15
