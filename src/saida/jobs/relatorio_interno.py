"""
Relatorio interno de execucao — enviado para rpa.ademicon@gmail.com.

NAO vai para o ADM/cliente. E um relatorio operacional completo para
acompanhamento interno do RPA Gerar Boleto, contendo:

  - Resumo geral (total, baixados, adiantados, nao baixados, falhas, taxa)
  - Duracao do lote
  - Breakdown de erros por categoria com grafico matplotlib (se disponivel)
  - Breakdown por consultor (baixados x adiantados x nao baixados x falhas)
  - Lista detalhada de cada categoria de problema
  - Lista de cotas para REEXECUTAR (safeguard — tentativas >= 3, FALHA)
  - Cotas nao encontradas na pagina do cliente

Adaptado de rpa_ofertar_lance/saida/relatorio_interno.py.
Enviado ao final da SAIDA, logo apos o email do cliente.
Nunca levanta excecao — falha silenciosa (nao derruba o lote).

Contagens usam a ULTIMA tentativa de cada cota (maior id_cota por
grupo+cota), igual ao email do cliente (_buscar_metricas_lote).
"""

import base64
import io
import os
import re
import traceback
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build

from shared.google_auth import criar_servico_sheets
from shared.log import log_info, log_erro
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    listar_cotas_nao_encontradas,
)
from saida.lib.db import get_conn

EMAIL_RELATORIO_INTERNO = os.getenv(
    "EMAIL_RELATORIO_INTERNO",
    "rpa.ademicon@gmail.com",
)

CATEGORIAS_ORDEM = [
    "Erro de Acesso/Login",
    "Excluído/Desistente",
    "Cota Indisponível",
    "Modalidade Diferente",
    "Valor a Pagar",
    "AVAPRO Instável",
    "3 Tentativas (Safeguard)",
    "Outros Erros",
    "Outros NAO_BAIXADO",
]

CORES_GRAF = {
    "Erro de Acesso/Login":     "#C62828",
    "Excluído/Desistente":      "#5D4037",
    "Cota Indisponível":        "#EF6C00",
    "Modalidade Diferente":     "#00838F",
    "Valor a Pagar":            "#558B2F",
    "AVAPRO Instável":          "#F9A825",
    "3 Tentativas (Safeguard)": "#6A1B9A",
    "Outros Erros":             "#37474F",
    "Outros NAO_BAIXADO":       "#78909C",
}

CORES_HTML = {
    "Erro de Acesso/Login":     "#C62828",
    "Excluído/Desistente":      "#5D4037",
    "Cota Indisponível":        "#EF6C00",
    "Modalidade Diferente":     "#00838F",
    "Valor a Pagar":            "#2E7D32",
    "AVAPRO Instável":          "#F57F17",
    "3 Tentativas (Safeguard)": "#6A1B9A",
    "Outros Erros":             "#37474F",
    "Outros NAO_BAIXADO":       "#546E7A",
}

