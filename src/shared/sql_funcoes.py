"""
Wrappers Python das funcoes SQL definidas no schema RPA_GerarBoleto.
Importado pelos tres pontos de entrada (entrada, processamento, saida).
"""

import json
from typing import List, Dict, Optional, Any

from entrada.lib.db import get_conn


# ============================================================
# ENTRADA / RESERVA DE LOTE
# ============================================================

def marcar_lotes_parados_como_falha(minutos: int = 10) -> List[Dict[str, Any]]:
    """
    Move lotes PROCESSANDO parados ha mais de p_minutos para FALHA.
    Retorna a lista dos lotes afetados.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM marcar_lotes_parados_como_falha(%s)", (minutos,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        conn.commit()
    return [dict(zip(cols, row)) for row in rows]


def reservar_lote_interrompido(modalidade: str, maquina: str) -> Optional[Dict[str, Any]]:
    """
    Tenta reservar um lote PENDENTE/FALHA do mes atual da modalidade.
    Atualiza para PROCESSANDO. Retorna o lote ou None se nao houver.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reservar_lote_interrompido(%s, %s)",
                (modalidade, maquina),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
        conn.commit()
    if not row:
        return None
    return dict(zip(cols, row))


def reservar_proximo_adm_e_criar_fila(modalidade: str, maquina: str) -> Optional[Dict[str, Any]]:
    """
    Reserva o proximo ADM elegivel para a modalidade e cria a fila.
    Retorna os dados do lote criado ou None se nao houver ADM elegivel.

    Se o ADM foi reservado via flag reexecucao_*, reseta o flag na mesma
    transacao para evitar loop infinito em caso de falha do lote.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reservar_proximo_adm_e_criar_fila(%s, %s)",
                (modalidade, maquina),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
            dados = dict(zip(cols, row)) if row else None
            # Reseta o flag de reexecucao imediatamente apos reservar o lote.
            # Sem isso, qualquer falha no lote causaria loop infinito porque
            # o flag ficaria true e o ADM seria elegivel na proxima iteracao.
            if dados and dados.get("modo_reexecucao"):
                col = f"reexecucao_{modalidade.lower()}"
                cur.execute(
                    f"UPDATE tbl_adm SET {col} = false WHERE id_adm = %s",
                    (dados["id_adm"],),
                )
        conn.commit()
    return dados


def atualizar_caminhos_fila_adm(
    id_fila_adm: int,
    caminho_base: Optional[str] = None,
    caminho_log: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_caminhos_fila_adm(%s, %s, %s)",
                (id_fila_adm, caminho_base, caminho_log),
            )
        conn.commit()


def atualizar_data_vencimento_fila_adm(id_fila_adm: int, data_vencimento) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_data_vencimento_fila_adm(%s, %s)",
                (id_fila_adm, data_vencimento),
            )
        conn.commit()


def obter_dados_adm_por_fila(id_fila_adm: int) -> Optional[Dict[str, Any]]:
    """
    Retorna os dados consolidados do lote + ADM, ja resolvendo
    nome_aba, modo_reexecucao e ultimo_mes_ref conforme a modalidade.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM obter_dados_adm_por_fila(%s)",
                (id_fila_adm,),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if not row:
        return None
    return dict(zip(cols, row))


def obter_credenciais_adm_por_fila(id_fila_adm: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM obter_credenciais_adm_por_fila(%s)",
                (id_fila_adm,),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if not row:
        return None
    return dict(zip(cols, row))


# ============================================================
# COTAS
# ============================================================

def inserir_fila_cotas_em_lote(id_fila_adm: int, cotas: List[Dict[str, Any]]) -> int:
    """
    Insere varias cotas em massa. Recebe lista de dicts com:
      nome_cliente, nome_consultor, grupo, cota, nome_aba,
      pode_unificar, observacao
    Retorna a quantidade efetivamente inserida.
    """
    payload = json.dumps(cotas, ensure_ascii=False)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT inserir_fila_cotas_em_lote(%s, %s::jsonb)",
                (id_fila_adm, payload),
            )
            qtd = cur.fetchone()[0]
        conn.commit()
    return int(qtd or 0)


def atualizar_total_cotas_fila_adm(id_fila_adm: int, total_cotas: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_total_cotas_fila_adm(%s, %s)",
                (id_fila_adm, total_cotas),
            )
        conn.commit()


