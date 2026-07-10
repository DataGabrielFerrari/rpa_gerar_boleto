-- ============================================================
-- INSERT de 2 ADMs novos (Karoline) em tbl_adm
-- Banco: RPA_GerarBoleto  (geracao de boletos)
--
-- Colunas conforme as funcoes do banco (migration_multi_adm.sql):
--   reservar_proximo_adm_e_criar_fila / obter_proximo_trabalho /
--   listar_adms_disponiveis
--
-- Elegibilidade (listar_adms_disponiveis) para modalidade AMBOS:
--   - modalidade IN ('MOTORS','IMOVEL','AMBOS')
--   - nome_aba_<mod> NOT NULL  E  mes_ref_alvo_<mod> NOT NULL
--   - ultimo_mes_ref_<mod> NULL  => primeira execucao (fica elegivel)
--
-- mes_ref no formato YYYYMM  ->  202607 (julho/2026)
-- Senha em texto puro (o login le a coluna 'senha' direto).
-- ============================================================

BEGIN;

-- 1) Karoline - 11684  (ATIVO = TRUE)
INSERT INTO tbl_adm (
    nome,
    email,
    matricula,
    senha,
    link_planilha,
    nome_aba_imovel,
    nome_aba_motors,
    modalidade,
    mes_ref_alvo_imovel,
    mes_ref_alvo_motors,
    ultimo_mes_ref_imovel,
    ultimo_mes_ref_motors,
    reexecucao_imovel,
    reexecucao_motors,
    ativo
) VALUES (
    'Karoline - 11684',
    'rpa.ademicon@gmail.com',
    '11684',
    'j@xpU92y6vCi5*k',
    'https://docs.google.com/spreadsheets/d/1y7S5hBiiadhIv2XPSgp1Hxyl5oPPNidynyTR8LJiCUw/edit?gid=0#gid=0',
    'IMOVEL',
    'MOTORS',
    'AMBOS',
    202607,
    202607,
    NULL,
    NULL,
    TRUE,
    FALSE,
    TRUE
)
RETURNING id_adm, nome, matricula, modalidade, ativo,
          reexecucao_imovel, reexecucao_motors;

-- 2) Karoline - 7185  (ATIVO = TRUE)
INSERT INTO tbl_adm (
    nome,
    email,
    matricula,
    senha,
    link_planilha,
    nome_aba_imovel,
    nome_aba_motors,
    modalidade,
    mes_ref_alvo_imovel,
    mes_ref_alvo_motors,
    ultimo_mes_ref_imovel,
    ultimo_mes_ref_motors,
    reexecucao_imovel,
    reexecucao_motors,
    ativo
) VALUES (
    'Karoline - 7185',
    'rpa.ademicon@gmail.com',
    '7185',
    'j@xpU92y6vCi5*k',
    'https://docs.google.com/spreadsheets/d/1lmcrr0gO7aIIHD0Tn-8P5gm6aB0yhzE3a0S_0NeoKaY/edit?gid=0#gid=0',
    'IMOVEL',
    'MOTORS',
    'AMBOS',
    202607,
    202607,
    NULL,
    NULL,
    TRUE,
    FALSE,
    TRUE
)
RETURNING id_adm, nome, matricula, modalidade, ativo,
          reexecucao_imovel, reexecucao_motors;

COMMIT;

-- Conferencia pos-insert:
-- SELECT id_adm, nome, matricula, modalidade, ativo,
--        nome_aba_imovel, nome_aba_motors,
--        mes_ref_alvo_imovel, mes_ref_alvo_motors,
--        ultimo_mes_ref_imovel, ultimo_mes_ref_motors,
--        reexecucao_imovel, reexecucao_motors
-- FROM tbl_adm
-- WHERE matricula IN ('11684','7185')
-- ORDER BY id_adm;