MESES_ABREV = {1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
               7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"}


# =========================================================
# DB HELPERS
# =========================================================

def _fetchone(sql: str, params: tuple):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _fetchall(sql: str, params: tuple):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []


# =========================================================
# QUERIES
# =========================================================

# CTE compartilhada: ultima tentativa de cada cota do lote
_CTE_ULTIMA = """
    WITH ultima AS (
        SELECT DISTINCT ON (fc.grupo, fc.cota)
            fc.nome_cliente,
            fc.nome_consultor,
            fc.grupo,
            fc.cota,
            fc.status,
            COALESCE(fc.tentativas, 0)  AS tentativas,
            COALESCE(fc.observacao, '') AS observacao,
            COALESCE(fc.parcelas_atraso, 0) AS parcelas_atraso
        FROM tbl_fila_cotas fc
        WHERE fc.id_fila_adm = %s
        ORDER BY fc.grupo, fc.cota, fc.id_cota DESC
    )
"""


def _resumo_lote(id_fila_adm: int) -> dict:
    sql = _CTE_ULTIMA + """
        SELECT
            COUNT(*)                                                             AS total,
            COALESCE(SUM(CASE WHEN status = 'BAIXADO'     THEN 1 ELSE 0 END), 0) AS baixados,
            COALESCE(SUM(CASE WHEN status = 'ADIANTADO'   THEN 1 ELSE 0 END), 0) AS adiantados,
            COALESCE(SUM(CASE WHEN status = 'NAO_BAIXADO' THEN 1 ELSE 0 END), 0) AS nao_baixados,
            COALESCE(SUM(CASE WHEN status = 'FALHA'       THEN 1 ELSE 0 END), 0) AS falhas,
            COALESCE(SUM(CASE WHEN status IN ('PENDENTE','PROCESSANDO')
                              THEN 1 ELSE 0 END), 0)                             AS pendentes,
            COALESCE(SUM(CASE WHEN status = 'BAIXADO' AND parcelas_atraso > 0
                              THEN 1 ELSE 0 END), 0)                             AS com_atraso
        FROM ultima
    """
    row = _fetchone(sql, (id_fila_adm,))
    if not row:
        return {}
    resumo = {
        "total":        int(row[0]),
        "baixados":     int(row[1]),
        "adiantados":   int(row[2]),
        "nao_baixados": int(row[3]),
        "falhas":       int(row[4]),
        "pendentes":    int(row[5]),
        "com_atraso":   int(row[6]),
    }
    # Link do Drive + duracao (min/max das cotas do lote)
    row2 = _fetchone(
        """
        SELECT
            fa.link_drive,
            (SELECT MIN(fc.hora_inicio) FROM tbl_fila_cotas fc
              WHERE fc.id_fila_adm = fa.id_fila_adm) AS hora_inicio,
            (SELECT MAX(fc.hora_fim) FROM tbl_fila_cotas fc
              WHERE fc.id_fila_adm = fa.id_fila_adm)  AS hora_fim
        FROM tbl_fila_adm fa
        WHERE fa.id_fila_adm = %s
        """,
        (id_fila_adm,),
    )
    if row2:
        resumo["link_drive"]  = str(row2[0] or "")
        resumo["hora_inicio"] = row2[1]
        resumo["hora_fim"]    = row2[2]
    return resumo


def _categorizar(obs: str, status: str, tentativas: int) -> str:
    """Categoriza um problema (FALHA/NAO_BAIXADO) pela observacao."""
    o = (obs or "").lower()
    if "login" in o or "cdp" in o or "meus-clientes" in o:
        return "Erro de Acesso/Login"
    if "exclu" in o or "desist" in o:
        return "Excluído/Desistente"
    if "indispon" in o or "localizada" in o or "duplicado" in o:
        return "Cota Indisponível"
    if "modalidade diferente" in o or "outra modalidade" in o:
        return "Modalidade Diferente"
    if "valor a pagar" in o:
        return "Valor a Pagar"
    if ("respondeu" in o or "carregamento" in o or "instavel" in o
            or "instável" in o or "travamento" in o or "checkbox" in o
            or "vencimento incorreto" in o):
        return "AVAPRO Instável"
    if status == "FALHA" and tentativas >= 3:
        return "3 Tentativas (Safeguard)"
    if status == "FALHA":
        return "Outros Erros"
    return "Outros NAO_BAIXADO"


def _problemas_lote(id_fila_adm: int) -> List[Dict[str, Any]]:
    """
    Retorna todas as cotas com problema (FALHA/NAO_BAIXADO) do lote,
    ja com a categoria calculada. Uma query so — counts e detalhes
    derivados em Python.
    """
    sql = _CTE_ULTIMA + """
        SELECT
            COALESCE(NULLIF(TRIM(nome_cliente), ''),   '(sem nome)')       AS nome_cliente,
            COALESCE(NULLIF(TRIM(nome_consultor), ''), 'SEM CONSULTOR')    AS consultor,
            grupo,
            cota,
            tentativas,
            COALESCE(NULLIF(TRIM(observacao), ''),     '(sem observacao)') AS observacao,
            status
        FROM ultima
        WHERE status IN ('FALHA', 'NAO_BAIXADO')
        ORDER BY nome_consultor, grupo, cota
    """
    rows = _fetchall(sql, (id_fila_adm,))
    problemas = []
    for r in rows:
        problemas.append({
            "nome_cliente": str(r[0]),
            "consultor":    str(r[1]),
            "grupo":        str(r[2]),
            "cota":         str(r[3]),
            "tentativas":   int(r[4]),
            "observacao":   str(r[5]),
            "status":       str(r[6]),
            "categoria":    _categorizar(str(r[5]), str(r[6]), int(r[4])),
        })
    return problemas


def _cotas_por_consultor(id_fila_adm: int) -> list:
    sql = _CTE_ULTIMA + """
        SELECT
            COALESCE(NULLIF(TRIM(nome_consultor), ''), 'SEM CONSULTOR')    AS consultor,
            COUNT(*)                                                       AS total,
            SUM(CASE WHEN status = 'BAIXADO'     THEN 1 ELSE 0 END)        AS baixados,
            SUM(CASE WHEN status = 'ADIANTADO'   THEN 1 ELSE 0 END)        AS adiantados,
            SUM(CASE WHEN status = 'NAO_BAIXADO' THEN 1 ELSE 0 END)        AS nao_baixados,
            SUM(CASE WHEN status = 'FALHA'       THEN 1 ELSE 0 END)        AS falhas
        FROM ultima
        GROUP BY consultor
        ORDER BY baixados DESC, total DESC
    """
    rows = _fetchall(sql, (id_fila_adm,))
    return [
        {
            "consultor":    str(r[0]),
            "total":        int(r[1]),
            "baixados":     int(r[2]),
            "adiantados":   int(r[3]),
            "nao_baixados": int(r[4]),
            "falhas":       int(r[5]),
        }
        for r in rows
    ]


# =========================================================
# GRAFICO
# =========================================================

def _gerar_grafico_png(categorias: dict, nome_adm: str, modalidade: str) -> Optional[bytes]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    dados = {}
    for cat in CATEGORIAS_ORDEM:
        if categorias.get(cat, 0) > 0:
            dados[cat] = categorias[cat]
    for cat, total in categorias.items():
        if cat not in dados and total > 0:
            dados[cat] = total

    if not dados:
        return None

    labels = list(dados.keys())
    values = list(dados.values())
    cores  = [CORES_GRAF.get(lbl, "#90A4AE") for lbl in labels]

    fig, ax = plt.subplots(figsize=(11, max(3.5, len(labels) * 0.75 + 2)))
    fig.patch.set_facecolor("#FAFAFA")
    ax.set_facecolor("#FFFFFF")

    bars = ax.barh(labels, values, color=cores, edgecolor="white", height=0.55)

    for bar, val in zip(bars, values):
        ax.text(
            bar.get_width() + max(values) * 0.01,
            bar.get_y() + bar.get_height() / 2,
            str(val),
            va="center", ha="left",
            fontsize=10, fontweight="bold", color="#333333",
        )

    total_erros = sum(values)
    ax.set_xlabel("Quantidade de cotas", fontsize=10, color="#555555")
    ax.set_title(
        f"Problemas por categoria  —  {nome_adm}  ({modalidade})\n"
        f"Total: {total_erros}  ·  {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        fontsize=12, fontweight="bold", pad=14, color="#212121",
    )
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.22)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#EEEEEE")
    ax.spines["bottom"].set_color("#EEEEEE")
    ax.tick_params(axis="y", labelsize=9, colors="#333333")
    ax.tick_params(axis="x", labelsize=8, colors="#888888")
    ax.xaxis.grid(True, color="#F0F0F0", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# =========================================================
# HTML
# =========================================================

def _badge(texto: str, cor: str) -> str:
    return (
        f'<span style="background-color:{cor}; color:#FFFFFF; font-size:10px; '
        f'font-weight:700; padding:2px 8px; border-radius:10px; white-space:nowrap;">'
        f'{texto}</span>'
    )


def _tabela_cotas(cotas: list, mostrar_consultor: bool = True) -> str:
    if not cotas:
        return '<p style="color:#9E9E9E; font-size:12px; margin:0;">(nenhuma)</p>'

    header_consultor = (
        '<th style="padding:8px 10px; text-align:left; font-size:10px; '
        'color:#888; font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        'border-bottom:2px solid #EEEEEE;">Consultor</th>'
        if mostrar_consultor else ""
    )
    linhas = []
    for c in cotas:
        col_consultor = (
            f'<td style="padding:8px 10px; font-size:12px; color:#616161; '
            f'border-bottom:1px solid #F5F5F5;">{c.get("consultor","")}</td>'
            if mostrar_consultor else ""
        )
        obs = c.get("observacao", "")
        obs_exib = (obs[:90] + "…") if len(obs) > 90 else obs
        linhas.append(
            f'<tr>'
            f'{col_consultor}'
            f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5;">{c["nome_cliente"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#555; white-space:nowrap; '
            f'border-bottom:1px solid #F5F5F5;">{c["grupo"]}&nbsp;/&nbsp;{c["cota"]}</td>'
            f'<td style="padding:8px 10px; font-size:11px; color:#888; '
            f'border-bottom:1px solid #F5F5F5;">{obs_exib}</td>'
            f'</tr>'
        )

    return (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px; overflow:hidden;">'
        f'<thead><tr style="background:#FAFAFA;">'
        f'{header_consultor}'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Cliente</th>'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Grupo / Cota</th>'
        f'<th style="padding:8px 10px; text-align:left; font-size:10px; color:#888; '
        f'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
        f'border-bottom:2px solid #EEEEEE;">Observação</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(linhas)}</tbody>'
        f'</table>'
    )


def _secao_html(titulo: str, cor: str, conteudo: str) -> str:
    return f"""
    <tr><td style="padding:20px 32px 0 32px;">
      <div style="border-left:4px solid {cor}; padding-left:14px; margin-bottom:10px;">
        <span style="font-family:'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:700;
          color:{cor}; text-transform:uppercase; letter-spacing:1px;">{titulo}</span>
      </div>
      {conteudo}
    </td></tr>"""


def _montar_html(
    nome_adm: str,
    modalidade: str,
    resumo: dict,
    categorias: dict,
    por_consultor: list,
    safeguard: list,
    detalhe_cats: dict,
    nao_encontradas: list,
    mes_formatado: str,
    duracao_str: str,
) -> str:
    total        = resumo.get("total", 0)
    baixados     = resumo.get("baixados", 0)
    adiantados   = resumo.get("adiantados", 0)
    nao_baixados = resumo.get("nao_baixados", 0)
    falhas       = resumo.get("falhas", 0)
    pendentes    = resumo.get("pendentes", 0)
    com_atraso   = resumo.get("com_atraso", 0)
    taxa         = f"{baixados/total*100:.1f}%" if total > 0 else "—"

    # ── Breakdown por consultor ──
    linhas_cons = []
    for c in por_consultor:
        taxa_c = f"{c['baixados']/c['total']*100:.0f}%" if c['total'] > 0 else "—"
        linhas_cons.append(
            f'<tr>'
            f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5;">{c["consultor"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#424242; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["total"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#2E7D32; font-weight:700; '
            f'text-align:center; border-bottom:1px solid #F5F5F5;">{c["baixados"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#1565C0; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["adiantados"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#E65100; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["nao_baixados"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#B71C1C; text-align:center; '
            f'border-bottom:1px solid #F5F5F5;">{c["falhas"]}</td>'
            f'<td style="padding:8px 10px; font-size:12px; color:#1565C0; font-weight:700; '
            f'text-align:center; border-bottom:1px solid #F5F5F5;">{taxa_c}</td>'
            f'</tr>'
        )
    _th = (
        'style="padding:8px 10px; font-size:10px; color:{cor}; font-weight:700; '
        'text-transform:uppercase; letter-spacing:1px; border-bottom:2px solid #EEEEEE; '
        'text-align:{al};"'
    )
    tabela_consultor = (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
        f'<thead><tr style="background:#FAFAFA;">'
        f'<th {_th.format(cor="#888", al="left")}>Consultor</th>'
        f'<th {_th.format(cor="#888", al="center")}>Total</th>'
        f'<th {_th.format(cor="#2E7D32", al="center")}>Baixados</th>'
        f'<th {_th.format(cor="#1565C0", al="center")}>Adiant.</th>'
        f'<th {_th.format(cor="#E65100", al="center")}>Não baix.</th>'
        f'<th {_th.format(cor="#B71C1C", al="center")}>Falhas</th>'
        f'<th {_th.format(cor="#1565C0", al="center")}>Taxa</th>'
        f'</tr></thead><tbody>{"".join(linhas_cons)}</tbody></table>'
        if linhas_cons else '<p style="color:#9E9E9E; font-size:12px; margin:0;">(nenhum dado)</p>'
    )

    # ── Categorias de problema ──
    linhas_cat = []
    total_problemas = sum(categorias.values())
    for cat in CATEGORIAS_ORDEM:
        n = categorias.get(cat, 0)
        if n == 0:
            continue
        cor = CORES_HTML.get(cat, "#546E7A")
        pct = f"{n/total_problemas*100:.0f}%" if total_problemas > 0 else ""
        bar_w = int(n / max(categorias.values()) * 120) if categorias else 0
        linhas_cat.append(
            f'<tr>'
            f'<td style="padding:8px 12px; font-size:12px; color:#212121; font-weight:600; '
            f'border-bottom:1px solid #F5F5F5; white-space:nowrap;">'
            f'{_badge(cat, cor)}</td>'
            f'<td style="padding:8px 12px; border-bottom:1px solid #F5F5F5;">'
            f'<div style="background:{cor}; height:8px; width:{bar_w}px; border-radius:4px; display:inline-block;"></div></td>'
            f'<td style="padding:8px 12px; font-size:13px; color:{cor}; font-weight:700; '
            f'border-bottom:1px solid #F5F5F5; text-align:right; white-space:nowrap;">{n} &nbsp;<span style="font-size:10px;color:#aaa;">{pct}</span></td>'
            f'</tr>'
        )
    tabela_cats = (
        f'<table width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
        f'<tbody>{"".join(linhas_cat)}</tbody></table>'
        if linhas_cat else
        '<p style="color:#2E7D32; font-size:13px; font-weight:600; margin:0;">✓ Nenhum problema registrado</p>'
    )

    # ── Safeguard: lista para reexecutar ──
    if safeguard:
        linhas_sg = []
        for c in safeguard:
            obs_exib = (c['observacao'][:80] + "…") if len(c['observacao']) > 80 else c['observacao']
            linhas_sg.append(
                f'<tr>'
                f'<td style="padding:8px 10px; font-size:12px; color:#6A1B9A; font-weight:700; '
                f'border-bottom:1px solid #F5F5F5; white-space:nowrap;">{c["grupo"]}&nbsp;/&nbsp;{c["cota"]}</td>'
                f'<td style="padding:8px 10px; font-size:12px; color:#212121; font-weight:600; '
                f'border-bottom:1px solid #F5F5F5;">{c["nome_cliente"]}</td>'
                f'<td style="padding:8px 10px; font-size:12px; color:#616161; '
                f'border-bottom:1px solid #F5F5F5;">{c["consultor"]}</td>'
                f'<td style="padding:8px 10px; font-size:11px; color:#B71C1C; font-weight:700; '
                f'text-align:center; border-bottom:1px solid #F5F5F5;">{c["tentativas"]}x</td>'
                f'<td style="padding:8px 10px; font-size:11px; color:#888; '
                f'border-bottom:1px solid #F5F5F5;">{obs_exib}</td>'
                f'</tr>'
            )
        _th_sg = (
            'style="padding:8px 10px; text-align:{al}; font-size:10px; color:#6A1B9A; '
            'font-weight:700; text-transform:uppercase; letter-spacing:1px; '
            'border-bottom:2px solid #CE93D8;"'
        )
        tabela_sg = (
            f'<div style="background:#FFF3E0; border:1px solid #FFB74D; border-radius:6px; '
            f'padding:10px 14px; margin-bottom:12px; font-size:12px; color:#E65100; font-weight:700;">'
            f'⚠ {len(safeguard)} cota(s) esgotaram as tentativas — reexecução manual necessária.</div>'
            f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse; border:1px solid #EEEEEE; border-radius:6px;">'
            f'<thead><tr style="background:#F3E5F5;">'
            f'<th {_th_sg.format(al="left")}>Grupo / Cota</th>'
            f'<th {_th_sg.format(al="left")}>Cliente</th>'
            f'<th {_th_sg.format(al="left")}>Consultor</th>'
            f'<th {_th_sg.format(al="center")}>Tent.</th>'
            f'<th {_th_sg.format(al="left")}>Último erro</th>'
            f'</tr></thead><tbody>{"".join(linhas_sg)}</tbody></table>'
        )
    else:
        tabela_sg = '<p style="color:#2E7D32; font-size:13px; font-weight:600; margin:0;">✓ Nenhuma cota em safeguard</p>'

    # ── Cotas nao encontradas ──
    secao_nao_enc = ""
    if nao_encontradas:
        cotas_ne = [
            {
                "nome_cliente": str(c.get("nome_cliente") or "(sem nome)"),
                "grupo":        str(c.get("grupo") or ""),
                "cota":         str(c.get("cota") or ""),
                "observacao":   "não localizada na página do cliente",
            }
            for c in nao_encontradas
        ]
        secao_nao_enc = _secao_html(
            f"Cotas Não Encontradas ({len(cotas_ne)})",
            "#E65100",
            _tabela_cotas(cotas_ne, mostrar_consultor=False),
        )

    # ── Detalhes por categoria ──
    secoes_detalhe = ""
    for cat in CATEGORIAS_ORDEM:
        cotas = detalhe_cats.get(cat, [])
        if not cotas or cat == "3 Tentativas (Safeguard)":
            continue
        cor = CORES_HTML.get(cat, "#546E7A")
        secoes_detalhe += _secao_html(
            f"{cat} ({len(cotas)})",
            cor,
            _tabela_cotas(cotas[:50]),
        )

    link_drive = resumo.get("link_drive", "")
    link_html = (
        f'<a href="{link_drive}" style="color:#1565C0;">{link_drive}</a>'
        if link_drive else "(sem link)"
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Relatorio Interno — Gerar Boleto</title></head>
<body style="margin:0;padding:0;background:#EEEEEE;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellspacing="0" cellpadding="0" bgcolor="#EEEEEE">
<tr><td align="center" style="padding:28px 12px;">

<table width="680" cellspacing="0" cellpadding="0" bgcolor="#FFFFFF"
  style="max-width:680px;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr><td bgcolor="#1B5E20" style="padding:24px 32px; background:#1B5E20;">
    <div style="color:#C8E6C9;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;">
      INTERNO &middot; RPA ADEMICON &middot; USO OPERACIONAL
    </div>
    <div style="color:#FFFFFF;font-size:22px;font-weight:700;margin-top:8px;">
      Relatório de Execução — Gerar Boleto
    </div>
    <div style="color:#A5D6A7;font-size:13px;margin-top:4px;">
      {nome_adm} &nbsp;&middot;&nbsp; {modalidade} &nbsp;&middot;&nbsp; {mes_formatado}
      &nbsp;&middot;&nbsp; {datetime.now().strftime('%d/%m/%Y %H:%M')}
    </div>
  </td></tr>

  <!-- Stats principais -->
  <tr><td style="padding:20px 32px 0 32px;">
    <table width="100%" cellspacing="0" cellpadding="0"
      style="border:1px solid #EEEEEE;border-radius:8px;overflow:hidden;">
    <tr>
      <td width="16%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:24px;font-weight:700;color:#424242;">{total}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Total</div>
      </td>
      <td width="16%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:24px;font-weight:700;color:#2E7D32;">{baixados}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Baixados</div>
      </td>
      <td width="16%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:24px;font-weight:700;color:#1565C0;">{adiantados}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Adiantados</div>
      </td>
      <td width="16%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:24px;font-weight:700;color:#E65100;">{nao_baixados}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Não baixados</div>
      </td>
      <td width="16%" align="center" style="padding:16px 4px;border-right:1px solid #EEE;">
        <div style="font-size:24px;font-weight:700;color:#B71C1C;">{falhas}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Falhas</div>
      </td>
      <td width="16%" align="center" style="padding:16px 4px;">
        <div style="font-size:24px;font-weight:700;color:#1565C0;">{taxa}</div>
        <div style="font-size:9px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:4px;font-weight:700;">Taxa sucesso</div>
      </td>
    </tr>
    </table>
    <div style="font-size:11px;color:#9E9E9E;margin-top:6px;text-align:right;">
      Duração: {duracao_str}
      {"&nbsp;|&nbsp;<span style='color:#2E7D32;'>"+str(com_atraso)+" boleto(s) com parcela em atraso</span>" if com_atraso > 0 else ""}
      {"&nbsp;|&nbsp;<span style='color:#E65100;'>"+str(pendentes)+" pendentes</span>" if pendentes > 0 else ""}
      {"&nbsp;|&nbsp;<span style='color:#6A1B9A;font-weight:700;'>"+str(len(safeguard))+" para reexecutar</span>" if safeguard else ""}
    </div>
  </td></tr>

  <!-- Categorias -->
  <tr><td style="padding:16px 32px 0 32px;">
    <div style="font-family:'Segoe UI',Arial,sans-serif;font-size:11px;font-weight:700;
      color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
      Problemas por categoria (gráfico em anexo)
    </div>
    {tabela_cats}
  </td></tr>

  <!-- Por consultor -->
  {_secao_html("Por Consultor / Funcionário", "#1565C0", tabela_consultor)}

  <!-- Safeguard -->
  {_secao_html("⚠ Para Reexecutar (Safeguard)", "#6A1B9A", tabela_sg)}

  <!-- Cotas nao encontradas -->
  {secao_nao_enc}

  <!-- Detalhes por categoria -->
  {secoes_detalhe}

  <!-- Drive -->
  <tr><td style="padding:16px 32px 0 32px;">
    <div style="font-size:11px;color:#888;font-weight:700;text-transform:uppercase;
      letter-spacing:1px;margin-bottom:4px;">Pasta no Drive</div>
    <div style="font-size:12px;">{link_html}</div>
  </td></tr>

  <!-- Footer -->
  <tr><td bgcolor="#F5F5F5" style="padding:14px 32px;border-top:1px solid #EEE;
    font-size:10px;color:#9E9E9E;text-align:center;margin-top:20px;">
    Email automatico gerado pelo RPA &middot; USO INTERNO &middot; Nao responder
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _montar_txt(
    nome_adm: str,
    modalidade: str,
    resumo: dict,
    categorias: dict,
    por_consultor: list,
    safeguard: list,
    nao_encontradas: list,
    duracao_str: str,
    mes_formatado: str,
) -> str:
    total = resumo.get("total", 0)
    taxa  = f"{resumo.get('baixados',0)/total*100:.1f}%" if total > 0 else "—"
    linhas = [
        "=" * 60,
        "RELATORIO INTERNO — RPA GERAR BOLETO (USO OPERACIONAL)",
        "=" * 60,
        f"ADM        : {nome_adm}",
        f"Modalidade : {modalidade}",
        f"Mes ref    : {mes_formatado}",
        f"Geracao    : {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"Duracao    : {duracao_str}",
        "",
        "RESUMO",
        f"  Total          : {total}",
        f"  Baixados       : {resumo.get('baixados',0)}",
        f"  Adiantados     : {resumo.get('adiantados',0)}",
        f"  Nao baixados   : {resumo.get('nao_baixados',0)}",
        f"  Falhas         : {resumo.get('falhas',0)}",
        f"  Pendentes      : {resumo.get('pendentes',0)}",
        f"  Com atraso     : {resumo.get('com_atraso',0)}",
        f"  Taxa sucesso   : {taxa}",
        "",
        "PROBLEMAS POR CATEGORIA",
    ]
    for cat in CATEGORIAS_ORDEM:
        n = categorias.get(cat, 0)
        if n:
            linhas.append(f"  {cat:<30}: {n}")
    linhas += ["", "POR CONSULTOR"]
    for c in por_consultor:
        taxa_c = f"{c['baixados']/c['total']*100:.0f}%" if c['total'] > 0 else "—"
        linhas.append(
            f"  {c['consultor'][:28]:<28} total={c['total']} "
            f"baixados={c['baixados']} adiant={c['adiantados']} "
            f"nao_baix={c['nao_baixados']} falhas={c['falhas']} taxa={taxa_c}"
        )
    if safeguard:
        linhas += ["", f"PARA REEXECUTAR ({len(safeguard)} cotas — safeguard)"]
        for c in safeguard:
            linhas.append(
                f"  {c['grupo']}/{c['cota']} | {c['nome_cliente'][:30]} "
                f"| {c['consultor'][:20]} | {c['tentativas']}x | {c['observacao'][:60]}"
            )
    if nao_encontradas:
        linhas += ["", f"COTAS NAO ENCONTRADAS ({len(nao_encontradas)})"]
        for c in nao_encontradas:
            linhas.append(
                f"  {c.get('grupo','')}/{c.get('cota','')} | {str(c.get('nome_cliente') or '')[:40]}"
            )
    linhas += ["", "---", "Email automatico — uso interno. Nao responder."]
    return "\n".join(linhas)


# =========================================================
# HELPERS
# =========================================================

def _duracao(resumo: dict) -> str:
    try:
        inicio = resumo.get("hora_inicio")
        fim    = resumo.get("hora_fim") or datetime.now()
        if inicio:
            delta = fim - inicio
            mins  = int(delta.total_seconds() // 60)
            segs  = int(delta.total_seconds() % 60)
            return f"{mins}m {segs}s"
    except Exception:
        pass
    return "—"


def _formatar_mes(mes_ref) -> str:
    try:
        s   = str(mes_ref)
        ano = s[:4]
        mes = int(s[4:6])
        return f"{MESES_ABREV.get(mes,'?')}/{ano}"
    except Exception:
        return str(mes_ref or "")


def _get_gmail_service():
    sheets_service = criar_servico_sheets()
    creds = sheets_service._http.credentials
    return build("gmail", "v1", credentials=creds)


# =========================================================
# PONTO DE ENTRADA
# =========================================================

def gerar_relatorio_interno(id_fila_adm: int) -> bool:
    """
    Gera e envia o relatorio interno completo para rpa.ademicon@gmail.com.
    Chamado na SAIDA logo apos o email do cliente.
    Nunca levanta excecao — falha silenciosa (retorna False).
    """
    caminho_log = None
    try:
        dados = obter_dados_adm_por_fila(id_fila_adm)
        if not dados:
            return False

        caminho_log = dados.get("caminho_log")
        nome_adm    = str(dados.get("nome") or "")
        modalidade  = str(dados.get("modalidade") or "")
        mes_ref     = dados.get("mes_ref")

        log_info(caminho_log, "RELATORIO", id_fila_adm,
                 "Coletando dados do lote",
                 f"nome_adm={nome_adm} modalidade={modalidade}")

        resumo = _resumo_lote(id_fila_adm)
        if not resumo:
            log_erro(caminho_log, "RELATORIO", id_fila_adm,
                     "Resumo do lote", "lote sem cotas / nao encontrado")
            return False

        problemas     = _problemas_lote(id_fila_adm)
        por_consultor = _cotas_por_consultor(id_fila_adm)
        duracao_str   = _duracao(resumo)
        mes_formatado = _formatar_mes(mes_ref)

        try:
            nao_encontradas = listar_cotas_nao_encontradas(id_fila_adm)
        except Exception:
            nao_encontradas = []

        # Counts + detalhes por categoria (derivados da lista de problemas)
        categorias: Dict[str, int] = {}
        detalhe_cats: Dict[str, list] = {}
        for p in problemas:
            cat = p["categoria"]
            categorias[cat] = categorias.get(cat, 0) + 1
            detalhe_cats.setdefault(cat, []).append(p)

        # Safeguard: FALHA com tentativas >= 3 (independente da categoria)
        safeguard = [
            p for p in problemas
            if p["status"] == "FALHA" and p["tentativas"] >= 3
        ]

        log_info(caminho_log, "RELATORIO", id_fila_adm,
                 "Dados coletados",
                 f"total={resumo.get('total',0)} "
                 f"baixados={resumo.get('baixados',0)} "
                 f"problemas={len(problemas)} "
                 f"safeguard={len(safeguard)} "
                 f"nao_encontradas={len(nao_encontradas)} "
                 f"duracao={duracao_str}")

        png_bytes = _gerar_grafico_png(categorias, nome_adm, modalidade)

        corpo_html = _montar_html(
            nome_adm, modalidade, resumo, categorias, por_consultor,
            safeguard, detalhe_cats, nao_encontradas, mes_formatado, duracao_str,
        )
        corpo_txt = _montar_txt(
            nome_adm, modalidade, resumo, categorias, por_consultor,
            safeguard, nao_encontradas, duracao_str, mes_formatado,
        )

        total_problemas = sum(categorias.values())
        assunto = (
            f"[INTERNO] Gerar Boleto | {nome_adm} | {modalidade} | {mes_formatado} | "
            f"{resumo.get('baixados',0)} baixados / "
            f"{total_problemas} problemas"
            + (f" / {len(safeguard)} reexecutar" if safeguard else "")
        )

        msg = MIMEMultipart("mixed")
        msg["To"]      = EMAIL_RELATORIO_INTERNO
        msg["Subject"] = assunto

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(corpo_txt,  "plain", "utf-8"))
        alt.attach(MIMEText(corpo_html, "html",  "utf-8"))
        msg.attach(alt)

        if png_bytes:
            anexo = MIMEBase("image", "png")
            anexo.set_payload(png_bytes)
            encoders.encode_base64(anexo)
            ts       = datetime.now().strftime("%Y%m%d_%H%M")
            nome_arq = re.sub(r"[^a-zA-Z0-9_-]", "_",
                              f"{nome_adm}_{modalidade}_{ts}") + ".png"
            anexo.add_header("Content-Disposition",
                             f'attachment; filename="{nome_arq}"')
            msg.attach(anexo)

        service = _get_gmail_service()
        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result  = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        log_info(caminho_log, "RELATORIO", id_fila_adm,
                 "Relatorio interno enviado",
                 f"para={EMAIL_RELATORIO_INTERNO} "
                 f"message_id={result.get('id','?')}")
        return True

    except Exception as e:
        try:
            tb = traceback.format_exc()
            log_erro(caminho_log, "RELATORIO", id_fila_adm,
                     "Falha ao gerar/enviar relatorio interno",
                     f"{e}\n{tb}")
        except Exception:
            pass
        return False