def contar_cotas_pendentes(id_fila_adm: int) -> int:
    """Quantas cotas estao em status PENDENTE para o lote."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND status = 'PENDENTE'
                """,
                (id_fila_adm,),
            )
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def contar_cotas_total(id_fila_adm: int) -> int:
    """Total de cotas (qualquer status) para o lote."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                """,
                (id_fila_adm,),
            )
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def buscar_proxima_cota_pendente(id_fila_adm: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM buscar_proxima_cota_pendente(%s)",
                (id_fila_adm,),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if not row:
        return None
    return dict(zip(cols, row))


def marcar_cota_processando(id_cota: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT marcar_cota_processando(%s)", (id_cota,))
        conn.commit()


def finalizar_cota_resultado(
    id_cota: int,
    status: str,
    observacao: Optional[str] = None,
    caminho_boleto: Optional[str] = None,
    caminho_evidencia: Optional[str] = None,
    parcelas_atraso: Optional[int] = None,
) -> None:
    """
    Finaliza cota com status BAIXADO/NAO_BAIXADO/ADIANTADO.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT finalizar_cota_resultado(%s,%s,%s,%s,%s,%s)",
                (id_cota, status, observacao, caminho_boleto, caminho_evidencia, parcelas_atraso),
            )
        conn.commit()


def finalizar_cotas_lote_resultado(
    ids_cota: list,
    status: str,
    observacao: Optional[str] = None,
    caminho_evidencia: Optional[str] = None,
) -> None:
    """
    Finaliza multiplas cotas com o mesmo status em UMA unica conexao DB.
    Muito mais rapido que chamar finalizar_cota_resultado N vezes.
    """
    if not ids_cota:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for id_c in ids_cota:
                cur.execute(
                    "SELECT finalizar_cota_resultado(%s,%s,%s,%s,%s,%s)",
                    (id_c, status, observacao, None, caminho_evidencia, None),
                )
        conn.commit()


def aplicar_finalizacoes_lote(fins: list) -> None:
    """
    Grava TODAS as finalizacoes do payload em UMA unica conexao DB.

    Cada entrada de `fins` deve ter:
      id_cota, status (BAIXADO|NAO_BAIXADO|ADIANTADO|FALHA),
      observacao, caminho_boleto, caminho_evidencia, parcelas_atraso.

    Muito mais rapido que uma conexao por cota (elimina latencia Aiven x N).
    """
    if not fins:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for fin in fins:
                id_c = fin.get("id_cota")
                st   = (fin.get("status") or "").upper()
                if st in ("BAIXADO", "NAO_BAIXADO", "ADIANTADO"):
                    cur.execute(
                        "SELECT finalizar_cota_resultado(%s,%s,%s,%s,%s,%s)",
                        (
                            id_c,
                            st,
                            fin.get("observacao"),
                            fin.get("caminho_boleto"),
                            fin.get("caminho_evidencia"),
                            fin.get("parcelas_atraso"),
                        ),
                    )
                elif st == "FALHA":
                    cur.execute(
                        "SELECT finalizar_cota_falha(%s,%s,%s)",
                        (
                            id_c,
                            (fin.get("observacao") or "FALHA sem observacao")[:500],
                            fin.get("caminho_evidencia") or "",
                        ),
                    )
        conn.commit()


def fechar_pendentes_mesmo_grupo_cota(id_cota: int, status: str, observacao: str) -> int:
    """
    Fecha todas as linhas PENDENTE do mesmo (id_fila_adm, nome_aba, grupo, cota)
    que nao sejam id_cota. Usado para evitar reprocessamento de retries quando
    a cota ja foi finalizada como ADIANTADO/BAIXADO/NAO_BAIXADO.
    Retorna quantas linhas foram fechadas.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tbl_fila_cotas dst
                SET status = %s,
                    observacao = %s,
                    hora_atualizado = NOW()
                FROM tbl_fila_cotas src
                WHERE src.id_cota = %s
                  AND dst.id_fila_adm = src.id_fila_adm
                  AND dst.nome_aba IS NOT DISTINCT FROM src.nome_aba
                  AND dst.grupo     = src.grupo
                  AND dst.cota      = src.cota
                  AND dst.id_cota  != src.id_cota
                  AND dst.status    = 'PENDENTE'
            """, (status, observacao, id_cota))
            fechados = cur.rowcount
        conn.commit()
    return fechados


def finalizar_cota_falha(
    id_cota: int,
    observacao: str,
    caminho_evidencia: str,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT finalizar_cota_falha(%s,%s,%s)",
                (id_cota, observacao, caminho_evidencia),
            )
        conn.commit()


# ============================================================
# FECHAMENTO DO LOTE
# ============================================================

def finalizar_fila_adm(
    id_fila_adm: int,
    status: str,
    observacao: Optional[str] = None,
) -> None:
    """
    Atualiza status do lote e recalcula contadores a partir das cotas.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT finalizar_fila_adm(%s, %s, %s)",
                (id_fila_adm, status, observacao),
            )
        conn.commit()


def listar_chaves_cotas_lote(id_fila_adm: int) -> set:
    """
    Retorna um set de (grupo_zfill6, cota_zfill4) de TODAS as cotas ja
    inseridas no lote, independente do status (BAIXADO, FALHA, PENDENTE etc).

    Usado pelo leitor_planilha para nao re-inserir cotas ja processadas
    em retomadas apos queda — o NOT EXISTS da SQL function so protege
    contra PENDENTE/PROCESSANDO, permitindo duplicatas com BAIXADO.
    """
    import re as _re
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT grupo, cota FROM tbl_fila_cotas WHERE id_fila_adm = %s",
                (id_fila_adm,),
            )
            rows = cur.fetchall()
    resultado = set()
    for grupo, cota in rows:
        g6 = _re.sub(r"\D", "", str(grupo or "")).zfill(6)
        c4 = _re.sub(r"\D", "", str(cota or "")).zfill(4)
        resultado.add((g6, c4))
    return resultado


