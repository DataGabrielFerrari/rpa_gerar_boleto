-- =========================================================
-- MIGRATION: Multi-ADM — execução sequencial automática
-- Banco: RPA_GerarBoleto
-- =========================================================
-- Aplique este arquivo UMA VEZ sobre o schema existente.
-- É idempotente: pode ser reexecutado sem efeitos colaterais.
-- =========================================================

BEGIN;

-- =========================================================
-- DROP das funções que serão recriadas (ordem segura)
-- =========================================================
DROP FUNCTION IF EXISTS listar_adms_disponiveis(VARCHAR);
DROP FUNCTION IF EXISTS obter_proximo_trabalho(VARCHAR, TEXT);
DROP FUNCTION IF EXISTS reiniciar_cotas_falha(INTEGER);
DROP FUNCTION IF EXISTS buscar_proxima_cota_pendente(INTEGER);

-- =========================================================
-- 1) reiniciar_cotas_falha
--    Recoloca em PENDENTE as cotas que ficaram FALHA ou
--    PROCESSANDO quando o lote anterior foi interrompido.
--    Chamada automaticamente pelo obter_proximo_trabalho.
-- =========================================================
CREATE OR REPLACE FUNCTION reiniciar_cotas_falha(
    p_id_fila_adm INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_qtd INTEGER;
BEGIN
    UPDATE tbl_fila_cotas
    SET
        status                 = 'PENDENTE',
        hora_inicio            = NULL,
        hora_fim               = NULL,
        caminho_boleto         = NULL,
        caminho_evidencia_falha = NULL,
        observacao             = 'RECOLOCADO EM PENDENTE PARA REPROCESSAMENTO',
        hora_atualizado        = NOW()
    WHERE id_fila_adm = p_id_fila_adm
      AND status IN ('FALHA', 'PROCESSANDO');

    GET DIAGNOSTICS v_qtd = ROW_COUNT;
    RETURN v_qtd;
END;
$$;

-- =========================================================
-- 2) buscar_proxima_cota_pendente  (corrigido)
--    Adicionados: nome_aba e cpf_cnpj no retorno.
-- =========================================================
CREATE OR REPLACE FUNCTION buscar_proxima_cota_pendente(
    p_id_fila_adm INTEGER
)
RETURNS TABLE (
    id_cota        INTEGER,
    nome_cliente   VARCHAR(200),
    nome_consultor VARCHAR(150),
    grupo          VARCHAR(30),
    cota           VARCHAR(30),
    nome_aba       VARCHAR(100),
    pode_unificar  VARCHAR(3),
    cpf_cnpj       VARCHAR(4),
    tentativas     INTEGER
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        fc.id_cota,
        fc.nome_cliente,
        fc.nome_consultor,
        fc.grupo,
        fc.cota,
        fc.nome_aba,
        fc.pode_unificar,
        fc.cpf_cnpj,
        fc.tentativas
    FROM tbl_fila_cotas fc
    WHERE fc.id_fila_adm = p_id_fila_adm
      AND fc.status = 'PENDENTE'
    ORDER BY fc.id_cota
    LIMIT 1;
END;
$$;

-- =========================================================
-- 3) obter_proximo_trabalho  — ORQUESTRADOR MULTI-ADM
--
--    Ponto único de entrada para o loop do Python.
--    Prioridade:
--      a) Lote PENDENTE do mês atual             → retoma
--         (FALHA = usuário analisa e seta PENDENTE manualmente)
--      b) Novo ADM elegível                      → cria lote
--      c) Nenhum disponível                      → retorna vazio
--
--    Ao retomar, chama reiniciar_cotas_falha automaticamente
--    para que o Python não precise se preocupar com isso.
--
--    Coluna retomada:
--      TRUE  = lote existente retomado
--      FALSE = novo lote criado
--
--    Loop Python sugerido:
--    ┌─────────────────────────────────────────────────────┐
--    │  while True:                                        │
--    │      r = db.call("obter_proximo_trabalho", mod, maq)│
--    │      if not r: break                                │
--    │      # configura caminhos, vencimento, etc.         │
--    │      while True:                                    │
--    │          c = db.call("buscar_proxima_cota_pendente",│
--    │                       r.id_fila_adm)                │
--    │          if not c: break                            │
--    │          db.call("marcar_cota_processando", c.id)   │
--    │          # processa boleto...                       │
--    │          db.call("finalizar_cota_resultado", ...)   │
--    │      db.call("fechar_lote_adm", r.id_fila_adm, ...) │
--    └─────────────────────────────────────────────────────┘
-- =========================================================
CREATE OR REPLACE FUNCTION obter_proximo_trabalho(
    p_modalidade VARCHAR(12),
    p_maquina    TEXT
)
RETURNS TABLE (
    id_fila_adm     INTEGER,
    id_adm          INTEGER,
    nome            VARCHAR(150),
    maquina         TEXT,
    email           VARCHAR(150),
    link_planilha   TEXT,
    nome_aba        VARCHAR(100),
    modalidade      VARCHAR(12),
    mes_ref         INTEGER,
    data_vencimento DATE,
    modo_reexecucao BOOLEAN,
    ultimo_mes_ref  INTEGER,
    retomada        BOOLEAN
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_id_fila_adm INTEGER;
BEGIN
    -- -------------------------------------------------------
    -- Validações de entrada
    -- -------------------------------------------------------
    IF p_modalidade NOT IN ('MOTORS','IMOVEL') THEN
        RAISE EXCEPTION 'Modalidade inválida: %. Use MOTORS ou IMOVEL.', p_modalidade;
    END IF;
    IF p_maquina IS NULL OR TRIM(p_maquina) = '' THEN
        RAISE EXCEPTION 'Nome da máquina não pode ser vazio.';
    END IF;

    -- -------------------------------------------------------
    -- CAMINHO A: retomar lote PENDENTE do mês atual
    -- (FALHA fica de fora: o usuário analisa e seta PENDENTE
    --  manualmente antes de o bot poder pegar de volta)
    -- -------------------------------------------------------
    SELECT fa.id_fila_adm
    INTO v_id_fila_adm
    FROM tbl_fila_adm fa
    INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
    WHERE a.ativo = TRUE
      AND fa.modalidade = p_modalidade
      AND fa.status = 'PENDENTE'
      AND DATE_TRUNC('month', fa.hora_criado) = DATE_TRUNC('month', CURRENT_DATE)
    ORDER BY fa.hora_criado   -- mais antigo primeiro
    FOR UPDATE OF fa SKIP LOCKED
    LIMIT 1;

    IF FOUND THEN
        -- Muda para PROCESSANDO e registra máquina
        UPDATE tbl_fila_adm
        SET
            status          = 'PROCESSANDO',
            maquina         = p_maquina,
            hora_inicio     = COALESCE(hora_inicio, NOW()),
            hora_atualizado = NOW()
        WHERE id_fila_adm = v_id_fila_adm;

        -- Reseta cotas FALHA/PROCESSANDO para PENDENTE
        PERFORM reiniciar_cotas_falha(v_id_fila_adm);

        -- Retorna dados completos do ADM
        RETURN QUERY
        SELECT
            fa.id_fila_adm,
            fa.id_adm,
            a.nome,
            p_maquina,
            a.email,
            a.link_planilha,
            CASE fa.modalidade
                WHEN 'MOTORS' THEN a.nome_aba_motors
                WHEN 'IMOVEL' THEN a.nome_aba_imovel
            END AS nome_aba,
            fa.modalidade,
            fa.mes_ref,
            fa.data_vencimento,
            fa.modo_reexecucao,
            CASE fa.modalidade
                WHEN 'MOTORS' THEN a.ultimo_mes_ref_motors
                WHEN 'IMOVEL' THEN a.ultimo_mes_ref_imovel
            END AS ultimo_mes_ref,
            TRUE AS retomada
        FROM tbl_fila_adm fa
        INNER JOIN tbl_adm a ON a.id_adm = fa.id_adm
        WHERE fa.id_fila_adm = v_id_fila_adm;

        RETURN;  -- sai aqui, não cai no Caminho B
    END IF;

    -- -------------------------------------------------------
    -- CAMINHO B: criar novo lote para o próximo ADM elegível
    -- (delega para reservar_proximo_adm_e_criar_fila que já
    --  faz FOR UPDATE SKIP LOCKED internamente)
    -- -------------------------------------------------------
    RETURN QUERY
    SELECT
        r.id_fila_adm,
        r.id_adm,
        r.nome,
        r.maquina,
        r.email,
        r.link_planilha,
        r.nome_aba,
        r.modalidade,
        r.mes_ref,
        NULL::DATE AS data_vencimento,  -- Python preenche depois com atualizar_data_vencimento_fila_adm
        r.modo_reexecucao,
        r.ultimo_mes_ref,
        FALSE AS retomada
    FROM reservar_proximo_adm_e_criar_fila(p_modalidade, p_maquina) r;

    -- Se reservar_proximo_adm_e_criar_fila retornou vazio,
    -- RETURN QUERY não emite linhas → função retorna vazio → Python encerra o loop.
END;
$$;

-- =========================================================
-- 4) listar_adms_disponiveis  — monitoramento (sem reservar)
--
--    Mostra todos os ADMs que ainda têm trabalho a fazer:
--      - lote PENDENTE do mês para retomar automaticamente, OU
--      - elegíveis para novo lote
--    (Lotes FALHA não aparecem aqui: precisam de análise manual
--     antes de serem setados para PENDENTE pelo usuário)
--    Não bloqueia nenhuma linha. Use para logs e dashboards.
--
--    Exemplo:
--      SELECT * FROM listar_adms_disponiveis('MOTORS');
--      SELECT * FROM listar_adms_disponiveis();  -- ambas as modalidades
-- =========================================================
CREATE OR REPLACE FUNCTION listar_adms_disponiveis(
    p_modalidade VARCHAR(12) DEFAULT NULL
)
RETURNS TABLE (
    id_adm         INTEGER,
    nome           VARCHAR(150),
    modalidade     VARCHAR(12),
    mes_ref_alvo   INTEGER,
    ultimo_mes_ref INTEGER,
    reexecucao     BOOLEAN,
    tem_lote_falha BOOLEAN,
    status_atual   VARCHAR(20)
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        a.id_adm,
        a.nome,
        mod.m                                                    AS modalidade,
        CASE mod.m WHEN 'MOTORS' THEN a.mes_ref_alvo_motors
                   WHEN 'IMOVEL' THEN a.mes_ref_alvo_imovel
        END                                                      AS mes_ref_alvo,
        CASE mod.m WHEN 'MOTORS' THEN a.ultimo_mes_ref_motors
                   WHEN 'IMOVEL' THEN a.ultimo_mes_ref_imovel
        END                                                      AS ultimo_mes_ref,
        CASE mod.m WHEN 'MOTORS' THEN a.reexecucao_motors
                   WHEN 'IMOVEL' THEN a.reexecucao_imovel
        END                                                      AS reexecucao,
        -- tem lote PENDENTE do mês para retomar automaticamente?
        -- (FALHA aparece separado — exige intervenção manual antes)
        EXISTS (
            SELECT 1 FROM tbl_fila_adm fa
            WHERE fa.id_adm = a.id_adm
              AND fa.modalidade = mod.m
              AND fa.status = 'PENDENTE'
              AND DATE_TRUNC('month', fa.hora_criado) = DATE_TRUNC('month', CURRENT_DATE)
        )                                                        AS tem_lote_pendente,
        -- status do lote mais recente desta modalidade
        COALESCE(
            (SELECT fa2.status FROM tbl_fila_adm fa2
             WHERE fa2.id_adm = a.id_adm AND fa2.modalidade = mod.m
             ORDER BY fa2.hora_criado DESC LIMIT 1),
            'SEM_LOTE'
        )::VARCHAR(20)                                           AS status_atual

    FROM tbl_adm a
    CROSS JOIN (VALUES ('MOTORS'::VARCHAR(12)), ('IMOVEL'::VARCHAR(12))) AS mod(m)

    WHERE a.ativo = TRUE
      AND (p_modalidade IS NULL OR mod.m = p_modalidade)

      -- ADM tem configuração para esta modalidade
      AND (
            (mod.m = 'MOTORS' AND a.modalidade IN ('MOTORS','AMBOS')
             AND a.nome_aba_motors IS NOT NULL AND a.mes_ref_alvo_motors IS NOT NULL)
         OR (mod.m = 'IMOVEL' AND a.modalidade IN ('IMOVEL','AMBOS')
             AND a.nome_aba_imovel IS NOT NULL AND a.mes_ref_alvo_imovel IS NOT NULL)
      )

      -- Sem lote PROCESSANDO ativo (já está em andamento em outra máquina)
      AND NOT EXISTS (
            SELECT 1 FROM tbl_fila_adm fa
            WHERE fa.id_adm = a.id_adm
              AND fa.modalidade = mod.m
              AND fa.status = 'PROCESSANDO'
      )

      -- Tem trabalho a fazer (retomada OU novo ciclo)
      AND (
            -- a) tem lote PENDENTE do mês (retomável automaticamente)
            EXISTS (
                SELECT 1 FROM tbl_fila_adm fa
                WHERE fa.id_adm = a.id_adm
                  AND fa.modalidade = mod.m
                  AND fa.status = 'PENDENTE'
                  AND DATE_TRUNC('month', fa.hora_criado) = DATE_TRUNC('month', CURRENT_DATE)
            )
         -- b) primeira execução (nunca rodou)
         OR CASE mod.m WHEN 'MOTORS' THEN a.ultimo_mes_ref_motors
                       WHEN 'IMOVEL' THEN a.ultimo_mes_ref_imovel
            END IS NULL
         -- c) novo ciclo (mes_ref_alvo > ultimo executado)
         OR CASE mod.m WHEN 'MOTORS' THEN a.mes_ref_alvo_motors
                       WHEN 'IMOVEL' THEN a.mes_ref_alvo_imovel
            END
            > CASE mod.m WHEN 'MOTORS' THEN a.ultimo_mes_ref_motors
                         WHEN 'IMOVEL' THEN a.ultimo_mes_ref_imovel
            END
         -- d) mesmo mês com reexecução ativa
         OR (
              CASE mod.m WHEN 'MOTORS' THEN a.mes_ref_alvo_motors
                         WHEN 'IMOVEL' THEN a.mes_ref_alvo_imovel
              END
              = CASE mod.m WHEN 'MOTORS' THEN a.ultimo_mes_ref_motors
                           WHEN 'IMOVEL' THEN a.ultimo_mes_ref_imovel
              END
              AND CASE mod.m WHEN 'MOTORS' THEN a.reexecucao_motors
                             WHEN 'IMOVEL' THEN a.reexecucao_imovel
              END = TRUE
           )
      )

    ORDER BY
        -- retomadas primeiro (maior prioridade)
        EXISTS (
            SELECT 1 FROM tbl_fila_adm fa
            WHERE fa.id_adm = a.id_adm
              AND fa.modalidade = mod.m
              AND fa.status = 'PENDENTE'
              AND DATE_TRUNC('month', fa.hora_criado) = DATE_TRUNC('month', CURRENT_DATE)
        ) DESC,
        a.id_adm ASC;
