-- =========================================================
-- MIGRATION: Trigger hora_atualizado automatica
-- Banco: RPA_GerarBoleto
-- =========================================================
-- Atualiza hora_atualizado em TODO INSERT/UPDATE de
-- tbl_fila_adm e tbl_fila_cotas, sem depender do codigo
-- lembrar de setar a coluna.
--
-- HORARIO: gravado SEMPRE em America/Sao_Paulo (horario do
-- Brasil), independente do fuso do servidor Postgres ou da
-- maquina (VM ou local) que executou o comando.
--
-- Idempotente: pode ser reexecutado sem efeitos colaterais.
-- =========================================================

BEGIN;

-- =========================================================
-- 1) Funcao compartilhada da trigger
-- =========================================================
CREATE OR REPLACE FUNCTION fn_set_hora_atualizado()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- NOW() e timestamptz (instante absoluto); AT TIME ZONE converte
    -- para o horario de parede do Brasil antes de gravar na coluna
    -- timestamp. Assim o valor e o mesmo rodando da VM ou da maquina
    -- local, qualquer que seja o fuso delas ou do servidor.
    NEW.hora_atualizado := (NOW() AT TIME ZONE 'America/Sao_Paulo');
    RETURN NEW;
END;
$$;

-- =========================================================
-- 2) Trigger em tbl_fila_adm
-- =========================================================
DROP TRIGGER IF EXISTS trg_fila_adm_hora_atualizado ON tbl_fila_adm;

CREATE TRIGGER trg_fila_adm_hora_atualizado
    BEFORE INSERT OR UPDATE ON tbl_fila_adm
    FOR EACH ROW
    EXECUTE FUNCTION fn_set_hora_atualizado();

-- =========================================================
-- 3) Trigger em tbl_fila_cotas
-- =========================================================
DROP TRIGGER IF EXISTS trg_fila_cotas_hora_atualizado ON tbl_fila_cotas;

CREATE TRIGGER trg_fila_cotas_hora_atualizado
    BEFORE INSERT OR UPDATE ON tbl_fila_cotas
    FOR EACH ROW
    EXECUTE FUNCTION fn_set_hora_atualizado();

COMMIT;

-- =========================================================
-- VERIFICACAO (rodar depois, opcional)
-- =========================================================
-- Confere se as triggers existem:
--   SELECT tgname, tgrelid::regclass
--   FROM pg_trigger
--   WHERE tgname LIKE 'trg_fila_%hora_atualizado%';
--
-- Confere fuso da sessao e tipo das colunas:
--   SHOW timezone;
--   SELECT table_name, column_name, data_type
--   FROM information_schema.columns
--   WHERE column_name = 'hora_atualizado'
--     AND table_name IN ('tbl_fila_adm', 'tbl_fila_cotas');
--
-- OBS: se hora_atualizado for 'timestamp WITH time zone'
-- (timestamptz), troque a linha da funcao por:
--   NEW.hora_atualizado := NOW();
-- (timestamptz ja guarda o instante absoluto; a conversao
--  de exibicao e feita pelo cliente).
