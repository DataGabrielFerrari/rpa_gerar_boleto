-- ============================================================
-- MIGRATION: coluna selecionar_atraso em tbl_adm
-- Banco: RPA_GerarBoleto
--
-- selecionar_atraso:
--   TRUE  (default) = comportamento normal: emite parcelas em atraso + mes ref.
--   FALSE           = NAO emite atraso. Em cada cota seleciona so a parcela do
--                     mes ref (em dia). Cota que so tem parcela(s) em atraso
--                     (sem mes ref) NAO gera boleto: vira NAO_BAIXADO, com print
--                     em Evidencias/NAO_BAIXADOS/Atrasados nao emitidos/. Ao
--                     unificar, junta apenas as cotas com parcela do mes ref.
--
-- Idempotente: usa IF NOT EXISTS.
-- ============================================================

BEGIN;

ALTER TABLE tbl_adm
    ADD COLUMN IF NOT EXISTS selecionar_atraso BOOLEAN NOT NULL DEFAULT TRUE;

COMMIT;

-- ------------------------------------------------------------
-- Para marcar um ADM como "nao emitir atraso":
--   UPDATE tbl_adm SET selecionar_atraso = FALSE WHERE id_adm = <id>;
--
-- Conferir:
--   SELECT id_adm, nome, matricula, selecionar_atraso FROM tbl_adm ORDER BY id_adm;
-- ------------------------------------------------------------
