-- =========================================================
-- migration_ordem_planilha.sql
--
-- Adiciona coluna ordem_planilha em tbl_fila_cotas e atualiza
-- a funcao buscar_proxima_cota_pendente para respeitar a
-- sequencia original da planilha do ADM.
--
-- Aplicar com:
--   psql -U <user> -d <banco> -f migration_ordem_planilha.sql
-- =========================================================

-- 1) Adiciona coluna (idempotente)
ALTER TABLE tbl_fila_cotas
    ADD COLUMN IF NOT EXISTS ordem_planilha INTEGER;

-- 2) Preenche retroativamente as linhas antigas com base no id_cota
--    (dentro de cada lote, ordena por id_cota e atribui 1, 2, 3...)
UPDATE tbl_fila_cotas fc
SET ordem_planilha = sub.rn
FROM (
    SELECT id_cota,
           ROW_NUMBER() OVER (PARTITION BY id_fila_adm ORDER BY id_cota) AS rn
    FROM tbl_fila_cotas
    WHERE ordem_planilha IS NULL
) sub
WHERE fc.id_cota = sub.id_cota;

-- 3) Atualiza inserir_fila_cotas_em_lote para gravar ordem_planilha
--    (o JSON passado pelo Python precisa ter o campo "ordem_planilha")
CREATE OR REPLACE FUNCTION inserir_fila_cotas_em_lote(
    p_id_fila_adm INTEGER,
    p_cotas       JSONB
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_qtd INTEGER;
BEGIN
    INSERT INTO tbl_fila_cotas (
        id_fila_adm,
        nome_cliente,
        nome_consultor,
        grupo,
        cota,
        nome_aba,
        pode_unificar,
        cpf_cnpj,
        observacao,
        ordem_planilha,
        status
    )
    SELECT
        p_id_fila_adm,
        (elem->>'nome_cliente')::VARCHAR(200),
        (elem->>'nome_consultor')::VARCHAR(150),
        (elem->>'grupo')::VARCHAR(30),
        (elem->>'cota')::VARCHAR(30),
        (elem->>'nome_aba')::VARCHAR(100),
        COALESCE((elem->>'pode_unificar')::VARCHAR(3), 'SIM'),
        (elem->>'cpf_cnpj')::VARCHAR(4),
        (elem->>'observacao')::TEXT,
        (elem->>'ordem_planilha')::INTEGER,
        'PENDENTE'
    FROM jsonb_array_elements(p_cotas) AS elem
    ON CONFLICT DO NOTHING;

    GET DIAGNOSTICS v_qtd = ROW_COUNT;
    RETURN v_qtd;
END;
$$;

-- 4) Atualiza buscar_proxima_cota_pendente para ORDER BY ordem_planilha
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
    ORDER BY
        COALESCE(fc.ordem_planilha, fc.id_cota),
        fc.id_cota
    LIMIT 1;
END;
$$;