def pausar_fila_adm(id_fila_adm: int) -> None:
    """
    Anota pausa manual na observacao do lote, sem alterar o status.
    O lote fica PROCESSANDO — o operador muda manualmente para PENDENTE
    quando quiser retomar, e reservar_lote_interrompido o pega normalmente.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tbl_fila_adm
                SET observacao = COALESCE(observacao || ' | ', '') ||
                    'Pausado manualmente pelo operador em ' ||
                    to_char(NOW(), 'DD/MM/YYYY HH24:MI:SS')
                WHERE id_fila_adm = %s
                """,
                (id_fila_adm,),
            )
        conn.commit()


def fechar_lote_adm(
    id_fila_adm: int,
    status: str,
    observacao: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fechamento completo: finaliza lote + atualiza ultimo_mes_ref do ADM
    se status=SUCESSO. Retorna dict com metricas finais do lote.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM fechar_lote_adm(%s, %s, %s)",
                (id_fila_adm, status, observacao),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
        conn.commit()
    if not row:
        return None
    return dict(zip(cols, row))


def atualizar_link_drive_fila_adm(id_fila_adm: int, link_drive: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_link_drive_fila_adm(%s, %s)",
                (id_fila_adm, link_drive),
            )
        conn.commit()


def atualizar_ultima_execucao_adm(id_adm: int, modalidade: str, mes_ref: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_ultima_execucao_adm(%s, %s, %s)",
                (id_adm, modalidade, mes_ref),
            )
        conn.commit()


# ============================================================
# COTAS NAO ENCONTRADAS
# ============================================================

def inserir_cota_nao_encontrada(
    id_fila_adm: int,
    nome_cliente: str,
    grupo: str,
    cota: str,
) -> None:
    """
    Registra cotas que aparecem no sistema (Newcon) mas nao
    estao na fila do lote (planilha).
    """
    grupo = str(grupo).strip().zfill(6)
    cota = str(cota).strip().zfill(4)

    # Idempotente: nao duplica a mesma (lote, grupo, cota) em reexecucao
    # ou quando o worker retenta o mesmo cliente.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tbl_cotas_nao_encontradas
                    (id_fila_adm, nome_cliente, grupo, cota)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM tbl_cotas_nao_encontradas
                    WHERE id_fila_adm = %s AND grupo = %s AND cota = %s
                )
                """,
                (id_fila_adm, nome_cliente, grupo, cota,
                 id_fila_adm, grupo, cota),
            )
        conn.commit()


def listar_cotas_nao_encontradas(id_fila_adm: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nome_cliente, grupo, cota
                FROM tbl_cotas_nao_encontradas
                WHERE id_fila_adm = %s
                ORDER BY grupo, cota
                """,
                (id_fila_adm,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def marcar_cota_reaparecida(
    id_cota_processada: int,
    grupo_origem: str,
    cota_origem: str,
) -> bool:
    """
    Anexa idempotentemente uma nota na observacao de uma cota ja finalizada
    (BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA) quando ela reaparece na pagina
    do cliente durante a pesquisa de OUTRA cota do mesmo cliente.

    Nota anexada (template fixo):
        "Cota ja processada anteriormente; reapareceu durante pesquisa
         da cota {grupo}/{cota}"

    A nota e idempotente: se a observacao ja contiver exatamente esta
    nota para esta cota_origem, nao duplica. Permite multiplas notas se
    a cota reaparecer em pesquisas de diferentes cotas origem.

    NAO altera status, parcelas_atraso, caminho_boleto ou data_atualizacao
    da cota processada - so faz append no campo observacao.

    Args:
      id_cota_processada: id_cota da cota ja finalizada que reapareceu.
      grupo_origem: grupo da cota atual (a que esta sendo processada agora).
      cota_origem: cota da cota atual.

    Returns:
      True se a observacao foi efetivamente alterada (nota nova adicionada),
      False se ja existia (nao duplicou) ou cota nao encontrada.
    """
    grupo_origem = str(grupo_origem).strip().zfill(6)
    cota_origem = str(cota_origem).strip().zfill(4)
    nota = (
        f"Cota ja processada anteriormente; reapareceu durante pesquisa "
        f"da cota {grupo_origem}/{cota_origem}"
    )

    with get_conn() as conn:
        with conn.cursor() as cur:
            # UPDATE idempotente: so anexa se a nota ainda nao estiver na
            # observacao. Usa POSITION para checar substring antes de
            # concatenar. CASE COALESCE para tratar observacao NULL.
            cur.execute(
                """
                UPDATE tbl_fila_cotas
                SET observacao = CASE
                    WHEN observacao IS NULL OR observacao = ''
                        THEN %s
                    WHEN POSITION(%s IN observacao) > 0
                        THEN observacao
                    ELSE observacao || ' | ' || %s
                END
                WHERE id_cota = %s
                  AND (observacao IS NULL OR POSITION(%s IN observacao) = 0)
                """,
                (nota, nota, nota, id_cota_processada, nota),
            )
            afetadas = cur.rowcount
        conn.commit()

    return afetadas > 0


# ============================================================
# UTILITARIOS
# ============================================================

def verificar_cota_existe_na_fila(
    id_fila_adm: int,
    grupo: str,
    cota: str,
) -> bool:
    grupo = str(grupo).strip().zfill(6)
    cota = str(cota).strip().zfill(4)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND grupo = %s
                  AND cota = %s
                LIMIT 1
                """,
                (id_fila_adm, grupo, cota),
            )
            return cur.fetchone() is not None


def verificar_cota_existe_na_fila_com_tipo(
    id_fila_adm: int,
    grupo: str,
    cota: str,
    cpf_cnpj: Optional[str],
) -> bool:
    """
    Verifica se existe cota na fila com mesmo grupo+cota E mesmo tipo (CPF/CNPJ).
    Cotas com cpf_cnpj NULL no banco sao tratadas como CPF por padrao.
    """
    grupo = str(grupo).strip().zfill(6)
    cota = str(cota).strip().zfill(4)
    cpf_cnpj_norm = (cpf_cnpj or "CPF").upper().strip() or "CPF"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND grupo = %s
                  AND cota = %s
                  AND COALESCE(NULLIF(UPPER(TRIM(cpf_cnpj)), ''), 'CPF') = %s
                LIMIT 1
                """,
                (id_fila_adm, grupo, cota, cpf_cnpj_norm),
            )
            return cur.fetchone() is not None


