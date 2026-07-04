"""
Atualiza a planilha do ADM com o resultado de cada cota processada.
Cruzamento por GRUPO + COTA.
"""

import re
import unicodedata
from typing import List, Tuple, Optional, Dict

from shared.google_auth import criar_servico_sheets
from shared.log import log_info, log_erro
from shared.sql_funcoes import obter_dados_adm_por_fila
from saida.lib.db import get_conn


def _normalizar(texto: str) -> str:
    if texto is None:
        return ""
    t = str(texto).strip().lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\s+", "_", t)
    t = re.sub(r"[^a-z0-9_]", "", t)
    return t


def _col_to_letter(col_idx_1based: int) -> str:
    s = ""
    n = col_idx_1based
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _extract_spreadsheet_id(link_or_id: str) -> str:
    if not link_or_id:
        return ""
    if "/" not in link_or_id and len(link_or_id) > 20:
        return link_or_id.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link_or_id)
    if m:
        return m.group(1)
    return link_or_id.strip()


# Cores por status (RGB 0-1)
_CORES_STATUS = {
    "BAIXADO":     {"red": 0.204, "green": 0.659, "blue": 0.325},  # verde
    "NÃO BAIXADO": {"red": 1.0,   "green": 0.757, "blue": 0.027},  # amarelo (atenção)
    "NAO BAIXADO": {"red": 1.0,   "green": 0.757, "blue": 0.027},
    "ADIANTADO":   {"red": 0.263, "green": 0.627, "blue": 0.918},  # azul (de boa)
    "DUPLICADA":   {"red": 0.608, "green": 0.608, "blue": 0.608},  # cinza
    "FALHA":       {"red": 0.918, "green": 0.263, "blue": 0.208},  # vermelho
}


