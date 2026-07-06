# lib/vencimento.py
from datetime import date, timedelta

from config.modalidades import dia_vencimento


# ------------------------------
# Calculo da Pascoa (algoritmo de Meeus)
# ------------------------------
def _calcular_pascoa(ano: int) -> date:
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


# ------------------------------
# Feriados nacionais Brasil
# ------------------------------
def _feriados_nacionais(ano: int) -> set:
    pascoa = _calcular_pascoa(ano)

    return {
        # Feriados fixos
        date(ano, 1, 1),    # Confraternizacao Universal
        date(ano, 4, 21),   # Tiradentes
        date(ano, 5, 1),    # Dia do Trabalhador
        date(ano, 9, 7),    # Independencia
        date(ano, 10, 12),  # Nossa Senhora Aparecida
        date(ano, 11, 2),   # Finados
        date(ano, 11, 15),  # Proclamacao da Republica
        date(ano, 12, 25),  # Natal

        # Feriados moveis
        pascoa - timedelta(days=2),   # Sexta-feira Santa
        pascoa,                       # Pascoa
        pascoa + timedelta(days=60),  # Corpus Christi
    }


# ------------------------------
# Proximo dia util
# ------------------------------
def proximo_dia_util(d: date) -> date:
    feriados = _feriados_nacionais(d.year)

    while True:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        if d in feriados:
            d += timedelta(days=1)
            continue

        break

    return d


# ------------------------------
# Regra de vencimento
# ------------------------------
def calcular_vencimento(mes_ref: int, modalidade: str) -> date:
    """
    mes_ref no formato YYYYMM.
    Vencimento base depende da modalidade:
      - MOTORS: dia 7
      - IMOVEL: dia 15
    Se cair em fim de semana ou feriado nacional, avanca para o proximo dia util.
    """
    ano = mes_ref // 100
    mes = mes_ref % 100
    dia = dia_vencimento(modalidade)

    venc = date(ano, mes, dia)
    return proximo_dia_util(venc)
