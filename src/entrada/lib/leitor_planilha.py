"""
Leitor da planilha do ADM. Adaptado para o schema unificado MOTORS+IMOVEL.

Fluxo:
1) Busca dados do lote no banco (modalidade, link_planilha, nome_aba, modo_reexecucao)
2) Para cada aba aplicavel:
   - le faixa larga (A:Z)
   - encontra o cabecalho
   - filtra linhas com status REEXECUTAR (modo reexec) ou nao bloqueadas (modo normal)
   - em modo normal, atualiza para NAO BAIXADO os itens validos da planilha
3) Insere as cotas na fila usando a funcao SQL inserir_fila_cotas_em_lote
4) Atualiza total_cotas no lote
"""

import os
import sys
import logging
from typing import List, Dict, Optional

from googleapiclient.errors import HttpError

CURRENT_DIR = os.path.dirname(__file__)
ENTRADA_DIR = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.dirname(ENTRADA_DIR)

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if ENTRADA_DIR not in sys.path:
    sys.path.insert(0, ENTRADA_DIR)

from shared.google_auth import criar_servico_sheets
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    inserir_fila_cotas_em_lote,
    atualizar_total_cotas_fila_adm,
    contar_cotas_total,
    listar_chaves_cotas_lote,
)
from shared.log import log_info, log_erro
from shared.notificador import notificar_falha


class DuplicataPlanilha(Exception):
    """Aviso de duplicatas (grupo+cota) ignoradas na planilha do ADM."""
    pass


class ColunaFaltandoPlanilha(Exception):
    """
    Coluna essencial ausente no cabecalho da planilha do ADM
    (GRUPO / COTA / BOLETO / NOME DO CLIENTE) ou cabecalho nao encontrado.
    Deve marcar o lote como FALHA e seguir para o proximo ADM.
    """
    pass
from config.modalidades import BLOQUEADOS

from utils.texto_utils import split_abas, normalizar_status as _normalizar_status_helper
from utils.cabecalho_utils import encontrar_cabecalho
from utils.sheets_utils import (
    extrair_id_planilha,
    ler_range,
    coluna_para_letra,
    atualizar_boleto_em_lote,
    escrever_valores_celulas,
)


log = logging.getLogger(__name__)


def _status_normalizado(texto: str) -> str:
    """
    Normaliza um status pra comparacao:
      - upper case
      - remove acentos (NÃO -> NAO)
      - colapsa espacos multiplos
      - strip
    """
    return _normalizar_status_helper(texto)


def _deve_bloquear(status_norm: str) -> bool:
    return status_norm in BLOQUEADOS


def _esta_nao_baixado(status_norm: str) -> bool:
    return status_norm == "NAO BAIXADO"


def _esta_reexecutar(status_norm: str) -> bool:
    return status_norm == "REEXECUTAR"