def listar_cotas_cliente_mesmo_tipo(
    id_fila_adm: int,
    nome_cliente: str,
    cpf_cnpj: Optional[str],
) -> List[tuple]:
    """
    Retorna lista de tuplas (grupo, cota) de TODAS as cotas no lote do
    mesmo cliente (mesmo nome, comparacao case-insensitive) e mesmo tipo
    (CPF/CNPJ). Usado para detectar quais cotas do banco nao apareceram
    na tabela de unificacao do Newcon.

    Considera os status PENDENTE e PROCESSANDO (ainda nao finalizadas).
    Cotas com cpf_cnpj NULL no banco sao tratadas como CPF por padrao.
    """
    cpf_cnpj_norm = (cpf_cnpj or "CPF").upper().strip() or "CPF"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT grupo, cota
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND TRIM(UPPER(nome_cliente)) = TRIM(UPPER(%s))
                  AND COALESCE(NULLIF(UPPER(TRIM(cpf_cnpj)), ''), 'CPF') = %s
                  AND status IN ('PENDENTE', 'PROCESSANDO')
                ORDER BY grupo, cota
                """,
                (id_fila_adm, nome_cliente, cpf_cnpj_norm),
            )
            rows = cur.fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def cliente_tem_outro_tipo(
    id_fila_adm: int,
    nome_cliente: str,
    cpf_cnpj_atual: Optional[str],
) -> bool:
    """
    Verifica se existe outra cota no mesmo lote com mesmo nome_cliente
    mas com cpf_cnpj diferente do atual. Usado para decidir se o nome
    do arquivo do boleto precisa de sufixo " - CPF" ou " - CNPJ".
    Cotas com cpf_cnpj NULL sao tratadas como CPF por padrao.
    """
    cpf_cnpj_norm = (cpf_cnpj_atual or "CPF").upper().strip() or "CPF"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND TRIM(UPPER(nome_cliente)) = TRIM(UPPER(%s))
                  AND COALESCE(NULLIF(UPPER(TRIM(cpf_cnpj)), ''), 'CPF') <> %s
                LIMIT 1
                """,
                (id_fila_adm, nome_cliente, cpf_cnpj_norm),
            )
            return cur.fetchone() is not None


