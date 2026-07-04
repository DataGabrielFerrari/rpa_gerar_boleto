"""
ENTRADA / Orquestrador chamado pelo PAD 1x por execucao.

Recebe MODALIDADE em argv[1] (MOTORS|IMOVEL).

Fluxo:
  1) housekeeping (marca lotes parados como FALHA)
  2) tenta retomar lote interrompido do mes (PENDENTE/FALHA)
  3) se nao houver, reserva proximo ADM elegivel e cria fila
  4) cria estrutura de pastas e log
  5) calcula data_vencimento (depende de mes_ref + modalidade)
  6) le planilha e enfileira cotas
  7) imprime JSON em stdout para o PAD consumir

Saida (stdout): JSON unica linha
{
  "status": "SUCESSO|SEM_LOTE|SEM_COTAS|FALHA",
  "id_fila_adm": int|null,
  "caminho_log": str|null,
  "observacao": str
}
"""

import os
import sys
import re
import json
import getpass
import traceback

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

SRC_DIR = os.path.dirname(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if load_dotenv:
    load_dotenv(os.path.join(ROOT_DIR, ".env"), override=True)

from config.modalidades import validar_modalidade
from shared.log import log_info
from shared.notificador import notificar_falha
from shared.sql_funcoes import (
    marcar_lotes_parados_como_falha,
    reservar_lote_interrompido,
    reservar_proximo_adm_e_criar_fila,
    atualizar_caminhos_fila_adm,
    atualizar_data_vencimento_fila_adm,
    obter_dados_adm_por_fila,
    finalizar_fila_adm,
    obter_parametro_int,
    contar_cotas_pendentes,
)
from entrada.lib.vencimento import calcular_vencimento
from entrada.lib.leitor_planilha import ler_planilhas, ColunaFaltandoPlanilha


DEFAULT_AUTO_UNLOCK_MINUTOS = 10


def _emitir_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()


def _payload_minimo(
    status: str,
    id_fila_adm=None,
    caminho_log=None,
    observacao: str = "",
    total_cotas: int = 0,
    id_adm=None,
    mes_ref=None,
) -> dict:
    return {
        "status": status,
        "id_fila_adm": id_fila_adm,
        "caminho_log": caminho_log,
        "observacao": observacao,
        "total_cotas": int(total_cotas or 0),
        "id_adm": id_adm,
        "mes_ref": mes_ref,
    }


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _get_usuario_windows() -> str:
    usuario = (os.environ.get("USERNAME") or "").strip()
    if usuario:
        return usuario
    try:
        return (getpass.getuser() or "").strip() or "DESCONHECIDO"
    except Exception:
        return "DESCONHECIDO"


def _sanitize_folder_name(nome: str) -> str:
    nome = (nome or "").strip()
    nome = re.sub(r'[\\/:*?"<>|]', "_", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    return nome or "SEM_NOME"


def _get_lotes_root() -> str:
    # Raiz onde os lotes sao criados. Ex. de estrutura final:
    #   {LOTES_ROOT}\{Nome_ADM}_{id_adm}\{MODALIDADE}\fila_{id_fila_adm}
    #
    # 1) Se LOTES_ROOT estiver no .env, usa exatamente esse valor.
    # 2) Senao, detecta o Google Drive automaticamente. O Drive monta como:
    #      - espelho:   C:\Users\<user>\My Drive  (ou "Meu Drive" em PT-BR)
    #      - streaming: G:\My Drive  /  G:\Meu Drive  (a letra pode variar)
    #    Testamos os candidatos e usamos o primeiro cujo Drive exista.
    lotes_root = (os.environ.get("LOTES_ROOT") or "").strip()
    if lotes_root:
        return os.path.abspath(lotes_root)

    subpasta = "lotes_boleto"
    nomes_drive = ("My Drive", "Meu Drive")
    usuario = os.environ.get("USERNAME") or "adminrpa"

    candidatos_drive = []
    # Espelho (mirror) dentro do perfil do usuario
    for nome in nomes_drive:
        candidatos_drive.append(os.path.join("C:\\", "Users", usuario, nome))
    # Streaming em letras de unidade (G:, H:, ... e ate Z:)
    for letra in ("G", "H", "I", "J", "K", "L", "M"):
        for nome in nomes_drive:
            candidatos_drive.append(f"{letra}:\\{nome}")

    for base_drive in candidatos_drive:
        if os.path.isdir(base_drive):
            return os.path.abspath(os.path.join(base_drive, subpasta))

    # Fallback: caminho de espelho padrao (mesmo que ainda nao exista, sera criado)
    return os.path.abspath(
        os.path.join("C:\\", "Users", usuario, "My Drive", subpasta)
    )


def _montar_log_path(nome_adm: str, id_adm, id_fila_adm: int) -> str:
    """
    Log centralizado FORA da pasta do lote:
      {lotes}\\log\\{nome_adm}_{id_adm}\\log_{id_fila_adm}.txt

    Estrutura: dentro de 'lotes' existe a pasta 'log' com uma subpasta
    por ADM (mesmo nome das pastas de lote) e um arquivo por fila.
    """
    lotes_root = _get_lotes_root()
    pasta_adm = f"{_sanitize_folder_name(nome_adm)}_{id_adm}"
    log_dir = os.path.join(lotes_root, "log", pasta_adm)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"log_{id_fila_adm}.txt")


def _criar_estrutura_lote(
    nome_adm: str, id_adm: int, modalidade: str, id_fila_adm: int
) -> dict:
    lotes_root = _get_lotes_root()
    os.makedirs(lotes_root, exist_ok=True)

    pasta_adm = f"{_sanitize_folder_name(nome_adm)}_{id_adm}"
    pasta_modalidade = modalidade
    pasta_fila = f"fila_{id_fila_adm}"

    lote_dir = os.path.join(lotes_root, pasta_adm, pasta_modalidade, pasta_fila)
    boletos_dir = os.path.join(lote_dir, "Boletos")
    evidencias_dir = os.path.join(lote_dir, "Evidencias")

    os.makedirs(boletos_dir, exist_ok=True)
    os.makedirs(evidencias_dir, exist_ok=True)

    # Log centralizado: {lotes}\log\{nome_adm}_{id_adm}\log_{id_fila_adm}.txt
    # (fora da pasta do lote — subpasta por ADM dentro de lotes\log)
    log_txt_path = _montar_log_path(nome_adm, id_adm, id_fila_adm)
    if not os.path.exists(log_txt_path):
        with open(log_txt_path, "w", encoding="utf-8") as arq:
            arq.write(
                f"# LOG - {nome_adm} | id_fila_adm={id_fila_adm} "
                f"| modalidade={modalidade}\n"
            )

    return {
        "lote_dir": lote_dir,
        "boletos_dir": boletos_dir,
        "evidencias_dir": evidencias_dir,
        "log_dir": os.path.dirname(log_txt_path),
        "log_txt_path": log_txt_path,
    }


def _executar(modalidade: str, maquina: str) -> dict:
    auto_unlock_min = obter_parametro_int(
        "auto_unlock_minutos", DEFAULT_AUTO_UNLOCK_MINUTOS
    )

    afetados = marcar_lotes_parados_como_falha(auto_unlock_min)
    if afetados:
        _log_stderr(
            f"[AUTO-UNLOCK] {len(afetados)} lote(s) -> FALHA "
            f"por inatividade > {auto_unlock_min} min."
        )

    lote = reservar_lote_interrompido(modalidade, maquina)
    retomado = bool(lote)

    if retomado:
        id_fila_adm = lote["id_fila_adm"]
        _log_stderr(f"[RETOMADO] id_fila_adm={id_fila_adm}")

        dados = obter_dados_adm_por_fila(id_fila_adm)
        if not dados:
            return _payload_minimo(
                status="FALHA",
                id_fila_adm=id_fila_adm,
                caminho_log=None,
                observacao="Lote retomado nao encontrado em obter_dados_adm_por_fila",
            )

        if dados.get("caminho_base"):
            # Se o lote antigo ja tinha caminho_log no banco, mantem (consistencia
            # dentro do mesmo lote). Se nao tinha, usa o novo padrao centralizado
            # {lotes}\log\{adm}\log_{id_fila_adm}.txt e grava no banco.
            _log_existente = dados.get("caminho_log")
            paths = {
                "lote_dir": dados["caminho_base"],
                "log_txt_path": _log_existente or _montar_log_path(
                    dados["nome"], dados["id_adm"], id_fila_adm
                ),
            }
            os.makedirs(paths["lote_dir"], exist_ok=True)
            log_dir = os.path.dirname(paths["log_txt_path"])
            os.makedirs(log_dir, exist_ok=True)
            if not _log_existente:
                atualizar_caminhos_fila_adm(
                    id_fila_adm,
                    caminho_base=paths["lote_dir"],
                    caminho_log=paths["log_txt_path"],
                )
        else:
            paths = _criar_estrutura_lote(
                dados["nome"], dados["id_adm"], dados["modalidade"], id_fila_adm
            )
            atualizar_caminhos_fila_adm(
                id_fila_adm,
                caminho_base=paths["lote_dir"],
                caminho_log=paths["log_txt_path"],
            )

        log_info(
            paths["log_txt_path"],
            etapa="ENTRADA",
            id_dado=id_fila_adm,
            acao="Retomar lote",
            detalhe=f"modalidade={modalidade}",
        )

        if not dados.get("data_vencimento"):
            data_venc = calcular_vencimento(int(dados["mes_ref"]), modalidade)
            atualizar_data_vencimento_fila_adm(id_fila_adm, data_venc)

        # Em retomada, ler_planilhas vai dedupar contra o que ja foi inserido
        # antes e retornar 0 — isso NAO significa "nenhuma cota pra processar".
        # O que importa pra decidir SEM_COTAS e quantas cotas seguem como
        # PENDENTE em tbl_fila_cotas. Sem essa checagem, a retomada finaliza
        # o lote como SUCESSO mesmo com cotas pendentes no banco.
        try:
            ler_planilhas(id_fila_adm, log_txt_path=paths["log_txt_path"])
        except ColunaFaltandoPlanilha as e_col:
            # Coluna essencial faltando no cabecalho: email ja enviado pelo
            # leitor. Marca o lote como FALHA e segue para o proximo ADM.
            finalizar_fila_adm(id_fila_adm, "FALHA", str(e_col))
            return _payload_minimo(
                status="FALHA",
                id_fila_adm=id_fila_adm,
                caminho_log=paths["log_txt_path"],
                observacao=str(e_col),
            )

        qtd_pendentes = contar_cotas_pendentes(id_fila_adm)

        log_info(
            paths["log_txt_path"],
            etapa="ENTRADA",
            id_dado=id_fila_adm,
            acao="Cotas pendentes ao RETOMAR o lote",
            detalhe=f"qtd_pendentes={qtd_pendentes} modalidade={modalidade}",
        )

        if qtd_pendentes == 0:
            finalizar_fila_adm(
                id_fila_adm, "SUCESSO", "Nenhuma cota para processar"
            )
            return _payload_minimo(
                status="SEM_COTAS",
                id_fila_adm=id_fila_adm,
                caminho_log=paths["log_txt_path"],
                observacao="Nenhuma cota para processar",
                total_cotas=0,
            )

        return _payload_minimo(
            status="SUCESSO",
            id_fila_adm=id_fila_adm,
            caminho_log=paths["log_txt_path"],
            observacao=f"Lote retomado ({qtd_pendentes} cota(s) PENDENTE)",
            total_cotas=qtd_pendentes,
        )

    novo = reservar_proximo_adm_e_criar_fila(modalidade, maquina)
    if not novo:
        _log_stderr("[SEM_LOTE] Nenhum ADM elegivel para a modalidade")
        return _payload_minimo(
            status="SEM_LOTE",
            id_fila_adm=None,
            caminho_log=None,
            observacao="Nenhum ADM elegivel para a modalidade",
        )

    id_fila_adm = novo["id_fila_adm"]
    id_adm = novo["id_adm"]
    nome_adm = novo["nome"]
    mes_ref = int(novo["mes_ref"])

    paths = _criar_estrutura_lote(nome_adm, id_adm, modalidade, id_fila_adm)
    atualizar_caminhos_fila_adm(
        id_fila_adm,
        caminho_base=paths["lote_dir"],
        caminho_log=paths["log_txt_path"],
    )

    data_venc = calcular_vencimento(mes_ref, modalidade)
    atualizar_data_vencimento_fila_adm(id_fila_adm, data_venc)

    log_info(
        paths["log_txt_path"],
        etapa="ENTRADA",
        id_dado=id_fila_adm,
        acao="Criar lote",
        detalhe=f"id_adm={id_adm} nome={nome_adm} modalidade={modalidade}",
    )

    try:
        total_cotas = ler_planilhas(id_fila_adm, log_txt_path=paths["log_txt_path"])
    except ColunaFaltandoPlanilha as e_col:
        # Coluna essencial faltando no cabecalho: email ja enviado pelo leitor.
        # Marca o lote recem-criado como FALHA e segue para o proximo ADM.
        finalizar_fila_adm(id_fila_adm, "FALHA", str(e_col))
        return _payload_minimo(
            status="FALHA",
            id_fila_adm=id_fila_adm,
            caminho_log=paths["log_txt_path"],
            observacao=str(e_col),
        )

    try:
        _qtd_pend_inicio = contar_cotas_pendentes(id_fila_adm)
    except Exception:
        _qtd_pend_inicio = total_cotas
    log_info(
        paths["log_txt_path"],
        etapa="ENTRADA",
        id_dado=id_fila_adm,
        acao="Cotas pendentes ao INICIAR o lote",
        detalhe=(
            f"qtd_pendentes={_qtd_pend_inicio} total_lidas_planilha={total_cotas} "
            f"modalidade={modalidade} id_adm={id_adm} nome_adm={nome_adm}"
        ),
    )

    if total_cotas == 0:
        finalizar_fila_adm(id_fila_adm, "SUCESSO", "Nenhuma cota para processar")
        return _payload_minimo(
            status="SEM_COTAS",
            id_fila_adm=id_fila_adm,
            caminho_log=paths["log_txt_path"],
            observacao="Nenhuma cota para processar",
            total_cotas=0,
        )

    return _payload_minimo(
        status="SUCESSO",
        id_fila_adm=id_fila_adm,
        caminho_log=paths["log_txt_path"],
        observacao="Lote criado",
        total_cotas=total_cotas,
        id_adm=id_adm,
        mes_ref=mes_ref,
    )


def main() -> int:
    if len(sys.argv) < 2:
        _emitir_json(
            _payload_minimo(
                status="FALHA",
                id_fila_adm=None,
                caminho_log=None,
                observacao="Argumento MODALIDADE nao recebido (argv[1])",
            )
        )
        return 1

    try:
        modalidade = validar_modalidade(sys.argv[1])
    except ValueError as e:
        _emitir_json(
            _payload_minimo(
                status="FALHA",
                id_fila_adm=None,
                caminho_log=None,
                observacao=str(e),
            )
        )
        return 1

    maquina = _get_usuario_windows()

    try:
        payload = _executar(modalidade, maquina)
    except Exception as e:
        _log_stderr(traceback.format_exc())
        try:
            notificar_falha(
                etapa="ENTRADA",
                erro=e,
                id_fila_adm=None,
                caminho_log=None,
                script_path=__file__,
                contexto_extra=f"modalidade={modalidade} maquina={maquina}",
            )
        except Exception:
            pass

        _emitir_json(
            _payload_minimo(
                status="FALHA",
                id_fila_adm=None,
                caminho_log=None,
                observacao=f"{type(e).__name__}: {e}",
            )
        )
        return 1
    _emitir_json(payload)
    return 0 if payload["status"] in ("SUCESSO", "SEM_COTAS", "SEM_LOTE") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _log_stderr(traceback.format_exc())
        _emitir_json(
            _payload_minimo(
                status="FALHA",
                id_fila_adm=None,
                caminho_log=None,
                observacao=f"Excecao toplevel: {type(e).__name__}: {e}",
            )
        )
        sys.exit(1)
