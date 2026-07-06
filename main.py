"""
ORQUESTRADOR PRINCIPAL do RPA gerar boleto AVAPRO.

Roda o ciclo inteiro, substituindo o PAD antigo:
  1. ENTRADA       - reserva ADM + cria fila_adm + popula fila_cotas
  2. LOGIN         - abre Edge com CDP + loga no AVAPRO + Meus Clientes
  3. PROCESSAMENTO - loop por cota PENDENTE; worker por cliente (unifica)
  4. SAIDA         - drive + email + planilha + fechamento do lote

Tratativa de erro forte na etapa 3:
- O worker (src/processamento/main.py) classifica cada cota em:
    * DEFINITIVO  -> BAIXADO/NAO_BAIXADO/ADIANTADO ou FALHA-duplicado;
                     o proprio worker grava no banco.
    * RETRIABLE   -> FALHA grave/transitoria (CDP caiu, timeout de botao,
                     site fora, cards nao renderizaram); o worker NAO grava
                     e devolve retriable=true, deixando a cota PROCESSANDO.
- Para FALHA retriable, o orquestrador RE-LOGA e retenta a MESMA cota ate
  MAX_TENTATIVAS_COTA vezes. Esgotado, marca a cota como FALHA no banco e
  segue para a proxima cota (sem email por cota individual).

Uso:
    python main.py MODALIDADE          (MOTORS por enquanto)

Saidas:
    0  - sucesso
    1  - falha de etapa
    2  - argumento invalido
    3  - SEM_LOTE / SEM_COTAS
"""

import os
import re
import sys
import json
import time
import signal
import subprocess
from typing import Optional, Dict, Any, List

# Flag de parada suave: setada pelo handler de Ctrl+C.
# O loop de cotas checa essa flag ENTRE cotas (nunca no meio de uma).
_PARAR = False


def _handler_sigint(signum, frame):
    global _PARAR
    if not _PARAR:
        _PARAR = True
        print(
            "\n[MAIN] Ctrl+C recebido — finalizando o boleto atual e pausando...",
            flush=True,
        )


signal.signal(signal.SIGINT, _handler_sigint)


# ============================================================
# Pausa por tecla ESPACO (Windows)
#
# ESPACO 1x -> pausa no PROXIMO ponto seguro (nunca no meio de um boleto:
#              a tecla fica no buffer do console e e detectada assim que o
#              worker atual termina).
# ESPACO 2x -> retoma imediatamente.
# Ctrl+C    -> continua funcionando normalmente (parada suave com PAUSADO
#              no banco), inclusive durante a pausa.
# ============================================================

def _drenar_tecla_espaco() -> bool:
    """
    Le todas as teclas pendentes no buffer do console (nao bloqueia).
    Retorna True se ESPACO estava entre elas. Ignora as demais teclas.
    Em ambiente sem console Windows (ex: agendador), vira no-op.
    """
    try:
        import msvcrt
    except ImportError:
        return False
    pressionado = False
    try:
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == " ":
                pressionado = True
    except Exception:
        return False
    return pressionado


def _checar_pausa_teclado(contexto: str = "") -> None:
    """
    Chamada nos pontos seguros do fluxo (entre cotas/etapas).
    Se ESPACO foi pressionado desde a ultima checagem, PAUSA aqui e so
    retoma quando ESPACO for pressionado de novo (ou Ctrl+C para encerrar).
    """
    if not _drenar_tecla_espaco():
        return

    onde = f" ({contexto})" if contexto else ""
    print("", flush=True)
    print("+" + "=" * 58 + "+", flush=True)
    print(f"|  PAUSADO pelo teclado{onde}", flush=True)
    print("|  Pressione ESPACO para retomar  |  Ctrl+C para encerrar", flush=True)
    print("+" + "=" * 58 + "+", flush=True)

    while True:
        # Ctrl+C durante a pausa: deixa o fluxo normal de parada suave agir.
        if _PARAR:
            print("[PAUSA] Ctrl+C recebido durante a pausa — encerrando suavemente.", flush=True)
            return
        if _drenar_tecla_espaco():
            print("[PAUSA] ESPACO pressionado — retomando execucao...", flush=True)
            print("", flush=True)
            return
        time.sleep(0.2)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, ".env"), override=True)
except Exception:
    pass


ENTRADA_MAIN = os.path.join(SRC_DIR, "entrada", "main.py")
LOGIN_MAIN = os.path.join(SRC_DIR, "processamento", "jobs", "login.py")
PROCESSAMENTO_MAIN = os.path.join(SRC_DIR, "processamento", "main.py")
SAIDA_MAIN = os.path.join(SRC_DIR, "saida", "main.py")

# Trava de seguranca contra loop infinito (cota nunca sai de PENDENTE).
MAX_COTAS_POR_LOTE = 1000

# Retentativas por cota em caso de FALHA retriable (grave/transitoria).
MAX_TENTATIVAS_COTA = 2

# Timeouts de subprocess (segundos).
TIMEOUT_ENTRADA_S = 120
TIMEOUT_LOGIN_S = 180
TIMEOUT_WORKER_S = 180
TIMEOUT_SAIDA_S = 600


# ============================================================
# Subprocess utils
# ============================================================

def _imprimir(secao, msg):
    print(f"[{secao}] {msg}", flush=True)


def _imprimir_err(secao, msg):
    print(f"[{secao}] ERRO: {msg}", file=sys.stderr, flush=True)


