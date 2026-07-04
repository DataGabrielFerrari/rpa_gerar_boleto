-- =========================================================
-- MIGRATION: Retry por registro — tentativas via DB
-- Banco: RPA_GerarBoleto
-- =========================================================
-- Aplique este arquivo UMA VEZ sobre o schema existente.
-- É idempotente: pode ser reexecutado sem efeitos colaterais.
-- =========================================================
--
-- O que muda:
--   Antes: orquestrador retentava a MESMA cota em memória (até 3x),
--          sobrescrevendo caminho_evidencia_falha a cada tentativa.
--   Agora: cada FALHA marca o registro atual e insere um NOVO registro
--          PENDENTE com tentativas+1. A planilha e o email lêem somente
--          o registro de maior id_cota por (grupo, cota) — a última
--          tentativa.
-- =========================================================

BEGIN;

-- =========================================================
-- 0) Ajuste da constraint única para permitir múltiplas
--    tentativas da mesma (id_fila_adm, nome_aba, grupo, cota)
--
--    O índice original bloqueava o INSERT da segunda tentativa
--    com UniqueViolation. Recriamos incluindo a coluna tentativas
--    para que cada tentativa (1, 2, 3) possa coexistir no banco.
-- =========================================================
DROP INDEX IF EXISTS ux_tbl_fila_cotas_lote_aba_grupo_cota;

CREATE UNIQUE INDEX ux_tbl_fila_cotas_lote_aba_grupo_cota
    ON tbl_fila_cotas (id_fila_adm, COALESCE(nome_aba, ''), grupo, cota, tentativas);

-- =========================================================
-- 1) inserir_cota_retry
--    Copia todos os campos de dados de p_id_cota e insere um
--    novo registro PENDENTE com tentativas+1.
--    Retorna o id_cota do novo registro.
-- =========================================================
CREATE OR REPLACE FUNCTION inserir_cota_retry(
    p_id_cota INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_origem      tbl_fila_cotas%ROWTYPE;
    v_new_id      INTEGER;
BEGIN
    SELECT * INTO v_origem
    FROM tbl_fila_cotas
    WHERE id_cota = p_id_cota;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'inserir_cota_retry: id_cota % não encontrado', p_id_cota;
    END IF;

    INSERT INTO tbl_fila_cotas (
        id_fila_adm,
        nome_cliente,
        nome_consultor,
        grupo,
        cota,
        nome_aba,
        pode_unificar,
        cpf_cnpj,
        status,
        tentativas,
        observacao,
        hora_atualizado
    ) VALUES (
        v_origem.id_fila_adm,
        v_origem.nome_cliente,
        v_origem.nome_consultor,
        v_origem.grupo,
        v_origem.cota,
        v_origem.nome_aba,
        v_origem.pode_unificar,
        v_origem.cpf_cnpj,
        'PENDENTE',
        v_origem.tentativas + 1,
        NULL,
        NOW()
    )
    RETURNING id_cota INTO v_new_id;

    RETURN v_new_id;
END;
$$;

-- =========================================================
-- 2) atualizar_observacao_cota
--    Substitui a observacao de um registro já finalizado.
--    Usado pelo orquestrador para gravar "FALHA [3/3] — ..."
--    em cotas não-retriable que o worker já finalizou sem o
--    prefixo de tentativa.
-- =========================================================
CREATE OR REPLACE FUNCTION atualizar_observacao_cota(
    p_id_cota    INTEGER,
    p_observacao TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE tbl_fila_cotas
    SET observacao      = p_observacao,
        hora_atualizado = NOW()
    WHERE id_cota = p_id_cota;
END;
$$;

COMMIT;

-- =========================================================
-- VERIFICAÇÕES PÓS-MIGRATION
-- =========================================================
--
-- 1) Confirmar que as funções existem:
--
-- SELECT proname, pg_get_function_arguments(oid)
-- FROM pg_proc
-- WHERE proname IN ('inserir_cota_retry', 'atualizar_observacao_cota')
-- AND pronamespace = 'public'::regnamespace;
--
-- 2) Teste manual de inserir_cota_retry (use um id_cota real):
--
-- BEGIN;
-- SELECT inserir_cota_retry(999);   -- substitua 999 por id_cota real
-- SELECT id_cota, grupo, cota, status, tentativas
-- FROM tbl_fila_cotas
-- WHERE grupo = (SELECT grupo FROM tbl_fila_cotas WHERE id_cota = 999)
--   AND cota  = (SELECT cota  FROM tbl_fila_cotas WHERE id_cota = 999);
-- ROLLBACK;
--
-- =========================================================
