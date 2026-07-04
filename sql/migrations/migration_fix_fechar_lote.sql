-- =========================================================
-- FIX: fechar_lote_adm — aceitar qualquer status para FALHA
--
-- Problema: quando o login falha, o orquestrador chama
-- fechar_lote_adm(id, 'FALHA', ...) mas o lote pode estar
-- em status diferente de PROCESSANDO (ex: auto-unlock o
-- marcou FALHA durante o retry de conexão, ou nunca saiu
-- de PENDENTE). A função lançava exceção e o lote ficava
-- sem fechamento.
--
-- Solução: para FALHA, aceita qualquer lote existente.
--          para SUCESSO, mantém a exigência de PROCESSANDO.
-- =========================================================

BEGIN;

CREATE OR REPLACE FUNCTION fechar_lote_adm(
    p_id_fila_adm INTEGER,
    p_status      VARCHAR(20),
    p_observacao  TEXT DEFAULT NULL
)
RETURNS TABLE (
    id_fila_adm        INTEGER,
    id_adm             INTEGER,
    modalidade         VARCHAR(12),
    mes_ref            INTEGER,
    status_final       VARCHAR(20),
    total_cotas        INTEGER,
    cotas_baixadas     INTEGER,
    cotas_nao_baixadas INTEGER,
    cotas_adiantadas   INTEGER,
    cotas_erro         INTEGER,
    cotas_pendentes    INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_id_adm     INTEGER;
    v_modalidade VARCHAR(12);
    v_mes_ref    INTEGER;
    v_pendentes  INTEGER;
BEGIN
    IF p_status NOT IN ('SUCESSO','FALHA') THEN
        RAISE EXCEPTION 'Status inválido: %. Use SUCESSO ou FALHA.', p_status;
    END IF;

    -- SUCESSO exige PROCESSANDO (fluxo normal).
    -- FALHA aceita qualquer status (cleanup de emergência:
    --   login falhou, auto-unlock atuou antes, etc.)
    SELECT fa.id_adm, fa.modalidade, fa.mes_ref
    INTO v_id_adm, v_modalidade, v_mes_ref
    FROM tbl_fila_adm fa
    WHERE fa.id_fila_adm = p_id_fila_adm
      AND (
            fa.status = 'PROCESSANDO'     -- obrigatório para SUCESSO
         OR p_status  = 'FALHA'           -- dispensa checagem para FALHA
      );

    IF NOT FOUND THEN
        -- Só chega aqui se p_status=SUCESSO e lote não está PROCESSANDO.
        RAISE EXCEPTION
            'Lote não encontrado ou não está PROCESSANDO (necessário para SUCESSO). id_fila_adm=%',
            p_id_fila_adm;
    END IF;

    SELECT COUNT(*) FILTER (WHERE fc.status IN ('PENDENTE','PROCESSANDO'))
    INTO v_pendentes
    FROM tbl_fila_cotas fc
    WHERE fc.id_fila_adm = p_id_fila_adm;

    -- Recalcula contadores e grava status final.
    PERFORM finalizar_fila_adm(p_id_fila_adm, p_status, p_observacao);

    -- Atualiza ultimo_mes_ref do ADM somente em SUCESSO.
    IF p_status = 'SUCESSO' THEN
        PERFORM atualizar_ultima_execucao_adm(v_id_adm, v_modalidade, v_mes_ref);
    END IF;

    RETURN QUERY
    SELECT
        fa.id_fila_adm,
        fa.id_adm,
        fa.modalidade,
        fa.mes_ref,
        fa.status,
        fa.total_cotas,
        fa.cotas_baixadas,
        fa.cotas_nao_baixadas,
        fa.cotas_adiantadas,
        fa.cotas_erro,
        v_pendentes
    FROM tbl_fila_adm fa
    WHERE fa.id_fila_adm = p_id_fila_adm;
END;
$$;

COMMIT;
