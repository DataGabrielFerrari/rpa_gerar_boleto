import re
import time

from googleapiclient.errors import HttpError


# Status HTTP transitorios do Google API: vale a pena retentar.
# 429 = rate limit / quota
# 500/502/503/504 = instabilidade temporaria do servico
_HTTP_STATUS_TRANSIENT = {429, 500, 502, 503, 504}


def extrair_id_planilha(link: str) -> str:
    if not link:
        raise ValueError("link_planilha está vazio.")
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link)
    if not m:
        raise ValueError(f"Não consegui extrair o ID da planilha do link: {link}")
    return m.group(1)


def _executar_com_retry(callable_request, max_tentativas: int = 4, base_wait: float = 1.5):
    """
    Executa uma chamada Google API com retry exponencial em erros transitorios
    (HttpError 429/500/502/503/504). Outras excecoes propagam imediatamente
    sem retry.

    Tempos de espera entre tentativas (base_wait=1.5):
      tentativa 1 -> 1.5s
      tentativa 2 -> 3.0s
      tentativa 3 -> 6.0s
    Total max ~10s antes de desistir.

    Se TODAS as tentativas falharem, levanta a ultima HttpError - quem chama
    decide o que fazer (no leitor_planilha, isso vira FALHA do lote, nao
    SEM_COTAS silencioso).
    """
    ultima_exc = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            return callable_request()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            try:
                status_int = int(status)
            except (TypeError, ValueError):
                status_int = None
            ultima_exc = e
            # Erro nao-transitorio: propaga sem retry
            if status_int not in _HTTP_STATUS_TRANSIENT:
                raise
            # Esgotou tentativas: propaga
            if tentativa == max_tentativas:
                raise
            # Backoff exponencial e tenta de novo
            espera = base_wait * (2 ** (tentativa - 1))
            time.sleep(espera)
    if ultima_exc is not None:
        raise ultima_exc


def ler_range(service, spreadsheet_id: str, range_a1: str):
    def _call():
        return service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            majorDimension="ROWS",
        ).execute()

    resp = _executar_com_retry(_call)
    return resp.get("values", [])


def coluna_para_letra(idx_zero_based: int) -> str:
    idx = idx_zero_based + 1
    letras = ""
    while idx > 0:
        idx, resto = divmod(idx - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def letra_para_indice(letra: str) -> int:
    """Converte uma letra de coluna (A, B, ..., Z, AA, ...) para indice 0-based."""
    resultado = 0
    for c in letra.strip().upper():
        resultado = resultado * 26 + (ord(c) - 64)
    return resultado - 1


def obter_sheet_id(service, spreadsheet_id: str, aba: str):
    """Resolve o sheetId (gid numerico) de uma aba pelo nome. Retorna None se nao achar."""
    def _call():
        return service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title)",
        ).execute()

    meta = _executar_com_retry(_call)
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == aba:
            return props.get("sheetId")
    return None


def pintar_fundo_branco_em_lote(
    service,
    spreadsheet_id: str,
    aba: str,
    idx_col_zero_based: int,
    linhas: list[int],
):
    """
    Pinta o fundo de branco nas celulas (linha, coluna) informadas.
    Usado para limpar o fundo colorido (verde/azul/etc) da coluna BOLETO
    quando o status e reescrito para "NAO BAIXADO".

    Falhas aqui NAO devem quebrar o pipeline: a escrita do valor ja ocorreu
    antes. Por isso engolimos excecoes (retornamos silenciosamente).
    """
    if not linhas:
        return

    try:
        sheet_id = obter_sheet_id(service, spreadsheet_id, aba)
        if sheet_id is None:
            return

        branco = {"red": 1.0, "green": 1.0, "blue": 1.0}
        requests = []
        for row_num in linhas:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_num - 1,
                        "endRowIndex": row_num,
                        "startColumnIndex": idx_col_zero_based,
                        "endColumnIndex": idx_col_zero_based + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {"backgroundColor": branco}
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        body = {"requests": requests}

        def _call():
            return service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body=body,
            ).execute()

        _executar_com_retry(_call)
    except Exception:
        # Formatacao e cosmetica; nunca deve abortar o lote.
        return


def escrever_valores_celulas(
    service,
    spreadsheet_id: str,
    aba: str,
    letra_col: str,
    linhas_valores: list,  # list of (row_num_1based: int, valor: str)
):
    """
    Escreve valores arbitrarios em celulas especificas da coluna `letra_col`.
    Usado para marcar duplicatas com "DUPLICADA com {nome_cliente}" etc.
    """
    if not linhas_valores:
        return

    data = [
        {
            "range": f"'{aba}'!{letra_col}{row}",
            "values": [[valor]],
        }
        for row, valor in linhas_valores
    ]
    body = {"valueInputOption": "RAW", "data": data}

    def _call():
        return service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body,
        ).execute()

    _executar_com_retry(_call)


def atualizar_boleto_em_lote(service, spreadsheet_id: str, aba: str, letra_col_boleto: str, linhas: list[int]):
    if not linhas:
        return

    data = []
    for row_num in linhas:
        rng = f"{aba}!{letra_col_boleto}{row_num}"
        data.append({"range": rng, "values": [["NÃO BAIXADO"]]})

    body = {"valueInputOption": "RAW", "data": data}

    def _call():
        return service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body,
        ).execute()

    _executar_com_retry(_call)

    # Limpa o fundo colorido (verde/azul/etc) da coluna BOLETO, deixando branco,
    # para nao poluir a planilha quando o status vira "NAO BAIXADO".
    pintar_fundo_branco_em_lote(
        service=service,
        spreadsheet_id=spreadsheet_id,
        aba=aba,
        idx_col_zero_based=letra_para_indice(letra_col_boleto),
        linhas=linhas,
    )
