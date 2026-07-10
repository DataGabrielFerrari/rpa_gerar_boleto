# teste sincronizacao
"""
WORKER do AVAPRO - processa UM CLIENTE por execucao (suporta unificacao).

Chamado pelo orquestrador main.py em loop:
    python src/processamento/main.py {id_cota}

Recebe o id_cota da proxima cota pendente; ao entrar na pagina do cliente,
processa TODAS as cotas daquele cliente que estiverem no lote: marca o
checkbox de cada uma, le o atraso (expandindo o 'Mostrar mais') e emite UM
unico boleto. >1 cota selecionada => 'BOLETO UNIFICADO {NOME}'. Cada cota
envolvida e finalizada no banco com o MESMO PDF e o seu proprio atraso.

Tratativa de erro (forte):
- Falhas DEFINITIVAS (BAIXADO/NAO_BAIXADO/ADIANTADO/duplicado) sao gravadas
  no banco imediatamente.
- Falhas GRAVES/TRANSITORIAS (CDP caiu, timeout de botao, site fora,
  cards nao renderizaram) NAO sao gravadas: o worker retorna
  status=FALHA + retriable=true e deixa a cota PROCESSANDO, para o
  orquestrador re-logar e retentar (ate 3x). So apos esgotar e que vira
  FALHA definitiva (gravada pelo orquestrador) e dispara e-mail.
- Cards do cliente que nao renderizam: reabre o cliente e tenta de novo
  ate 3x, salvando um print de cada tentativa na pasta FALHA do cliente.
- A captura do download e ARMADA antes do clique em 'Emitir boleto'
  (page.expect_download), e o clique e um clique normal (nao suspeito).
- Pasta de FALHA do cliente so e criada quando um print/evidencia e
  realmente salvo (nada de pastas vazias).

Saida (stdout, ultima linha): JSON
{
  "status": "BAIXADO|NAO_BAIXADO|ADIANTADO|FALHA",
  "retriable": bool,
  "observacao": str,
  "caminho_boleto": str|null,
  "caminho_evidencia_falha": str|null,
  "parcelas_atraso": int|null,
  "id_cota": int,
  "houve_unificacao": bool,
  "cotas_distintas": [[grupo, cota], ...]
}
"""

import os
import re
import sys
import json
import time
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(CURRENT_DIR)
ROOT_DIR = os.path.dirname(SRC_DIR)

# Arquivo de "passo atual" do worker. O worker grava aqui a etapa em que esta;
# se o orquestrador matar o worker por timeout, ele le este arquivo para montar
# uma observacao ESPECIFICA (ex: "timeout ao clicar em 'Gerar boleto'") em vez
# do generico "excedeu timeout".
PASSO_FILE = os.path.join(ROOT_DIR, ".rpa_worker_passo.txt")


def _registrar_passo(id_cota, passo: str) -> None:
    """Grava a etapa atual do worker (para diagnostico de timeout)."""
    try:
        with open(PASSO_FILE, "w", encoding="utf-8") as _f:
            _f.write(f"{id_cota}|{passo}")
    except Exception:
        pass

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, ".env"), override=True)
except Exception:
    pass

from playwright.sync_api import sync_playwright

from shared.log import log_info, log_erro
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    marcar_cota_processando,
    finalizar_cota_resultado,
    finalizar_cota_falha,
    finalizar_cotas_lote_resultado,
    aplicar_finalizacoes_lote,
    inserir_cota_nao_encontrada,
    listar_cotas_nao_encontradas,
    marcar_cota_ja_baixada_reunificada,
)

from processamento.lib.navegador import (
    conectar_ao_edge,
    achar_aba_avapro,
    garantir_url_meus_clientes,
)
from processamento.lib import avapro
from processamento.lib.avapro import (
    RES_UM, RES_ZERO, RES_MUITOS, RES_TIMEOUT,
)
from processamento.lib.arquivos import (
    nome_arquivo_boleto,
    nome_arquivo_boleto_unificado,
    destino_sem_colisao,
    pasta_falha_cota,
    pasta_nao_baixado_cota,
    pasta_boletos,
    pasta_adiantado_cota,
    pasta_verificar_adiantados,
    pasta_cotas_nao_localizadas_planilha,
    pasta_excluidos,
    pasta_desistentes,
    pasta_cotas_nao_encontradas_cota,
)


# Quantas vezes reabrir o cliente quando os cards de cota nao renderizam.
MAX_TENTATIVAS_CARDS = 3


# ============================================================
# JSON / log
# ============================================================

