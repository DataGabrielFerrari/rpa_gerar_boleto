# RPA Gerar Boleto AVAPRO — Documentação Técnica Completa

> **Objetivo:** automatizar a emissão de boletos do portal AVAPRO (avapro.ademicon.com.br) para cotas de consórcio Ademicon, salvando os PDFs organizados e notificando via e-mail ao final.
>
> Este RPA é o sucessor do `rpa_gerar_boleto` (que operava no Newcon). A lógica de negócio é equivalente, mas a interface do site é completamente diferente (SPA React vs sistema legado).

---

## Índice

1. [Visão geral da arquitetura](#1-visão-geral-da-arquitetura)
2. [Fluxo completo de execução](#2-fluxo-completo-de-execução)
3. [Estrutura de pastas](#3-estrutura-de-pastas)
4. [Banco de dados](#4-banco-de-dados)
5. [Etapa ENTRADA](#5-etapa-entrada)
6. [Etapa LOGIN](#6-etapa-login)
7. [Etapa PROCESSAMENTO (worker)](#7-etapa-processamento-worker)
8. [Modal de seleção de parcelas (AVAPRO)](#8-modal-de-seleção-de-parcelas-avapro)
9. [Etapa SAÍDA](#9-etapa-saída)
10. [Nomenclatura dos arquivos PDF](#10-nomenclatura-dos-arquivos-pdf)
11. [Regras de negócio críticas](#11-regras-de-negócio-críticas)
12. [Tratamento de erros e retentativas](#12-tratamento-de-erros-e-retentativas)
13. [Variáveis de ambiente (.env)](#13-variáveis-de-ambiente-env)
14. [Módulos e responsabilidades](#14-módulos-e-responsabilidades)
15. [Diferenças em relação ao rpa_gerar_boleto (Newcon)](#15-diferenças-em-relação-ao-rpa_gerar_boleto-newcon)
16. [Problemas conhecidos e soluções implementadas](#16-problemas-conhecidos-e-soluções-implementadas)

---

## 1. Visão geral da arquitetura

O RPA é dividido em **4 etapas sequenciais**, cada uma rodando como subprocess independente orquestrado pelo `main.py` raiz:

```
main.py (orquestrador)
  │
  ├─ 1) src/entrada/main.py        → reserva ADM, lê planilha, popula banco
  ├─ 2) src/processamento/jobs/login.py → abre Edge CDP, loga no AVAPRO
  ├─ 3) src/processamento/main.py  → worker por cota (loop)
  └─ 4) src/saida/main.py          → Drive, e-mail, planilha, fechamento
```

Cada subprocess comunica com o orquestrador via **JSON na última linha do stdout**. O orquestrador lê esse JSON e decide o próximo passo.

**Tecnologia de automação:** Playwright (Python), conectando ao Microsoft Edge via CDP (Chrome DevTools Protocol) na porta 9222. O Edge é aberto pelo `login.py` e fica aberto; os workers conectam nele sem reabrir.

**Banco de dados:** PostgreSQL (Aiven Cloud) — banco `RPA_GerarBoleto`.

---

## 2. Fluxo completo de execução

```
python main.py MOTORS   (ou IMOVEL)
```

### Loop multi-ADM

O orquestrador roda um ciclo por ADM (Administrador de consórcio) até não sobrar mais ADMs elegíveis para a modalidade:

```
WHILE há ADMs disponíveis:
  1. ENTRADA  → reserva ADM + cria lote + popula fila de cotas
  2. LOGIN    → abre Edge + loga no AVAPRO + navega para /meus-clientes
  3. PROCESSAMENTO (loop por cota PENDENTE):
       WHILE há cotas PENDENTE:
         → worker processa 1 cliente (pode unificar múltiplas cotas)
         → resultado gravado no banco
         → se FALHA retriable: re-loga e retenta (máx. 3x)
  4. SAÍDA    → upload Drive + e-mail + atualiza planilha + fecha lote
  → próximo ADM
```

### Códigos de saída do main.py

| Código | Significado |
|--------|-------------|
| 0 | Sucesso |
| 1 | Falha de etapa |
| 2 | Argumento inválido |
| 3 | SEM_LOTE / SEM_COTAS |

---

## 3. Estrutura de pastas

```
rpa_gerar_boleto_avapro/
├── main.py                        ← Orquestrador principal
├── .env                           ← Credenciais (DB, Google, etc.)
├── src/
│   ├── config/
│   │   └── modalidades.py         ← Regras de dia por modalidade
│   ├── entrada/
│   │   ├── main.py                ← Etapa 1: reserva ADM + fila
│   │   ├── lib/
│   │   │   ├── boleto_rules.py
│   │   │   ├── db.py              ← get_conn() para PostgreSQL
│   │   │   ├── leitor_planilha.py ← Lê Google Sheets
│   │   │   ├── mes_ref.py
│   │   │   └── vencimento.py      ← Calcula data_vencimento
│   │   └── utils/
│   ├── processamento/
│   │   ├── main.py                ← Worker: processa 1 cota
│   │   ├── jobs/
│   │   │   └── login.py           ← Abre Edge + loga no AVAPRO
│   │   └── lib/
│   │       ├── avapro.py          ← Todas as operações Playwright no AVAPRO
│   │       ├── arquivos.py        ← Nomenclatura de arquivos e pastas
│   │       └── navegador.py       ← Conexão CDP, localizar aba, /meus-clientes
│   ├── saida/
│   │   ├── main.py
│   │   └── jobs/
│   │       ├── atualizar_planilha.py
│   │       └── enviar_email.py
│   └── shared/
│       ├── google_auth.py
│       ├── log.py
│       ├── notificador.py
│       ├── notificar_pad.py
│       ├── reautenticar.py
│       └── sql_funcoes.py         ← Todos os wrappers do banco
```

### Pastas de saída (geradas em runtime)

```
{caminho_base}/
├── Boletos/
│   └── {Nome do Consultor}/
│       ├── Junho 001644 0351-00 LUIZ APARECIDO ARAUJO NASCIMENTO.pdf
│       └── Maio Junho 001644 0351-00 JOAO CARLOS DA SILVA.pdf
├── Evidencias/
│   ├── FALHAS/
│   │   └── {Nome Cliente}_{grupo}_{cota}/
│   │       └── *.png              ← Screenshots de erros (somente prints, sem .txt)
│   └── ADIANTADOS/
│       ├── {Nome Cliente}_{grupo}_{cota}/
│       │   └── ADIANTADO_*.png
│       └── verificar_adiantados/
│           └── AVISO_*.png        ← Toast "sem cobrancas"
├── Evidencias.zip                 ← Zip de toda a pasta Evidencias/ (gerado na Saída)
└── Evidencias_Cotas_Faltantes/
    └── *.png                      ← Clientes com cotas não encontradas na planilha
```

---

## 4. Banco de dados

**Banco:** `RPA_GerarBoleto` (PostgreSQL no Aiven)

### Tabelas principais

| Tabela | Descrição |
|--------|-----------|
| `tbl_fila_adm` | Um registro por lote/ADM. Contém modalidade, mes_ref, caminho_base, status do lote |
| `tbl_fila_cotas` | Uma linha por cota a processar. Status: PENDENTE → PROCESSANDO → BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA |
| `tbl_cotas_nao_encontradas` | Cotas que apareceram no AVAPRO mas não estão na planilha do ADM |

### Status do lote (`tbl_fila_adm`)

`PENDENTE → PROCESSANDO → CONCLUIDO / FALHA`

### Status da cota (`tbl_fila_cotas`)

| Status | Significado |
|--------|-------------|
| PENDENTE | Aguardando processamento |
| PROCESSANDO | Worker em execução (ou travado) |
| BAIXADO | Boleto emitido e PDF salvo com sucesso |
| NAO_BAIXADO | Cota não encontrada, modalidade errada, ou outro impedimento definitivo |
| ADIANTADO | Cliente sem parcelas disponíveis para emissão (todas pagas ou futuras) |
| FALHA | Erro técnico (retriable esgotado ou erro definitivo) |

### Campo `tentativas`

Contador de retentativas por cota. Ao esgotar `MAX_TENTATIVAS_COTA = 3`, a cota é marcada como FALHA definitiva com prefixo `FALHA [3/3] — <mensagem>`.

---

## 5. Etapa ENTRADA

**Script:** `src/entrada/main.py`  
**Invocação:** `python src/entrada/main.py MOTORS`

### O que faz

1. **Housekeeping:** chama `marcar_lotes_parados_como_falha()` — lotes PROCESSANDO parados há mais de 10 min voltam para FALHA
2. **Retomada:** tenta reservar lote interrompido do mês atual (`reservar_lote_interrompido`)
3. **Novo lote:** se não houver lote a retomar, reserva próximo ADM elegível (`reservar_proximo_adm_e_criar_fila`)
4. Cria estrutura de pastas e arquivo de log
5. Calcula `data_vencimento` (baseada em `mes_ref` + `modalidade`)
6. Lê a planilha Google Sheets do ADM (`leitor_planilha.py`)
7. Enfileira cotas em `tbl_fila_cotas`
8. Retorna JSON com `id_fila_adm`, `caminho_log`, `total_cotas`

### JSON de saída

```json
{
  "status": "SUCESSO|SEM_LOTE|SEM_COTAS|FALHA",
  "id_fila_adm": 42,
  "caminho_log": "C:\\lotes\\...\\log.txt",
  "total_cotas": 87,
  "observacao": ""
}
```

---

## 6. Etapa LOGIN

**Script:** `src/processamento/jobs/login.py`  
**Invocação:** `python src/processamento/jobs/login.py {id_fila_adm}`

### O que faz

- Abre o Microsoft Edge com `--remote-debugging-port=9222`
- Navega para `https://avapro.ademicon.com.br`
- Faz login com as credenciais do `.env`
- Navega para `/meus-clientes` e aguarda o campo de busca aparecer
- **Mantém o Edge aberto** — os workers se conectam via CDP sem reabrir

### Configuração de download via CDP

Após conectar, envia `Browser.setDownloadBehavior` para forçar downloads na pasta `~/Downloads` do usuário (em vez de UUID temporário do Playwright):

```python
cdp.send("Browser.setDownloadBehavior", {
    "behavior": "allow",
    "downloadPath": str(Path.home() / "Downloads"),
    "eventsEnabled": False,
})
```

---

## 7. Etapa PROCESSAMENTO (worker)

**Script:** `src/processamento/main.py`  
**Invocação:** `python src/processamento/main.py {id_cota}`

O orquestrador chama o worker **uma vez por cota PENDENTE**. O worker processa **um cliente inteiro** — se o cliente tiver múltiplas cotas no lote, todas são processadas juntas em um único boleto (unificado).

### Fluxo interno do worker

```
1. Carrega contexto da cota (banco)
2. Mapeia todas as cotas PENDENTE do lote
3. Conecta ao Edge via CDP
4. Garante que está em /meus-clientes
5. Pesquisa o cliente no AVAPRO (com fallbacks)
6. Entra na página do cliente
7. Lista cotas visíveis na tela
8. Casa cotas da tela com o lote (3 categorias: selecionáveis, já processadas, não encontradas)
9. Para cada cota selecionável:
   a. Clica "Mostrar mais" → lê vencimento → classifica modalidade
   b. Se modalidade errada: ignora (salva screenshot)
   c. Fecha modal "Mostrar mais"
   d. Marca o checkbox da cota
10. Clica "Baixar documentos" → "Emitir boleto"
11. Modal de seleção de parcelas:
    a. Verifica se card já está expandido (não clica de novo se sim)
    b. Lê parcelas da tabela (colunas corretas: tds[0]=checkbox, tds[1]=parcela, tds[2]=vencimento)
    c. Seleciona: parcelas com vencimento < mês ref (atraso) + vencimento == mês ref
    d. Nunca seleciona: vencimento > mês ref (futuras), checkboxes disabled
12. Clica "Continuar"
13. Tela de resumo "Resumo da emissão de boleto" → clica "Baixar"
14. Monitora ~/Downloads por até 180s aguardando PDF novo
15. Move PDF para destino final com nome correto
16. Grava resultado no banco
```

### Pesquisa do cliente (fallbacks)

Tenta 3 variações em ordem:

1. `"850 1239"` — grupo sem zero-pad + espaço + cota sem zero-pad
2. `"000850\1239"` — grupo com zfill(6) + barra invertida + cota com zfill(4)
3. `"1239"` — apenas o número da cota

Para cada variação que retorna zero resultados, salva screenshot em `FALHAS/{cliente}/BUSCA_ZERO_*.png`.

### Casamento de cotas (tela vs lote)

Ao entrar na página do cliente, o worker lista todas as cotas visíveis e as classifica em:

- **Selecionáveis:** estão no lote E status PENDENTE/PROCESSANDO → serão marcadas
- **Já processadas:** estão no lote mas já foram finalizadas num run anterior → apenas loga, não reprocessa
- **Não encontradas:** não estão no lote em nenhum status → registra em `tbl_cotas_nao_encontradas` (aparece no e-mail final)

### Regra de modalidade

O AVAPRO mostra cotas de **MOTORS** e **IMÓVEL** juntas na mesma página do cliente. O worker detecta a modalidade de cada cota pelo dia do vencimento:

- **dia < 15 → MOTORS** (vencimento ~dia 7)
- **dia ≥ 15 → IMÓVEL** (vencimento ~dia 15)

Se a cota pertence a modalidade diferente do lote em execução, ela é **ignorada** (salva screenshot como `MODALIDADE_ERRADA_*.png` e gravada como `NAO_BAIXADO` no banco).

### Boleto unificado

Se o cliente tem **múltiplas cotas** no lote E ambas têm `pode_unificar = Sim`, o worker marca todas os checkboxes antes de clicar "Emitir boleto". O AVAPRO gera um único PDF contendo todas as cotas — chamado "Boleto Unificado".

- Nome do arquivo unificado: `Boleto Unificado {Nome do Cliente}.pdf`
- Se `pode_unificar = Não` em qualquer das cotas, cada uma é emitida separadamente em runs individuais

---

## 8. Modal de seleção de parcelas (AVAPRO)

Esta é a parte mais complexa do fluxo e teve múltiplas correções. Documentada em detalhe.

### Estrutura do HTML do modal

```
div[role="dialog"]  ← modal "Selecione as parcelas para emissão do boleto"
  header
    h2 "Selecione as parcelas..."
    span "N cota(s) selecionada(s)"
  div.flex-1.overflow-y-auto (scroll area dos cards)
    div.space-y-3 (filtros)
      input[placeholder="Buscar por grupo, cota ou parcela"]
      [data-slot="popover-trigger"]  ← "Data base pendência" (calendário)
        button.flex.w-full  "Hoje"
      [data-slot="popover-trigger"]  ← "Segmento e status"
    div.rounded-xl.border  ← card de cada cota
      div.flex.items-center.justify-between (header do card)
        button.min-w-0.flex-1  ← botão TOGGLE (expande/colapsa)
          p.text-base.font-semibold  "Grupo 000850 | Cota 1239"
        span.text-sm  "Imóveis" (ou "Motors")
      div.px-4.pb-4  ← conteúdo expandido (parcelas)
        div (checkbox "Selecionar todas")
        div[data-slot="scroll-area"]
          table
            thead > tr > th (Parcela, Vencimento, Juros/multa, Valor, Status)
            tbody > tr (uma por parcela)
              td[0]  ← checkbox: button[role="checkbox"][data-slot="checkbox"]
              td[1]  ← Número da parcela ("38/220")
              td[2]  ← Vencimento ("15/05/2026")
              td[3]  ← Juros/multa ("--")
              td[4]  ← Valor da parcela ("R$ 316,90")
              td[5]  ← Status (badge "Em atraso" ou "Futura")
  footer
    button "Cancelar"
    button "Continuar"  ← habilitado apenas se ≥ 1 parcela selecionada
```

### Comportamento do modal

- O modal abre com o **primeiro card já expandido** (conteúdo visível)
- Clicar no botão toggle quando expandido **fecha o card** (toggle bidirecional)
- Parcelas "Futura" têm o checkbox com `disabled=""` e `data-disabled=""` — não podem ser clicadas
- O botão "Continuar" fica desabilitado enquanto 0 parcelas estão selecionadas

### Casos especiais na abertura do modal

**"Nenhuma parcela disponível para pagamento na data selecionada."**
Mensagem exibida quando a "Data base pendência" (padrão = "Hoje") é anterior ao vencimento de todas as parcelas.
- Se aparecer **sem nenhum card** visível → trata como **ADIANTADO**, tira print na pasta `ADIANTADOS/`.
- Se aparecer **com card visível** mas parcela do mês ref com checkbox disabled → ver seção abaixo.

**Parcela do mês ref com status "Futura" (disabled)**
Ocorre quando hoje é antes do dia de vencimento do mês corrente (ex: hoje = 02/jun, vencimento = 15/jun).
O worker detecta a situação e automaticamente:
1. Clica no botão "Data base pendência" → abre o calendário
2. Navega para o mês correto (se necessário)
3. Clica no dia do vencimento (ex: 15)
4. Relê as parcelas — a parcela do mês ref agora está disponível
5. Seleciona todas normalmente (atraso + mês ref)

### Após "Continuar" — tela de resumo

Ao clicar "Continuar" **NÃO inicia o download diretamente**. O AVAPRO exibe uma nova tela:

```
"Resumo da emissão de boleto"
  Cotas selecionadas
  Grupo 000820 | Cota 2560
  [tabela com parcela selecionada]
  ← Voltar    [Baixar]    [Enviar por e-mail]
```

O worker precisa clicar o botão **"Baixar"** para iniciar o download do PDF.

### Seleção de parcelas — regras implementadas

```python
# 1. Parcelas futuras (vencimento > mês ref) → NUNCA selecionar
e_futura = vdt.year > ano_ref or (vdt.year == ano_ref and vdt.month > mes_ref)
if e_futura:
    continue

# 2. Parcela do mês ref
e_mes_ref = vdt.month == mes_ref and vdt.year == ano_ref

# 3. Parcela em atraso (determinado pela DATA, não pelo status do site)
e_atraso_data = vdt.year < ano_ref or (vdt.year == ano_ref and vdt.month < mes_ref)

# 4. Seleciona se atraso OU mês ref (sem filtro por valor — seleciona todas válidas)
deve_selecionar = e_atraso_data or e_mes_ref

# 5. Só conta como "atraso" se o clique funcionou (checkbox não disabled)
if deve_selecionar:
    ok = _clicar_checkbox_parcela(page, checkbox_loc)
    if ok:
        if e_atraso_data:
            parcelas_atraso_count += 1
        if e_mes_ref:
            mes_ref_encontrado = True
```

> **Nota:** Não há deduplicação por valor. Se houver múltiplas parcelas válidas com o mesmo vencimento, **todas** são selecionadas.

### Detecção de "adiantado" no modal

Uma cota é considerada **adiantada** (sem cobrança disponível) apenas se **nenhuma parcela pôde ser selecionada** (`cota_selecionadas_count == 0`), ou se o modal exibir "Nenhuma parcela disponível" sem cards.

Exemplos:
- Cliente com Abril + Maio em atraso + Junho "Futura": **altera data base para dia 15, seleciona as 3 parcelas, emite normalmente**
- Cliente com Junho como única parcela e está "Futura": **altera data base, seleciona, emite**
- Modal sem nenhum card ("Nenhuma parcela disponível"): **ADIANTADO**

---

## 9. Etapa SAÍDA

**Script:** `src/saida/main.py`  
**Invocação:** `python src/saida/main.py {id_fila_adm}`

### O que faz

1. **Zipa** a pasta `Evidencias/` (FALHAS + ADIANTADOS) em `Evidencias.zip` no caminho_base
2. Upload dos PDFs para o Google Drive na pasta do ADM
3. Atualiza a planilha Google Sheets com resultados (BAIXADO/NAO_BAIXADO/ADIANTADO/FALHA)
4. Envia e-mail de conclusão com resumo e link do Drive
5. Fecha o lote no banco (`tbl_fila_adm` → CONCLUIDO)

O e-mail contém apenas o **resumo de contadores** (Baixados / Não baixados / Adiantados / Com atraso) e o **link do Drive**. As evidências ficam na pasta zipada no lote local.

---

## 10. Nomenclatura dos arquivos PDF

### Formato (igual ao rpa_gerar_boleto original)

```
{meses por extenso} {grupo zfill6} {cota zfill4}-00 {NOME CLIENTE MAIÚSCULO}.pdf
```

**Exemplos:**
```
Junho 001644 0351-00 LUIZ APARECIDO ARAUJO NASCIMENTO.pdf
Maio Junho 001644 0351-00 JOAO CARLOS DA SILVA.pdf
Marco Abril Maio Junho 001234 0567-00 EMPRESA LTDA.pdf
```

### Regras dos meses

- Os meses vêm da **coluna Vencimento do modal** de parcelas (não do campo "Atraso(s)" do "Mostrar mais")
- Ordenados cronologicamente (mais antigo primeiro)
- Sem duplicar o mesmo mês
- Mês ref é incluído nos meses se a parcela pôde ser selecionada
- Meses em português: Janeiro, Fevereiro, Marco, Abril, Maio, Junho, Julho, Agosto, Setembro, Outubro, Novembro, Dezembro

### Boleto unificado

```
Boleto Unificado {Nome do Cliente}.pdf
```

### Colisão de nomes

Se o arquivo já existir no destino (ex: lote retomado que baixou mas não finalizou no banco), o arquivo existente é **sobrescrito** pelo novo download. Não são criados sufixos `(2)`, `(3)`, pois para o mesmo grupo/cota o boleto é sempre o mesmo.

---

## 11. Regras de negócio críticas

### Modalidade

| Modalidade | Dia de vencimento | Regra de filtro |
|------------|-------------------|-----------------|
| MOTORS | ~dia 7 (antes do dia 15) | Ignora parcelas com dia ≥ 15 |
| IMÓVEL | ~dia 15 (dia 15 em diante) | Ignora parcelas com dia < 15 |

A regra usa o mesmo corte de dia 15 que `config.modalidades.deve_pular_dia`.

### Mês de referência (mes_ref)

- Determinado pela data do lote na `tbl_fila_adm`
- Parcelas do **mes_ref** = parcela corrente (cobrança do mês atual)
- Parcelas de **meses anteriores** = em atraso
- Parcelas de **meses futuros** = nunca selecionar

### Detalhes da cota não encontrados

Quando "Mostrar mais" retorna "Detalhes da cota não encontrados.", o worker:
1. Fecha o diálogo
2. Infere a modalidade pelo ícone Imóveis no card (casa SVG)
3. Continua o processamento normalmente
4. Adiciona `| Detalhes da cota nao encontrado` na observação do banco

### Cotas duplicadas no AVAPRO

O AVAPRO às vezes exibe a mesma cota em 2+ cards (defeito visual). O worker deduplica por `(grupo, cota)` e loga aviso.

### Campo `pode_unificar`

Controlado na planilha do ADM:
- `Sim` (ou vazio): cota pode entrar no boleto unificado
- `Não`: cota deve ser emitida individualmente, mesmo se o cliente tem outras cotas no lote

---

## 12. Tratamento de erros e retentativas

### Tipos de falha

| Tipo | Descrição | Comportamento |
|------|-----------|---------------|
| **DEFINITIVA** | Cota não existe, modalidade errada, cliente duplicado | Worker grava no banco; sem retry |
| **RETRIABLE** | CDP morreu, timeout, cards não renderizaram, site fora | Worker NÃO grava; orquestrador retenta (máx. 3x) |

### Ciclo de retry

```
Tentativa 1 → FALHA retriable
  → Re-loga no AVAPRO
  → Insere novo registro PENDENTE com tentativas=2
Tentativa 2 → FALHA retriable
  → Re-loga
  → Insere PENDENTE com tentativas=3
Tentativa 3 → FALHA retriable
  → Grava FALHA definitiva: "FALHA [3/3] — <mensagem>"
  → Sem mais retry
```

### Screenshots de evidência

Toda falha gera screenshot na pasta `Evidencias/FALHAS/{Nome Cliente}_{grupo}_{cota}/`.  
**Apenas prints (`.png`) são salvos nessa pasta — sem arquivos `.txt` de log.**  
Tracebacks de exceção vão exclusivamente para o `log.txt` do lote.

| Prefixo do arquivo | Situação |
|--------------------|----------|
| `BUSCA_ZERO_*` | Busca retornou zero resultados para esta variação |
| `BUSCA_SEM_RESULTADO` | Todas as variações de busca falharam |
| `BUSCA_MULTIPLOS_*` | Busca retornou múltiplos clientes |
| `CARDS_VAZIOS_T{N}` | Cards do cliente não renderizaram |
| `MODALIDADE_ERRADA_*` | Cota com vencimento de modalidade errada |
| `MODAL_PARCELAS_ERRO` | Modal de parcelas não apareceu |
| `DIALOG_ERRO_PARCELAS_T{N}` | Dialog "Não foi possível carregar as parcelas" (t1/t2/t3) |
| `MODAL_CONTINUAR_ERRO` | Falha ao clicar "Continuar" |
| `EMITIR_SEM_RESPOSTA` | Timeout de 180s sem PDF e sem toast |
| `AVISO_*` | Toast desconhecido ou de erro após emitir boleto |
| `NAO_BAIXADO_*` | Toast definitivo (versão diferente, cota bloqueada) |

### Tratamento de toasts pós-emissão

Após clicar "Emitir boleto", o worker monitora toasts por até 180s:

| Categoria | Padrão | Comportamento |
|-----------|--------|---------------|
| **Sucesso** | `sucesso / gerado / emitido / realizado` | Loga, continua aguardando o PDF |
| **NAO_BAIXADO** | `nao foi possivel / versao diferente / cota bloqueada` | Print + **sai imediatamente** → NAO_BAIXADO definitivo |
| **Erro retriable** | `erro / falha` | Print + **sai imediatamente** → FALHA retriable |
| **Desconhecido** | qualquer outro texto | Print + **sai imediatamente** → FALHA retriable |

> Qualquer toast que não seja de sucesso **interrompe o loop de 180s na hora**, sem esperar mais.

### Detecção de toast "sem cobrança"

Após clicar "Emitir boleto", se aparecer toast com texto `/nao existem cobranças/i`:
- Salva screenshot em `Evidencias/ADIANTADOS/verificar_adiantados/`
- Grava status `ADIANTADO` no banco

### Retry com UniqueViolation

Em lotes retomados (`[RETOMADO]`), a linha de retry (tentativas=2 ou 3) pode já existir na fila de uma execução anterior. Quando `inserir_cota_retry` falha com `UniqueViolation`, o orquestrador trata como "retry já na fila — ok" e continua sem erro. O registro PENDENTE existente será processado normalmente.

---

## 13. Variáveis de ambiente (.env)

```env
# Banco de dados PostgreSQL (Aiven)
DB_HOST=rpa001-rpaademicon01.f.aivencloud.com
DB_PORT=11269
DB_NAME=RPA_GerarBoleto
DB_USER=avnadmin
DB_PASSWORD=...

# Credenciais AVAPRO (para login automático)
AVAPRO_EMAIL=...
AVAPRO_PASSWORD=...

# Google (para Drive e Sheets)
GOOGLE_CREDENTIALS_JSON=...   # ou caminho para o arquivo de credenciais
```

---

## 14. Módulos e responsabilidades

### `src/processamento/lib/avapro.py`

Contém **todas** as operações Playwright específicas do AVAPRO. Nenhum outro módulo faz locator/click no browser. Funções principais:

| Função | Descrição |
|--------|-----------|
| `gerar_variacoes_busca()` | Gera as 3 formas de busca do cliente |
| `digitar_busca()` | Digita no campo de busca com debounce de 3s |
| `aguardar_resultado_pesquisa()` | Aguarda resultado estabilizar (evita flash transitório) |
| `listar_cotas_na_pagina()` | Lê todos os cards de cota da página do cliente |
| `localizar_card_cota()` | Encontra o card `div` de uma cota específica |
| `clicar_mostrar_mais()` | Expande o card "Mostrar mais" |
| `ler_dados_cota_expandida()` | Lê vencimento, atraso(s), parcela atual do modal expandido |
| `classificar_modalidade_por_vencimento()` | Retorna "MOTORS" ou "IMOVEL" pelo dia do vencimento |
| `marcar_checkbox_cota()` | Marca o checkbox de seleção da cota |
| `clicar_baixar_documentos_emitir_boleto()` | Clica no dropdown "Baixar documentos" → "Emitir boleto" |
| `selecionar_parcelas_no_modal()` | Trata o modal de seleção de parcelas (função central) |
| `aguardar_e_clicar_baixar_resumo()` | Clica "Baixar" na tela de resumo após "Continuar" |
| `clicar_continuar_modal_parcelas()` | Clica "Continuar" no footer do modal |
| `snapshot_pdfs_downloads()` | Captura baseline dos PDFs em ~/Downloads |
| `aguardar_pdf_novo_em_downloads()` | Aguarda PDF novo estável aparecer em ~/Downloads |
| `fechar_modal()` | Fecha modal genérico (Escape + candidatos de botão fechar) |
| `detectar_toast_sem_cobrancas()` | Detecta toast "Não existem cobranças disponíveis" |

### `src/processamento/lib/arquivos.py`

Nomenclatura de arquivos e pastas. Funções principais:

| Função | Descrição |
|--------|-----------|
| `nome_arquivo_boleto()` | `{meses} {grupo} {cota}-00 {NOME}.pdf` |
| `nome_arquivo_boleto_unificado()` | `Boleto Unificado {Nome}.pdf` |
| `destino_sem_colisao()` | Adiciona `(2)`, `(3)` se arquivo já existe |
| `pasta_boletos()` | `{caminho_base}/Boletos/{Consultor}/` |
| `pasta_falha_cota()` | `{caminho_base}/Evidencias/FALHAS/{Nome}_{grupo}_{cota}/` |
| `pasta_adiantado_cota()` | `{caminho_base}/Evidencias/ADIANTADOS/{Nome}_{grupo}_{cota}/` |

### `src/processamento/lib/navegador.py`

Conexão CDP e navegação:

| Função | Descrição |
|--------|-----------|
| `conectar_ao_edge()` | Conecta via CDP + configura `Browser.setDownloadBehavior` |
| `achar_aba_avapro()` | Localiza a aba do AVAPRO entre as páginas abertas |
| `garantir_url_meus_clientes()` | Navega para `/meus-clientes` se não estiver lá |

### `src/shared/sql_funcoes.py`

Todos os wrappers do banco. Funções de processamento usadas pelo worker:

| Função | Descrição |
|--------|-----------|
| `obter_dados_adm_por_fila()` | Retorna caminho_base, caminho_log, modalidade do lote |
| `marcar_cota_processando()` | Muda status para PROCESSANDO |
| `finalizar_cota_resultado()` | Grava BAIXADO/NAO_BAIXADO/ADIANTADO com observação |
| `finalizar_cota_falha()` | Grava FALHA com evidência |
| `buscar_proxima_cota_pendente()` | Retorna próxima cota PENDENTE do lote |
| `inserir_cota_retry()` | Cria novo registro PENDENTE com tentativas+1 |
| `inserir_cota_nao_encontrada()` | Registra cota vista no AVAPRO mas ausente da planilha |
| `listar_cotas_nao_encontradas()` | Lista todas as cotas fora da planilha do lote |

---

## 15. Diferenças em relação ao rpa_gerar_boleto (Newcon)

| Aspecto | rpa_gerar_boleto (Newcon) | rpa_gerar_boleto_avapro (AVAPRO) |
|---------|--------------------------|----------------------------------|
| Sistema alvo | Newcon (sistema legado, formulário web clássico) | AVAPRO (SPA React) |
| Seleção de parcelas | Tabela HTML com `#ctl00_Conteudo_grdBoleto_Avulso`, clique no ícone de parcela | Modal "Selecione as parcelas para emissão do boleto" com checkboxes |
| Detecção de atraso | `mes_linha != mes_ref` (comparação direta de mês) | Mesma lógica: `vdt.month < mes_ref` (vencimento antes do mês ref) |
| Unificação | Checkbox "Unificar parcelas" no formulário Newcon | Múltiplos checkboxes de cota marcados antes de emitir |
| Download | `page.expect_download()` | Monitoramento da pasta `~/Downloads` + click em "Baixar" na tela de resumo |
| Autenticação | Sessão mantida pelo PAD | Login automático via Playwright + CDP |
| Orquestrador | PAD (ferramenta externa) | `main.py` Python nativo |
| Nomenclatura de arquivo | `{meses} {nome_cliente_sistema}` | `{meses} {grupo} {cota}-00 {NOME}` (mesmo formato) |

---

## 16. Problemas conhecidos e soluções implementadas

### 1. Card do modal fecha ao tentar expandir

**Causa:** O modal abre com o primeiro card já expandido. O código tentava clicar no botão toggle sem verificar se já estava aberto, fechando o que estava expandido.

**Solução:** `_card_ja_expandido()` verifica se a `<table>` já está visível dentro do card antes de clicar. Se estiver, retorna `True` sem clicar.

### 2. Não lia as parcelas da tabela (nada era selecionado)

**Causa:** `_ler_parcelas_do_card` lia `tds[0]` como número da parcela, mas `tds[0]` é a **célula do checkbox** (vazia). `tds[1]` era lido como vencimento, mas `tds[1]` é o **número da parcela**. Resultado: toda linha pulada (número vazio → `continue`).

**Solução:** Colunas corrigidas para o HTML real do AVAPRO:
- `tds[0]` = checkbox (button[role="checkbox"])
- `tds[1]` = Parcela
- `tds[2]` = Vencimento
- `tds[5]` = Status

### 3. Tentativa de clicar em checkbox disabled (parcelas "Futura")

**Causa:** O AVAPRO desabilita checkboxes de parcelas futuras com `disabled=""` e `data-disabled=""`. O Playwright não consegue clicar neles.

**Solução:** `_clicar_checkbox_parcela()` verifica os atributos `disabled` e `data-disabled` antes de tentar clicar, retornando `False` imediatamente.

### 4. Cota marcada como adiantada quando tem parcelas em atraso

**Causa:** A condição de adiantado era `not mes_ref_encontrado`. Se o mês ref estava "Futura" (disabled) mas havia parcelas em atraso (Abril, Maio), a cota era marcada como adiantada e não emitia boleto.

**Solução:** Condição mudada para `cota_selecionadas_count == 0` — só é adiantado se **nenhuma** parcela pôde ser selecionada.

### 5. Nome do arquivo com "N parcelas em atraso" em vez de meses por extenso

**Causa:** Nomenclatura implementada diferente do rpa_gerar_boleto original.

**Solução:** `nome_arquivo_boleto()` agora recebe `meses_parcelas: list` e gera `{meses} {grupo} {cota}-00 {NOME}.pdf`, idêntico ao formato original.

### 6. Contagem de atraso errada (incluía mês ref)

**Causa:** `parcelas_atraso_count` era incrementado pelo status "Em atraso" do site, que pode incluir a parcela do mês ref se o vencimento já passou.

**Solução:** Atraso determinado pela **data** (`vdt.month < mes_ref`), não pelo status do site. A parcela do mês ref nunca conta como atraso mesmo se o site a exibir em vermelho.

### 7. Download não iniciava após "Continuar"

**Causa:** O AVAPRO exibe uma tela intermediária "Resumo da emissão de boleto" com um botão "Baixar" **antes** de iniciar o download. O código ia direto monitorar Downloads sem clicar nesse botão.

**Solução:** `aguardar_e_clicar_baixar_resumo()` aguarda e clica o botão "Baixar" na tela de resumo. Se o botão não aparecer (versões futuras do AVAPRO), o código loga aviso e segue monitorando Downloads normalmente (fallback).

### 8. Busca retornando estado transitório (MUITOS quando deveria ser UM)

**Causa:** A SPA do AVAPRO tem debounce; o resultado anterior desaparece por milissegundos durante o filtro. O código classificava como MUITOS nesse flash.

**Solução:** `aguardar_resultado_pesquisa()` exige que o sinal (UM/ZERO/MUITOS) seja **estável por 3 ciclos consecutivos** de 200ms (~600ms de estabilidade) antes de decidir.

---

*Documentação gerada em 2026-06-01. Atualizar sempre que houver mudanças no fluxo do AVAPRO ou nas regras de negócio.*