def _resumo_payload(secao: str, payload: dict) -> None:
    """
    Imprime um resumo legivel do payload JSON retornado por um subprocess.
    Substitui o _imprimir(secao, f"resposta: {payload}") bruto.
    """
    status  = (payload.get("status") or "?").upper()
    obs     = str(payload.get("observacao") or "").strip()
    elapsed = payload.get("_elapsed_s")
    tempo   = f"({elapsed:.1f}s) " if elapsed is not None else ""

    if secao == "WORKER":
        # Caminho COMPLETO do boleto/evidencia (nao apenas o nome do arquivo).
        caminho = (
            payload.get("caminho_boleto")
            or payload.get("caminho_evidencia_falha")
            or payload.get("caminho_evidencia")
            or payload.get("nome_arquivo")
            or ""
        )
        caminho = str(caminho).strip()

        simbolo = {
            "BAIXADO": "✓",
            "ADIANTADO": "~",
            "NAO_BAIXADO": "~",
            "FALHA": "✗",
        }.get(status, "?")

        retriable = bool(payload.get("retriable", False))
        tag = " [retry]" if (status == "FALHA" and retriable) else ""

        # Sempre imprime as tres informacoes pedidas: status, caminho completo e observacao.
        _imprimir(secao, f"{simbolo} STATUS: {status}{tag} {tempo}".rstrip())
        _imprimir(secao, f"    CAMINHO: {caminho or '(sem caminho)'}")
        _imprimir(secao, f"    OBSERVACAO: {obs or '(sem observacao)'}")

        # Cotas não selecionadas — motivo por cota (útil para diagnóstico).
        for ns in (payload.get("cotas_nao_selecionadas") or []):
            g = ns.get("grupo", "?")
            c = ns.get("cota", "?")
            motivo = ns.get("motivo", "?")
            _imprimir(secao, f"  ! {g}/{c} nao selecionada — {motivo[:100]}")

        # Toasts capturados durante a emissão.
        for t in (payload.get("toasts_capturados") or []):
            _imprimir(secao, f"  > Toast: {str(t)[:120]}")

    elif secao == "LOGIN":
        if status == "SUCESSO":
            _imprimir(secao, "✓ Login realizado com sucesso")
        else:
            _imprimir(secao, f"✗ FALHA → {obs or status}")

    elif secao == "ENTRADA":
        if status in ("SUCESSO", "SEM_LOTE", "SEM_COTAS"):
            _imprimir(secao, f"✓ {status}" + (f" → {obs}" if obs else ""))
        else:
            _imprimir(secao, f"✗ {status} → {obs or '(sem obs)'}")

    elif secao == "SAIDA":
        if status == "SUCESSO":
            link  = payload.get("link_drive") or ""
            metr  = payload.get("metricas") or ""
            extra = f" | drive={link}" if link else ""
            extra += f" | {metr}" if metr else ""
            _imprimir(secao, f"✓ SUCESSO{extra}")
        else:
            _imprimir(secao, f"✗ {status} → {obs or '(sem obs)'}")

    else:
        _imprimir(secao, f"{status}" + (f" → {obs}" if obs else ""))


def _imprimir_nao_encontradas_novas(nome_cliente_atual, novos_registros):
    """
    Imprime visualmente as cotas que apareceram no AVAPRO do cliente atual
    mas nao estao na planilha. Aparece durante o loop, conforme o lote
    roda, pra voce acompanhar em tempo real.

    Formato:
        +-- Cotas no AVAPRO fora da planilha (cliente: NOME) ---+
        |  001234/0567   NOME DO CLIENTE                        |
        |  001234/0890   NOME DO CLIENTE                        |
        +-------------------------------------------------------+
    """
    if not novos_registros:
        return
    largura = 64
    titulo = f" Cotas no AVAPRO fora da planilha ({len(novos_registros)}) "
    if len(titulo) > largura - 4:
        titulo = titulo[:largura - 4]
    pad_titulo = largura - 2 - len(titulo)
    print("", flush=True)
    print("+" + titulo + "-" * pad_titulo + "+", flush=True)
    print(f"|  cliente em processo: {(nome_cliente_atual or '')[:largura-24]:<{largura-24}}|",
          flush=True)
    print("+" + "-" * largura + "+", flush=True)
    for r in novos_registros:
        g = str(r.get("grupo") or "?")
        c = str(r.get("cota") or "?")
        nome = str(r.get("nome_cliente") or "")
        linha = f"  {g}/{c}   {nome}"
        if len(linha) > largura - 1:
            linha = linha[:largura - 4] + "..."
        print(f"|{linha:<{largura}}|", flush=True)
    print("+" + "-" * largura + "+", flush=True)
    print("", flush=True)


def _imprimir_sumario_nao_encontradas(registros):
    """
    Imprime sumario final ao terminar o lote: todas as cotas que apareceram
    no AVAPRO mas nao estavam na planilha, ordenadas por grupo/cota.
    """
    if not registros:
        return
    largura = 64
    titulo = f" SUMARIO: {len(registros)} cota(s) no AVAPRO fora da planilha "
    if len(titulo) > largura - 4:
        titulo = titulo[:largura - 4]
    pad_titulo = largura - 2 - len(titulo)
    print("", flush=True)
    print("+" + titulo + "=" * pad_titulo + "+", flush=True)
    registros_ord = sorted(
        registros, key=lambda r: (r.get("grupo") or "", r.get("cota") or "")
    )
    for r in registros_ord:
        g = str(r.get("grupo") or "?")
        c = str(r.get("cota") or "?")
        nome = str(r.get("nome_cliente") or "")
        linha = f"  {g}/{c}   {nome}"
        if len(linha) > largura - 1:
            linha = linha[:largura - 4] + "..."
        print(f"|{linha:<{largura}}|", flush=True)
    print("+" + "=" * largura + "+", flush=True)
    print(
        "  (cotas tambem registradas em tbl_cotas_nao_encontradas - "
        "vao aparecer no email final.)",
        flush=True,
    )
    print("", flush=True)


