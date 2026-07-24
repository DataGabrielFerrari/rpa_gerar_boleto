# RPA Gerar Boleto AVAPRO — Handoff / Migração de máquina

> **Para o Claude (ou dev) da nova máquina:** leia este arquivo primeiro e depois o `docs/DOCUMENTACAO.md` (documentação técnica completa e ainda válida — arquitetura, fluxo, modal de parcelas, regras de negócio, erros conhecidos). Este handoff cobre o que a documentação não cobre: estado atual, setup e o diagnóstico de tamanho da pasta. Gerado em 23/07/2026 na migração da VM antiga.

---

## 1. Resumo em 10 linhas

Automatiza a **emissão de boletos** no portal AVAPRO (avapro.ademicon.com.br) para cotas de consórcio Ademicon. Orquestrador Python nativo:

```
python main.py MOTORS   (ou IMOVEL)
```

`main.py` roda 4 etapas como subprocess (comunicação via JSON na última linha do stdout): **entrada** (reserva ADM, lê planilha Google Sheets, popula fila) → **login** (abre Edge com CDP porta 9222, loga no AVAPRO) → **processamento** (worker em loop, 1 cliente por vez, boleto unificado quando possível) → **saída** (Drive, e-mail, planilha, fecha lote). Loop multi-ADM até acabar os elegíveis. Banco: PostgreSQL Aiven, **`RPA_GerarBoleto`**. Retry retriable até 3x por cota. Tudo detalhado no `DOCUMENTACAO.md`.

## 2. Diagnóstico dos 168 MB — NADA corrompido

O peso extra é **inteiramente** de `credenciais/edge_cdp_profile/` (~365 MB no disco): é o **perfil de usuário do Microsoft Edge** que o `login.py` cria/usa ao abrir o navegador com CDP. Cache, Service Workers, Edge Wallet, Safe Browsing etc. — o Edge regenera tudo sozinho.

- **Pode apagar o conteúdo antes de copiar para a máquina nova** (ou nem copiar a pasta). Na primeira execução o Edge recria o perfil. Único efeito colateral: o primeiro login não terá sessão/cookies salvos — o RPA já loga sozinho, então irrelevante.
- A pasta já está no `.gitignore` (commit "ignora perfil Edge"), por isso não vai para o git.
- Código-fonte real do projeto: ~1,6 MB (src + sql + docs + main.py). Íntegro. Os únicos arquivos zero-byte são `__init__.py` (normal em pacotes Python).

```powershell
# opcional, antes de copiar:
Remove-Item -Recurse -Force C:\rpa_gerar_boleto\credenciais\edge_cdp_profile
```

## 3. Correção feita nesta migração

`requirements.txt` estava **incompleto**: não listava `playwright` (essencial — toda a automação) nem `requests`. Corrigido em 23/07/2026. Se a venv nova falhar com `ModuleNotFoundError`, confira o requirements atualizado.

## 4. Setup na máquina nova (checklist)

```bat
cd C:\rpa_gerar_boleto
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
:: Edge do Windows é usado via CDP — não precisa de "playwright install" de browser,
:: mas o Microsoft Edge precisa estar instalado e atualizado.
```

1. Copiar projeto para **`C:\rpa_gerar_boleto`** (memória do projeto: os caminhos foram migrados do Drive para o C).
2. `.env` na raiz: `DB_HOST/PORT/NAME/USER/PASSWORD` (Aiven) + credenciais AVAPRO (`AVAPRO_EMAIL`/`AVAPRO_PASSWORD`) — ver seção 13 do DOCUMENTACAO.md. Testar: `python -c "from src.entrada.lib.db import get_conn; get_conn(); print('ok')"`.
3. `credentials/token.json` (OAuth Google para Sheets/Drive/Gmail) pode invalidar ao trocar de máquina — se der erro, reautenticar via `src/shared/google_auth.py` / `src/shared/reautenticar.py` (usa `credentials/client_secret.json`).
4. Pasta de lotes/saída (`caminho_base` em `tbl_fila_adm` / pasta configurada): a pasta `lotes` **não foi copiada** da VM — verificar se o caminho base configurado existe na máquina nova e criar se preciso.
5. `instalar_dependencias.bat` na raiz automatiza venv + pip (confira o conteúdo antes de rodar).
6. Migrations em `sql/migrations/` — o banco Aiven já está migrado; só aplicar se criar banco novo. `sql/insert_adm_karoline.sql` já aplicado (ADM Karoline).
7. `.rpa_worker_passo.txt` na raiz é arquivo de estado do worker (última cota/passo) — pode ser apagado; é recriado em runtime.

## 5. Estado do projeto na migração (23/07/2026)

- Git: 3 commits, o último "Versao final". Alterações não commitadas pequenas: `.gitignore`, `LICENSE`, `requirements.txt` (correção acima), `src/entrada/lib/mes_ref.py`, `src/entrada/utils/cabecalho_utils.py`, `src/entrada/utils/texto_utils.py`, `src/shared/log.py`. O código em disco é o vigente — considerar commitar na máquina nova.
- Scripts utilitários avulsos na raiz: `reenviar_email.py` (reenvia e-mail de um lote) e `consultar_falhas.py` (consulta falhas no banco).
- `docs/DOCUMENTACAO.md` (01/06/2026) continua sendo a referência técnica: fluxo das 4 etapas, estrutura do modal de parcelas do AVAPRO (a parte mais frágil), regras de modalidade (dia < 15 = MOTORS, ≥ 15 = IMÓVEL), nomenclatura dos PDFs, retry, e os 8 problemas conhecidos já resolvidos. **Não regredir os fixes da seção 16.**
- RPA irmão: `C:\rpa_ofertar_lance` (oferta lances no mesmo portal, mas orquestrado por PAD, banco `RPA_OfertarLance`). Handoff dele: `C:\rpa_ofertar_lance\DOCUMENTACAO_HANDOFF.md`.