def _emitir_json(payload: dict) -> None:
    limpo = {k: v for k, v in payload.items() if not k.startswith("_")}
    sys.stdout.write(json.dumps(limpo, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _payload(
    status: str,
    observacao: str,
    id_cota: Optional[int] = None,
    retriable: bool = False,
    caminho_boleto: Optional[str] = None,
    caminho_evidencia_falha: Optional[str] = None,
    parcelas_atraso: Optional[int] = None,
    houve_unificacao: bool = False,
    cotas_distintas: Optional[List[List[str]]] = None,
    finalizacoes: Optional[List[Dict[str, Any]]] = None,
    toasts_capturados: Optional[List[str]] = None,
    cotas_nao_selecionadas: Optional[List[Dict[str, str]]] = None,
) -> dict:
    return {
        "status": status,
        "retriable": bool(retriable),
        "observacao": observacao,
        "caminho_boleto": caminho_boleto,
        "caminho_evidencia_falha": caminho_evidencia_falha,
        "parcelas_atraso": parcelas_atraso,
        "id_cota": id_cota,
        "houve_unificacao": houve_unificacao,
        "cotas_distintas": cotas_distintas or [],
        "_finalizacoes": finalizacoes or [],
        # Campos de diagnóstico — impressos pelo orquestrador no terminal.
        "toasts_capturados": toasts_capturados or [],
        "cotas_nao_selecionadas": cotas_nao_selecionadas or [],
    }


def _fin(
    id_cota: int,
    status: str,
    observacao: str,
    caminho_boleto: Optional[str] = None,
    caminho_evidencia: Optional[str] = None,
    parcelas_atraso: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "id_cota": id_cota,
        "status": status,
        "observacao": observacao,
        "caminho_boleto": caminho_boleto,
        "caminho_evidencia": caminho_evidencia,
        "parcelas_atraso": parcelas_atraso,
    }


def _fechar_modal_com_retry(
    page,
    pasta_falha: "Path",
    caminho_log: "Path",
    id_cota: int,
    g,
    c,
) -> Optional[dict]:
    """
    Tenta fechar o modal de detalhes da cota aberto pelo 'Mostrar mais'.

    Retenta ate 3 vezes verificando se o modal realmente sumiu apos cada
    tentativa. Em cada falha tira um print de evidencia. Se o modal persistir
    apos todas as tentativas, retorna um dict de _payload FALHA retriable
    para que o worker possa retornar imediatamente. Retorna None em sucesso.
    """
    _MAX = 3
    for _t in range(1, _MAX + 1):
        try:
            avapro.fechar_modal(page)
        except Exception:
            pass
        page.wait_for_timeout(300)
        if not avapro.modal_aberto(page):
            return None  # fechou com sucesso
        # Modal ainda aberto — print + log
        _pp = _print_falha(
            page, pasta_falha,
            f"Falha_Fechar_Modal_T{_t}",
        )
        _log_err(
            caminho_log, id_cota,
            f"Modal de detalhes nao fechou (tentativa {_t}/{_MAX})",
            f"grupo={g} cota={c} print={_pp or '-'}",
        )
        page.wait_for_timeout(700 * _t)

    # Todas as tentativas esgotadas
    _obs = f"Falha ao fechar janela de detalhes da cota após {_MAX} tentativas — AVAPRO pode estar instável."
    _pp_def = _print_falha(page, pasta_falha, "Falha_Modal_Nao_Fechado_Definitivo")
    return _payload(
        "FALHA", _obs,
        id_cota=id_cota,
        retriable=True,
        caminho_evidencia_falha=_pp_def,
        finalizacoes=[_fin(id_cota, "FALHA", _obs, caminho_evidencia=_pp_def)],
    )


def _texto_atraso(atraso) -> str:
    """
    Texto de atraso no padrao rpa_gerar_boleto:
      None/0 -> 'Nenhuma parcela em atraso'
      1      -> '1 parcela em atraso'
      N      -> 'N parcelas em atraso'
    """
    n = atraso if isinstance(atraso, int) else None
    if not n or n <= 0:
        return "Nenhuma parcela em atraso"
    if n == 1:
        return "1 parcela em atraso"
    return f"{n} parcelas em atraso"


def _obs_baixado(unificado, eh_origem, origem_grupo, origem_cota, atraso_proprio,
                 pode_unificar_nao: bool = False) -> str:
    """
    Observacao de cota BAIXADA.

      - cota unica (pode_unificar=NAO) -> '<N> parcela(s) em atraso - Não unificado'
      - cota unica (pode_unificar=SIM) -> '<N> parcela(s) em atraso'
      - unificado (origem)             -> 'Boleto unificado | <atraso>'
      - unificado (demais)             -> 'Boleto unificado no processamento da cota
                                          GGGGGG/CCCC | <atraso>'

    Cada cota exibe o seu PROPRIO numero de parcelas em atraso, nao o da origem.
    """
    if not unificado:
        base = _texto_atraso(atraso_proprio)
        return f"{base} - Não unificado" if pode_unificar_nao else base
    if eh_origem:
        return f"Boleto unificado | {_texto_atraso(atraso_proprio)}"
    return (
        f"Boleto unificado a partir da cota "
        f"{origem_grupo}/{origem_cota} | {_texto_atraso(atraso_proprio)}"
    )


def _log(caminho_log, id_cota, acao, detalhe=""):
    if not caminho_log:
        return
    try:
        log_info(caminho_log, "PROCESSAMENTO", id_cota, acao, detalhe)
    except Exception:
        pass


def _log_tempo(caminho_log, id_cota, marco, t_ref):
    """Loga tempo decorrido desde t_ref (em segundos). Overhead: apenas time.time()."""
    try:
        elapsed = round(time.time() - t_ref, 2)
        _log(caminho_log, id_cota, f"[TEMPO] {marco}", f"{elapsed}s")
    except Exception:
        pass


def _log_err(caminho_log, id_cota, acao, detalhe=""):
    if not caminho_log:
        return
    try:
        log_erro(caminho_log, "PROCESSAMENTO", id_cota, acao, detalhe)
    except Exception:
        pass


# Contexto global de evidencias: preenchido no inicio de _processar_cota.
# Permite que TODOS os _print_* registrem no log do lote o caminho COMPLETO
# de cada screenshot, sem mudar a assinatura das dezenas de chamadas.
_EVID_CTX: Dict[str, Any] = {"caminho_log": None, "id_cota": None}


def _log_evidencia(destino, origem: str) -> None:
    """Registra o caminho COMPLETO do print no log do lote + stderr."""
    try:
        caminho_abs = str(Path(destino).resolve())
    except Exception:
        caminho_abs = str(destino)
    _stderr(f"[EVIDENCIA] Screenshot salvo ({origem}) | caminho_completo={caminho_abs}")
    try:
        if _EVID_CTX.get("caminho_log"):
            log_info(
                _EVID_CTX["caminho_log"],
                "PROCESSAMENTO",
                _EVID_CTX.get("id_cota"),
                f"[EVIDENCIA] Screenshot salvo ({origem})",
                f"caminho_completo={caminho_abs}",
            )
    except Exception:
        pass


def _print_falha(page, pasta_destino: Path, prefixo: str) -> Optional[str]:
    """
    Salva um screenshot na pasta de FALHA do cliente. A pasta so e criada
    AQUI (preguicoso) - nunca antes - para nao deixar pastas vazias.
    """
    try:
        pasta_destino.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Encurta o prefixo para caber no MAX_PATH do Windows (260)
        prefixo = str(prefixo)[:60].rstrip()
        destino = pasta_destino / f"{prefixo}_{ts}.png"
        if len(str(destino)) > 240:
            corte = len(str(destino)) - 240
            prefixo = prefixo[: max(10, len(prefixo) - corte)].rstrip()
            destino = pasta_destino / f"{prefixo}_{ts}.png"
        page.screenshot(path=str(destino), full_page=True)
        _log_evidencia(destino, "FALHA")
        return str(destino)
    except Exception:
        return None


def _print_excluido(page, caminho_base: Optional[str], nome_cliente: str, grupo: str, cota: str) -> Optional[str]:
    """
    Salva screenshot FULL PAGE na pasta NAO_BAIXADOS/1 - Excluidos/ para
    evidencia de cota com badge 'Excluído' no AVAPRO.
    """
    try:
        pasta = pasta_excluidos(caminho_base)
        pasta.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        g6 = re.sub(r"\D", "", str(grupo or "")).zfill(6)
        c4 = re.sub(r"\D", "", str(cota or "")).zfill(4)
        nome_safe = re.sub(r'[\\/:*?"<>|\s]+', "_", nome_cliente)[:50]
        destino = pasta / f"{nome_safe}_{g6}_{c4}_{ts}.png"
        page.screenshot(path=str(destino), full_page=True)
        _log_evidencia(destino, "EXCLUIDO")
        return str(destino)
    except Exception:
        return None


def _print_desistente(page, caminho_base: Optional[str], nome_cliente: str, grupo: str, cota: str) -> Optional[str]:
    """
    Salva screenshot FULL PAGE na pasta NAO_BAIXADOS/2 - Desistentes/ para
    evidencia de cota com badge 'Desistente' no AVAPRO.
    """
    try:
        pasta = pasta_desistentes(caminho_base)
        pasta.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        g6 = re.sub(r"\D", "", str(grupo or "")).zfill(6)
        c4 = re.sub(r"\D", "", str(cota or "")).zfill(4)
        nome_safe = re.sub(r'[\\/:*?"<>|\s]+', "_", nome_cliente)[:50]
        destino = pasta / f"{nome_safe}_{g6}_{c4}_{ts}.png"
        page.screenshot(path=str(destino), full_page=True)
        _log_evidencia(destino, "DESISTENTE")
        return str(destino)
    except Exception:
        return None


def _print_adiantado(page, pasta_destino: Path, prefixo: str) -> Optional[str]:
    """
    Salva print rapido na pasta ADIANTADOS do cliente. Mesma logica do
    _print_falha mas em pasta separada (mesmo nivel de FALHAS e Boletos).

    IMPORTANTE: usa full_page=False para ser mais rapido - o toast some
    em ~5s e precisamos capturar antes. O viewport ja mostra o toast no
    canto da tela.

    Pasta e criada preguicosamente aqui - nao aparece se nao houver
    nenhuma cota adiantada no lote.
    """
    try:
        pasta_destino.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Encurta o prefixo para caber no MAX_PATH do Windows (260)
        prefixo = str(prefixo)[:60].rstrip()
        destino = pasta_destino / f"{prefixo}_{ts}.png"
        if len(str(destino)) > 240:
            corte = len(str(destino)) - 240
            prefixo = prefixo[: max(10, len(prefixo) - corte)].rstrip()
            destino = pasta_destino / f"{prefixo}_{ts}.png"
        # full_page=False pra ser rapido (toast some em ~5s)
        page.screenshot(path=str(destino), full_page=False)
        _log_evidencia(destino, "ADIANTADO")
        return str(destino)
    except Exception:
        return None


# ============================================================
# Contexto / mapeamento
# ============================================================

def _carregar_contexto(id_cota: int) -> Dict[str, Any]:
    from entrada.lib.db import get_conn

    # nome_consultor entra aqui pra montar a pasta destino do boleto
    # como Boletos/{Nome do Consultor}/. Mesma regra do rpa_gerar_boleto.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_cota, id_fila_adm, nome_cliente, grupo, cota,
                       nome_aba, cpf_cnpj, pode_unificar, observacao, status,
                       nome_consultor
                FROM tbl_fila_cotas
                WHERE id_cota = %s
                """,
                (id_cota,),
            )
            row = cur.fetchone()

    if not row:
        raise ValueError(f"id_cota={id_cota} nao encontrado em tbl_fila_cotas")

    ctx = {
        "id_cota": row[0],
        "id_fila_adm": row[1],
        "nome_cliente": row[2],
        "grupo": row[3],
        "cota": row[4],
        "nome_aba": row[5],
        "cpf_cnpj": row[6],
        "pode_unificar": row[7],
        "observacao": row[8],
        "status_atual": row[9],
        "nome_consultor": row[10],
    }

    dados_lote = obter_dados_adm_por_fila(ctx["id_fila_adm"])
    if not dados_lote:
        raise ValueError(f"Lote id_fila_adm={ctx['id_fila_adm']} nao encontrado")

    ctx["caminho_log"] = dados_lote.get("caminho_log")
    ctx["caminho_base"] = dados_lote.get("caminho_base")
    ctx["modalidade"] = dados_lote.get("modalidade")
    ctx["mes_ref"] = dados_lote.get("mes_ref")  # YYYYMM, ex: 202507
    return ctx


def _mapear_cotas_do_lote(id_fila_adm: int, id_cota_primaria: Optional[int] = None) -> Dict[tuple, Dict[str, Any]]:
    """
    Cotas do lote ainda nao finalizadas (PENDENTE/PROCESSANDO):
      (grupo_zfill6, cota_zfill4) -> {id_cota, nome_cliente, cpf_cnpj, pode_unificar}

    `pode_unificar` indica se esta cota pode ser incluida em um boleto
    unificado junto com outras cotas do mesmo cliente:
      'Sim' (ou None) -> pode ser incluida no unificado
      'Nao'           -> deve ser emitida em boleto individual (sem unificar)

    Quando ha retentativas, o mesmo (grupo, cota) pode ter dois registros
    (original PROCESSANDO + retry PROCESSANDO). Nesse caso preferimos o
    id_cota_primaria (o que o orquestrador passou para este worker) —
    assim sel_ids contem o id correto e o bloco de sucesso nao marca
    erroneamente NAO_BAIXADO.
    """
    from entrada.lib.db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_cota, grupo, cota, nome_cliente, cpf_cnpj, pode_unificar
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND status IN ('PENDENTE', 'PROCESSANDO')
                ORDER BY id_cota DESC
                """,
                (id_fila_adm,),
            )
            rows = cur.fetchall()

    mapa: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        g6 = re.sub(r"\D", "", str(r[1] or "")).zfill(6)
        c4 = re.sub(r"\D", "", str(r[2] or "")).zfill(4)
        chave = (g6, c4)
        entrada = {
            "id_cota": r[0],
            "nome_cliente": r[3],
            "cpf_cnpj": r[4],
            "pode_unificar": str(r[5] or "").strip() if r[5] is not None else None,
        }
        if chave not in mapa:
            mapa[chave] = entrada
        elif id_cota_primaria is not None and r[0] == id_cota_primaria:
            # Preferencia explicita: se este e o id_cota do worker atual, usa ele
            mapa[chave] = entrada
    return mapa


def _mapear_chaves_lote_completo(id_fila_adm: int) -> Dict[tuple, Dict[str, Any]]:
    """
    Cotas do lote em QUALQUER status (PENDENTE/PROCESSANDO/BAIXADO/
    NAO_BAIXADO/ADIANTADO/FALHA). Retorna:
      (grupo_zfill6, cota_zfill4) -> {id_cota, status}

    Usado em dois diffs paralelos quando o worker entra no cliente:

    1. Cotas na tela do AVAPRO que NAO estao no lote em nenhum status
       -> tbl_cotas_nao_encontradas (regra do rpa_gerar_boleto antigo,
       aparecem no email final).

    2. Cotas na tela que estao no lote MAS ja foram finalizadas em outro
       run (BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA) -> anexa nota na obser-
       vacao via marcar_cota_reaparecida, pra rastreabilidade (a cota
       atual disparou a aparicao). Nao reprocessa.

    Sem essa distincao, uma cota BAIXADA num run anterior viraria "nao
    encontrada" no run seguinte se reaparecesse na tela.
    """
    from entrada.lib.db import get_conn

    mapa: Dict[tuple, Dict[str, Any]] = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_cota, grupo, cota, status, caminho_boleto, pode_unificar
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                """,
                (id_fila_adm,),
            )
            for r in cur.fetchall():
                g6 = re.sub(r"\D", "", str(r[1] or "")).zfill(6)
                c4 = re.sub(r"\D", "", str(r[2] or "")).zfill(4)
                mapa.setdefault((g6, c4), {
                    "id_cota": r[0],
                    "status": str(r[3] or "").upper(),
                    "caminho_boleto": r[4],
                    "pode_unificar": str(r[5] or "").strip() if r[5] is not None else None,
                })
    return mapa


# ============================================================
# Pesquisa
# ============================================================

def _pesquisar_com_fallbacks(
    page,
    grupo,
    cota,
    nome_cliente,
    caminho_log,
    id_cota,
    pasta_falha: Optional[Path] = None,
    pasta_nao_baixado: Optional[Path] = None,
):
    """
    Tenta as variacoes geradas por gerar_variacoes_busca():
      1) "grupo cota" com espaco
      2) "grupo\\cota" com barra
      3) nome completo MAIUSCULO sem acento

    Cada variacao que retorna 'Nenhum cliente encontrado' (RES_ZERO)
    salva um print antes de tentar a proxima estrategia:
    - Se pasta_nao_baixado fornecida: salva em NAO_BAIXADOS (busca falhou =
      resultado definitivo, nao falha tecnica).
    - Caso contrario (retry de re-entrada apos modal): salva em pasta_falha.

    Retorna (status, anchor_ou_lista, termo_usado, ultimo_print).
    """
    variacoes = avapro.gerar_variacoes_busca(grupo, cota, nome_cliente)
    if not variacoes:
        return RES_ZERO, None, "", None

    ultimo_status = RES_ZERO
    ultimos_anchors = None
    ultimo_print: Optional[str] = None

    for termo in variacoes:
        _log(caminho_log, id_cota, "Pesquisar AVAPRO", f"termo={termo!r}")
        avapro.limpar_busca(page)
        avapro.digitar_busca(page, termo)
        status, anchors = avapro.aguardar_resultado_pesquisa(page, timeout_s=5)
        _log(
            caminho_log, id_cota, "Resultado pesquisa",
            f"termo={termo!r} status={status} qtd={len(anchors) if anchors else 0}",
        )
        if status == RES_UM:
            return RES_UM, anchors[0], termo, None
        if status == RES_MUITOS:
            return RES_MUITOS, anchors, termo, None

        # ZERO ou TIMEOUT: tira print pra auditoria antes de tentar a proxima variacao.
        # Prints vao para NAO_BAIXADOS (resultado definitivo, nao falha tecnica),
        # ou para FALHAS no contexto de retry de re-entrada apos modal.
        prefixo_safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(termo))[:40]
        _pasta_print = pasta_nao_baixado if pasta_nao_baixado is not None else pasta_falha
        if _pasta_print is not None:
            ultimo_print = _print_falha(
                page, _pasta_print,
                f"BUSCA_{status}_{prefixo_safe}",
            )

        ultimo_status = status
        ultimos_anchors = anchors

    return ultimo_status, ultimos_anchors, variacoes[-1], ultimo_print


def _entrar_via_busca(
    page, grupo, cota, nome_cliente, caminho_log, id_cota,
    pasta_falha: Optional[Path] = None,
    pasta_nao_baixado: Optional[Path] = None,
):
    """
    Pesquisa e entra no cliente. Retorna ('OK', None, None) em sucesso, ou
    ('ZERO'|'MUITOS'|'TIMEOUT'|'ERRO_ENTRAR', detalhe, ultimo_print) em falha.
    """
    status, anchor, termo, ultimo_print = _pesquisar_com_fallbacks(
        page, grupo, cota, nome_cliente, caminho_log, id_cota,
        pasta_falha=pasta_falha,
        pasta_nao_baixado=pasta_nao_baixado,
    )
    if status in (RES_ZERO, RES_TIMEOUT):
        return status, termo, ultimo_print
    if status == RES_MUITOS:
        return RES_MUITOS, anchor, None  # anchor = lista
    try:
        avapro.entrar_no_cliente(page, anchor)
    except Exception as e:
        return "ERRO_ENTRAR", str(e), None
    return "OK", None, None


# ============================================================
# Fluxo principal por cliente
# ============================================================

def _data_ref_do_lote(ctx: Dict[str, Any]) -> datetime:
    """
    Converte mes_ref do lote (YYYYMM, ex: 202507) em datetime(ano, mes, 1).
    Fallback para datetime.now() se mes_ref ausente ou invalido.
    """
    mes_ref = ctx.get("mes_ref")
    if mes_ref:
        try:
            mes_ref = int(mes_ref)
            ano = mes_ref // 100
            mes = mes_ref % 100
            if 1 <= mes <= 12 and ano >= 2000:
                return datetime(ano, mes, 1)
        except Exception:
            pass
    return datetime.now()


def _processar_cota(ctx: Dict[str, Any]) -> dict:
    id_cota = ctx["id_cota"]
    id_fila_adm = ctx["id_fila_adm"]
    nome_cliente = ctx["nome_cliente"]
    grupo = ctx["grupo"]
    cota = ctx["cota"]
    nome_consultor = ctx.get("nome_consultor") or ""
    caminho_log = ctx["caminho_log"]
    caminho_base = ctx["caminho_base"]
    # Modalidade do lote (MOTORS|IMOVEL). Usada pra filtrar a insercao em
    # tbl_cotas_nao_encontradas: so registra cotas da MESMA modalidade
    # que esta rodando - as da outra estao em outra aba da planilha.
    modalidade_lote = (ctx.get("modalidade") or "").strip().upper()

    # Habilita o registro do caminho completo de TODO screenshot no log do lote
    _EVID_CTX["caminho_log"] = caminho_log
    _EVID_CTX["id_cota"] = id_cota

    t0 = time.time()
    _log(
        caminho_log, id_cota, "Iniciar processamento",
        f"cliente={nome_cliente!r} grupo={grupo} cota={cota}",
    )

    # Pasta de FALHAS desta cota: usa grupo+cota como sufixo (nunca colide
    # entre cotas do mesmo cliente / lotes diferentes).
    pasta_falha = pasta_falha_cota(caminho_base, nome_cliente, grupo, cota)
    # Pasta de NAO_BAIXADOS: evidencias de cotas nao emitidas por motivo
    # definitivo (cliente nao localizado, modalidade errada, valor zero,
    # toast definitivo...). Separada de FALHAS (erros tecnicos/retriable).
    pasta_nao_baixado = pasta_nao_baixado_cota(caminho_base, nome_cliente, grupo, cota)
    # Pasta especifica para cotas nao encontradas na busca (BUSCA_ZERO,
    # Cota_Nao_Apareceu_Na_Tela, Cota_Sem_Registro_No_Banco).
    # Fica em NAO_BAIXADOS/3 - Cotas não encontradas/{cliente}/ para
    # diferenciar de NAO_BAIXADOS genericos (valor zero, modalidade errada, etc.)
    pasta_nao_encontrada = pasta_cotas_nao_encontradas_cota(caminho_base, nome_cliente, grupo, cota)
    _log(caminho_log, id_cota, "[PASSO] Mapeando cotas do lote (banco)")
    lote_map = _mapear_cotas_do_lote(id_fila_adm, id_cota_primaria=id_cota)
    _log(caminho_log, id_cota, "[PASSO] Iniciando Playwright")

    with sync_playwright() as p:
        # --- conecta ao Edge (CDP) ---
        try:
            _log(caminho_log, id_cota, "[PASSO] Conectando ao Edge via CDP (porta 9222)")
            browser, context = conectar_ao_edge(p)
            _log(caminho_log, id_cota, "[PASSO] Conectado ao Edge via CDP")
        except Exception as e:
            _log_err(caminho_log, id_cota, "CDP indisponivel", f"{e}")
            return _payload(
                "FALHA", f"Nao conectou ao Edge via CDP: {e}",
                id_cota=id_cota, retriable=True,
            )

        page = achar_aba_avapro(context)
        _log(caminho_log, id_cota, "[PASSO] Aba AVAPRO selecionada", f"url={page.url}")
        page.bring_to_front()
        _log(caminho_log, id_cota, "[PASSO] Aba trazida para frente")

        # --- garante /meus-clientes ---
        try:
            garantir_url_meus_clientes(page)
            _log_tempo(caminho_log, id_cota, "meus_clientes_pronto", t0)
        except Exception as e:
            pp = _print_falha(page, pasta_falha, "Pagina_Nao_Carregou")
            _log_err(caminho_log, id_cota, "Meus Clientes nao abriu", f"{e}")
            return _payload(
                "FALHA", f"Nao abriu /meus-clientes: {e}",
                id_cota=id_cota, retriable=True, caminho_evidencia_falha=pp,
            )

        # --- pesquisa + entra (com retry interno p/ cards que nao renderizam) ---
        cotas_tela: List[Dict[str, str]] = []
        ultimo_print: Optional[str] = None

        for tentativa in range(1, MAX_TENTATIVAS_CARDS + 1):
            res, det, _print_busca = _entrar_via_busca(
                page, grupo, cota, nome_cliente, caminho_log, id_cota,
                pasta_falha=pasta_falha,
                pasta_nao_baixado=pasta_nao_encontrada,
            )
            _log_tempo(caminho_log, id_cota, "entrada_cliente_concluida", t0)

            # Verifica imediatamente se a pagina do cliente carregou (h2 visivel).
            # Se h2 visivel -> NAO_BAIXADO em caso de timeout do modal (pagina ok, sistema nao respondeu).
            # Se h2 nao visivel -> FALHA (pagina nao carregou corretamente).
            _cliente_pagina_ok = False
            _h2_nome_pagina = ""  # nome real exibido no h2 (ex: "COTA TRADE LTDA")
            if res == "OK":
                try:
                    _h2_loc = page.locator("h2.text-2xl.font-semibold.py-6").first
                    _h2_vis = _h2_loc.is_visible(timeout=5000)
                    _h2_nome_pagina = (_h2_loc.inner_text(timeout=2000) or "").strip() if _h2_vis else ""
                    _cliente_pagina_ok = bool(_h2_vis and _h2_nome_pagina)
                except Exception:
                    _cliente_pagina_ok = False
                _log(caminho_log, id_cota,
                     "H2 pagina cliente",
                     f"visivel={'sim' if _cliente_pagina_ok else 'nao'} texto={repr(_h2_nome_pagina) if _cliente_pagina_ok else '-'}")

            if res in (RES_ZERO, RES_TIMEOUT):
                # Nenhuma das 3 formas de busca (grupo cota / grupo\\cota /
                # nome completo sem acento) localizou o cliente. Resultado definitivo
                # (problema de dado: cota fora da carteira, cancelada ou digitada errada).
                # Os 3 prints ja foram salvos em NAO_BAIXADOS por _pesquisar_com_fallbacks.
                # Reutiliza o ultimo print como evidencia no banco (sem novo screenshot).
                motivo = (
                    "cliente nao encontrado na busca"
                    if res == RES_ZERO
                    else "timeout aguardando o resultado da busca"
                )
                pp = _print_busca  # ultimo print ja salvo em NAO_BAIXADOS
                _log(
                    caminho_log, id_cota, "Cliente nao encontrado na busca",
                    f"todas as variacoes falharam (ultimo termo={det!r}); "
                    f"ultimo print={pp or '-'}",
                )
                obs_db = f"Cota indisponivel: {motivo}"
                return _payload(
                    "NAO_BAIXADO", f"{obs_db} (termo={det!r})",
                    id_cota=id_cota, caminho_evidencia_falha=pp,
                    finalizacoes=[_fin(id_cota, "NAO_BAIXADO", obs_db, caminho_evidencia=pp)],
                )

            if res == RES_MUITOS:
                pp = _print_falha(page, pasta_nao_baixado, "Multiplos_Clientes_Encontrados")
                qtd = len(det) if det else 0
                obs = f"Busca retornou {qtd} resultados — cliente duplicado ou termo ambiguo"
                return _payload(
                    "NAO_BAIXADO", obs, id_cota=id_cota, caminho_evidencia_falha=pp,
                    finalizacoes=[_fin(id_cota, "NAO_BAIXADO", obs, caminho_evidencia=pp)],
                )

            if res == "ERRO_ENTRAR":
                pp = _print_falha(page, pasta_falha, f"Erro_Entrar_No_Cliente_T{tentativa}")
                ultimo_print = pp
                _log_err(caminho_log, id_cota, "Falha ao entrar no cliente",
                         f"tentativa={tentativa} detalhe={det}")
                if tentativa < MAX_TENTATIVAS_CARDS:
                    try:
                        garantir_url_meus_clientes(page)
                    except Exception:
                        pass
                    continue
                return _payload(
                    "FALHA", f"Nao consegui entrar no cliente: {det}",
                    id_cota=id_cota, retriable=True, caminho_evidencia_falha=pp,
                )

            # res == 'OK' -> entrou no cliente; tenta listar os cards
            _log(caminho_log, id_cota, "Entrou no cliente",
                 f"tentativa={tentativa} url={page.url}")
            try:
                cotas_tela, cotas_duplicadas = avapro.listar_cotas_na_pagina(page)
            except Exception as e:
                cotas_tela, cotas_duplicadas = [], []
                _log_err(caminho_log, id_cota, "Erro ao listar cotas", f"{e}")

            # AVISO: o AVAPRO as vezes mostra a MESMA cota em 2+ cards
            # (defeito visual deles). Deduplicamos e registramos para auditoria.
            if cotas_duplicadas:
                _log_err(
                    caminho_log, id_cota, "AVISO cota duplicada no AVAPRO",
                    f"o AVAPRO exibiu em 2+ cards a(s) cota(s) "
                    f"{', '.join(cotas_duplicadas)} - tratada(s) como 1 (mesmo contrato)",
                )

            if cotas_tela:
                _log(
                    caminho_log, id_cota, "Cotas na tela",
                    f"tentativa={tentativa} qtd={len(cotas_tela)} "
                    f"cotas={[(c['grupo'], c['cota']) for c in cotas_tela]}",
                )
                break

            # cards vazios -> print e reabre (proxima iteracao)
            ultimo_print = _print_falha(page, pasta_falha, f"Cotas_Nao_Apareceram_T{tentativa}")
            _log_err(caminho_log, id_cota, "Cards do cliente nao renderizaram",
                     f"tentativa={tentativa}/{MAX_TENTATIVAS_CARDS}")
            if tentativa < MAX_TENTATIVAS_CARDS:
                try:
                    garantir_url_meus_clientes(page)
                except Exception:
                    pass

        if not cotas_tela:
            # Esgotou as 3 reaberturas - trata como grave/transitorio (retry no main)
            return _payload(
                "FALHA",
                (
                    f"Pagina do cliente '{nome_cliente}' abriu mas os cards de cota "
                    f"nao apareceram na tela apos {MAX_TENTATIVAS_CARDS} "
                    f"recarregamentos (grupo={grupo} cota={cota}). "
                    f"Possivel lentidao do AVAPRO ou problema de sessao. "
                    f"Evidencia salva em: {ultimo_print or 'nao capturada'}"
                ),
                id_cota=id_cota, retriable=True, caminho_evidencia_falha=ultimo_print,
            )

        # --- casa cotas da tela com o lote ---
        # Tres categorias possiveis pra cada cota que aparece na tela:
        #
        # 1. selecionaveis    -> esta no lote E ainda pendente (PENDENTE/
        #                        PROCESSANDO). Vai ser marcada/emitida.
        #
        # 2. nao_encontradas  -> NAO existe no lote em status nenhum.
        #                        Registra em tbl_cotas_nao_encontradas pro
        #                        email final (regra do rpa_gerar_boleto).
        #
        # 3. ja_processadas   -> existe no lote MAS ja foi finalizada
        #                        (BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA) em
        #                        run anterior. Anexa nota idempotente na
        #                        observacao via marcar_cota_reaparecida
        #                        ("reapareceu durante pesquisa da cota X")
        #                        pra rastreabilidade. Nao reprocessa.
        STATUS_PENDENTES = {"PENDENTE", "PROCESSANDO"}
        chaves_lote_completo = _mapear_chaves_lote_completo(id_fila_adm)

        # cota origem = a cota que disparou esse run do worker (a "X" na nota).
        # marcar_cota_reaparecida ja faz zfill(6)/zfill(4) internamente,
        # entao passamos os valores como vieram do contexto sem normalizar.
        grupo_origem = grupo
        cota_origem = cota

        # `pode_unificar` da cota primaria (a que disparou este worker).
        # Se for 'Nao', esta cota deve ser emitida sozinha — nenhuma outra
        # cota do mesmo cliente sera incluida no mesmo boleto.
        _g6_primaria = re.sub(r"\D", "", str(grupo or "")).zfill(6)
        _c4_primaria = re.sub(r"\D", "", str(cota or "")).zfill(4)
        _chave_primaria = (_g6_primaria, _c4_primaria)
        _pode_unificar_primaria = (
            lote_map.get(_chave_primaria, {}).get("pode_unificar") or "Sim"
        )
        _primaria_pode_unificar = (
            _pode_unificar_primaria.strip().lower()
            not in ("nao", "não", "n", "false", "0")
        )

        selecionaveis: List[Dict[str, Any]] = []
        candidatos_nao_encontradas: List[tuple] = []
        ja_processadas: List[tuple] = []
        # Cotas ja BAIXADAS que reaparecem: bloqueiam no modal (evita dupla
        # emissao). chave (g6,c4) -> {id_cota, caminho_boleto}.
        cotas_baixadas_bloqueadas: Dict[tuple, Dict[str, Any]] = {}
        # Rastreamento de diagnóstico para o terminal.
        _nao_selecionadas_info: List[Dict[str, str]] = []
        for c in cotas_tela:
            chave = (c["grupo"], c["cota"])
            if chave in lote_map:
                # Esta no lote e pendente -> avaliar se inclui no batch.
                info = lote_map[chave]
                _pu = (info.get("pode_unificar") or "Sim").strip().lower()
                _esta_pode = _pu not in ("nao", "não", "n", "false", "0")

                if chave == _chave_primaria:
                    # Cota primaria: sempre incluida.
                    selecionaveis.append({
                        "id_cota": info["id_cota"],
                        "grupo": c["grupo"],
                        "cota": c["cota"],
                        "atraso": None,
                    })
                elif _primaria_pode_unificar and _esta_pode:
                    # Cota secundaria: inclui apenas se AMBAS (primaria e
                    # esta) tiverem pode_unificar = Sim.
                    selecionaveis.append({
                        "id_cota": info["id_cota"],
                        "grupo": c["grupo"],
                        "cota": c["cota"],
                        "atraso": None,
                    })
                else:
                    # pode_unificar = Nao em alguma das partes: deixa esta
                    # cota pendente para o proximo ciclo do orquestrador.
                    _motivo_pu = (
                        f"pode_unificar=NAO "
                        f"(primaria={_pode_unificar_primaria!r} esta={_pu!r})"
                    )
                    _log(
                        caminho_log, id_cota,
                        "Cota secundaria ignorada neste ciclo (pode_unificar=Nao)",
                        f"grupo={c['grupo']} cota={c['cota']} "
                        f"pode_unificar_primaria={_pode_unificar_primaria!r} "
                        f"pode_unificar_esta={_pu!r}",
                    )
                    _nao_selecionadas_info.append({
                        "grupo": c["grupo"], "cota": c["cota"],
                        "motivo": _motivo_pu,
                    })
            elif chave in chaves_lote_completo:
                # Esta no lote mas ja foi finalizada num run anterior.
                info = chaves_lote_completo[chave]
                id_cota_processada = info["id_cota"]
                status_anterior = info["status"]

                # Bloqueia se JA foi baixada: status BAIXADO OU caminho_boleto
                # preenchido (cobre inclusive registros ja marcados FALHA por
                # esta mesma regra em execucoes anteriores — protecao permanente).
                if status_anterior == "BAIXADO" or info.get("caminho_boleto"):
                    # JA BAIXADA num boleto anterior. NAO pode ser reselecionada
                    # (evita dupla emissao). Registra para bloquear no modal e
                    # depois marcar FALHA com observacao especifica.
                    cotas_baixadas_bloqueadas[chave] = {
                        "id_cota": id_cota_processada,
                        "caminho_boleto": info.get("caminho_boleto"),
                    }
                    ja_processadas.append((c["grupo"], c["cota"], status_anterior))
                    _log(
                        caminho_log, id_cota,
                        "Cota ja BAIXADA reapareceu — sera bloqueada no modal",
                        f"grupo={c['grupo']} cota={c['cota']} "
                        f"boleto_anterior={info.get('caminho_boleto')!r}",
                    )
                elif status_anterior == "NAO_BAIXADO":
                    # Nao esta pendente mas NAO foi baixada: pode ser unificada
                    # de novo (regra: se apareceu no sistema, unifica junto).
                    # Passa pelo fluxo normal de selecao — que retrata excluido/
                    # modalidade errada e salva print em NAO_BAIXADOS se for o caso.
                    _pu_nb = (info.get("pode_unificar") or "Sim").strip().lower()
                    _esta_pode_nb = _pu_nb not in ("nao", "não", "n", "false", "0")
                    if _primaria_pode_unificar and _esta_pode_nb:
                        selecionaveis.append({
                            "id_cota": id_cota_processada,
                            "grupo": c["grupo"],
                            "cota": c["cota"],
                            "atraso": None,
                        })
                        _log(
                            caminho_log, id_cota,
                            "Cota NAO_BAIXADA reincluida na unificacao",
                            f"grupo={c['grupo']} cota={c['cota']} "
                            f"id_cota_reincluida={id_cota_processada}",
                        )
                    else:
                        ja_processadas.append((c["grupo"], c["cota"], status_anterior))
                else:
                    # ADIANTADO / FALHA / PROCESSANDO: mantem comportamento atual
                    # (apenas anota reaparecimento, nao reprocessa).
                    ja_processadas.append((c["grupo"], c["cota"], status_anterior))
            else:
                # Nao existe no lote em status nenhum.
                # NAO inserimos direto - antes precisamos checar a modalidade
                # da cota (lida do vencimento depois de 'Mostrar mais'),
                # porque o cliente pode ter cotas de MOTORS e IMOVEL e a outra
                # modalidade esta na planilha em OUTRA ABA (lote separado).
                # So registra como 'nao encontrada' se for da mesma modalidade
                # que esta rodando agora.
                candidatos_nao_encontradas.append((c["grupo"], c["cota"]))

        # --- filtra candidatas a nao_encontrada por modalidade ---
        # Para cada candidata, abre 'Mostrar mais', le o vencimento e
        # classifica como MOTORS (dia<15) ou IMOVEL (dia>=15). So insere
        # em tbl_cotas_nao_encontradas se bater com a modalidade do lote
        # (ou se nao foi possivel determinar - fallback conservador,
        # mantem o comportamento antigo de registrar).
        #
        # OTIMIZACAO: candidatas que ja estao em tbl_cotas_nao_encontradas
        # para este lote nao precisam passar pelo 'Mostrar mais' de novo.
        # Isso evita gastar tempo extra na tela quando o mesmo cliente e
        # visitado multiplas vezes (ocorre quando pode_unificar = NÃO faz
        # o orquestrador rodar um worker separado por cota do mesmo cliente).
        try:
            _ja_registradas_nf: set = {
                (
                    re.sub(r"\D", "", str(r["grupo"] or "")).zfill(6),
                    re.sub(r"\D", "", str(r["cota"] or "")).zfill(4),
                )
                for r in listar_cotas_nao_encontradas(id_fila_adm)
            }
        except Exception as _e_nf:
            _log_err(caminho_log, id_cota,
                     "Aviso: nao consegui ler cotas_nao_encontradas para skip",
                     f"{type(_e_nf).__name__}: {_e_nf}")
            _ja_registradas_nf = set()

        nao_encontradas: List[tuple] = []
        ignoradas_outra_modalidade: List[tuple] = []
        for (g, c) in candidatos_nao_encontradas:
            _g_norm = re.sub(r"\D", "", str(g or "")).zfill(6)
            _c_norm = re.sub(r"\D", "", str(c or "")).zfill(4)

            # Ja registrada neste lote: conta mas nao reabre 'Mostrar mais'.
            if (_g_norm, _c_norm) in _ja_registradas_nf:
                nao_encontradas.append((g, c))
                _log(
                    caminho_log, id_cota,
                    "Cota nao encontrada ja registrada — Mostrar mais ignorado",
                    f"grupo={g} cota={c} cliente={nome_cliente}",
                )
                continue
            mod_detectada: Optional[str] = None
            venc_str: Optional[str] = None
            try:
                card_nf = avapro.localizar_card_cota(page, g, c)

                # ── Badge 'Excluído' em cota candidata ────────────────────
                # Se a cota nao esta no lote mas tambem esta marcada como
                # 'Excluido' no AVAPRO, nao abre 'Mostrar mais' e nao
                # registra como cota_nao_encontrada. Apenas loga e ignora.
                if avapro.verificar_badge_excluido(card_nf):
                    _g_excl_nf = re.sub(r"\D", "", str(g or "")).zfill(6)
                    _c_excl_nf = re.sub(r"\D", "", str(c or "")).zfill(4)
                    _log(
                        caminho_log, id_cota,
                        "Cota candidata (fora do lote) EXCLUIDA — ignorada sem registro",
                        f"grupo={_g_excl_nf} cota={_c_excl_nf} "
                        f"cliente={nome_cliente!r}",
                    )
                    continue  # finally roda fechar_modal (no-op); proxima candidata
                elif avapro.verificar_badge_desistente(card_nf):
                    _g_desis_nf = re.sub(r"\D", "", str(g or "")).zfill(6)
                    _c_desis_nf = re.sub(r"\D", "", str(c or "")).zfill(4)
                    _log(
                        caminho_log, id_cota,
                        "Cota candidata (fora do lote) DESISTENTE — ignorada sem registro",
                        f"grupo={_g_desis_nf} cota={_c_desis_nf} "
                        f"cliente={nome_cliente!r}",
                    )
                    continue  # finally roda fechar_modal (no-op); proxima candidata
                # ─────────────────────────────────────────────────────────

                avapro.clicar_mostrar_mais(page, card_nf)
                dados_nf = avapro.ler_dados_cota_expandida(page)
                venc_str = dados_nf.get("vencimento_str")
                mod_detectada = avapro.classificar_modalidade_por_vencimento(
                    dados_nf.get("vencimento_dt")
                )
            except Exception as e:
                _log_err(
                    caminho_log, id_cota,
                    "Falha ao ler vencimento de cota fora do lote",
                    f"grupo={g} cota={c}: {type(e).__name__}: {e}",
                )
            finally:
                _err_fechar_nf = _fechar_modal_com_retry(
                    page, pasta_falha, caminho_log, id_cota, g, c
                )
                if _err_fechar_nf:
                    _log_err(
                        caminho_log, id_cota,
                        "Modal nao fechou apos 3 tentativas — abortando processamento",
                        f"grupo={g} cota={c}",
                    )
                    return _err_fechar_nf

            # Se detectamos modalidade E ela e DIFERENTE da que esta rodando,
            # ignora - essa cota pertence ao lote da outra modalidade.
            if (
                mod_detectada
                and modalidade_lote
                and mod_detectada != modalidade_lote
            ):
                ignoradas_outra_modalidade.append((g, c, mod_detectada))
                _log(
                    caminho_log, id_cota,
                    "Cota de outra modalidade ignorada (nao registrada)",
                    f"grupo={g} cota={c} vencimento={venc_str!r} "
                    f"detectada={mod_detectada} rodando={modalidade_lote!r} "
                    f"cliente={nome_cliente}",
                )
                continue

            # Mesma modalidade (ou nao detectada): registra como antes.
            nao_encontradas.append((g, c))
            try:
                inserir_cota_nao_encontrada(
                    id_fila_adm,
                    nome_cliente,
                    g,
                    c,
                )
            except Exception as e:
                _log_err(
                    caminho_log, id_cota,
                    "Falha ao registrar cota nao encontrada",
                    f"grupo={g} cota={c} "
                    f"{type(e).__name__}: {e}",
                )

        if nao_encontradas:
            _log(
                caminho_log, id_cota,
                "Cotas do AVAPRO fora do lote registradas",
                f"qtd={len(nao_encontradas)} "
                f"cotas={[f'{g}/{c}' for g, c in nao_encontradas]} "
                f"cliente={nome_cliente}",
            )
            # Screenshot será tirado APÓS marcar os checkboxes das cotas da planilha,
            # para que a imagem mostre claramente quais foram selecionadas (da planilha)
            # e quais ficaram sem seleção (extras no sistema). Ver bloco abaixo.

        if ignoradas_outra_modalidade:
            _log(
                caminho_log, id_cota,
                "Cotas de outra modalidade ignoradas no diff",
                f"qtd={len(ignoradas_outra_modalidade)} "
                f"cotas={[f'{g}/{c}({m})' for g, c, m in ignoradas_outra_modalidade]} "
                f"rodando={modalidade_lote!r} cliente={nome_cliente}",
            )

        if ja_processadas:
            _log(
                caminho_log, id_cota,
                "Cotas ja processadas reapareceram na tela",
                f"qtd={len(ja_processadas)} "
                f"cotas={[f'{g}/{c} ({st})' for g, c, st in ja_processadas]} "
                f"cliente={nome_cliente} "
                f"observacao anexada nas cotas com referencia a "
                f"{grupo_origem}/{cota_origem}",
            )

        _log(
            caminho_log, id_cota, "Cotas do lote casadas",
            f"qtd={len(selecionaveis)} ids={[s['id_cota'] for s in selecionaveis]}",
        )
        # IDs das cotas DESTE cliente que estao no lote — usado para NAO_BAIXADO em lote.
        # Importante: nao usar lote_map.values() (teria TODAS as cotas do lote inteiro).
        _ids_cliente_atual = [s["id_cota"] for s in selecionaveis]

        # --- Cota primaria nao apareceu na tela do cliente ---
        # Mesmo que existam cotas secundarias selecionaveis, se a cota que
        # este worker foi despachado para processar nao esta entre os cards
        # visiveis, nao emite nada: tira print e encerra como NAO_BAIXADO.
        _primaria_na_tela = any(
            s["id_cota"] == id_cota for s in selecionaveis
        )
        if not _primaria_na_tela:
            _cotas_visiveis_str = ", ".join(
                f"{c['grupo']}/{c['cota']}" for c in cotas_tela
            ) or "nenhuma"
            obs_nao_apareceu = (
                f"Cota {_g6_primaria}/{_c4_primaria} nao apareceu na pagina do cliente "
                f"'{nome_cliente}' — cards visiveis: {_cotas_visiveis_str}."
            )
            pp = _print_falha(page, pasta_nao_encontrada, "Cota_Nao_Apareceu_Na_Tela")
            _log_err(caminho_log, id_cota, "Cota primaria ausente na tela", obs_nao_apareceu)
            return _payload(
                "NAO_BAIXADO", obs_nao_apareceu,
                id_cota=id_cota,
                retriable=False,
                caminho_evidencia_falha=pp,
                finalizacoes=[_fin(id_cota, "NAO_BAIXADO", obs_nao_apareceu,
                                   caminho_evidencia=pp)],
            )

        if not selecionaveis:
            pp = _print_falha(page, pasta_nao_encontrada, "Cota_Sem_Registro_No_Banco")
            obs_db = "Cota indisponivel: cota nao localizada na pagina do cliente"
            return _payload(
                "NAO_BAIXADO", obs_db, id_cota=id_cota, caminho_evidencia_falha=pp,
                finalizacoes=[_fin(id_cota, "NAO_BAIXADO", obs_db, caminho_evidencia=pp)],
            )

        # --- Verificacao PRIORITARIA: cota primaria com badge 'Excluido' ---
        #
        # Feita ANTES de qualquer 'Mostrar mais' ou selecao de checkbox.
        # Se a cota que foi pesquisada estiver marcada como 'Excluido' no
        # AVAPRO, nao ha nada a emitir: registra NAO_BAIXADO definitivo,
        # tira print full_page em Falhas\Excluidos\ e encerra sem retry.
        _g6_primaria_check = re.sub(r"\D", "", str(grupo or "")).zfill(6)
        _c4_primaria_check = re.sub(r"\D", "", str(cota or "")).zfill(4)
        try:
            _card_primaria_check = avapro.localizar_card_cota(
                page, _g6_primaria_check, _c4_primaria_check, timeout_ms=8000
            )
            if avapro.verificar_badge_excluido(_card_primaria_check):
                _pp_excl = _print_excluido(
                    page, caminho_base, nome_cliente,
                    _g6_primaria_check, _c4_primaria_check,
                )
                _obs_excl = f"Cota {_g6_primaria_check}/{_c4_primaria_check} com status 'Excluído' no AVAPRO — boleto não emitido."
                _log_err(
                    caminho_log, id_cota,
                    "Cota EXCLUIDA no AVAPRO — NAO_BAIXADO definitivo",
                    f"grupo={_g6_primaria_check} cota={_c4_primaria_check} "
                    f"cliente={nome_cliente!r} print={_pp_excl or '-'}",
                )
                return _payload(
                    "NAO_BAIXADO", _obs_excl,
                    id_cota=id_cota,
                    retriable=False,
                    caminho_evidencia_falha=_pp_excl,
                    finalizacoes=[
                        _fin(id_cota, "NAO_BAIXADO", _obs_excl,
                             caminho_evidencia=_pp_excl)
                    ],
                )
            elif avapro.verificar_badge_desistente(_card_primaria_check):
                _pp_desis = _print_desistente(
                    page, caminho_base, nome_cliente,
                    _g6_primaria_check, _c4_primaria_check,
                )
                _obs_desis = f"Cota {_g6_primaria_check}/{_c4_primaria_check} com status 'Desistente' no AVAPRO — boleto não emitido."
                _log_err(
                    caminho_log, id_cota,
                    "Cota DESISTENTE no AVAPRO — NAO_BAIXADO definitivo",
                    f"grupo={_g6_primaria_check} cota={_c4_primaria_check} "
                    f"cliente={nome_cliente!r} print={_pp_desis or '-'}",
                )
                return _payload(
                    "NAO_BAIXADO", _obs_desis,
                    id_cota=id_cota,
                    retriable=False,
                    caminho_evidencia_falha=_pp_desis,
                    finalizacoes=[
                        _fin(id_cota, "NAO_BAIXADO", _obs_desis,
                             caminho_evidencia=_pp_desis)
                    ],
                )
        except Exception as _e_excl_check:
            _log_err(
                caminho_log, id_cota,
                "Aviso: falha ao verificar badge Excluido/Desistente da cota primaria (segue fluxo)",
                f"{type(_e_excl_check).__name__}: {_e_excl_check}",
            )

        # --- por cota: expande p/ ler vencimento (modalidade), fecha modal,
        #              marca checkbox ---
        #
        # REGRA DE MODALIDADE (aplicada mesmo para cotas que estao no lote):
        #   - Lote MOTORS  → ignora cotas com vencimento >= dia 15 (sao IMOVEL)
        #   - Lote IMOVEL  → ignora cotas com vencimento <  dia 15 (sao MOTORS)
        #
        # NOVO: se 'Mostrar mais' retornar 'Detalhes da cota nao encontrados',
        #   o robo clica em Fechar e infere a modalidade pelo icone Imoveis:
        #   - icone Imoveis presente -> IMOVEL
        #   - sem icone              -> MOTORS
        #   A cota e marcada normalmente e a flag detalhes_nao_encontrado=True
        #   e propagada para adicionar '| Detalhes da cota nao encontrado' na
        #   observacao final do banco.
        #
        # NOTA: parcelas_atraso NAO e mais lido aqui - vem do novo modal de
        #   selecao de parcelas (apos clicar 'Emitir boleto').
        selecionadas: List[Dict[str, Any]] = []
        ignoradas_modalidade_lote: List[Dict[str, Any]] = []
        for s in selecionaveis:
            g, c = s["grupo"], s["cota"]
            _modalidade_ok = True
            s["detalhes_nao_encontrado"] = False  # flag para a observacao final
            try:
                card = avapro.localizar_card_cota(page, g, c)

                # ── Badge 'Excluído' ───────────────────────────────────────
                # Verificado ANTES de clicar 'Mostrar mais'. Se o card ja
                # esta excluido no AVAPRO, registra NAO_BAIXADO e pula para
                # a proxima cota sem abrir nenhum modal.
                if avapro.verificar_badge_excluido(card):
                    _g_excl = re.sub(r"\D", "", str(g or "")).zfill(6)
                    _c_excl = re.sub(r"\D", "", str(c or "")).zfill(4)
                    _obs_excl_sel = f"Cota {_g_excl}/{_c_excl} com status 'Excluído' no AVAPRO — boleto não emitido."
                    _log_err(
                        caminho_log, id_cota,
                        "Cota selecionavel EXCLUIDA no AVAPRO — pulando sem Mostrar mais "
                        "(print adiado para apos marcar checkboxes das cotas normais)",
                        f"grupo={_g_excl} cota={_c_excl} cliente={nome_cliente!r}",
                    )
                    # Print ADIADO: tirado apos marcar os checkboxes das cotas
                    # normais, para que a foto mostre o estado real da seleção
                    # (normais selecionadas + excluída visível sem checkbox).
                    ignoradas_modalidade_lote.append({
                        "id_cota": s["id_cota"],
                        "grupo": g,
                        "cota": c,
                        "obs": _obs_excl_sel,
                        "print": None,           # preenchido após o loop
                        "_g_excl": _g_excl,      # guardado para o print adiado
                        "_c_excl": _c_excl,
                    })
                    _nao_selecionadas_info.append({
                        "grupo": g, "cota": c,
                        "motivo": "cota excluida no AVAPRO (badge Excluído detectado)",
                    })
                    continue  # finally roda fechar_modal (no-op); proxima cota
                elif avapro.verificar_badge_desistente(card):
                    _g_desis = re.sub(r"\D", "", str(g or "")).zfill(6)
                    _c_desis = re.sub(r"\D", "", str(c or "")).zfill(4)
                    _obs_desis_sel = f"Cota {_g_desis}/{_c_desis} com status 'Desistente' no AVAPRO — boleto não emitido."
                    _log_err(
                        caminho_log, id_cota,
                        "Cota selecionavel DESISTENTE no AVAPRO — pulando sem Mostrar mais "
                        "(print adiado para apos marcar checkboxes das cotas normais)",
                        f"grupo={_g_desis} cota={_c_desis} cliente={nome_cliente!r}",
                    )
                    ignoradas_modalidade_lote.append({
                        "id_cota": s["id_cota"],
                        "grupo": g,
                        "cota": c,
                        "obs": _obs_desis_sel,
                        "print": None,
                        "_g_excl": _g_desis,
                        "_c_excl": _c_desis,
                        "_desistente": True,
                    })
                    _nao_selecionadas_info.append({
                        "grupo": g, "cota": c,
                        "motivo": "cota desistente no AVAPRO (badge Desistente detectado)",
                    })
                    continue  # finally roda fechar_modal (no-op); proxima cota
                # ─────────────────────────────────────────────────────────

                avapro.clicar_mostrar_mais(page, card)

                # Verifica se apareceu 'Detalhes da cota nao encontrados.'
                if avapro.detectar_detalhes_nao_encontrados(page):
                    # Fecha o dialogo
                    avapro.fechar_dialog_detalhes_nao_encontrados(page)
                    page.wait_for_timeout(300)
                    s["detalhes_nao_encontrado"] = True
                    # Infere modalidade pelo icone Imoveis no card
                    _tem_imoveis = avapro.verificar_imoveis_no_card(page, card)
                    mod_cota_s = "IMOVEL" if _tem_imoveis else "MOTORS"
                    venc_str_s = None
                    _log(
                        caminho_log, id_cota,
                        "Mostrar mais: Detalhes nao encontrados — modalidade inferida",
                        f"grupo={g} cota={c} imoveis={_tem_imoveis} "
                        f"mod_inferida={mod_cota_s}",
                    )
                else:
                    dados = avapro.ler_dados_cota_expandida(page)
                    venc_str_s = dados.get("vencimento_str")
                    venc_dt_s  = dados.get("vencimento_dt")
                    mod_cota_s = avapro.classificar_modalidade_por_vencimento(venc_dt_s)
                    _log(
                        caminho_log, id_cota, "Vencimento lido",
                        f"grupo={g} cota={c} "
                        f"assembleia={dados.get('assembleia_atual')!r} "
                        f"vencimento={venc_str_s!r}",
                    )

                # Rede de seguranca: se o vencimento NAO foi classificado
                # (mod_cota_s=None), decide pelo SEGMENTO do card (Imoveis x
                # Veiculos), que o AVAPRO mostra explicitamente. Sem isso, uma
                # cota de Veiculos (MOTORS) acabava selecionada num lote IMOVEL.
                if mod_cota_s is None:
                    try:
                        _seg_imoveis = avapro.verificar_imoveis_no_card(page, card)
                        mod_cota_s = "IMOVEL" if _seg_imoveis else "MOTORS"
                        _log(
                            caminho_log, id_cota,
                            "Modalidade inferida pelo segmento do card (vencimento indisponivel)",
                            f"grupo={g} cota={c} imoveis={_seg_imoveis} mod={mod_cota_s}",
                        )
                    except Exception:
                        pass

                if mod_cota_s and modalidade_lote and mod_cota_s != modalidade_lote:
                    # Vencimento revela que esta cota pertence a OUTRA modalidade.
                    # Nao marca checkbox, nao emite boleto.
                    _modalidade_ok = False
                    obs_ignorada = f"Cota de {mod_cota_s} no lote de {modalidade_lote} — modalidade diferente, boleto não emitido."
                    # Screenshot obrigatório: finalizar_cota_resultado exige
                    # caminho_evidencia para NAO_BAIXADO.
                    _g_digits = re.sub(r"\D", "", str(g))
                    _c_digits = re.sub(r"\D", "", str(c))
                    _pp_mod = _print_falha(
                        page, pasta_nao_baixado,
                        f"Cota_De_Outro_Produto_{_g_digits}_{_c_digits}",
                    )
                    _log(
                        caminho_log, id_cota,
                        "Cota do lote ignorada — modalidade incorreta pelo vencimento",
                        f"grupo={g} cota={c} vencimento={venc_str_s!r} "
                        f"detectada={mod_cota_s} rodando={modalidade_lote!r} "
                        f"cliente={nome_cliente} evidencia={_pp_mod or '-'}",
                    )
                    ignoradas_modalidade_lote.append({
                        "id_cota": s["id_cota"],
                        "grupo": g,
                        "cota": c,
                        "obs": obs_ignorada,
                        "print": _pp_mod,
                    })
                    _nao_selecionadas_info.append({
                        "grupo": g, "cota": c,
                        "motivo": (
                            f"modalidade {mod_cota_s!r} != {modalidade_lote!r}"
                            + (f" (venc={venc_str_s!r})" if venc_str_s else "")
                        ),
                    })
            except Exception as e:
                _log_err(caminho_log, id_cota, "Falha ao ler vencimento",
                         f"grupo={g} cota={c}: {e}")
            finally:
                _err_fechar = _fechar_modal_com_retry(
                    page, pasta_falha, caminho_log, id_cota, g, c
                )
                if _err_fechar:
                    _log_err(
                        caminho_log, id_cota,
                        "Modal nao fechou apos 3 tentativas — abortando processamento",
                        f"grupo={g} cota={c}",
                    )
                    return _err_fechar

            if not _modalidade_ok:
                continue  # Nao marca checkbox — cota sera finalizada como NAO_BAIXADO

            try:
                _registrar_passo(id_cota, f"marcando checkbox da cota {g}/{c} na tela do cliente")
                avapro.marcar_checkbox_cota(
                    page, g, c,
                    log_fn=lambda acao, detalhe="": _log(caminho_log, id_cota, acao, detalhe),
                )
                selecionadas.append(s)
                _log(caminho_log, id_cota, "Checkbox marcado", f"grupo={g} cota={c}")
            except Exception as e:
                _log_err(caminho_log, id_cota, "Checkbox nao marcado",
                         f"grupo={g} cota={c}: {e}")
                s["erro_selecao"] = str(e)
                _nao_selecionadas_info.append({
                    "grupo": g, "cota": c,
                    "motivo": f"checkbox nao encontrado: {str(e)[:80]}",
                })

        # Screenshots das cotas excluidas (badge 'Excluído'): tirados AQUI,
        # depois que os checkboxes das cotas normais foram marcados.
        # O print mostra as normais selecionadas + a excluída sem checkbox,
        # o que é evidência melhor do que um print tirado antes de qualquer seleção.
        for _ig_excl in ignoradas_modalidade_lote:
            if "_g_excl" in _ig_excl:  # entradas com badge Excluido ou Desistente
                try:
                    _eh_desistente = _ig_excl.get("_desistente", False)
                    if _eh_desistente:
                        _pp_excl_defer = _print_desistente(
                            page, caminho_base, nome_cliente,
                            _ig_excl["_g_excl"], _ig_excl["_c_excl"],
                        )
                        _tipo_badge = "desistente"
                    else:
                        _pp_excl_defer = _print_excluido(
                            page, caminho_base, nome_cliente,
                            _ig_excl["_g_excl"], _ig_excl["_c_excl"],
                        )
                        _tipo_badge = "excluida"
                    _ig_excl["print"] = _pp_excl_defer
                    _log(
                        caminho_log, id_cota,
                        f"Print cota {_tipo_badge} salvo (apos selecao das normais)",
                        f"grupo={_ig_excl['_g_excl']} cota={_ig_excl['_c_excl']} "
                        f"arquivo={_pp_excl_defer or '-'}",
                    )
                except Exception as _e_excl_print:
                    _log_err(
                        caminho_log, id_cota,
                        "Falha ao tirar print adiado da cota excluida/desistente",
                        f"grupo={_ig_excl.get('_g_excl')} cota={_ig_excl.get('_c_excl')}: "
                        f"{type(_e_excl_print).__name__}: {_e_excl_print}",
                    )

        # Screenshot das cotas faltantes: tirado AQUI, após marcar os checkboxes
        # das cotas da planilha. Assim o print mostra quais estão selecionadas
        # (da planilha) e quais ficaram sem seleção (extras no sistema).
        if nao_encontradas:
            try:
                pasta_nf = pasta_cotas_nao_localizadas_planilha(caminho_base)
                pasta_nf.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                nome_safe = re.sub(r'[\\/:*?"<>|\s]+', "_", nome_cliente)[:50]
                destino_print_nf = pasta_nf / f"{nome_safe}_{ts}.png"
                page.screenshot(path=str(destino_print_nf), full_page=True)
                _sel_str = [f"{s['grupo']}/{s['cota']}" for s in selecionadas]
                _log(
                    caminho_log, id_cota,
                    "Screenshot Evidencias_Cotas_Faltantes salvo",
                    f"arquivo={destino_print_nf.name} "
                    f"cotas_faltantes={[f'{g}/{c}' for g, c in nao_encontradas]} "
                    f"cotas_selecionadas={_sel_str}",
                )
            except Exception as e:
                _log_err(
                    caminho_log, id_cota,
                    "Falha ao salvar screenshot de Evidencias_Cotas_Faltantes",
                    f"{type(e).__name__}: {e}",
                )

        if not selecionadas:
            # Caso A: todas as cotas eram da modalidade errada — NAO_BAIXADO definitivo.
            if ignoradas_modalidade_lote:
                ids_ignorados = {ig["id_cota"] for ig in ignoradas_modalidade_lote}
                # Cada cota ignorada por modalidade errada JÁ tem screenshot tirado
                # no momento da detecção (campo "print"). Obrigatório para o DB:
                # finalizar_cota_resultado exige caminho_evidencia para NAO_BAIXADO.
                fins_ignoradas = [
                    _fin(ig["id_cota"], "NAO_BAIXADO", ig["obs"],
                         caminho_evidencia=ig.get("print"))
                    for ig in ignoradas_modalidade_lote
                ]
                # Screenshot de fallback para o caso de a cota primária não ter
                # aparecido entre as ignoradas (situação improvável mas defensiva).
                _pp_fallback: Optional[str] = None
                if id_cota not in ids_ignorados:
                    _pp_fallback = _print_falha(page, pasta_nao_baixado,
                                                "Cota_De_Outro_Produto")
                    fins_ignoradas.append(
                        _fin(id_cota, "NAO_BAIXADO",
                             f"Nenhuma cota da modalidade {modalidade_lote!r} "
                             f"disponivel para emissao",
                             caminho_evidencia=_pp_fallback)
                    )
                # Evidência para o fallback do orquestrador (caso _aplicar_finalizacoes
                # falhe no worker — o orq. tenta novamente com este caminho).
                _pp_evidencia_mod = (
                    next((ig.get("print") for ig in ignoradas_modalidade_lote
                          if ig.get("print")), None)
                    or _pp_fallback
                )
                obs_mod = f"Nenhuma cota de {modalidade_lote} disponível — {len(ignoradas_modalidade_lote)} cota(s) de outra modalidade ignorada(s)."
                _log(caminho_log, id_cota, "Lote encerrado sem emissao",
                     obs_mod)
                return _payload(
                    "NAO_BAIXADO", obs_mod, id_cota=id_cota,
                    caminho_evidencia_falha=_pp_evidencia_mod,
                    finalizacoes=fins_ignoradas,
                    cotas_nao_selecionadas=_nao_selecionadas_info,
                )
            # Caso B: nenhum checkbox marcado por erro tecnico → FALHA retriable.
            pp = _print_falha(page, pasta_falha, "Erro_Selecionar_Parcela")
            _cotas_tentadas = ", ".join(
                f"{s['grupo']}/{s['cota']}" for s in selecionaveis
            )
            obs = f"Nenhum checkbox encontrado para {len(selecionaveis)} cota(s) — possível falha de carregamento do AVAPRO."
            return _payload(
                "FALHA", obs, id_cota=id_cota, retriable=True,
                caminho_evidencia_falha=pp,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # ============================================================
        # Emissao: clique + modal de selecao de parcelas + download
        # ============================================================
        #
        # Novo fluxo (nova implantacao AVAPRO):
        #   1. Snapshot da pasta Downloads ANTES do clique.
        #   2. Clica "Emitir boleto" (botao primario do card).
        #   3. Modal "Selecione as parcelas para emissão do boleto" aparece.
        #      Para cada card de cota no modal:
        #        a) Expande o card.
        #        b) Se 'Detalhes da cota nao encontrados.' → fechar + imoveis ok.
        #        c) Seleciona: todas parcelas 'Em atraso' + parcela do mes ref.
        #        d) Se parcela do mes ref NAO encontrada → cota adiantada (modal).
        #   4. Cotas adiantadas (modal): print + salva em ADIANTADOS/{nome}_{gc}/.
        #      Se TODAS as cotas sao adiantadas → retorna ADIANTADO sem continuar.
        #   5. Clica "Continuar" no modal → browser inicia download do PDF.
        #   6. Loop 180s:
        #        a) PDF novo estavel em Downloads → move p/ destino → BAIXADO.
        #        b) Toast "sem cobrancas" → print em verificar_adiantados → ADIANTADO.
        #        c) Toast com "erro/falha" → print em FALHAS → FALHA.
        #   7. Timeout → print EMITIR_SEM_RESPOSTA → FALHA.
        #
        # parcelas_atraso: contado pelo modal (status 'Em atraso' por cota).
        # Nome do arquivo: '{situacao_atraso} {Nome}.pdf' — padrao rpa_gerar_boleto.
        # ============================================================

        # Snapshot Downloads ANTES de clicar Emitir boleto.
        # Registramos tambem o TIMESTAMP para detectar re-downloads:
        # se o AVAPRO salvar um arquivo com o mesmo nome que ja estava em
        # Downloads (ex: boleto do mesmo cliente processado antes), a
        # comparacao por nome nao detecta como novo — mas o mtime sim.
        _snapshot_dl = avapro.snapshot_pdfs_downloads()
        _snapshot_time = time.time()

        # --- Clica "Emitir boleto" (botao primario do card) ---
        _registrar_passo(id_cota, "clicando em 'Emitir boleto'")
        avapro.clicar_baixar_documentos_emitir_boleto(
            page,
            log_fn=lambda acao, detalhe="": _log(caminho_log, id_cota, acao, detalhe),
        )
        _log_tempo(caminho_log, id_cota, "emitir_boleto_clicado", t0)

        _log(
            caminho_log, id_cota, "Emitir Boleto clicado",
            "aguardando modal de selecao de parcelas",
        )

        # --- Modal de selecao de parcelas ---
        # Aguarda ate 2 minutos por tentativa. Em falha, volta para Meus
        # Clientes, re-pesquisa o cliente e re-clica Emitir boleto (sem
        # re-login — leve, nao sobrecarrega o sistema). Maximo 2 tentativas.
        # Se _cliente_pagina_ok, nao faz retry: o h2 ja confirmou a pagina;
        # timeout no modal = erro mapeado → NAO_BAIXADO direto.
        MAX_TENT_MODAL = 1 if _cliente_pagina_ok else 2
        _modal_result  = None
        _ultimo_pp_modal: Optional[str] = None

        for _tent_modal in range(1, MAX_TENT_MODAL + 1):

            # Na primeira tentativa o clique em Emitir boleto ja foi feito.
            # Nas demais: volta pra Meus Clientes, re-pesquisa e re-clica.
            if _tent_modal > 1:
                _log(caminho_log, id_cota,
                     f"Retry modal leve {_tent_modal}/{MAX_TENT_MODAL}",
                     "voltando para Meus Clientes sem re-login")
                try:
                    garantir_url_meus_clientes(page)
                except Exception as _e_nav:
                    _log_err(caminho_log, id_cota,
                             "Falha ao voltar para Meus Clientes", str(_e_nav))
                res_re, _, _pp_re = _entrar_via_busca(
                    page, grupo, cota, nome_cliente,
                    caminho_log, id_cota, pasta_falha=pasta_falha,
                )
                if res_re != "OK":
                    _log_err(caminho_log, id_cota,
                             f"Re-entrada no cliente falhou no retry {_tent_modal}",
                             str(res_re))
                    break

                # Verifica se a pagina do cliente carregou (h2 visivel).
                # Se h2 visivel -> NAO_BAIXADO em caso de nova falha no modal.
                # Se h2 nao visivel -> FALHA tecnica.
                try:
                    _h2_loc = page.locator("h2.text-2xl.font-semibold.py-6").first
                    _h2_visivel = _h2_loc.is_visible(timeout=5000)
                    _h2_texto_retry = (_h2_loc.inner_text(timeout=2000) or "").strip() if _h2_visivel else ""
                    _cliente_pagina_ok = bool(_h2_visivel and _h2_texto_retry)
                    if _cliente_pagina_ok:
                        _h2_nome_pagina = _h2_texto_retry  # atualiza com nome visto no retry
                except Exception:
                    _cliente_pagina_ok = False
                _log(caminho_log, id_cota,
                     "Pagina do cliente no retry",
                     f"carregou={'sim' if _cliente_pagina_ok else 'nao'} texto={repr(_h2_nome_pagina) if _cliente_pagina_ok else '-'}")

                try:
                    avapro.clicar_baixar_documentos_emitir_boleto(
                        page,
                        log_fn=lambda acao, detalhe="": _log(caminho_log, id_cota, acao, detalhe),
                    )
                except Exception as _e_emit:
                    _log_err(caminho_log, id_cota,
                             f"Re-clique Emitir boleto falhou no retry {_tent_modal}",
                             str(_e_emit))
                    break

            # Detecta dialog de erro antes de entrar no modal
            _erro_dialog = avapro.detectar_e_fechar_erro_parcelas(page, timeout_ms=5000)
            if _erro_dialog:
                _vai_nb_dialog = _cliente_pagina_ok and (_tent_modal >= MAX_TENT_MODAL)
                if not _vai_nb_dialog:
                    _ultimo_pp_modal = _print_falha(
                        page, pasta_falha, f"Erro_Carregar_Parcelas_T{_tent_modal}"
                    )
                _log_err(caminho_log, id_cota,
                         f"Dialog de erro ao carregar parcelas (t{_tent_modal})",
                         f"{_erro_dialog} | print={_ultimo_pp_modal or '-'}")
                avapro.fechar_dialog_erro_parcelas(page)
                if _tent_modal < MAX_TENT_MODAL:
                    continue
                # Pagina carregou mas modal nao abre → erro mapeado → NAO_BAIXADO
                if _cliente_pagina_ok:
                    _pp_nb = _print_falha(page, pasta_nao_baixado,
                                          f"Erro_Carregar_Parcelas_T{_tent_modal}")
                    _obs_nb = f"Boleto não emitido para {_h2_nome_pagina} — sistema não respondeu."
                    _log_err(caminho_log, id_cota, "NAO_BAIXADO (erro mapeado)", _obs_nb)
                    # Grava todas as cotas do lote em UMA conexao (evita timeout por N conexoes)
                    finalizar_cotas_lote_resultado(_ids_cliente_atual, "NAO_BAIXADO", _obs_nb, _pp_nb)
                    return _payload(
                        "NAO_BAIXADO", _obs_nb,
                        id_cota=id_cota, retriable=False,
                        caminho_evidencia_falha=_pp_nb,
                        cotas_nao_selecionadas=_nao_selecionadas_info,
                    )
                return _payload(
                    "FALHA",
                    "Erro de carregamento no AVAPRO. "
                    "O boleto não pôde ser emitido mesmo após 2 tentativas.",
                    id_cota=id_cota, retriable=True,
                    caminho_evidencia_falha=_ultimo_pp_modal,
                    cotas_nao_selecionadas=_nao_selecionadas_info,
                )

            # Sem dialog → processa modal.
            # selecionar_parcelas_no_modal:
            #   1) aguarda o modal abrir
            #   2) altera Venc. boleto para a data correta do mes ref
            #   3) so entao le as parcelas/mensagens
            # O retry_cb (verificacao de checkboxes antes da data ser alterada)
            # foi removido pois interferia quando o modal abria com
            # "Nenhuma parcela disponivel" (Venc. boleto = Hoje).
            _registrar_passo(id_cota, "selecionando parcelas no modal de emissao")
            _modal_result = avapro.selecionar_parcelas_no_modal(
                page, _data_ref_do_lote(ctx),
                modalidade=ctx.get("modalidade", "IMOVEL"),
                cotas_bloqueadas=set(cotas_baixadas_bloqueadas.keys()),
            )
            _log_tempo(caminho_log, id_cota, "modal_parcelas_concluido", t0)

            if not _modal_result.get("erro"):
                break  # sucesso — sai do loop

            # Falha no modal: tira print em FALHAS para auditoria.
            # Excecao: se a pagina carregou e e a ultima tentativa (vai para NAO_BAIXADO),
            # nao duplica o print em FALHAS — so vai o print em NAO_BAIXADOS.
            _vai_nao_baixado = _cliente_pagina_ok and (_tent_modal >= MAX_TENT_MODAL)
            if _modal_result.get("dialog_erro_aberto"):
                if not _vai_nao_baixado:
                    _ultimo_pp_modal = _print_falha(
                        page, pasta_falha, f"Erro_Carregar_Parcelas_T{_tent_modal}"
                    )
                avapro.fechar_dialog_erro_parcelas(page)
            else:
                if not _vai_nao_baixado:
                    _ultimo_pp_modal = _print_falha(
                        page, pasta_falha, f"Modal_Parcelas_Falhou_T{_tent_modal}"
                    )
            _log_err(caminho_log, id_cota,
                     f"Falha no modal de parcelas (t{_tent_modal})",
                     f"{_modal_result['erro']} | print={_ultimo_pp_modal or '-'}")
            if _tent_modal >= MAX_TENT_MODAL:
                # Pagina carregou mas modal nao abre → erro mapeado → NAO_BAIXADO
                if _cliente_pagina_ok:
                    _pp_nb = _print_falha(page, pasta_nao_baixado,
                                          f"Modal_Parcelas_Falhou_T{_tent_modal}")
                    _obs_nb = f"Boleto não emitido para {_h2_nome_pagina} — sistema não respondeu."
                    _log_err(caminho_log, id_cota, "NAO_BAIXADO (erro mapeado)", _obs_nb)
                    # Grava todas as cotas do lote em UMA conexao (evita timeout por N conexoes)
                    finalizar_cotas_lote_resultado(_ids_cliente_atual, "NAO_BAIXADO", _obs_nb, _pp_nb)
                    return _payload(
                        "NAO_BAIXADO", _obs_nb,
                        id_cota=id_cota, retriable=False,
                        caminho_evidencia_falha=_pp_nb,
                        cotas_nao_selecionadas=_nao_selecionadas_info,
                    )
                return _payload(
                    "FALHA",
                    "Erro de carregamento no AVAPRO. "
                    "O boleto não pôde ser emitido mesmo após 2 tentativas.",
                    id_cota=id_cota, retriable=True,
                    caminho_evidencia_falha=_ultimo_pp_modal,
                    cotas_nao_selecionadas=_nao_selecionadas_info,
                )
            # Ainda tem tentativa: continua o loop (volta pra Meus Clientes no topo)

        if _modal_result is None or _modal_result.get("erro"):
            pp = _print_falha(page, pasta_falha, "Modal_Parcelas_Falhou")
            obs = "Erro de carregamento no AVAPRO após 2 tentativas. O boleto não pôde ser emitido."
            _log_err(caminho_log, id_cota, obs, "")
            return _payload(
                "FALHA", obs,
                id_cota=id_cota, retriable=True, caminho_evidencia_falha=pp,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        _log(
            caminho_log, id_cota, "Modal de parcelas processado",
            f"total_selecionadas={_modal_result['total_selecionadas']} "
            f"adiantados_modal={len(_modal_result['adiantados_modal'])} "
            f"por_cota={_modal_result['por_cota']}",
        )

        # --- Caso especial: "Nenhuma parcela disponivel para pagamento na data selecionada" ---
        # Significa que o cliente ja pagou / esta adiantado.
        # Tira print na pasta ADIANTADOS e retorna sem tentar emitir.
        if _modal_result.get("nenhuma_parcela_disponivel"):
            _log(
                caminho_log, id_cota,
                "Modal: Nenhuma parcela disponivel — tratando como ADIANTADO",
                f"grupo={grupo} cota={cota} cliente={nome_cliente!r}",
            )
            try:
                _pasta_ad_np = pasta_adiantado_cota(caminho_base, nome_cliente, grupo, cota)
                _pp_ad_np = _print_adiantado(page, _pasta_ad_np, "Adiantado_Sem_Parcelas_Disponiveis")
            except Exception:
                _pp_ad_np = _print_falha(page, pasta_falha, "Adiantado_Sem_Parcelas_Disponiveis")
            return _payload(
                "ADIANTADO",
                "Nenhuma parcela disponivel para pagamento (cliente adiantado)",
                id_cota=id_cota,
                caminho_evidencia_falha=_pp_ad_np,
            )

        # --- Trata adiantados detectados pelo modal (mes ref nao encontrado) ---
        # Cotas cujo mes ref nao apareceu no card do modal = ja pagaram essa parcela.
        # Print salvo AGORA (modal ainda aberto) em ADIANTADOS/{nome}_{gc}/.
        fins_adiantados_modal: List[Dict[str, Any]] = []
        _adiantados_modal_set = set()
        for (g_ad, c_ad) in _modal_result.get("adiantados_modal", []):
            _adiantados_modal_set.add((g_ad, c_ad))
            # Identifica a selecionada correspondente
            _s_ad = next(
                (s for s in selecionadas
                 if s["grupo"] == g_ad and s["cota"] == c_ad),
                None,
            )
            if not _s_ad:
                continue
            try:
                pasta_ad = pasta_adiantado_cota(
                    caminho_base, nome_cliente, g_ad, c_ad
                )
                _pp_ad = _print_adiantado(
                    page, pasta_ad, "Adiantado_Parcela_Ja_Paga"
                )
            except Exception:
                _pp_ad = _print_falha(
                    page, pasta_falha,
                    f"Adiantado_{re.sub(r'[^0-9]','',str(g_ad))}_{re.sub(r'[^0-9]','',str(c_ad))}",
                )
            # Observacao especifica do modal (ex: 'Nenhuma parcela disponível'
            # ou 'Valor a pagar negativo no modal: -R$ 337,30'), se houver.
            obs_ad = (
                (_modal_result.get("obs_adiantados", {}) or {}).get((g_ad, c_ad))
                or "Todas as parcelas foram pagas (adiantado)"
            )
            fins_adiantados_modal.append(
                _fin(_s_ad["id_cota"], "ADIANTADO", obs_ad,
                     caminho_evidencia=_pp_ad)
            )
            _log(
                caminho_log, id_cota,
                "Cota adiantada (modal)",
                f"grupo={g_ad} cota={c_ad} obs={obs_ad!r} print={_pp_ad or '-'}",
            )

        # Remove cotas adiantadas do modal da lista de selecionadas a emitir
        selecionadas_para_emitir = [
            s for s in selecionadas
            if (s["grupo"], s["cota"]) not in _adiantados_modal_set
        ]

        # Se TODAS as cotas sao adiantadas (modal) → retorna sem precisar de
        # Continuar ou PDF
        if not selecionadas_para_emitir:
            # Coleta a observacao da cota primaria (usa a especifica do modal
            # se existir — ex: valor negativo / nenhuma parcela disponivel)
            obs_db_ad = (
                (_modal_result.get("obs_adiantados", {}) or {}).get((grupo, cota))
                or "Todas as parcelas foram pagas (adiantado)"
            )
            _pp_primary_ad = next(
                (f.get("caminho_evidencia") for f in fins_adiantados_modal
                 if any(s["id_cota"] == f["id_cota"] and s["id_cota"] == id_cota
                        for s in selecionadas)),
                next(
                    (f.get("caminho_evidencia") for f in fins_adiantados_modal),
                    None,
                ),
            )
            _log(
                caminho_log, id_cota,
                "Todas as cotas adiantadas (mes ref nao encontrado) — sem emissao",
                f"qtd={len(fins_adiantados_modal)}",
            )
            # Garante que a cota primaria esta nas finalizacoes
            if id_cota not in {f["id_cota"] for f in fins_adiantados_modal}:
                fins_adiantados_modal.append(
                    _fin(id_cota, "ADIANTADO", obs_db_ad,
                         caminho_evidencia=_pp_primary_ad)
                )
            return _payload(
                "ADIANTADO", obs_db_ad, id_cota=id_cota,
                caminho_evidencia_falha=_pp_primary_ad,
                houve_unificacao=False,
                cotas_distintas=[[s["grupo"], s["cota"]] for s in selecionadas],
                finalizacoes=fins_adiantados_modal,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # --- Verifica footer do modal antes de clicar "Gerar boleto" ---
        # Le o valor total exibido no footer (ex: R$ 352,21 ou -R$ 0,10).
        # Regras:
        #   - Negativo ou entre R$ 0,01 e R$ 49,99 → ADIANTADO (cliente sem debito real)
        #   - >= R$ 50,00 → avanca e clica "Gerar boleto"
        _VALOR_MINIMO_FOOTER = 50.0
        _valor_footer = avapro.ler_valor_total_footer_modal(page)
        _log(
            caminho_log, id_cota,
            "Footer modal parcelas lido",
            f"valor_footer={_valor_footer}",
        )
        if _valor_footer is not None and _valor_footer < _VALOR_MINIMO_FOOTER:
            # Valor insuficiente: negativo (credito) ou abaixo do minimo
            _pasta_ad_footer = pasta_adiantado_cota(caminho_base, nome_cliente, grupo, cota)
            _pp_footer = _print_adiantado(page, _pasta_ad_footer, "Adiantado_Footer_Valor")
            _obs_footer = (
                f"Valor total no footer abaixo do mínimo para emissão "
                f"(R$ {_valor_footer:.2f}) — cliente adiantado ou sem débito real."
            )
            _log(caminho_log, id_cota, "Footer abaixo do minimo — ADIANTADO", _obs_footer)
            _stderr(f"[WORKER] {_obs_footer}")
            _fins_footer: list = list(fins_adiantados_modal)
            for _s_ft in selecionadas_para_emitir:
                _fins_footer.append(
                    _fin(_s_ft["id_cota"], "ADIANTADO", _obs_footer,
                         caminho_evidencia=_pp_footer)
                )
            if id_cota not in {f["id_cota"] for f in _fins_footer}:
                _fins_footer.append(
                    _fin(id_cota, "ADIANTADO", _obs_footer,
                         caminho_evidencia=_pp_footer)
                )
            return _payload(
                "ADIANTADO", _obs_footer,
                id_cota=id_cota,
                retriable=False,
                caminho_evidencia_falha=_pp_footer,
                finalizacoes=_fins_footer,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # --- Clica "Continuar" no modal → tela de resumo → "Baixar" → download ---
        try:
            _registrar_passo(id_cota, "clicando em 'Gerar boleto' (confirmar parcelas do modal)")
            avapro.clicar_continuar_modal_parcelas(page)
            _log_tempo(caminho_log, id_cota, "continuar_clicado", t0)
            _log(caminho_log, id_cota, "Continuar clicado no modal de parcelas", "")
        except Exception as e:
            pp = _print_falha(page, pasta_falha, "Erro_Continuar_Emissao")
            obs_falha = "Erro ao confirmar as parcelas no AVAPRO. O boleto não foi gerado."
            _log_err(caminho_log, id_cota, "Continuar falhou", obs_falha)
            return _payload(
                "FALHA", obs_falha,
                id_cota=id_cota, retriable=True, caminho_evidencia_falha=pp,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # --- Modal "Pagamento": verifica data de vencimento e clica Baixar ---
        #
        # Apos "Gerar boleto", o AVAPRO exibe o modal "Pagamento" com:
        #   - "Pagamento via Boleto"
        #   - "Data de vencimento: DD/MM/YYYY"  <-- deve ser igual ao vencimento do DB
        #   - "Valor a pagar agora: R$ X"       <-- adicionado na observacao do resultado
        #
        # Se a data exibida nao bate com o vencimento calculado pelo _calcular_vencimento,
        # tira screenshot nomeado, fecha o modal e re-emite (ate MAX_TENT_VENC vezes).
        # Na ultima tentativa sem sucesso retorna FALHA com evidencia clara.
        # -----------------------------------------------------------------------
        _venc_esperado_dt = _modal_result.get("vencimento_esperado_dt")
        _valor_pagar_str: Optional[str] = None
        MAX_TENT_VENC = 3
        _pp_venc_inc: Optional[str] = None

        for _tent_venc in range(1, MAX_TENT_VENC + 1):
            # Na primeira tentativa, o modal ja esta aberto (viemos de Gerar boleto).
            # Nas demais, o modal foi fechado e Emitir boleto + parcelas ja foram re-feitos
            # no bloco de retry abaixo.

            # Le dados do modal "Pagamento"
            _registrar_passo(id_cota, "baixando o boleto (tela de pagamento / download do PDF)")
            _t_antes_pagamento = time.time()
            _dados_pag = avapro.ler_dados_modal_pagamento(page, timeout_ms=5000)
            _log_tempo(caminho_log, id_cota, "modal_pagamento_lido", t0)
            _log(caminho_log, id_cota, "[TEMPO] modal_pagamento_espera",
                 f"{round(time.time() - _t_antes_pagamento, 2)}s")
            _valor_pagar_str = _dados_pag.get("valor_str") or _valor_pagar_str
            _venc_modal_dt   = _dados_pag.get("vencimento_dt")
            _venc_modal_str  = _dados_pag.get("vencimento_str") or ""

            # --- Validacao: "Valor a pagar" deve ser > 0 ---
            # Negativo = cliente com credito = ADIANTADO.
            # Zero     = sem valor emitivel = NAO_BAIXADO.
            if _valor_pagar_str:
                try:
                    _vstr = str(_valor_pagar_str)
                    _negativo = "-" in _vstr
                    _valor_num_check = float(
                        re.sub(r"[^\d,]", "", _vstr).replace(",", ".")
                    )
                    if _negativo:
                        _valor_num_check = -_valor_num_check
                except Exception:
                    _valor_num_check = None

                if _valor_num_check is not None and _valor_num_check < 0:
                    # Valor negativo = credito no AVAPRO → ADIANTADO
                    _pasta_ad_neg = pasta_adiantado_cota(caminho_base, nome_cliente, grupo, cota)
                    _pp_ad_neg = _print_adiantado(page, _pasta_ad_neg,
                                                  f"Valor_Negativo_T{_tent_venc}")
                    _obs_ad_neg = (f"Valor a pagar negativo no AVAPRO: {_valor_pagar_str} "
                                   f"— cliente com crédito (adiantado).")
                    log_info(caminho_log, "PROCESSAMENTO", id_cota,
                             "Valor negativo — ADIANTADO",
                             f"valor={_valor_pagar_str!r} print={_pp_ad_neg or '-'}")
                    _stderr(f"[WORKER] {_obs_ad_neg}")
                    _fins_ad_neg: List[Dict[str, Any]] = list(fins_adiantados_modal)
                    for _s_ad_neg in selecionadas_para_emitir:
                        _fins_ad_neg.append(
                            _fin(_s_ad_neg["id_cota"], "ADIANTADO", _obs_ad_neg,
                                 caminho_evidencia=_pp_ad_neg)
                        )
                    if id_cota not in {f["id_cota"] for f in _fins_ad_neg}:
                        _fins_ad_neg.append(
                            _fin(id_cota, "ADIANTADO", _obs_ad_neg,
                                 caminho_evidencia=_pp_ad_neg)
                        )
                    return _payload(
                        "ADIANTADO", _obs_ad_neg,
                        id_cota=id_cota,
                        retriable=False,
                        caminho_evidencia_falha=_pp_ad_neg,
                        finalizacoes=_fins_ad_neg,
                        cotas_nao_selecionadas=_nao_selecionadas_info,
                    )

                if _valor_num_check is not None and _valor_num_check == 0:
                    # Zero = sem valor emitivel → NAO_BAIXADO
                    _pp_valor_zero = _print_falha(
                        page, pasta_nao_baixado,
                        f"Valor_Pagar_Zero_T{_tent_venc}",
                    )
                    _obs_val = f"Valor a pagar zero no AVAPRO: {_valor_pagar_str} — boleto não emitido."
                    _log_err(caminho_log, id_cota,
                             "Valor a pagar zero — NAO_BAIXADO definitivo",
                             f"valor={_valor_pagar_str!r} print={_pp_valor_zero or '-'}")
                    _stderr(f"[WORKER] {_obs_val}")
                    _fins_val_neg: List[Dict[str, Any]] = list(fins_adiantados_modal)
                    for _s_vn in selecionadas_para_emitir:
                        _fins_val_neg.append(
                            _fin(
                                _s_vn["id_cota"], "NAO_BAIXADO", _obs_val,
                                caminho_evidencia=_pp_valor_zero,
                            )
                        )
                    if id_cota not in {f["id_cota"] for f in _fins_val_neg}:
                        _fins_val_neg.append(
                            _fin(id_cota, "NAO_BAIXADO", _obs_val,
                                 caminho_evidencia=_pp_valor_zero)
                        )
                    return _payload(
                        "NAO_BAIXADO", _obs_val,
                        id_cota=id_cota,
                        retriable=False,
                        caminho_evidencia_falha=_pp_valor_zero,
                        finalizacoes=_fins_val_neg,
                        cotas_nao_selecionadas=_nao_selecionadas_info,
                    )

                _VALOR_MINIMO_BOLETO = 50.0
                if _valor_num_check is not None and 0 < _valor_num_check < _VALOR_MINIMO_BOLETO:
                    # Valor positivo mas abaixo do minimo → NAO_BAIXADO
                    _pp_val_min = _print_falha(
                        page, pasta_nao_baixado,
                        f"Valor_Abaixo_Minimo_T{_tent_venc}",
                    )
                    _obs_val_min = (
                        f"Valor a pagar abaixo do mínimo (R$ {_VALOR_MINIMO_BOLETO:.2f}): "
                        f"{_valor_pagar_str} — boleto não emitido."
                    )
                    _log_err(caminho_log, id_cota,
                             "Valor abaixo do mínimo — NAO_BAIXADO definitivo",
                             f"valor={_valor_pagar_str!r} minimo={_VALOR_MINIMO_BOLETO} "
                             f"print={_pp_val_min or '-'}")
                    _stderr(f"[WORKER] {_obs_val_min}")
                    _fins_val_min: List[Dict[str, Any]] = list(fins_adiantados_modal)
                    for _s_vm in selecionadas_para_emitir:
                        _fins_val_min.append(
                            _fin(
                                _s_vm["id_cota"], "NAO_BAIXADO", _obs_val_min,
                                caminho_evidencia=_pp_val_min,
                            )
                        )
                    if id_cota not in {f["id_cota"] for f in _fins_val_min}:
                        _fins_val_min.append(
                            _fin(id_cota, "NAO_BAIXADO", _obs_val_min,
                                 caminho_evidencia=_pp_val_min)
                        )
                    return _payload(
                        "NAO_BAIXADO", _obs_val_min,
                        id_cota=id_cota,
                        retriable=False,
                        caminho_evidencia_falha=_pp_val_min,
                        finalizacoes=_fins_val_min,
                        cotas_nao_selecionadas=_nao_selecionadas_info,
                    )

            # Verifica se a data bate (ignora hora — compara apenas data)
            _data_correta = True
            if _venc_esperado_dt is not None and _venc_modal_dt is not None:
                _d_esp = _venc_esperado_dt.date() if hasattr(_venc_esperado_dt, "date") else _venc_esperado_dt
                _d_mod = _venc_modal_dt.date()   if hasattr(_venc_modal_dt,   "date") else _venc_modal_dt
                _data_correta = (_d_esp == _d_mod)

            if _data_correta:
                # Tudo certo — clica Baixar
                _log(
                    caminho_log, id_cota,
                    f"Modal Pagamento OK (tentativa {_tent_venc})",
                    f"vencimento={_venc_modal_str!r} valor={_valor_pagar_str!r}",
                )
                try:
                    _t_antes_baixar = time.time()
                    clicou_baixar = avapro.aguardar_e_clicar_baixar_resumo(page, timeout_ms=10000)
                    _log_tempo(caminho_log, id_cota, "baixar_clicado", t0)
                    _log(caminho_log, id_cota, "[TEMPO] baixar_espera",
                         f"{round(time.time() - _t_antes_baixar, 2)}s")
                    if clicou_baixar:
                        _log(caminho_log, id_cota, "Baixar clicado no modal Pagamento",
                             f"valor={_valor_pagar_str!r}")
                    else:
                        _log(caminho_log, id_cota,
                             "Botao Baixar nao encontrado — aguardando download direto", "")
                except Exception as _e_bx:
                    _log_err(caminho_log, id_cota, "Falha ao clicar Baixar", f"{_e_bx}")
                break  # sai do loop de verificacao de data

            # ---- Data incorreta ----
            _esp_fmt = (
                _venc_esperado_dt.strftime("%d/%m/%Y")
                if _venc_esperado_dt else "desconhecida"
            )
            _rec_fmt = (
                _venc_modal_dt.strftime("%d/%m/%Y")
                if _venc_modal_dt else "nenhuma"
            )
            _obs_venc_inc = (
                f"Vencimento incorreto no boleto (t{_tent_venc}/{MAX_TENT_VENC}): "
                f"esperado={_esp_fmt} recebido={_rec_fmt} "
                f"cliente={nome_cliente!r}"
            )
            _log_err(caminho_log, id_cota, "Vencimento incorreto no modal Pagamento",
                     _obs_venc_inc)
            _stderr(f"[WORKER] {_obs_venc_inc}")

            # Screenshot com nome legivel para o operador
            _nome_print_vi = (
                f"Vencimento_Incorreto"
                f"_Esp{_esp_fmt.replace('/','')}"
                f"_Rec{_rec_fmt.replace('/','')}"
                f"_{re.sub(r'[^a-zA-Z0-9]', '_', nome_cliente)[:25]}"
                f"_T{_tent_venc}"
            )
            _pp_venc_inc = _print_falha(page, pasta_falha, _nome_print_vi)
            _log(caminho_log, id_cota, "Evidencia vencimento incorreto",
                 f"print={_pp_venc_inc or '-'}")

            if _tent_venc >= MAX_TENT_VENC:
                # Esgotou tentativas — FALHA
                return _payload(
                    "FALHA",
                    f"Vencimento incorreto após {MAX_TENT_VENC} tentativas — esperado {_esp_fmt}, recebido {_rec_fmt}.",
                    id_cota=id_cota,
                    retriable=False,
                    caminho_evidencia_falha=_pp_venc_inc,
                    cotas_nao_selecionadas=_nao_selecionadas_info,
                )

            # Fecha o modal "Pagamento" e re-emite
            _log(caminho_log, id_cota,
                 f"Fechando modal Pagamento e re-emitindo (t{_tent_venc})", "")
            avapro.fechar_modal_pagamento(page)
            page.wait_for_timeout(2000)

            # Re-clica Emitir boleto
            try:
                avapro.clicar_baixar_documentos_emitir_boleto(
                    page,
                    log_fn=lambda acao, detalhe="": _log(caminho_log, id_cota, acao, detalhe),
                )
            except Exception as _e_rei:
                _log_err(caminho_log, id_cota,
                         f"Re-clique Emitir boleto falhou (t{_tent_venc})", str(_e_rei))

            # Re-seleciona parcelas
            try:
                _modal_result2 = avapro.selecionar_parcelas_no_modal(
                    page, _data_ref_do_lote(ctx),
                    modalidade=ctx.get("modalidade", "IMOVEL"),
                )
                # Atualiza vencimento esperado se o novo modal recalculou
                _venc2 = _modal_result2.get("vencimento_esperado_dt")
                if _venc2 is not None:
                    _venc_esperado_dt = _venc2
                if not _modal_result2.get("pode_continuar"):
                    _log_err(caminho_log, id_cota,
                             f"Re-selecao de parcelas nao selecionou nada (t{_tent_venc})",
                             str(_modal_result2.get("erro")))
                    break
            except Exception as _e_rs:
                _log_err(caminho_log, id_cota,
                         f"Re-selecao de parcelas falhou (t{_tent_venc})", str(_e_rs))
                break

            # Re-clica Gerar boleto
            try:
                avapro.clicar_continuar_modal_parcelas(page)
            except Exception as _e_rc:
                _log_err(caminho_log, id_cota,
                         f"Re-clique Gerar boleto falhou (t{_tent_venc})", str(_e_rc))
                break
            # Loop volta para ler o modal Pagamento novamente

        # --- Determina atraso e nome do arquivo com base no modal ---
        # atraso da cota primaria (ou da primeira selecionada para emitir)
        _por_cota_modal = _modal_result.get("por_cota", {})
        _atraso_primaria = None
        for s in selecionadas_para_emitir:
            _chave_s = (s["grupo"], s["cota"])
            _info_modal = _por_cota_modal.get(_chave_s, {})
            s["atraso"] = _info_modal.get("parcelas_atraso") or 0
            if s["id_cota"] == id_cota:
                _atraso_primaria = s["atraso"]
        if _atraso_primaria is None and selecionadas_para_emitir:
            _atraso_primaria = selecionadas_para_emitir[0]["atraso"]

        unificado = len(selecionadas_para_emitir) > 1

        # --- nome do arquivo (padrao rpa_gerar_boleto: meses por extenso) ---
        _meses_modal = _modal_result.get("meses_parcelas") or []
        if unificado:
            nome_pdf = nome_arquivo_boleto_unificado(nome_cliente)
        else:
            s0 = selecionadas_para_emitir[0]
            nome_pdf = nome_arquivo_boleto(
                s0["grupo"], s0["cota"], nome_cliente, _meses_modal
            )

        # Subpasta do consultor dentro de Boletos/
        destino_dir = pasta_boletos(caminho_base, nome_consultor)
        _destino_base = destino_dir / nome_pdf
        destino_arq = destino_sem_colisao(destino_dir, nome_pdf)
        if destino_arq != _destino_base:
            # Colisao: o arquivo base ja existe (run anterior baixou mas nao
            # finalizou no DB). Sobrescreve em vez de criar " (2).pdf".
            _log(
                caminho_log, id_cota,
                "Boleto base ja existe — sobrescrevendo (evita duplicado '(2)')",
                f"arquivo={_destino_base.name}",
            )
            destino_arq = _destino_base
        destino_arq.parent.mkdir(parents=True, exist_ok=True)

        # --- Loop de monitoramento: aguarda PDF ou toast ---
        _pdf_baixado: Optional[Path] = None
        _dl_candidato: Optional[Path] = None
        _dl_candidato_size: int = -1
        _dl_estavel_count: int = 0
        _DL_CICLOS_ESTAVEIS = 2

        # Helper: normaliza acentos para comparacao case-insensitive sem acento
        def _norm_toast(t: str) -> str:
            import unicodedata as _ud
            return "".join(
                c for c in _ud.normalize("NFKD", str(t).lower())
                if not _ud.combining(c)
            )

        adiantado_info: Dict[str, Optional[str]] = {"texto": None, "print": None}
        _toast_nao_baixado: Optional[str] = None   # erro definitivo (nao retriable)
        _toast_adiantado_loc = page.locator(
            "text=/n[aã]o existem cobran[çc]as/i"
        ).first
        _toast_geral_sel = (
            "[data-sonner-toast], "
            "li[role='status'], li[role='alert'], "
            "[data-radix-toast-root], "
            "[role='status']:not(body), [role='alert']:not(body)"
        )

        _toasts_vistos: List[str] = []
        _print_ultimo_toast: Optional[str] = None
        _toast_erro_detectado: Optional[str] = None

        _log(
            caminho_log, id_cota, "Aguardando PDF em Downloads",
            f"timeout=180s arquivo_esperado={nome_pdf!r}",
        )

        deadline = time.time() + 180
        while time.time() < deadline:
            # 1) PDF novo e estavel na pasta Downloads?
            #
            # Duas estrategias combinadas para nao perder re-downloads:
            #   A) Nome nao estava no snapshot (arquivo genuinamente novo)
            #   B) Nome estava no snapshot MAS mtime >= _snapshot_time
            #      (mesmo nome, sobrescrito — AVAPRO reusa o nome do cliente)
            if _pdf_baixado is None:
                try:
                    _dl_dir = avapro.downloads_dir()
                    _novos = []
                    for _p in _dl_dir.glob("*.pdf"):
                        try:
                            _p_str = str(_p)
                            _p_mtime = _p.stat().st_mtime
                            if (
                                _p_str not in _snapshot_dl           # arquivo novo
                                or _p_mtime >= _snapshot_time - 1.0  # ou re-download
                            ):
                                _novos.append(_p)
                        except OSError:
                            continue
                    if _novos:
                        _novos.sort(
                            key=lambda p: p.stat().st_mtime, reverse=True
                        )
                        _cand = _novos[0]
                        try:
                            _sz = _cand.stat().st_size
                            if (
                                _sz > 0
                                and _cand == _dl_candidato
                                and _sz == _dl_candidato_size
                            ):
                                _dl_estavel_count += 1
                                if _dl_estavel_count >= _DL_CICLOS_ESTAVEIS:
                                    _log_tempo(caminho_log, id_cota, "pdf_chegou_em_downloads", t0)
                                    _log(
                                        caminho_log, id_cota,
                                        "PDF detectado em Downloads — movendo",
                                        f"origem={_cand.name} destino={destino_arq.name} "
                                        f"bytes={_sz}",
                                    )
                                    shutil.move(str(_cand), str(destino_arq))
                                    _log_tempo(caminho_log, id_cota, "pdf_movido_para_boletos", t0)
                                    _pdf_baixado = destino_arq
                            else:
                                _log(
                                    caminho_log, id_cota,
                                    "PDF candidato detectado (aguardando estabilizar)",
                                    f"arquivo={_cand.name} bytes={_sz} "
                                    f"era_snapshot={str(_cand) in _snapshot_dl}",
                                )
                                _dl_candidato = _cand
                                _dl_candidato_size = _sz
                                _dl_estavel_count = 0
                        except OSError:
                            _dl_candidato = None
                            _dl_candidato_size = -1
                            _dl_estavel_count = 0
                except Exception:
                    pass

            if _pdf_baixado:
                break

            # 2) Toast "sem cobrancas" (adiantado via toast)?
            # Diferente do adiantado_modal, este salva em verificar_adiantados/
            if adiantado_info["texto"] is None:
                try:
                    if (
                        _toast_adiantado_loc.count() > 0
                        and _toast_adiantado_loc.is_visible()
                    ):
                        try:
                            adiantado_info["texto"] = (
                                (_toast_adiantado_loc.inner_text() or "").strip()
                                or "Nao existem cobrancas disponiveis para a cota"
                            )
                        except Exception:
                            adiantado_info["texto"] = (
                                "Nao existem cobrancas disponiveis para a cota"
                            )
                        _texto_safe = re.sub(
                            r'[\\/:*?"<>|\s]+', "_",
                            adiantado_info["texto"],
                        )[:40]
                        print(
                            f"[TOAST] Foi identificado o toast: {adiantado_info['texto']}",
                            file=sys.stderr, flush=True,
                        )
                        _log(
                            caminho_log, id_cota, "Toast identificado",
                            f"Foi identificado o toast: {adiantado_info['texto']}",
                        )
                        # Salva em verificar_adiantados/ (nao em pasta da cota)
                        try:
                            pasta_vad = pasta_verificar_adiantados(caminho_base)
                            adiantado_info["print"] = _print_adiantado(
                                page, pasta_vad, f"Aviso_Sistema_{_texto_safe}",
                            )
                        except Exception:
                            adiantado_info["print"] = _print_falha(
                                page, pasta_falha, f"Aviso_Sistema_{_texto_safe}",
                            )
                        break
                except Exception:
                    pass

            # 3) Toasts genericos
            try:
                _todos_toasts = page.locator(_toast_geral_sel).all()
                for _t_el in _todos_toasts:
                    try:
                        if not _t_el.is_visible():
                            continue
                        _txt = (_t_el.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not _txt or _txt in _toasts_vistos:
                        continue
                    _toasts_vistos.append(_txt)

                    # Toast de SUCESSO: apenas loga, nao tira print e nao
                    # interrompe o loop — o download pode ter iniciado junto.
                    _e_sucesso = bool(re.search(
                        r"sucesso|success|gerado|emitido|realizado",
                        _txt, re.IGNORECASE,
                    ))
                    _txt_norm = _norm_toast(_txt)
                    _txt_safe = re.sub(r'[\\/:*?"<>|\s]+', "_", _txt)[:40]

                    # Toast de SUCESSO: apenas loga, nao interrompe o loop
                    _e_sucesso = bool(re.search(
                        r"sucesso|success|gerado|emitido|realizado",
                        _txt_norm,
                    ))
                    if _e_sucesso:
                        print(f"[TOAST-SUCESSO] {_txt}", file=sys.stderr, flush=True)
                        _log(caminho_log, id_cota, "Toast de sucesso (ignorado)",
                             f"toast={_txt!r}")
                        continue  # Nao salva print, nao interrompe loop

                    # Toast NAO_BAIXADO definitivo: problema da cota em si,
                    # nao adianta retentativa (ex: versao diferente de 0,
                    # boleto unificado nao permitido, cota bloqueada).
                    _e_nao_baixado = bool(re.search(
                        r"nao foi possivel|versao diferente|versão diferente"
                        r"|nao e possivel|nao é possível"
                        r"|cota.*bloqueada|bloqueada.*cota",
                        _txt_norm,
                    ))
                    if _e_nao_baixado:
                        _print_ultimo_toast = _print_falha(
                            page, pasta_nao_baixado, f"Boleto_Nao_Emitido_{_txt_safe}",
                        )
                        _toast_nao_baixado = _txt
                        print(f"[TOAST-NAO_BAIXADO] {_txt}", file=sys.stderr, flush=True)
                        _log_err(
                            caminho_log, id_cota,
                            "Toast NAO_BAIXADO definitivo — saindo sem retentativa",
                            f"toast={_txt!r} print={_print_ultimo_toast or '-'}",
                        )
                        break

                    # Toast de ERRO retriable: problema tecnico/sistema
                    _e_erro = bool(re.search(
                        r"err[oa]|falh[ao]",
                        _txt_norm,
                    ))
                    if _e_erro:
                        _print_ultimo_toast = _print_falha(
                            page, pasta_falha, f"Aviso_Sistema_{_txt_safe}",
                        )
                        _toast_erro_detectado = _txt
                        print(f"[TOAST-ERRO] {_txt}", file=sys.stderr, flush=True)
                        _log_err(
                            caminho_log, id_cota,
                            "Toast de erro — saindo sem esperar 180s",
                            f"toast={_txt!r} print={_print_ultimo_toast or '-'}",
                        )
                        break

                    # Toast desconhecido: qualquer toast nao-sucesso e tratado
                    # como erro retriable — sai imediatamente sem esperar 180s.
                    _print_ultimo_toast = _print_falha(
                        page, pasta_falha, f"Aviso_Sistema_{_txt_safe}",
                    )
                    _toast_erro_detectado = _txt
                    print(f"[TOAST-DESCONHECIDO→ERRO] {_txt}", file=sys.stderr, flush=True)
                    _log_err(
                        caminho_log, id_cota,
                        "Toast desconhecido — tratado como erro, saindo sem esperar 180s",
                        f"toast={_txt!r} print={_print_ultimo_toast or '-'}",
                    )
                    break
            except Exception:
                pass

            if _toast_nao_baixado or _toast_erro_detectado:
                break

            page.wait_for_timeout(200)

        # --- Classifica resultado ---

        # Caso 1: PDF salvo com sucesso → BAIXADO
        if (
            _pdf_baixado is not None
            and _pdf_baixado.exists()
            and _pdf_baixado.stat().st_size > 0
        ):
            _log(
                caminho_log, id_cota, "Boleto baixado com sucesso",
                f"arquivo={destino_arq.name} bytes={destino_arq.stat().st_size} "
                f"cotas={len(selecionadas_para_emitir)} unificado={unificado}",
            )
            # Cai pro bloco de sucesso abaixo

        # Caso 2: toast "sem cobrancas" → ADIANTADO (verificar_adiantados)
        elif adiantado_info["texto"]:
            caminho_print_ad = adiantado_info["print"]
            _log(
                caminho_log, id_cota,
                "Cota adiantada — toast sem cobrancas detectado",
                f"toast={adiantado_info['texto']!r} "
                f"print={caminho_print_ad or '-'}",
            )
            obs_db = "Todas as parcelas foram pagas (adiantado)"
            fins = list(fins_adiantados_modal)  # inclui eventuais adiantados do modal
            for s in selecionadas_para_emitir:
                fins.append(
                    _fin(s["id_cota"], "ADIANTADO", obs_db,
                         caminho_evidencia=caminho_print_ad,
                         parcelas_atraso=s.get("atraso"))
                )
            if id_cota not in {f["id_cota"] for f in fins}:
                fins.append(_fin(id_cota, "ADIANTADO", obs_db,
                                 caminho_evidencia=caminho_print_ad))
            return _payload(
                "ADIANTADO", obs_db, id_cota=id_cota,
                caminho_evidencia_falha=caminho_print_ad,
                parcelas_atraso=next(
                    (s.get("atraso") for s in selecionadas_para_emitir
                     if s["id_cota"] == id_cota),
                    None,
                ),
                houve_unificacao=unificado,
                cotas_distintas=[[s["grupo"], s["cota"]]
                                 for s in selecionadas_para_emitir],
                finalizacoes=fins,
                toasts_capturados=_toasts_vistos,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # Caso 3: toast NAO_BAIXADO definitivo (ex: versão diferente, cota bloqueada)
        elif _toast_nao_baixado:
            _cotas_str = ", ".join(
                f"{s['grupo']}/{s['cota']}" for s in selecionadas_para_emitir
            )
            obs_nb = f"Cota indisponível: {_toast_nao_baixado}"
            _log_err(caminho_log, id_cota, "NAO_BAIXADO por toast definitivo", obs_nb)
            fins_nb: List[Dict[str, Any]] = list(fins_adiantados_modal)
            for s in selecionadas_para_emitir:
                fins_nb.append(
                    _fin(s["id_cota"], "NAO_BAIXADO", obs_nb,
                         caminho_evidencia=_print_ultimo_toast)
                )
            if id_cota not in {f["id_cota"] for f in fins_nb}:
                fins_nb.append(_fin(id_cota, "NAO_BAIXADO", obs_nb,
                                    caminho_evidencia=_print_ultimo_toast))
            return _payload(
                "NAO_BAIXADO", obs_nb, id_cota=id_cota,
                caminho_evidencia_falha=_print_ultimo_toast,
                houve_unificacao=unificado,
                cotas_distintas=[[s["grupo"], s["cota"]]
                                 for s in selecionadas_para_emitir],
                finalizacoes=fins_nb,
                toasts_capturados=_toasts_vistos,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # Caso 4: toast de erro retriable ou timeout → FALHA retriable
        else:
            _cotas_str = ", ".join(
                f"{s['grupo']}/{s['cota']}" for s in selecionadas_para_emitir
            )
            if _toast_erro_detectado:
                pp = _print_ultimo_toast
                obs_falha = f"Aviso do AVAPRO ao emitir boleto: '{_toast_erro_detectado}'"
            else:
                pp = _print_falha(page, pasta_falha, "Erro_Emissao_Sem_Resposta")
                obs_falha = "Boleto não gerado — AVAPRO não respondeu após 180s (possível travamento)."
            _log_err(caminho_log, id_cota, "Falha ao emitir/baixar boleto",
                     obs_falha)
            return _payload(
                "FALHA", obs_falha,
                id_cota=id_cota, retriable=True, caminho_evidencia_falha=pp,
                houve_unificacao=unificado,
                cotas_distintas=[[s["grupo"], s["cota"]]
                                 for s in selecionadas_para_emitir],
                toasts_capturados=_toasts_vistos,
                cotas_nao_selecionadas=_nao_selecionadas_info,
            )

        # (Apenas BAIXADO chega aqui; ADIANTADO e FALHA ja retornaram acima.)

        # --- Navega de volta para /meus-clientes apos o download ---
        # Apos clicar "Baixar", o AVAPRO pode manter navegacao pendente no
        # browser. Se o proximo worker conectar com navegacao em andamento,
        # o Playwright espera ela concluir antes de executar o proximo goto —
        # causando pausa de decenas de segundos desnecessariamente.
        # Navegando aqui, o browser fica em estado limpo e o proximo worker
        # encontra /meus-clientes ja carregado.
        try:
            garantir_url_meus_clientes(page)
        except Exception:
            pass

        # --- Sucesso: finaliza TODAS as cotas ---
        cotas_distintas = [[s["grupo"], s["cota"]] for s in selecionadas_para_emitir]
        sel_ids = {s["id_cota"] for s in selecionadas_para_emitir}

        origem_grupo = re.sub(r"\D", "", str(grupo or "")).zfill(6)
        origem_cota  = re.sub(r"\D", "", str(cota  or "")).zfill(4)

        fins: List[Dict[str, Any]] = list(fins_adiantados_modal)

        # Cotas ignoradas (modalidade errada OU badge Excluído): NAO_BAIXADO
        for ig in ignoradas_modalidade_lote:
            if ig["id_cota"] not in sel_ids:
                fins.append(_fin(
                    ig["id_cota"], "NAO_BAIXADO", ig["obs"],
                    caminho_evidencia=ig.get("print"),
                ))

        # Cotas emitidas: BAIXADO com atraso do modal.
        # Se a cota tinha 'Detalhes da cota nao encontrado' no Mostrar mais,
        # adiciona '| Detalhes da cota nao encontrado' na observacao.
        for s in selecionadas_para_emitir:
            eh_origem = (s["id_cota"] == id_cota)
            _pu_s = (lote_map.get((
                re.sub(r"\D", "", str(s["grupo"] or "")).zfill(6),
                re.sub(r"\D", "", str(s["cota"]  or "")).zfill(4),
            ), {}).get("pode_unificar") or "")
            _nao_unif = _pu_s.strip().upper() in ("NÃO", "NAO", "N")
            obs = _obs_baixado(
                unificado, eh_origem, origem_grupo, origem_cota, s.get("atraso"),
                pode_unificar_nao=_nao_unif,
            )
            if s.get("detalhes_nao_encontrado"):
                obs = f"{obs} | Detalhes da cota nao encontrado"
            if _valor_pagar_str:
                obs = f"{obs} | Valor a pagar {_valor_pagar_str}"
            fins.append(_fin(
                s["id_cota"], "BAIXADO", obs,
                caminho_boleto=str(destino_arq), parcelas_atraso=s.get("atraso"),
            ))

        # --- Cotas ja BAIXADAS que o AVAPRO reexibiu no modal de unificacao ---
        # NAO tiveram parcela selecionada (dupla emissao evitada). Registra a
        # ocorrencia com observacao especifica referenciando o boleto anterior
        # (X) e o boleto atual (Y = destino_arq).
        for _chave_bloq in (_modal_result.get("cotas_bloqueadas_baixadas") or []):
            _info_bloq = cotas_baixadas_bloqueadas.get(_chave_bloq) or {}
            _id_bloq = _info_bloq.get("id_cota")
            _boleto_x = _info_bloq.get("caminho_boleto") or "(caminho nao registrado)"
            _obs_bloq = (
                f"Cota foi baixada no boleto de caminho {_boleto_x} e apareceu "
                f"para unificar novamente na hora de unificar as parcelas no "
                f"boleto {destino_arq}; parcela NAO reselecionada (dupla emissao evitada)."
            )
            if _id_bloq:
                try:
                    marcar_cota_ja_baixada_reunificada(_id_bloq, _obs_bloq)
                except Exception as _e_bloq:
                    _log_err(
                        caminho_log, id_cota,
                        "Falha ao registrar cota ja baixada reexibida",
                        f"chave={_chave_bloq} id_cota={_id_bloq}: {_e_bloq}",
                    )
            _log(
                caminho_log, id_cota,
                "Cota ja BAIXADA bloqueada no modal (dupla emissao evitada)",
                f"chave={_chave_bloq} boleto_anterior={_boleto_x} boleto_atual={destino_arq}",
            )

        if id_cota not in sel_ids:
            # O PDF foi baixado — a cota primaria foi processada mesmo que
            # id_cota nao esteja em sel_ids (ex: retry com novo id_cota que
            # nao entrou no lote_map por duplicata de grupo/cota).
            # Marcar NAO_BAIXADO aqui seria errado: o boleto existe.
            _log_err(
                caminho_log, id_cota,
                "AVISO: id_cota primario nao encontrado em sel_ids — "
                "marcando BAIXADO pois PDF foi gerado",
                f"id_cota={id_cota} sel_ids={sel_ids} arquivo={destino_arq.name}",
            )
            primary_status = "BAIXADO"
            primary_atraso = _atraso_primaria
            # Garante que a finalizacao BAIXADO para o id_cota primario existe
            if id_cota not in {f["id_cota"] for f in fins}:
                fins.append(_fin(
                    id_cota, "BAIXADO",
                    _obs_baixado(False, True, origem_grupo, origem_cota, primary_atraso),
                    caminho_boleto=str(destino_arq), parcelas_atraso=primary_atraso,
                ))
        else:
            primary_status = "BAIXADO"
            primary_atraso = next(
                (s.get("atraso") for s in selecionadas_para_emitir
                 if s["id_cota"] == id_cota),
                None,
            )

        tipo = "unificado" if unificado else "unico"
        elapsed = time.time() - t0
        obs_primary = (
            f"Boleto {tipo} ({len(selecionadas_para_emitir)} cota(s)) emitido em "
            f"{elapsed:.1f}s: {destino_arq.name}"
        )
        # Valor a pagar lido do modal "Pagamento" (ex: "R$ 341,54")
        if _valor_pagar_str:
            obs_primary = f"{obs_primary} | Valor a pagar {_valor_pagar_str}"
        _log(caminho_log, id_cota, "Cota concluida", obs_primary)
        return _payload(
            primary_status, obs_primary, id_cota=id_cota,
            caminho_boleto=str(destino_arq) if primary_status == "BAIXADO" else None,
            parcelas_atraso=primary_atraso,
            houve_unificacao=unificado, cotas_distintas=cotas_distintas,
            finalizacoes=fins,
            toasts_capturados=_toasts_vistos,
            cotas_nao_selecionadas=_nao_selecionadas_info,
        )


# ============================================================
# Aplicacao das finalizacoes no banco

# ============================================================


def _aplicar_finalizacoes(payload: dict) -> None:
    """
    Grava no banco TODAS as finalizacoes do payload em UMA unica conexao DB.

    Usa aplicar_finalizacoes_lote (1 conexao Aiven para N cotas) em vez de
    N chamadas individuais — elimina latencia de conexao multiplicada por cota.
    """
    fins = list(payload.get("_finalizacoes") or [])

    # Para FALHA definitivo (retriable=False) sem entradas em _finalizacoes,
    # o worker precisa gravar no banco — adiciona como entrada extra.
    status_main = (payload.get("status") or "").upper()
    id_cota_main = payload.get("id_cota")
    if (
        status_main == "FALHA"
        and not payload.get("retriable")
        and id_cota_main is not None
        and id_cota_main not in {f.get("id_cota") for f in fins}
    ):
        fins.append({
            "id_cota": id_cota_main,
            "status": "FALHA",
            "observacao": (payload.get("observacao") or "FALHA sem observacao")[:500],
            "caminho_evidencia": payload.get("caminho_evidencia_falha") or "",
        })

    if not fins:
        return

    try:
        aplicar_finalizacoes_lote(fins)
    except Exception as e:
        _stderr(
            f"[WORKER] _aplicar_finalizacoes: erro ao gravar lote no banco: "
            f"{type(e).__name__}: {e}"
        )


# ============================================================
# Entry point
# ============================================================

def main() -> int:
    if len(sys.argv) < 2:
        _emitir_json(_payload("FALHA", "argv[1] (id_cota) ausente", retriable=False))
        return 1

    try:
        id_cota = int(sys.argv[1])
    except ValueError:
        _emitir_json(_payload(
            "FALHA", f"id_cota invalido: {sys.argv[1]!r}", retriable=False,
        ))
        return 1

    try:
        ctx = _carregar_contexto(id_cota)
    except Exception as e:
        _stderr(traceback.format_exc())
        _emitir_json(_payload(
            "FALHA", f"Erro ao carregar contexto: {type(e).__name__}: {e}",
            id_cota=id_cota, retriable=True,
        ))
        return 1

    try:
        payload = _processar_cota(ctx)
    except Exception as e:
        _stderr(traceback.format_exc())
        _emitir_json(_payload(
            "FALHA", f"Excecao em _processar_cota: {type(e).__name__}: {e}",
            id_cota=id_cota, retriable=True,
        ))
        return 1

    # Grava finalizacoes definitivas no banco.
    # Nao chama para FALHA retriable: a cota deve permanecer PROCESSANDO
    # para o orquestrador reenfileirar.
    status = (payload.get("status") or "").upper()
    if not (status == "FALHA" and payload.get("retriable")):
        try:
            _aplicar_finalizacoes(payload)
        except Exception as e:
            _stderr(
                f"[WORKER] _aplicar_finalizacoes falhou globalmente: "
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            )

    _emitir_json(payload)
    return 0 if status != "FALHA" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        _emitir_json(_payload(
            "FALHA", f"Toplevel: {type(e).__name__}: {e}",
            retriable=True,
        ))
        sys.exit(1)
