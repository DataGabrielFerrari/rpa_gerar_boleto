# lib/mes_ref.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


def yyyymm(dt: datetime) -> int:
    """Converte datetime -> YYYYMM (int)."""
    return dt.year * 100 + dt.month


def is_valid_yyyymm(value: int) -> bool:
    """Valida se int está no formato YYYYMM com mês 01..12."""
    if not isinstance(value, int):
        return False
    year = value // 100
    month = value % 100
    return 1900 <= year <= 2200 and 1 <= month <= 12


def add_months(yyyymm_int: int, months: int = 1) -> int:
    """Soma meses a um YYYYMM (int)."""
    if not is_valid_yyyymm(yyyymm_int):
        raise ValueError(f"YYYYMM inválido: {yyyymm_int}")

    y = yyyymm_int // 100
    m = yyyymm_int % 100

    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1

    out = y * 100 + m
    if not is_valid_yyyymm(out):
        raise ValueError(f"Resultado YYYYMM inválido: {out}")
    return out


@dataclass(frozen=True)
class DecisaoMesRef:
    mes_ref: Optional[int]
    # True quando é reprocessamento do mesmo mes_ref (não atualiza planilha, etc.)
    modo_reexecucao: bool
    # True quando é elegível pra criar lote agora
    pode_criar_lote: bool


def decidir_mes_ref(
    *,
    mes_ref_alvo: Optional[int],
    ultimo_mes_ref: Optional[int],
    reexecucao: bool,
) -> DecisaoMesRef:
    """
    Decisão padrão do seu fluxo:
      - Se mes_ref_alvo estiver NULL => não roda (mes_ref=None, pode_criar_lote=False)
      - Se ultimo_mes_ref for NULL => roda (primeira execução)
      - Se mes_ref_alvo > ultimo_mes_ref => roda (novo ciclo)
      - Se mes_ref_alvo == ultimo_mes_ref => só roda se reexecucao=True
    """

    # Sem alvo => explicitamente "não executar"
    if mes_ref_alvo is None:
        return DecisaoMesRef(mes_ref=None, modo_reexecucao=False, pode_criar_lote=False)

    if not is_valid_yyyymm(mes_ref_alvo):
        raise ValueError(f"mes_ref_alvo inválido (esperado YYYYMM): {mes_ref_alvo}")

    if ultimo_mes_ref is not None and not is_valid_yyyymm(ultimo_mes_ref):
        raise ValueError(f"ultimo_mes_ref inválido (esperado YYYYMM): {ultimo_mes_ref}")

    # Primeira execução (ou ADM novo)
    if ultimo_mes_ref is None:
        return DecisaoMesRef(mes_ref=mes_ref_alvo, modo_reexecucao=False, pode_criar_lote=True)

    # Novo ciclo
    if mes_ref_alvo > ultimo_mes_ref:
        return DecisaoMesRef(mes_ref=mes_ref_alvo, modo_reexecucao=False, pode_criar_lote=True)

    # Mesmo ciclo: só com reexecucao
    if mes_ref_alvo == ultimo_mes_ref and reexecucao:
        return DecisaoMesRef(mes_ref=mes_ref_alvo, modo_reexecucao=True, pode_criar_lote=True)

    # Alvo menor que o último ou mesmo mês sem reexecução: não cria lote
    return DecisaoMesRef(mes_ref=mes_ref_alvo, modo_reexecucao=False, pode_criar_lote=False)


# Se você preferir manter as funções separadas (API parecida com sua versão antiga):

def pode_criar_lote(*, mes_ref: int, ultimo_mes_ref: Optional[int], reexecucao: bool) -> bool:
    if not is_valid_yyyymm(mes_ref):
        raise ValueError(f"mes_ref inválido (YYYYMM): {mes_ref}")

    if ultimo_mes_ref is None:
        return True

    if not is_valid_yyyymm(ultimo_mes_ref):
        raise ValueError(f"ultimo_mes_ref inválido (YYYYMM): {ultimo_mes_ref}")

    return (mes_ref > ultimo_mes_ref) or (mes_ref == ultimo_mes_ref and reexecucao)


def decidir_modo_reexecucao(*, mes_ref: int, ultimo_mes_ref: Optional[int], reexecucao: bool) -> bool:
    if ultimo_mes_ref is None:
        return False

    if not is_valid_yyyymm(mes_ref) or not is_valid_yyyymm(ultimo_mes_ref):
        raise ValueError(f"YYYYMM inválido: mes_ref={mes_ref}, ultimo_mes_ref={ultimo_mes_ref}")

    return (mes_ref == ultimo_mes_ref) and reexecucao