def _extrair_json_da_saida(saida):
    if not saida:
        return None
    for linha in reversed([l for l in saida.splitlines() if l.strip()]):
        try:
            obj = json.loads(linha.strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _rodar_script(secao, script_path, argv_extras, timeout_s=300):
    if not os.path.exists(script_path):
        raise RuntimeError(f"Script nao encontrado: {script_path}")

    cmd = [sys.executable, script_path] + [str(a) for a in argv_extras]
    _imprimir(secao, f"executando: {' '.join(cmd)}")

    t0 = time.monotonic()
    try:
        # stdout capturado (para extrair JSON).
        # stderr herdado (None = flui direto pro terminal em tempo real):
        # toasts, logs de debug e erros do worker aparecem imediatamente.
        # CREATE_NEW_PROCESS_GROUP (Windows): isola o worker do SIGINT do
        # terminal. Sem isso, Ctrl+C mata o Playwright no meio da execucao.
        # O orquestrador trata a parada via flag _PARAR — o worker termina
        # normalmente e so entao o loop verifica a flag e para.
        _creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,            # herda stderr do processo pai → terminal live
            text=True, encoding="utf-8",
            errors="replace", cwd=ROOT_DIR,
            creationflags=_creationflags,
        )
        try:
            stdout_capturado, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Mata a ARVORE inteira (python + driver node do playwright).
            # kill() puro deixa o node vivo segurando o pipe de stdout e a
            # limpeza do subprocess trava PARA SEMPRE (orquestrador congela).
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass
            try:
                proc.communicate(timeout=15)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{secao} excedeu timeout de {timeout_s}s")
    elapsed = time.monotonic() - t0

    payload = _extrair_json_da_saida(stdout_capturado)
    if payload is None:
        raise RuntimeError(
            f"{secao} nao retornou JSON valido. exit_code={proc.returncode}. "
            f"stdout (ult. 500): {(stdout_capturado or '')[-500:]!r}"
        )

    payload["_elapsed_s"] = round(elapsed, 1)
    _resumo_payload(secao, payload)
    return payload


def _rodar_login(id_fila_adm) -> bool:
    """Roda o login.py. Retorna True se logou com sucesso."""
    try:
        payload = _rodar_script(
            secao="LOGIN", script_path=LOGIN_MAIN,
            argv_extras=[id_fila_adm], timeout_s=TIMEOUT_LOGIN_S,
        )
    except RuntimeError as e:
        _imprimir_err("MAIN", f"LOGIN falhou: {e}")
        return False
    return (payload.get("status") or "").upper() == "SUCESSO"


# ============================================================
# Validacao de argumentos
# ============================================================

MODALIDADES_VALIDAS = {"MOTORS", "IMOVEL"}


def _validar_modalidade(arg):
    if not arg:
        raise ValueError("MODALIDADE nao informada (uso: python main.py MOTORS)")
    norm = arg.strip().upper()
    norm = (
        norm.replace("Á", "A").replace("À", "A").replace("Â", "A").replace("Ã", "A")
        .replace("É", "E").replace("Ê", "E").replace("Í", "I")
        .replace("Ó", "O").replace("Ô", "O").replace("Õ", "O")
        .replace("Ú", "U").replace("Ü", "U").replace("Ç", "C")
    )
    if norm not in MODALIDADES_VALIDAS:
        raise ValueError(
            f"MODALIDADE invalida: {arg!r}. Aceitos: {sorted(MODALIDADES_VALIDAS)}"
        )
    return norm


# ============================================================
# Processamento de UMA cota (uma unica execucao do worker)
# com logica de retry via novos registros no banco.
# ============================================================