def _get_sheet_id(service, spreadsheet_id: str, aba: str) -> Optional[int]:
    """Retorna o sheetId numérico de uma aba pelo nome."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == aba:
                return props.get("sheetId")
    except Exception:
        pass
    return None


def _col_index(letter: str) -> int:
    """Converte letra de coluna (ex: 'C') para índice 0-based."""
    result = 0
    for ch in letter.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def _find_columns(header_row: List[str]) -> Tuple[int, int, int, Optional[int]]:
    norm = [_normalizar(h) for h in header_row]

    def achar(*nomes):
        for n in nomes:
            nn = _normalizar(n)
            if nn in norm:
                return norm.index(nn)
        return None

    idx_grupo = achar("GRUPO")
    idx_cota = achar("COTA")
    idx_boleto = achar("BOLETO", "STATUS")
    idx_obs = achar(
        "OBSERVAÇÃO BOLETO",
        "OBSERVACAO BOLETO",
        "OBSERVAÇÃO",
        "OBSERVACAO",
    )

    faltando = []
    if idx_grupo is None:
        faltando.append("GRUPO")
    if idx_cota is None:
        faltando.append("COTA")
    if idx_boleto is None:
        faltando.append("BOLETO/STATUS")

    if faltando:
        raise RuntimeError(
            f"Header faltando colunas: {', '.join(faltando)}. Header: {header_row}"
        )

    return idx_grupo, idx_cota, idx_boleto, idx_obs


def _find_header_row(values, max_linhas=10):
    limite = min(len(values), max_linhas)
    for i in range(limite):
        row = values[i]
        if not any(str(c).strip() for c in row):
            continue
        try:
            return (i,) + _find_columns(row)
        except RuntimeError:
            continue
    raise RuntimeError(f"Cabecalho nao encontrado nas {limite} primeiras linhas")


def _fetch_cotas(id_fila_adm: int):
    """
    Retorna a ULTIMA tentativa de cada cota do lote (maior id_cota por
    nome_aba + grupo + cota). Com o sistema de retry via DB podem existir
    até 3 registros por cota; a planilha deve refletir apenas o resultado
    final (seja BAIXADO na tentativa 2 ou FALHA [3/3] na tentativa 3).

    O nome_aba é usado para filtrar qual aba da planilha atualizar.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (nome_aba, grupo, cota)
                    nome_aba, grupo, cota, status, observacao
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                ORDER BY nome_aba, grupo, cota, id_cota DESC
                """,
                (id_fila_adm,),
            )
            return cur.fetchall()


def _filtrar_cotas_por_aba(cotas, aba: str):
    """
    Filtra as cotas que pertencem a uma aba especifica.
    Cotas com nome_aba NULL sao tratadas como pertencentes a TODAS as
    abas (compatibilidade com lotes legacy criados antes da coluna
    nome_aba ser obrigatoria).
    """
    aba_norm = (aba or "").strip()
    resultado = []
    for row in cotas:
        nome_aba_db = (row[0] or "").strip()
        if not nome_aba_db or nome_aba_db == aba_norm:
            # mantem o formato antigo (grupo, cota, status, observacao)
            # que _atualizar_aba ja consome
            resultado.append((row[1], row[2], row[3], row[4]))
    return resultado


def _aplicar_cores(
    service,
    spreadsheet_id: str,
    aba: str,
    col_boleto: str,
    cores_por_linha: List[Tuple[int, dict]],
) -> None:
    """
    Aplica cor de fundo nas células da coluna BOLETO via batchUpdate de formatação.
    cores_por_linha: list of (row_num_1based, rgb_dict)
    """
    if not cores_por_linha:
        return
    sheet_id = _get_sheet_id(service, spreadsheet_id, aba)
    if sheet_id is None:
        return
    col_idx = _col_index(col_boleto)
    requests = []
    for row_num, rgb in cores_por_linha:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": rgb,
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception:
        pass  # cor é opcional — não quebra o fluxo se falhar


def _atualizar_aba(
    service,
    spreadsheet_id: str,
    aba: str,
    cotas,
    id_fila_adm: int,
    caminho_log: str,
) -> Dict[str, int]:
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{aba}'!A1:ZZ",
        majorDimension="ROWS",
    ).execute()
    values = resp.get("values", [])

    if not values:
        return {
            "matched": 0,
            "updated_status": 0,
            "updated_obs": 0,
            "not_found": len(cotas),
            "duplicated_keys": 0,
        }

    header_row_idx, idx_grupo, idx_cota, idx_boleto, idx_obs = _find_header_row(values)

    index = {}
    duplicated = 0

    for i in range(header_row_idx + 1, len(values)):
        row = values[i]
        g = (row[idx_grupo].strip() if idx_grupo < len(row) else "").zfill(6)
        c = (row[idx_cota].strip() if idx_cota < len(row) else "").zfill(4)

        if not g or not c or g == "000000" or c == "0000":
            continue

        key = (g, c)
        if key in index:
            duplicated += 1
            continue

        index[key] = i + 1  # planilha é 1-based

    col_boleto = _col_to_letter(idx_boleto + 1)
    col_obs = _col_to_letter(idx_obs + 1) if idx_obs is not None else None

    updates = []
    cores_por_linha: List[Tuple[int, dict]] = []
    matched = 0
    updated_status = 0
    updated_obs = 0
    not_found = 0

    for grupo, cota, status, obs in cotas:
        g = str(grupo or "").strip().zfill(6)
        c = str(cota or "").strip().zfill(4)

        if not g or not c:
            not_found += 1
            continue

        row_num = index.get((g, c))
        if not row_num:
            not_found += 1
            continue

        matched += 1
        # FALHA tecnica (esgotou retentativas) e exibida como FALHA na planilha,
        # com fundo vermelho. O detalhe tecnico fica na coluna de observacao.
        _status_upper = (status or "").upper()
        _status_planilha = status or ""
        updates.append(
            {
                "range": f"'{aba}'!{col_boleto}{row_num}",
                "values": [[_status_planilha]],
            }
        )
        updated_status += 1

        # Coleta cor para esta linha (FALHA -> vermelho, NÃO BAIXADO -> amarelo,
        # BAIXADO -> verde, ADIANTADO -> azul, DUPLICADA -> cinza).
        _chave_cor = _status_planilha.upper()
        _rgb = _CORES_STATUS.get(_chave_cor)
        if _rgb:
            cores_por_linha.append((row_num, _rgb))

        if col_obs is not None:
            updates.append(
                {
                    "range": f"'{aba}'!{col_obs}{row_num}",
                    "values": [[obs if obs is not None else ""]],
                }
            )
            updated_obs += 1

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    # Aplica cores de fundo na coluna BOLETO
    _aplicar_cores(service, spreadsheet_id, aba, col_boleto, cores_por_linha)

    if duplicated:
        log_erro(
            caminho_log,
            "PLANILHA",
            id_fila_adm,
            "Chaves duplicadas",
            f"aba={aba} duplicadas={duplicated}",
        )

    return {
        "matched": matched,
        "updated_status": updated_status,
        "updated_obs": updated_obs,
        "not_found": not_found,
        "duplicated_keys": duplicated,
    }


def atualizar_planilha_lote(id_fila_adm: int) -> Dict[str, int]:
    """
    Atualiza a planilha (aba correspondente à modalidade) com o resultado
    das cotas. Se qualquer aba falhar, levanta exceção no final para a
    etapa SAIDA marcar PLANILHA=ERRO.
    """
    dados = obter_dados_adm_por_fila(id_fila_adm)
    if not dados:
        raise RuntimeError(f"Lote {id_fila_adm} nao encontrado")

    caminho_log = dados["caminho_log"]
    link_planilha = dados["link_planilha"]
    nome_aba_raw = dados["nome_aba"]

    spreadsheet_id = _extract_spreadsheet_id(link_planilha)
    if not spreadsheet_id:
        msg = f"ADM={dados['nome']} sem spreadsheet_id"
        log_erro(caminho_log, "PLANILHA", id_fila_adm, "Validar planilha", msg)
        raise RuntimeError(msg)

    abas = [a.strip() for a in (nome_aba_raw or "").split(",") if a.strip()]
    if not abas:
        msg = (
            f"ADM={dados['nome']} sem nome_aba para modalidade "
            f"{dados['modalidade']}"
        )
        log_erro(caminho_log, "PLANILHA", id_fila_adm, "Validar abas", msg)
        raise RuntimeError(msg)

    cotas = _fetch_cotas(id_fila_adm)
    service = criar_servico_sheets()

    total = {
        "matched": 0,
        "updated_status": 0,
        "updated_obs": 0,
        "not_found": 0,
        "duplicated_keys": 0,
    }

    falhas_abas = []

    for aba in abas:
        try:
            cotas_da_aba = _filtrar_cotas_por_aba(cotas, aba)

            stats = _atualizar_aba(
                service=service,
                spreadsheet_id=spreadsheet_id,
                aba=aba,
                cotas=cotas_da_aba,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
            )

            for k in total:
                total[k] += stats[k]

            log_info(
                caminho_log,
                "PLANILHA",
                id_fila_adm,
                "Atualizar aba",
                f"aba={aba} {stats}",
            )
        except Exception as e:
            falhas_abas.append(f"{aba}: {type(e).__name__}: {e}")
            log_erro(
                caminho_log,
                "PLANILHA",
                id_fila_adm,
                "Atualizar aba",
                f"aba={aba} erro={type(e).__name__}: {e}",
            )

    if falhas_abas:
        raise RuntimeError(
            "Falha ao atualizar uma ou mais abas da planilha: "
            + " | ".join(falhas_abas)
        )

    return total