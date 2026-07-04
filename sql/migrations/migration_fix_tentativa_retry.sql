-- =========================================================
-- MIGRATION: inserir_cota_retry com tentativas explícita
-- Banco: RPA_GerarBoleto
-- =========================================================
-- Problema:
--   marcar_cota_processando já faz tentativas+1 no registro
--   da cota em processamento. Depois, inserir_cota_retry lia
--   esse valor já incrementado e somava mais 1, resultando em
--   tentativa=4 quando deveria ser 3 (última de 3).
--
-- Solução:
--   inserir_cota_retry recebe p_nova_tentativa explicitamente
--   do orquestrador (que sabe que é tentativa_atual + 1),
--   ignorando o valor do banco.
-- =========================================================

BEGIN;

CREATE OR REPLACE FUNCTION inserir_cota_retry(
    p_id_cota        INTEGER,
    p_nova_tentativa INTEGER
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
        p_nova_tentativa,   -- valor explícito, sem depender do DB
        NULL,
        NOW()
    )
    RETURNING id_cota INTO v_new_id;

    RETURN v_new_id;
END;
$$;

COMMIT;

-- =========================================================
-- VERIFICAÇÃO PÓS-MIGRATION
-- =========================================================
-- SELECT proname, pg_get_function_arguments(oid)
-- FROM pg_proc
-- WHERE proname = 'inserir_cota_retry'
--   AND pronamespace = 'public'::regnamespace;
-- Deve retornar: inserir_cota_retry(p_id_cota integer, p_nova_tentativa integer)
-- =========================================================