def ler_planilhas(id_fila_adm: int, log_txt_path: Optional[str] = None) -> int:
    """
    Le a planilha do ADM associado ao lote e enfileira as cotas
    em tbl_fila_cotas via funcao SQL inserir_fila_cotas_em_lote.

    O modo (normal vs reexecucao) e a modalidade (MOTORS|IMOVEL)
    sao lidos do proprio lote no banco.
    """
    dados_lote = obter_dados_adm_por_fila(id_fila_adm)
    if not dados_lote:
        raise ValueError(f"id_fila_adm nao encontrado: {id_fila_adm}")

    link_planilha = dados_lote["link_planilha"]
    nome_aba_raw = dados_lote["nome_aba"]
    modalidade = dados_lote["modalidade"]
    modo_reexecucao = bool(dados_lote["modo_reexecucao"])

    log_info(
        caminho_log=log_txt_path,
        etapa="LEITOR_PLANILHA",
        id_dado=id_fila_adm,
        acao="Iniciar leitura",
        detalhe=f"modalidade={modalidade} modo_reexecucao={modo_reexecucao}",
    )

    if not nome_aba_raw:
        log_erro(
            caminho_log=log_txt_path,
            etapa="LEITOR_PLANILHA",
            id_dado=id_fila_adm,
            acao="Validar nome_aba",
            detalhe=f"ADM sem nome_aba para modalidade={modalidade}",
        )
        atualizar_total_cotas_fila_adm(id_fila_adm, 0)
        return 0

    abas = split_abas(nome_aba_raw)
    spreadsheet_id = extrair_id_planilha(link_planilha)
    service = criar_servico_sheets()

    cotas_para_inserir: List[Dict] = []

    # Chaves ja existentes no banco para este lote (QUALQUER status).
    # Usado para nao re-inserir cotas ja BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA
    # em caso de retomada apos queda — o NOT EXISTS da SQL function so protege
    # contra PENDENTE/PROCESSANDO, entao sem este filtro boletos duplicados
    # sao gerados toda vez que o RPA e reiniciado no mesmo lote.
    try:
        _chaves_banco: set = listar_chaves_cotas_lote(id_fila_adm)
    except Exception as _e_chaves:
        log_erro(
            caminho_log=log_txt_path,
            etapa="LEITOR_PLANILHA",
            id_dado=id_fila_adm,
            acao="Aviso: nao foi possivel carregar chaves existentes do banco",
            detalhe=f"{type(_e_chaves).__name__}: {_e_chaves} — prosseguindo sem filtro",
        )
        _chaves_banco = set()

    # Dedup local: a chave unica do banco e
    #   (id_fila_adm, nome_aba, grupo, cota,
    #    UPPER(TRIM(nome_cliente)),
    #    COALESCE(NULLIF(UPPER(TRIM(cpf_cnpj)), ''), 'CPF')).
    # Isso permite que a MESMA (grupo, cota) apareca para clientes diferentes
    # OU para o mesmo cliente em "pastas" CPF e CNPJ distintas. Sem incluir
    # cliente+cpf_cnpj aqui, a 2a ocorrencia da mesma (grupo, cota) com
    # cliente diferente seria silenciosamente descartada como duplicata,
    # batendo com o que o NOT EXISTS da function tambem checa.
    #
    # Se a planilha do ADM tiver linhas duplicadas REAIS (operador colou
    # exatamente a mesma cota duas vezes para o mesmo cliente), o batch
    # JSONB vai com duplicatas e o NOT EXISTS nao protege contra elas
    # (so checa contra linhas ja gravadas, nao contra o proprio batch).
    # Resultado: UniqueViolation no INSERT. Evita aqui.
    chaves_vistas: set = set()
    total_duplicadas_planilha = 0
    duplicadas_detalhe: List[Dict] = []

    total_linhas_lidas = 0
    total_invalidas = 0
    total_bloqueadas = 0
    total_filtradas_reexec = 0
    total_enfileiradas = 0
    total_abas_puladas = 0
    invalidas_detalhe: List[Dict] = []  # linhas com campos essenciais faltando
    _ordem_planilha_counter = 0  # sequencia global de insercao (respeita ordem da planilha)

    for aba in abas:
        try:
            log_info(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Ler aba",
                detalhe=f"aba={aba}",
            )

            valores = ler_range(service, spreadsheet_id, f"{aba}!A:Z")

            if not valores:
                total_abas_puladas += 1
                log_erro(
                    caminho_log=log_txt_path,
                    etapa="LEITOR_PLANILHA",
                    id_dado=id_fila_adm,
                    acao="Ler aba",
                    detalhe=f"aba={aba} sem dados",
                )
                continue

            try:
                idx_cabecalho, idx = encontrar_cabecalho(valores)
            except ValueError as e_cab:
                # Coluna essencial ausente (GRUPO/COTA/BOLETO/NOME DO CLIENTE)
                # ou cabecalho nao encontrado. Avisa por email e marca o lote
                # como FALHA (a excecao propaga ate o entrada, que finaliza
                # como FALHA e segue para o proximo ADM).
                msg = (
                    f"Planilha do ADM com coluna essencial faltando no "
                    f"cabecalho da aba '{aba}' (modalidade={modalidade}). "
                    f"O lote sera marcado como FALHA. {e_cab}"
                )
                try:
                    notificar_falha(
                        etapa="LEITOR_PLANILHA",
                        erro=ColunaFaltandoPlanilha(msg),
                        id_fila_adm=id_fila_adm,
                        caminho_log=log_txt_path,
                        script_path=__file__,
                        contexto_extra=(
                            f"aba={aba}\n"
                            f"modalidade={modalidade}\n"
                            f"detalhe={e_cab}\n\n"
                            f"Acao recomendada: corrigir o cabecalho da planilha "
                            f"(colunas obrigatorias: GRUPO, COTA, BOLETO/STATUS, "
                            f"NOME DO CLIENTE) e, se necessario, setar o lote de "
                            f"volta para PENDENTE."
                        ),
                    )
                except Exception as _e_notif:
                    log_erro(
                        caminho_log=log_txt_path,
                        etapa="LEITOR_PLANILHA",
                        id_dado=id_fila_adm,
                        acao="Notificar coluna faltando",
                        detalhe=f"falha ao enviar email: {_e_notif}",
                    )
                raise ColunaFaltandoPlanilha(msg) from e_cab

            cabecalho = valores[idx_cabecalho]

            log_info(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Encontrar cabecalho",
                detalhe=f"aba={aba} linha_cabecalho={idx_cabecalho + 1}",
            )

            if len(valores) <= idx_cabecalho + 1:
                total_abas_puladas += 1
                log_erro(
                    caminho_log=log_txt_path,
                    etapa="LEITOR_PLANILHA",
                    id_dado=id_fila_adm,
                    acao="Ler aba",
                    detalhe=f"aba={aba} sem linhas abaixo do cabecalho",
                )
                continue

            letra_boleto = coluna_para_letra(idx["boleto"])
            letra_obs_boleto = (
                coluna_para_letra(idx["obs_boleto"])
                if idx.get("obs_boleto") is not None
                else None
            )
            linhas_para_atualizar: List[int] = []
            linhas_duplicadas_planilha: List[tuple] = []  # (row_num, valor_boleto)
            linhas_duplicadas_obs: List[tuple] = []       # (row_num, valor_obs)

            for i, r in enumerate(
                valores[idx_cabecalho + 1:], start=idx_cabecalho + 2
            ):
                total_linhas_lidas += 1

                def cell(j: int) -> str:
                    return (r[j] if j < len(r) else "").strip() if r else ""

                nome_cliente = cell(idx["cliente"])
                grupo = cell(idx["grupo"])
                cota = cell(idx["cota"])
                consultor = (
                    cell(idx["consultor"])
                    if idx.get("consultor") is not None
                    else ""
                ).strip() or "Boletos"
                boleto = cell(idx["boleto"])

                pode_unificar = None
                if idx.get("pode_unificar") is not None:
                    pode_unificar_raw = cell(idx["pode_unificar"]).strip()
                    if pode_unificar_raw:
                        _pu = pode_unificar_raw.upper()
                        # Remove acentos para cobrir NÃO/Não/nao/NAO
                        import unicodedata as _ud
                        _pu_norm = "".join(
                            c for c in _ud.normalize("NFKD", _pu)
                            if not _ud.combining(c)
                        )
                        if _pu_norm in ("SIM", "S", "YES", "1", "TRUE"):
                            pode_unificar = "SIM"
                        elif _pu_norm in ("NAO", "N", "NO", "0", "FALSE"):
                            pode_unificar = "NÃO"
                        else:
                            # Valor desconhecido: grava o original em maiusculo
                            # para o log identificar facilmente.
                            pode_unificar = _pu

                cpf_cnpj = None
                if idx.get("cpf_cnpj") is not None:
                    cpf_cnpj_raw = cell(idx["cpf_cnpj"]).upper()
                    cpf_cnpj = cpf_cnpj_raw if cpf_cnpj_raw in ("CPF", "CNPJ") else None

                observacao_boleto = None
                if idx.get("obs_boleto") is not None:
                    obs_raw = cell(idx["obs_boleto"])
                    observacao_boleto = obs_raw if obs_raw else None

                # Linha completamente em branco (sem nenhum dos tres campos
                # obrigatorios) — pula silenciosamente sem contar como invalida.
                # Linhas em branco no meio da planilha sao normais e nao devem
                # poluir os contadores nem o log de erros.
                if not nome_cliente and not grupo and not cota:
                    continue

                if not nome_cliente or not grupo or not cota:
                    total_invalidas += 1
                    # Registra detalhes para o alerta de campos faltando
                    campos_faltando = []
                    if not nome_cliente:
                        campos_faltando.append("NOME DO CLIENTE")
                    if not grupo:
                        campos_faltando.append("GRUPO")
                    if not cota:
                        campos_faltando.append("COTA")
                    invalidas_detalhe.append({
                        "aba": aba,
                        "linha": i,
                        "campos_faltando": campos_faltando,
                        "nome_cliente": nome_cliente or "(vazio)",
                        "grupo": grupo or "(vazio)",
                        "cota": cota or "(vazio)",
                    })
                    continue

                status_atual = _status_normalizado(boleto)

                if _deve_bloquear(status_atual):
                    total_bloqueadas += 1
                    continue

                if modo_reexecucao:
                    # so processa o que esta como REEXECUTAR
                    if not _esta_reexecutar(status_atual):
                        total_filtradas_reexec += 1
                        continue
                else:
                    # modo normal: marca como NAO BAIXADO os elegiveis
                    if not _esta_nao_baixado(status_atual):
                        linhas_para_atualizar.append(i)

                # Normaliza para a mesma forma que a function SQL grava
                # (LPAD grupo a 6 e cota a 4) - garante que dedup aqui case
                # com a chave unica do banco.
                grupo_norm = str(grupo).strip().zfill(6)
                cota_norm = str(cota).strip().zfill(4)

                # Chave de dedup ESTENDIDA - inclui cliente + cpf_cnpj para
                # alinhar com o indice unico atual do banco
                # (ux_tbl_fila_cotas_lote_aba_grupo_cota_cliente).
                # Mesma normalizacao que o indice usa: UPPER + TRIM no
                # nome_cliente; cpf_cnpj NULL/vazio vira 'CPF'.
                nome_cliente_norm = " ".join(str(nome_cliente).upper().split())
                cpf_cnpj_norm_dedup = (
                    (cpf_cnpj or "").upper().strip() or "CPF"
                )
                chave = (
                    aba,
                    grupo_norm,
                    cota_norm,
                    nome_cliente_norm,
                    cpf_cnpj_norm_dedup,
                )

                if chave in chaves_vistas:
                    total_duplicadas_planilha += 1
                    duplicadas_detalhe.append({
                        "aba": aba,
                        "grupo": grupo_norm,
                        "cota": cota_norm,
                        "linha": i,
                        "nome_cliente": nome_cliente,
                    })
                    # Coluna BOLETO recebe "DUPLICADA"
                    # Coluna OBSERVAÇÃO BOLETO recebe "DUPLICADA com {cliente}"
                    linhas_duplicadas_planilha.append((i, "DUPLICADA"))
                    linhas_duplicadas_obs.append((i, f"DUPLICADA com {nome_cliente}"))
                    log_erro(
                        caminho_log=log_txt_path,
                        etapa="LEITOR_PLANILHA",
                        id_dado=id_fila_adm,
                        acao="Dedup duplicata na planilha",
                        detalhe=(
                            f"aba={aba} grupo={grupo_norm} cota={cota_norm} "
                            f"cliente={nome_cliente_norm} cpf={cpf_cnpj_norm_dedup} "
                            f"linha={i}"
                        ),
                    )
                    continue

                # Filtro banco: nao re-inserir cotas ja existentes no lote
                # (qualquer status, inclusive BAIXADO). Protege contra duplicatas
                # em retomadas apos queda — o NOT EXISTS da SQL function so
                # protege contra PENDENTE/PROCESSANDO.
                _chave_banco = (grupo_norm, cota_norm)
                if _chave_banco in _chaves_banco:
                    log_info(
                        caminho_log=log_txt_path,
                        etapa="LEITOR_PLANILHA",
                        id_dado=id_fila_adm,
                        acao="Cota ja existe no lote — ignorando (retomada)",
                        detalhe=(
                            f"aba={aba} grupo={grupo_norm} cota={cota_norm} "
                            f"cliente={nome_cliente_norm} linha={i+1}"
                        ),
                    )
                    continue

                chaves_vistas.add(chave)

                _ordem_planilha_counter += 1
                cotas_para_inserir.append({
                    "nome_cliente": nome_cliente,
                    "nome_consultor": consultor,
                    "grupo": str(grupo),
                    "cota": str(cota),
                    "nome_aba": aba,
                    "pode_unificar": pode_unificar,
                    "cpf_cnpj": cpf_cnpj,
                    "observacao": observacao_boleto,
                    "ordem_planilha": _ordem_planilha_counter,
                })
                total_enfileiradas += 1

            if not modo_reexecucao and linhas_para_atualizar:
                log_info(
                    caminho_log=log_txt_path,
                    etapa="LEITOR_PLANILHA",
                    id_dado=id_fila_adm,
                    acao="Atualizar planilha",
                    detalhe=f"aba={aba} qtd_linhas={len(linhas_para_atualizar)}",
                )
                atualizar_boleto_em_lote(
                    service=service,
                    spreadsheet_id=spreadsheet_id,
                    aba=aba,
                    letra_col_boleto=letra_boleto,
                    linhas=linhas_para_atualizar,
                )

            if linhas_duplicadas_planilha:
                log_info(
                    caminho_log=log_txt_path,
                    etapa="LEITOR_PLANILHA",
                    id_dado=id_fila_adm,
                    acao="Marcar duplicatas na planilha",
                    detalhe=f"aba={aba} qtd={len(linhas_duplicadas_planilha)}",
                )
                escrever_valores_celulas(
                    service=service,
                    spreadsheet_id=spreadsheet_id,
                    aba=aba,
                    letra_col=letra_boleto,
                    linhas_valores=linhas_duplicadas_planilha,
                )
                if letra_obs_boleto and linhas_duplicadas_obs:
                    escrever_valores_celulas(
                        service=service,
                        spreadsheet_id=spreadsheet_id,
                        aba=aba,
                        letra_col=letra_obs_boleto,
                        linhas_valores=linhas_duplicadas_obs,
                    )

        except HttpError as e:
            # Erro do Google Sheets API. Se for transitorio (429/500/502/503/504),
            # ja foi retentado dentro de ler_range/atualizar_boleto_em_lote sem
            # sucesso. Propagar PARA CIMA significa: o entrada/main.py vai
            # retornar FALHA do lote (em vez de SEM_COTAS silencioso), assim
            # voce tenta de novo no proximo ciclo do PAD em vez de fechar o lote
            # como "vazio".
            log.exception("HttpError no Google API na aba=%s", aba)
            log_erro(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Processar aba (Google API)",
                detalhe=f"aba={aba} HttpError={e}",
            )
            raise RuntimeError(
                f"Falha de comunicacao com Google Sheets API na aba '{aba}': {e}"
            ) from e
        except ColunaFaltandoPlanilha:
            # Coluna essencial faltando -> propaga para o entrada marcar FALHA
            # e seguir para o proximo ADM. NAO deve ser tratada como "aba pulada".
            raise
        except Exception as e:
            # Erros nao-API (ex: cabecalho invalido, aba sem dados, parse error)
            # nao sao tratados como FALHA do lote - apenas pulam a aba.
            total_abas_puladas += 1
            log.exception("erro na aba=%s", aba)
            log_erro(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Processar aba",
                detalhe=f"aba={aba} erro={e}",
            )

    qtd_inseridas = 0
    if cotas_para_inserir:
        qtd_inseridas = inserir_fila_cotas_em_lote(id_fila_adm, cotas_para_inserir)

    # Mantem total_cotas coerente em retomada (usa o que ja existe no banco
    # + o que acabou de ser inserido, em vez de sobrescrever com qtd_inseridas).
    total_cotas_real = contar_cotas_total(id_fila_adm)
    atualizar_total_cotas_fila_adm(id_fila_adm, total_cotas_real)

    resumo = (
        f"id_fila_adm={id_fila_adm} "
        f"modalidade={modalidade} "
        f"reexecucao={modo_reexecucao} "
        f"abas={len(abas)} "
        f"abas_puladas={total_abas_puladas} "
        f"lidas={total_linhas_lidas} "
        f"invalidas={total_invalidas} "
        f"bloqueadas={total_bloqueadas} "
        f"filtradas_reexec={total_filtradas_reexec} "
        f"enfileiradas_local={total_enfileiradas} "
        f"inseridas_banco={qtd_inseridas} "
        f"duplicadas_planilha={total_duplicadas_planilha} "
        f"total_cotas_lote={total_cotas_real}"
    )

    log_info(
        caminho_log=log_txt_path,
        etapa="LEITOR_PLANILHA",
        id_dado=id_fila_adm,
        acao="Resumo",
        detalhe=resumo,
    )

    # Avisa por email se havia linhas com campos essenciais faltando
    # (nome_cliente, grupo ou cota vazios). O lote continua normalmente
    # com as linhas válidas — este email é apenas um alerta.
    if invalidas_detalhe:
        try:
            class CampoFaltandoPlanilha(Exception):
                pass

            linhas_fmt = "\n".join(
                f"  - aba={d['aba']} linha={d['linha']} "
                f"faltando=[{', '.join(d['campos_faltando'])}] "
                f"nome='{d['nome_cliente']}' grupo='{d['grupo']}' cota='{d['cota']}'"
                for d in invalidas_detalhe
            )
            qtd = len(invalidas_detalhe)
            mensagem = (
                f"{qtd} linha(s) da planilha ignorada(s) por campo(s) essencial(is) "
                f"em branco (nome_cliente / grupo / cota). "
                f"O lote foi processado normalmente com as linhas validas."
            )
            contexto_extra = (
                f"modalidade={modalidade}\n"
                f"qtd_linhas_invalidas={qtd}\n"
                f"\nLinhas com campos faltando:\n{linhas_fmt}\n"
                f"\nAcao recomendada: preencher os campos em branco na planilha "
                f"do ADM antes da proxima execucao."
            )
            notificar_falha(
                etapa="LEITOR_PLANILHA",
                erro=CampoFaltandoPlanilha(mensagem),
                id_fila_adm=id_fila_adm,
                caminho_log=log_txt_path,
                script_path=__file__,
                contexto_extra=contexto_extra,
                nivel="ALERTA",
            )
        except Exception as e_notif:
            log_erro(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Notificar campos faltando",
                detalhe=f"falha ao enviar email: {e_notif}",
            )

    # Avisa por email se a planilha do ADM tinha grupo+cota repetido(s)
    if duplicadas_detalhe:
        try:
            linhas_fmt = "\n".join(
                f"  - aba={d['aba']} grupo={d['grupo']} cota={d['cota']} "
                f"linha={d['linha']} cliente={d['nome_cliente']}"
                for d in duplicadas_detalhe
            )
            qtd = len(duplicadas_detalhe)
            mensagem = (
                f"{qtd} cota(s) duplicada(s) ignorada(s) na planilha do ADM "
                f"(modalidade={modalidade}). "
                f"Cada chave grupo+cota foi processada apenas uma vez."
            )
            contexto_extra = (
                f"modalidade={modalidade}\n"
                f"qtd_duplicadas_ignoradas={qtd}\n"
                f"\nDuplicatas ignoradas:\n{linhas_fmt}\n"
                f"\nAcao recomendada: limpar as linhas duplicadas na planilha "
                f"do ADM antes da proxima execucao."
            )
            notificar_falha(
                etapa="LEITOR_PLANILHA",
                erro=DuplicataPlanilha(mensagem),
                id_fila_adm=id_fila_adm,
                caminho_log=log_txt_path,
                script_path=__file__,
                contexto_extra=contexto_extra,
            )
        except Exception as e_notif:
            log_erro(
                caminho_log=log_txt_path,
                etapa="LEITOR_PLANILHA",
                id_dado=id_fila_adm,
                acao="Notificar duplicatas",
                detalhe=f"falha ao enviar email: {e_notif}",
            )

    return qtd_inseridas