def _processar_cota_uma_vez(
    id_cota: int,
    tentativa: int,
    id_fila_adm: int,
    caminho_log: Optional[str],
) -> tuple:
    """
    Executa o worker para uma cota e gerencia o resultado.

    Fluxo para FALHA:
      - Se tentativa < MAX_TENTATIVAS_COTA:
          * Finaliza o registro atual como FALHA (se for retriable, o worker
            nao gravou; se nao-retriable, o worker ja gravou).
          * Insere novo registro PENDENTE com tentativas+1 via inserir_cota_retry.
          * Retorna ("FALHA", retriable) — o loop principal pegara o novo
            PENDENTE na proxima iteracao.
      - Se tentativa == MAX_TENTATIVAS_COTA (ultima):
          * Marca FALHA definitiva com observacao "FALHA [N/N] — <mensagem>".
          * Nao insere novo registro.

    Retorna (status_final: str, retriable: bool).
    """
    payload: Dict[str, Any] = {}
    worker_erro = ""

    try:
        payload = _rodar_script(
            secao="WORKER", script_path=PROCESSAMENTO_MAIN,
            argv_extras=[id_cota], timeout_s=TIMEOUT_WORKER_S,
        )
        status = (payload.get("status") or "FALHA").upper()
        retriable = bool(payload.get("retriable", False))
    except RuntimeError as e:
        # Subprocess estourou (timeout / sem JSON): transitorio.
        worker_erro = str(e)
        _imprimir_err("MAIN", f"Worker estourou (cota {id_cota}, "
                              f"tentativa {tentativa}/{MAX_TENTATIVAS_COTA}): {e}")
        status, retriable = "FALHA", True
        payload = {"status": "FALHA", "retriable": True, "observacao": worker_erro}

    obs = (payload.get("observacao") or worker_erro or "")[:200]

    # ---------- log visual ----------
    if status != "FALHA":
        _imprimir("MAIN", f"  ✓ tentativa {tentativa}/{MAX_TENTATIVAS_COTA}  {status}"
                          + (f" — {obs[:100]}" if obs else ""))
    else:
        tag = " [retriable]" if retriable else ""
        _imprimir("MAIN", f"  ✗ tentativa {tentativa}/{MAX_TENTATIVAS_COTA}  FALHA{tag}"
                          + (f" — {obs[:100]}" if obs else ""))

    # ---------- resultado definitivo (nao e FALHA) ----------
    if status != "FALHA":
        # Fallback de seguranca: para NAO_BAIXADO/ADIANTADO tenta gravar
        # caso o worker tenha falhado silenciosamente no DB.
        if status in ("NAO_BAIXADO", "ADIANTADO"):
            try:
                from shared.sql_funcoes import finalizar_cota_resultado
                finalizar_cota_resultado(
                    id_cota=id_cota,
                    status=status,
                    observacao=payload.get("observacao"),
                    caminho_boleto=None,
                    caminho_evidencia=payload.get("caminho_evidencia_falha"),
                    parcelas_atraso=payload.get("parcelas_atraso"),
                )
            except Exception as e:
                # "cota ja finalizada" e comportamento normal quando o
                # worker ja gravou — logamos mas nao abortamos.
                _imprimir_err(
                    "MAIN",
                    f"  cota {id_cota}: fallback DB para {status}: "
                    f"{type(e).__name__}: {e}",
                )

        # Fecha outras linhas PENDENTE do mesmo grupo/cota (retries de runs anteriores)
        # para evitar reprocessamento duplicado.
        try:
            from shared.sql_funcoes import fechar_pendentes_mesmo_grupo_cota
            _fechados = fechar_pendentes_mesmo_grupo_cota(
                id_cota, status, f"Fechado automaticamente — mesmo grupo/cota ja {status}"
            )
            if _fechados:
                _imprimir("MAIN", f"  ↳ {_fechados} linha(s) PENDENTE do mesmo grupo/cota fechada(s) como {status}")
        except Exception as e:
            _imprimir_err("MAIN", f"  cota {id_cota}: erro ao fechar pendentes duplicados: {type(e).__name__}: {e}")

        return status, False

    # ---------- FALHA: decide retry ou definitivo ----------
    evidencia = payload.get("caminho_evidencia_falha") or ""

    if tentativa < MAX_TENTATIVAS_COTA:
        # Non-retriable com tentativas restantes: o worker ja gravou FALHA
        # definitiva no banco — nao retentar, encerra aqui.
        if not retriable:
            return "FALHA", False

        # Retriable: reseta o MESMO registro para PENDENTE com nova tentativa.
        # Nao cria nova linha — o orquestrador pegara o mesmo id_cota na
        # proxima chamada a buscar_proxima_cota_pendente.
        try:
            from shared.sql_funcoes import atualizar_cota_para_retry
            atualizar_cota_para_retry(id_cota, tentativa + 1)
            _imprimir("MAIN",
                      f"  ↺ retry — id_cota={id_cota} voltou para PENDENTE "
                      f"(tentativa {tentativa + 1}/{MAX_TENTATIVAS_COTA})")
        except Exception as e:
            _imprimir_err("MAIN",
                          f"  cota {id_cota}: falha ao atualizar para retry: "
                          f"{type(e).__name__}: {e}")

        return "FALHA", True

    # --- Ultima tentativa: FALHA definitiva com formato [N/N] ---
    obs_final = f"FALHA [{tentativa}/{MAX_TENTATIVAS_COTA}] — {obs}"
    _imprimir_err("MAIN",
                  f"Cota {id_cota} esgotou {MAX_TENTATIVAS_COTA} tentativas. "
                  f"obs={obs_final!r}")

    if retriable:
        # Worker nao gravou — finalizamos com a mensagem final.
        try:
            from shared.sql_funcoes import finalizar_cota_falha
            finalizar_cota_falha(id_cota, obs_final, evidencia)
        except Exception as e:
            _imprimir_err("MAIN",
                          f"  cota {id_cota}: falha ao gravar FALHA final: "
                          f"{type(e).__name__}: {e}")
    else:
        # Worker ja gravou com observacao propria — apenas atualizamos
        # para incluir o prefixo [N/N].
        try:
            from shared.sql_funcoes import atualizar_observacao_cota
            atualizar_observacao_cota(id_cota, obs_final)
        except Exception as e:
            _imprimir_err("MAIN",
                          f"  cota {id_cota}: falha ao atualizar obs final: "
                          f"{type(e).__name__}: {e}")

    return "FALHA", False


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60, flush=True)
    print(" RPA gerar boleto AVAPRO - orquestrador", flush=True)
    print("=" * 60, flush=True)
    print(" Teclas: ESPACO = pausar na proxima acao / retomar", flush=True)
    print("         Ctrl+C = encerrar suavemente (lote fica PAUSADO)", flush=True)
    print("=" * 60, flush=True)

    if len(sys.argv) < 2:
        _imprimir_err("MAIN", "uso: python main.py MODALIDADE")
        _imprimir_err("MAIN", f"valores aceitos: {sorted(MODALIDADES_VALIDAS)}")
        return 2

    try:
        modalidade = _validar_modalidade(sys.argv[1])
    except ValueError as e:
        _imprimir_err("MAIN", str(e))
        return 2

    _imprimir("MAIN", f"modalidade={modalidade}")

    # Importa sql_funcoes uma vez so, fora do loop.
    try:
        from shared.sql_funcoes import (
            buscar_proxima_cota_pendente,
            listar_cotas_nao_encontradas,
        )
    except Exception as e:
        _imprimir_err("MAIN", f"Falha ao importar sql_funcoes: {e}")
        return 1

    adm_concluidos = 0

    # ============================================================
    # Loop multi-ADM: roda um ciclo por ADM disponivel ate acabar.
    # Encerra quando ENTRADA retorna SEM_LOTE ou ocorre erro grave
    # de login (nao tenta o proximo ADM em erro de login).
    # ============================================================
    while True:

        # Pausa por teclado (ESPACO) antes de reservar o proximo ADM.
        _checar_pausa_teclado("antes da ENTRADA / proximo ADM")

        # --- 1) ENTRADA ---
        _imprimir("MAIN", "1/4 - rodando ENTRADA (reservar ADM e criar fila)")
        try:
            entrada_payload = _rodar_script(
                secao="ENTRADA", script_path=ENTRADA_MAIN,
                argv_extras=[modalidade], timeout_s=TIMEOUT_ENTRADA_S,
            )
        except RuntimeError as e:
            _imprimir_err("MAIN", str(e))
            return 1

        status_entrada = (entrada_payload.get("status") or "").upper()
        id_fila_adm   = entrada_payload.get("id_fila_adm")
        caminho_log   = entrada_payload.get("caminho_log")
        _id_adm_atual = entrada_payload.get("id_adm")
        _mes_ref_atual = entrada_payload.get("mes_ref")
        # caminho_base: necessário para criar o arquivo LEIA.txt pós-retry.
        # Tenta primeiro no payload da ENTRADA; se ausente, busca no banco.
        caminho_base  = entrada_payload.get("caminho_base")
        if not caminho_base and id_fila_adm:
            try:
                from shared.sql_funcoes import obter_dados_adm_por_fila as _obter_lote_cb
                _dados_lote_cb = _obter_lote_cb(id_fila_adm)
                caminho_base = (_dados_lote_cb or {}).get("caminho_base")
            except Exception:
                caminho_base = None

        # Sem mais ADMs disponiveis -> encerra o loop normalmente.
        if status_entrada == "SEM_LOTE":
            if adm_concluidos == 0:
                _imprimir("MAIN", "Nenhum ADM elegivel para esta modalidade - nada a fazer.")
                return 3
            _imprimir("MAIN", f"Nenhum ADM restante. Multi-ADM encerrado "
                              f"({adm_concluidos} ADM(s) processado(s)).")
            return 0

        # Lote criado mas sem cotas (planilha vazia) -> pula para o proximo.
        if status_entrada == "SEM_COTAS":
            _imprimir("MAIN", f"Lote {id_fila_adm} sem cotas - pulando para proximo ADM.")
            # Marca ultimo_mes_ref para nao reservar o mesmo ADM de novo no proximo ciclo.
            if _id_adm_atual and _mes_ref_atual:
                try:
                    from shared.sql_funcoes import atualizar_ultima_execucao_adm
                    atualizar_ultima_execucao_adm(_id_adm_atual, modalidade, int(_mes_ref_atual))
                    _imprimir("MAIN", f"ultimo_mes_ref atualizado (SEM_COTAS) — "
                                      f"id_adm={_id_adm_atual} mes_ref={_mes_ref_atual}")
                except Exception as _e_upd:
                    _imprimir_err("MAIN", f"Aviso: nao atualizei ultimo_mes_ref (SEM_COTAS): {_e_upd}")
            adm_concluidos += 1
            continue

        if status_entrada != "SUCESSO":
            _imprimir_err("MAIN", f"ENTRADA falhou - status={status_entrada} "
                                  f"obs={entrada_payload.get('observacao')!r}")
            return 1
        if not id_fila_adm:
            _imprimir_err("MAIN", f"ENTRADA SUCESSO mas sem id_fila_adm: {entrada_payload}")
            return 1

        total_cotas = int(entrada_payload.get("total_cotas") or 0)

        # Banner visual do lote reservado.
        print("", flush=True)
        print("+" + "-" * 58 + "+", flush=True)
        print(f"|  ENTRADA CONCLUIDA  -  {total_cotas:>4} cota(s) na fila"
              f"{' ' * max(0, 58 - 24 - 4 - 14)}|", flush=True)
        print(f"|  id_fila_adm = {id_fila_adm}"
              f"{' ' * max(0, 58 - 17 - len(str(id_fila_adm)))}|", flush=True)
        print("+" + "-" * 58 + "+", flush=True)
        print("", flush=True)

        _imprimir("MAIN", f"ENTRADA OK - id_fila_adm={id_fila_adm} "
                         f"total_cotas={total_cotas} caminho_log={caminho_log}")

        # --- 2) LOGIN ---
        _imprimir("MAIN", "2/4 - rodando LOGIN (AVAPRO via Edge CDP)")
        if not _rodar_login(id_fila_adm):
            # Erro grave de login: login.py ja marca o lote como FALHA no banco
            # (via finalizar_fila_adm) antes de retornar o JSON de falha.
            # Nao tenta fechar de novo aqui - apenas para o multi-ADM.
            _imprimir_err("MAIN", "LOGIN falhou - encerrando multi-ADM (lote fechado pelo login.py).")
            return 1
        _imprimir("MAIN", "LOGIN OK")

        # --- 3) PROCESSAMENTO ---
        _imprimir("MAIN", f"3/4 - PROCESSAMENTO - loop de cotas (id_fila_adm={id_fila_adm})")

        resumo = {"BAIXADO": 0, "NAO_BAIXADO": 0, "ADIANTADO": 0, "FALHA": 0}
        ids_processadas: set = set()
        iter_count = 0
        # Flag: se a ultima cota terminou em FALHA retriable, re-loga antes
        # de processar a proxima (recupera Edge morto / sessao expirada).
        precisa_relogin = False

        # Inicializa rastreamento de cotas fora da planilha (para DIFF visual).
        try:
            registros_iniciais = listar_cotas_nao_encontradas(id_fila_adm)
            nao_encontradas_acumuladas: set = {
                (r["grupo"], r["cota"]) for r in registros_iniciais
            }
        except Exception as e:
            _imprimir_err("MAIN", f"Aviso: nao consegui ler nao_encontradas inicial: {e}")
            nao_encontradas_acumuladas = set()

        while iter_count < MAX_COTAS_POR_LOTE:
            # Pausa por teclado (ESPACO) ENTRE cotas — nunca no meio de uma.
            _checar_pausa_teclado("entre cotas")

            # Checa flag de parada suave (Ctrl+C) ENTRE cotas — nunca no meio.
            if _PARAR:
                _imprimir("MAIN", "Parada solicitada — marcando lote como PAUSADO no banco.")
                try:
                    from shared.sql_funcoes import pausar_fila_adm
                    pausar_fila_adm(id_fila_adm)
                    _imprimir("MAIN", f"Lote {id_fila_adm} marcado como PAUSADO. Encerrando.")
                except Exception as _e_pause:
                    _imprimir_err("MAIN", f"Falha ao pausar no banco: {_e_pause}")
                return 0

            iter_count += 1
            try:
                proxima = buscar_proxima_cota_pendente(id_fila_adm)
            except Exception as e:
                _imprimir_err("MAIN", f"Erro ao consultar proxima cota: {e}")
                # Se for falha de conexao com o banco, aguarda o banco voltar
                # antes de desistir. Cobre instabilidades do Aiven (timeout/queda).
                _e_str = str(e).lower()
                if any(k in _e_str for k in ("timeout", "connection", "operational")):
                    from entrada.lib.db import aguardar_banco_disponivel
                    if aguardar_banco_disponivel():
                        # Banco voltou — tenta buscar novamente
                        try:
                            proxima = buscar_proxima_cota_pendente(id_fila_adm)
                        except Exception as e2:
                            _imprimir_err("MAIN", f"Banco voltou mas falhou de novo: {e2}")
                            return 1
                    else:
                        _imprimir_err("MAIN", "Banco indisponivel por muito tempo — encerrando.")
                        return 1
                else:
                    return 1

            if not proxima:
                _imprimir("MAIN", f"Sem mais cotas PENDENTE - fim do loop "
                                  f"({iter_count - 1} iteracao(oes)).")
                break

            id_cota      = proxima.get("id_cota")
            nome_cliente = proxima.get("nome_cliente")
            grupo        = proxima.get("grupo")
            cota         = proxima.get("cota")
            tentativa    = int(proxima.get("tentativas") or 1)

            if id_cota is None:
                _imprimir_err("MAIN", f"Proxima cota sem id_cota: {proxima!r}")
                return 1

            # Proteção anti-loop: rastreia (id_cota, tentativa). O mesmo
            # id_cota pode aparecer várias vezes com tentativas crescentes
            # (novo modelo: atualiza em vez de inserir nova linha no banco).
            # O par (id_cota, tentativa) idêntico indica loop real.
            _chave_iter = (id_cota, tentativa)
            if _chave_iter in ids_processadas:
                _imprimir_err(
                    "MAIN",
                    f"id_cota={id_cota} tentativa={tentativa} servido 2x — worker "
                    f"nao atualizou o registro. Abortando para nao loopar.",
                )
                return 1
            ids_processadas.add(_chave_iter)

            # Re-login apos FALHA retriable (recupera Edge / sessao).
            # Tenta ate 3 vezes com espera crescente entre elas.
            # Em cada falha tira print e salva em Evidencias/FALHAS/FALHA_LOGIN.
            # Se uma tentativa der certo apaga os prints anteriores da pasta.
            # Se esgotar as 3 tentativas marca o lote como FALHA e vai pro proximo ADM.
            if precisa_relogin:
                import shutil as _shutil
                _MAX_TENT_RELOGIN = 3
                _ESPERAS_RELOGIN  = [0, 120, 300]  # segundos antes de cada tentativa (1a imediata)
                _pasta_falha_login: Optional["Path"] = None
                _prints_relogin: list = []
                _relogin_ok = False

                for _tent_rel in range(1, _MAX_TENT_RELOGIN + 1):
                    _espera = _ESPERAS_RELOGIN[_tent_rel - 1]
                    if _espera > 0:
                        _imprimir("MAIN",
                                  f"  Re-login tentativa {_tent_rel}/{_MAX_TENT_RELOGIN}: "
                                  f"aguardando {_espera // 60} min antes de tentar...")
                        time.sleep(_espera)

                    _imprimir("MAIN",
                              f"  Re-login tentativa {_tent_rel}/{_MAX_TENT_RELOGIN}...")
                    _ok = _rodar_login(id_fila_adm)

                    if not _ok:
                        # Cria a pasta FALHA_LOGIN apenas na primeira falha real
                        if _pasta_falha_login is None and caminho_base:
                            try:
                                from pathlib import Path as _Path
                                _pasta_falha_login = _Path(caminho_base) / "Evidencias" / "FALHAS" / "FALHA_LOGIN"
                                _pasta_falha_login.mkdir(parents=True, exist_ok=True)
                            except Exception:
                                _pasta_falha_login = None
                        # Captura print de evidencia desta tentativa falha
                        if _pasta_falha_login is not None:
                            try:
                                from playwright.sync_api import sync_playwright as _swp
                                import requests as _req
                                _r = _req.get("http://127.0.0.1:9222/json/version", timeout=2)
                                if _r.status_code == 200:
                                    with _swp() as _pw:
                                        _br = _pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
                                        if _br.contexts:
                                            _pg = _br.contexts[0].pages[0]
                                            _ts = time.strftime("%Y%m%d_%H%M%S")
                                            _p_print = _pasta_falha_login / f"ReLogin_T{_tent_rel}_{_ts}.png"
                                            _pg.screenshot(path=str(_p_print), full_page=True)
                                            _prints_relogin.append(_p_print)
                                            _imprimir("MAIN", f"  Print salvo: {_p_print.name}")
                            except Exception as _e_print:
                                _imprimir_err("MAIN", f"  Aviso: nao consegui tirar print do re-login: {_e_print}")

                    if _ok:
                        _relogin_ok = True
                        # Apaga prints de tentativas anteriores (deu certo)
                        if _prints_relogin and _pasta_falha_login is not None:
                            try:
                                _shutil.rmtree(str(_pasta_falha_login), ignore_errors=True)
                                _imprimir("MAIN",
                                          f"  Re-login OK na tentativa {_tent_rel} — "
                                          f"pasta FALHA_LOGIN apagada.")
                            except Exception:
                                pass
                        else:
                            _imprimir("MAIN", f"  Re-login OK na tentativa {_tent_rel}.")
                        break

                    _imprimir_err("MAIN",
                                  f"  Re-login tentativa {_tent_rel}/{_MAX_TENT_RELOGIN} falhou.")

                precisa_relogin = False

                if not _relogin_ok:
                    # Esgotou tentativas — trava o lote e vai pro proximo ADM
                    _imprimir_err("MAIN",
                                  f"Re-login falhou {_MAX_TENT_RELOGIN} vezes — "
                                  f"marcando lote {id_fila_adm} como FALHA e avancando.")
                    try:
                        from shared.sql_funcoes import finalizar_fila_adm as _fin_fila
                        _fin_fila(id_fila_adm, "FALHA",
                                  f"Re-login falhou {_MAX_TENT_RELOGIN} tentativas consecutivas — fila TRAVADA.")
                    except Exception as _e_fin:
                        _imprimir_err("MAIN", f"Falha ao marcar lote como FALHA: {_e_fin}")
                    try:
                        from shared.notificador import notificar_falha as _notif_falha
                        _notif_falha(
                            etapa="RE-LOGIN — FILA TRAVADA",
                            erro=RuntimeError(
                                f"Re-login falhou {_MAX_TENT_RELOGIN} vezes. "
                                f"Lote {id_fila_adm} travado."
                            ),
                            id_fila_adm=id_fila_adm,
                            caminho_log=caminho_log,
                            nivel="ALERTA_MAXIMO",
                            contexto_extra=(
                                f"O robô tentou re-logar {_MAX_TENT_RELOGIN} vezes "
                                f"(esperas: 2 min e 5 min entre tentativas) e não conseguiu.\n"
                                f"Prints salvos em: {_pasta_falha_login or 'indisponivel'}\n\n"
                                f"AÇÃO NECESSÁRIA:\n"
                                f"1. Verifique as credenciais do ADM no banco\n"
                                f"2. Teste o login manualmente no AVAPRO\n"
                                f"3. Corrija e reinicie o RPA"
                            ),
                            anexos_extras=[str(p) for p in _prints_relogin if p.exists()] or None,
                        )
                    except Exception as _e_notif:
                        _imprimir_err("MAIN", f"Falha ao enviar email de fila travada: {_e_notif}")
                    break  # sai do loop de cotas -> continua pro proximo ADM

            contexto_cota = (
                f"id_cota={id_cota} cliente={nome_cliente!r} grupo={grupo} cota={cota} "
                f"tentativa={tentativa}/{MAX_TENTATIVAS_COTA}"
            )
            _imprimir("MAIN", f"Cota {iter_count}: {contexto_cota}")

            status_final, retriable = _processar_cota_uma_vez(
                id_cota=id_cota,
                tentativa=tentativa,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
            )
            resumo[status_final] = resumo.get(status_final, 0) + 1

            # Boleto emitido após retentativa(s): apaga a pasta de falha da cota
            # (deu certo, não precisa deixar rastro de erro).
            if status_final == "BAIXADO" and caminho_base:
                try:
                    import shutil as _shutil
                    from processamento.lib.arquivos import pasta_falha_cota as _pasta_falha_cota
                    _pasta_falha = _pasta_falha_cota(caminho_base, nome_cliente or "", grupo, cota)
                    if _pasta_falha.exists():
                        _shutil.rmtree(_pasta_falha, ignore_errors=True)
                        _imprimir("MAIN",
                                  f"  🗑 Pasta de falha apagada (boleto baixado com sucesso): "
                                  f"{_pasta_falha.name}")
                except Exception as _e_del:
                    _imprimir_err("MAIN",
                                  f"  Aviso: nao consegui apagar pasta de falha: "
                                  f"{type(_e_del).__name__}: {_e_del}")
            _icone_final = {"BAIXADO": "✓", "ADIANTADO": "~", "NAO_BAIXADO": "~"}.get(
                status_final, "✗"
            )
            _imprimir("MAIN", f"  {_icone_final} FINAL: {status_final}  (cota {id_cota})")

            # Se FALHA retriable, agenda re-login para a proxima iteracao.
            if status_final == "FALHA" and retriable:
                precisa_relogin = True

            # DIFF visual: cotas novas detectadas no AVAPRO apos esta cota.
            try:
                registros = listar_cotas_nao_encontradas(id_fila_adm)
                chaves_atual = {(r["grupo"], r["cota"]) for r in registros}
                novos_chaves = chaves_atual - nao_encontradas_acumuladas
                if novos_chaves:
                    novos_reg = [r for r in registros
                                 if (r["grupo"], r["cota"]) in novos_chaves]
                    novos_reg.sort(key=lambda r: (r["grupo"], r["cota"]))
                    _imprimir_nao_encontradas_novas(
                        nome_cliente_atual=nome_cliente,
                        novos_registros=novos_reg,
                    )
                    nao_encontradas_acumuladas = chaves_atual
            except Exception as e:
                _imprimir_err("MAIN", f"Aviso: nao consegui ler nao_encontradas: {e}")

        if iter_count >= MAX_COTAS_POR_LOTE:
            _imprimir_err("MAIN", f"Loop atingiu MAX_COTAS_POR_LOTE={MAX_COTAS_POR_LOTE}.")

        _imprimir(
            "MAIN",
            f"PROCESSAMENTO concluido - BAIXADO={resumo['BAIXADO']} "
            f"NAO_BAIXADO={resumo['NAO_BAIXADO']} ADIANTADO={resumo['ADIANTADO']} "
            f"FALHA={resumo['FALHA']}",
        )

        # Sumario final visual das cotas fora da planilha.
        try:
            todas_nao_encontradas = listar_cotas_nao_encontradas(id_fila_adm)
            if todas_nao_encontradas:
                _imprimir_sumario_nao_encontradas(todas_nao_encontradas)
        except Exception as e:
            _imprimir_err("MAIN", f"Aviso: sumario nao_encontradas falhou: {e}")

        # Pausa por teclado (ESPACO) antes da etapa de saida.
        _checar_pausa_teclado("antes da SAIDA")

        # --- 4) SAIDA ---
        _imprimir("MAIN", "4/4 - rodando SAIDA (drive + email + planilha + fechamento)")
        try:
            saida_payload = _rodar_script(
                secao="SAIDA", script_path=SAIDA_MAIN,
                argv_extras=[id_fila_adm], timeout_s=TIMEOUT_SAIDA_S,
            )
        except RuntimeError as e:
            _imprimir_err("MAIN", str(e))
            return 1

        status_saida = (saida_payload.get("status") or "").upper()
        if status_saida != "SUCESSO":
            _imprimir_err("MAIN", f"SAIDA com problemas - status={status_saida} "
                                  f"obs={saida_payload.get('observacao')!r} "
                                  f"etapas={saida_payload.get('etapas')}")
            return 1

        _imprimir("MAIN", f"SAIDA OK - link_drive={saida_payload.get('link_drive')} "
                          f"metricas={saida_payload.get('metricas')}")

        # Grava ultimo_mes_ref para evitar que o mesmo ADM seja reservado de novo.
        if _id_adm_atual and _mes_ref_atual:
            try:
                from shared.sql_funcoes import atualizar_ultima_execucao_adm
                atualizar_ultima_execucao_adm(_id_adm_atual, modalidade, int(_mes_ref_atual))
                _imprimir("MAIN", f"ultimo_mes_ref atualizado — id_adm={_id_adm_atual} "
                                  f"modalidade={modalidade} mes_ref={_mes_ref_atual}")
            except Exception as _e_upd:
                _imprimir_err("MAIN", f"Aviso: nao atualizei ultimo_mes_ref: {_e_upd}")

        adm_concluidos += 1
        _imprimir("MAIN", f"Ciclo ADM #{adm_concluidos} concluido. Buscando proximo ADM...")
        print("", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _imprimir_err("MAIN", "Interrompido pelo usuario (Ctrl+C)")
        sys.exit(130)
    except Exception as e:
        _imprimir_err("MAIN", f"Excecao toplevel: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