END;
$$;

COMMIT;

-- =========================================================
-- VERIFICAÇÕES PÓS-MIGRATION
-- =========================================================
--
-- 1) Confirma que as 4 funções existem:
--
-- SELECT proname, pg_get_function_arguments(oid)
-- FROM pg_proc
-- WHERE proname IN (
--     'reiniciar_cotas_falha',
--     'buscar_proxima_cota_pendente',
--     'obter_proximo_trabalho',
--     'listar_adms_disponiveis'
-- )
-- AND pronamespace = 'public'::regnamespace;
--
-- 2) Ver quantos ADMs estão disponíveis agora:
--
-- SELECT * FROM listar_adms_disponiveis();
--
-- 3) Simular próximo trabalho SEM reservar (só leitura):
--
-- SELECT * FROM listar_adms_disponiveis('MOTORS');
--
-- =========================================================
-- REFERÊNCIA RÁPIDA — funções do ciclo multi-ADM
-- =========================================================
--
--  Loop externo (por ADM):
--    obter_proximo_trabalho(modalidade, maquina)
--      → retomada=TRUE:  lote existente retomado, cotas FALHA já em PENDENTE
--      → retomada=FALSE: novo lote criado, ainda sem cotas
--      → vazio:          nenhum ADM disponível, encerrar loop
--
--  Após receber o lote:
--    atualizar_data_vencimento_fila_adm(id_fila_adm, data)
--    atualizar_caminhos_fila_adm(id_fila_adm, caminho_base, caminho_log)
--    obter_credenciais_adm_por_fila(id_fila_adm)
--
--  Se retomada=FALSE (novo lote), inserir cotas:
--    inserir_fila_cotas_em_lote(id_fila_adm, jsonb_cotas)
--    atualizar_total_cotas_fila_adm(id_fila_adm, total)
--
--  Loop interno (por cota):
--    buscar_proxima_cota_pendente(id_fila_adm)   → vazio = terminou
--    marcar_cota_processando(id_cota)
--    finalizar_cota_resultado(...)  ou  finalizar_cota_falha(...)
--
--  Fechar lote (fim do loop interno):
--    fechar_lote_adm(id_fila_adm, 'SUCESSO'|'FALHA', obs)
--      → atualiza contadores + ultimo_mes_ref do ADM automaticamente
--
--  Monitoramento a qualquer momento:
--    listar_adms_disponiveis(modalidade)
--    marcar_lotes_parados_como_falha(minutos)
-- =========================================================