def obter_url_newcon() -> Optional[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valor FROM tbl_parametros WHERE nome = 'url_newcon'"
            )
            row = cur.fetchone()
    return row[0] if row else None


def obter_url_avapro() -> Optional[str]:
    """
    Le a URL do AVAPRO de tbl_parametros (nome='url_avapro').
    Fallback de seguranca: retorna None se nao houver parametro - o
    chamador deve aplicar uma URL padrao (avapro.ademicon.com.br/login).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valor FROM tbl_parametros WHERE nome = 'url_avapro'"
            )
            row = cur.fetchone()
    return row[0] if row else None


def obter_parametro_int(nome: str, padrao: int) -> int:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valor FROM tbl_parametros WHERE nome = %s",
                    (nome,),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            return padrao
        return int(str(row[0]).strip())
    except Exception:
        return padrao


# ============================================================
# RETRY DE COTAS
# ============================================================

def inserir_cota_retry(id_cota: int, nova_tentativa: int) -> int:
    """
    Copia os dados da cota p_id_cota e insere um novo registro PENDENTE
    com o número de tentativa explicitamente informado.

    O valor explícito é necessário porque marcar_cota_processando já
    incrementa tentativas+1 no banco; se inserir_cota_retry calculasse
    internamente (tentativas+1), somaria mais uma vez e resultaria em
    tentativa=4 quando deveria ser 3.

    Usado pelo orquestrador: nova_tentativa = tentativa_atual + 1.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT inserir_cota_retry(%s, %s)", (id_cota, nova_tentativa))
            new_id = cur.fetchone()[0]
        conn.commit()
    return int(new_id)


def atualizar_cota_para_retry(id_cota: int, nova_tentativa: int) -> None:
    """
    Recoloca a cota em fila para nova tentativa, atualizando o MESMO registro.

    Ao contrário de inserir_cota_retry (que criava um novo id_cota a cada
    tentativa), esta função apenas reseta o status PROCESSANDO -> PENDENTE
    e grava o número de tentativa explícito.  O orquestrador buscará o
    mesmo id_cota na próxima chamada a buscar_proxima_cota_pendente.

    Nota: marcar_cota_processando já incrementou tentativas no DB antes de
    chamar esta função; nova_tentativa deve ser tentativa_atual + 1 (igual
    ao valor que marcar_cota_processando já gravou), para não somar duas
    vezes.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tbl_fila_cotas
                SET    status          = 'PENDENTE',
                       tentativas      = %s,
                       hora_atualizado = NOW()
                WHERE  id_cota = %s
                """,
                (nova_tentativa, id_cota),
            )
        conn.commit()


def listar_boletos_baixados(id_fila_adm: int) -> List[Dict[str, Any]]:
    """
    Retorna as cotas BAIXADO do lote com seus caminhos de boleto.
    Usado na saida para verificar se os arquivos realmente existem em disco.

    Retorna lista de dicts com:
      id_cota, nome_cliente, grupo, cota, caminho_boleto
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (grupo, cota)
                    id_cota, nome_cliente, grupo, cota, caminho_boleto
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND status = 'BAIXADO'
                  AND caminho_boleto IS NOT NULL
                  AND caminho_boleto <> ''
                ORDER BY grupo, cota, id_cota DESC
                """,
                (id_fila_adm,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def obter_consultor_por_cota(id_cota: int) -> Optional[str]:
    """Retorna nome_consultor de tbl_fila_cotas para o id_cota dado."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nome_consultor FROM tbl_fila_cotas WHERE id_cota = %s",
                (id_cota,),
            )
            row = cur.fetchone()
    return row[0] if row and row[0] else None


def atualizar_observacao_cota(id_cota: int, observacao: str) -> None:
    """
    Substitui a observacao de um registro já finalizado.
    Usado pelo orquestrador para gravar 'FALHA [3/3] — ...' em cotas
    não-retriable que o worker já finalizou sem o prefixo de tentativa.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT atualizar_observacao_cota(%s, %s)",
                (id_cota, observacao),
            )
        conn.commit()
