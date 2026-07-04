"""
SAIDA / pos-processamento. Chamado pelo PAD 1x ao final do lote.

Argumentos:
  argv[1] = id_fila_adm (int)

Saida (stdout): JSON unica linha
{
  "status": "SUCESSO|FALHA",
  "id_fila_adm": int,
  "id_adm": int|null,
  "modalidade": "MOTORS|IMOVEL"|null,
  "link_drive": str|null,
  "etapas": {
    "drive": "OK|ERRO",
    "email": "OK|ERRO",
    "relatorio": "OK|ERRO|NAO_EXECUTADO",
    "planilha": "OK|ERRO",
    "fechamento": "OK|ERRO"
  },
  "metricas": {
    "matched": int, "updated_status": int, "updated_obs": int,
    "not_found": int, "duplicated_keys": int
  }|null,
  "observacao": str
}
"""

import os
import sys
import json
import shutil
import traceback
from pathlib import Path

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

from shared.log import log_info, log_erro
from shared.notificador import notificar_falha
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    fechar_lote_adm,
    listar_boletos_baixados,
)

from saida.lib.drive_service import processar_drive_lote
from saida.jobs.enviar_email import enviar_email_lote
from saida.jobs.atualizar_planilha import atualizar_planilha_lote
from saida.jobs.relatorio_interno import gerar_relatorio_interno


def _emitir_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _executar(id_fila_adm: int) -> dict:
    dados = obter_dados_adm_por_fila(id_fila_adm)
    if not dados:
        return {
            "status": "FALHA",
            "id_fila_adm": id_fila_adm,
            "id_adm": None,
            "modalidade": None,
            "link_drive": None,
            "etapas": {
                "drive": "ERRO",
                "email": "ERRO",
                "planilha": "ERRO",
                "fechamento": "ERRO",
            },
            "metricas": None,
            "observacao": f"Lote {id_fila_adm} nao encontrado",
        }

    caminho_log = dados["caminho_log"]
    modalidade = dados["modalidade"]
    id_adm = dados["id_adm"]

    log_info(
        caminho_log,
        "SAIDA",
        id_fila_adm,
        "Iniciar saida",
        f"modalidade={modalidade}",
    )

    etapas = {
        "drive": "ERRO",
        "email": "ERRO",
        "relatorio": "NAO_EXECUTADO",
        "planilha": "ERRO",
        "fechamento": "ERRO",
    }
    link_drive = None
    metricas = None
    falhas = []

    # 0) Zipar Evidencias (FALHAS + ADIANTADOS) antes do Drive
    caminho_base = dados.get("caminho_base")
    if caminho_base:
        try:
            _pasta_ev = Path(caminho_base) / "Evidencias"
            if _pasta_ev.exists() and any(_pasta_ev.iterdir()):
                _zip_base = str(Path(caminho_base) / "Evidencias")
                shutil.make_archive(_zip_base, "zip", str(_pasta_ev.parent), "Evidencias")
                log_info(caminho_log, "SAIDA", id_fila_adm, "Evidencias zipadas",
                         f"arquivo=Evidencias.zip")
        except Exception as _e_zip:
            _stderr(f"ZIP Evidencias: {type(_e_zip).__name__}: {_e_zip}")
            try:
                log_erro(caminho_log, "SAIDA", id_fila_adm,
                         "Zipar Evidencias", str(_e_zip))
            except Exception:
                pass

    # 1) Verificar arquivos de boleto em disco
    try:
        _boletos_db = listar_boletos_baixados(id_fila_adm)
        _boletos_ausentes = [
            b for b in _boletos_db
            if not Path(b["caminho_boleto"]).exists()
        ]
        if _boletos_ausentes:
            _linhas = "\n".join(
                f"  - {b['nome_cliente']} | {b['grupo']}/{b['cota']} | {b['caminho_boleto']}"
                for b in _boletos_ausentes
            )
            _msg_ausentes = (
                f"{len(_boletos_ausentes)} boleto(s) marcado(s) como BAIXADO no banco "
                f"mas arquivo(s) não encontrado(s) em disco."
            )
            log_erro(caminho_log, "SAIDA", id_fila_adm,
                     "Boletos ausentes em disco", _msg_ausentes)

            class BoletoAusenteEmDisco(Exception):
                pass

            notificar_falha(
                etapa="SAIDA",
                erro=BoletoAusenteEmDisco(_msg_ausentes),
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
                script_path=__file__,
                contexto_extra=(
                    f"modalidade={modalidade}\n"
                    f"qtd_ausentes={len(_boletos_ausentes)}\n"
                    f"\nArquivos não encontrados:\n{_linhas}\n"
                    f"\nAção recomendada: verificar se os boletos foram gerados "
                    f"e se o caminho no banco está correto."
                ),
                nivel="ALERTA",
            )
        else:
            log_info(caminho_log, "SAIDA", id_fila_adm,
                     "Verificacao boletos em disco",
                     f"todos os {len(_boletos_db)} boleto(s) BAIXADO encontrados em disco")
    except Exception as _e_check:
        _stderr(f"VERIFICACAO BOLETOS: {type(_e_check).__name__}: {_e_check}")
        try:
            log_erro(caminho_log, "SAIDA", id_fila_adm,
                     "Verificar boletos em disco", str(_e_check))
        except Exception:
            pass

    # 2) Drive
    try:
        link_drive = processar_drive_lote(id_fila_adm)
        etapas["drive"] = "OK"
    except Exception as e:
        msg = f"DRIVE: {type(e).__name__}: {e}"
        falhas.append(msg)
        _stderr(msg)
        try:
            log_erro(caminho_log, "SAIDA", id_fila_adm, "Drive", str(e))
        except Exception:
            pass

    # 3) Email
    try:
        enviar_email_lote(id_fila_adm)
        etapas["email"] = "OK"
    except Exception as e:
        msg = f"EMAIL: {type(e).__name__}: {e}"
        falhas.append(msg)
        _stderr(msg)
        try:
            log_erro(caminho_log, "SAIDA", id_fila_adm, "Email", str(e))
        except Exception:
            pass

    # 3.5) Relatorio interno (apos o email do cliente) — NAO-FATAL:
    # falha no relatorio nunca derruba a saida nem o fechamento do lote.
    try:
        _rel_ok = gerar_relatorio_interno(id_fila_adm)
        etapas["relatorio"] = "OK" if _rel_ok else "ERRO"
        if not _rel_ok:
            _stderr("RELATORIO: falha ao gerar/enviar relatorio interno (ver log)")
    except Exception as e:
        etapas["relatorio"] = "ERRO"
        _stderr(f"RELATORIO: {type(e).__name__}: {e}")
        try:
            log_erro(caminho_log, "SAIDA", id_fila_adm, "Relatorio interno", str(e))
        except Exception:
            pass

    # 4) Planilha
    try:
        metricas = atualizar_planilha_lote(id_fila_adm)
        etapas["planilha"] = "OK"
    except Exception as e:
        msg = f"PLANILHA: {type(e).__name__}: {e}"
        falhas.append(msg)
        _stderr(msg)
        try:
            log_erro(caminho_log, "SAIDA", id_fila_adm, "Planilha", str(e))
        except Exception:
            pass

    # 5) Fechamento consistente com o resultado real da saida
    saida_ok = (
        etapas["drive"] == "OK"
        and etapas["email"] == "OK"
        and etapas["planilha"] == "OK"
    )

    status_lote = "SUCESSO" if saida_ok else "FALHA"
    observacao_lote = "; ".join(falhas) if falhas else None

    try:
        fechar_lote_adm(id_fila_adm, status_lote, observacao_lote)
        etapas["fechamento"] = "OK"
    except Exception as e:
        err_str = str(e)
        # Se o lote ja foi fechado por uma execucao anterior (drive/email/planilha
        # ja concluidos), tratar como OK para nao marcar saida como FALHA.
        ja_fechado = (
            "não encontrado ou não está PROCESSANDO" in err_str
            or "nao encontrado ou nao esta PROCESSANDO" in err_str
            or "Lote não encontrado" in err_str
        )
        if ja_fechado and saida_ok:
            _stderr(f"FECHAMENTO: lote {id_fila_adm} ja estava fechado — ignorando (saida OK)")
            try:
                log_info(caminho_log, "SAIDA", id_fila_adm, "Fechamento", "Lote ja fechado — ignorado (saida OK)")
            except Exception:
                pass
            etapas["fechamento"] = "OK"
        else:
            msg = f"FECHAMENTO: {type(e).__name__}: {e}"
            falhas.append(msg)
            _stderr(msg)
            try:
                log_erro(caminho_log, "SAIDA", id_fila_adm, "Fechamento", str(e))
            except Exception:
                pass

    status_final = (
        "SUCESSO"
        if saida_ok and etapas["fechamento"] == "OK"
        else "FALHA"
    )
    observacao = "; ".join(falhas) if falhas else "Saida concluida"

    return {
        "status": status_final,
        "id_fila_adm": id_fila_adm,
        "id_adm": id_adm,
        "modalidade": modalidade,
        "link_drive": link_drive,
        "etapas": etapas,
        "metricas": metricas,
        "observacao": observacao,
    }


def main() -> int:
    if len(sys.argv) < 2:
        _emitir_json(
            {
                "status": "FALHA",
                "id_fila_adm": None,
                "id_adm": None,
                "modalidade": None,
                "link_drive": None,
                "etapas": {
                    "drive": "ERRO",
                    "email": "ERRO",
                    "planilha": "ERRO",
                    "fechamento": "ERRO",
                },
                "metricas": None,
                "observacao": "argv[1] (id_fila_adm) ausente",
            }
        )
        return 1

    try:
        id_fila_adm = int(sys.argv[1])
    except ValueError:
        _emitir_json(
            {
                "status": "FALHA",
                "id_fila_adm": None,
                "id_adm": None,
                "modalidade": None,
                "link_drive": None,
                "etapas": {
                    "drive": "ERRO",
                    "email": "ERRO",
                    "planilha": "ERRO",
                    "fechamento": "ERRO",
                },
                "metricas": None,
                "observacao": f"id_fila_adm invalido: {sys.argv[1]!r}",
            }
        )
        return 1

    try:
        payload = _executar(id_fila_adm)
        _emitir_json(payload)
        return 0 if payload["status"] == "SUCESSO" else 1
    except Exception as e:
        _stderr(traceback.format_exc())

        caminho_log = None
        try:
            dados = obter_dados_adm_por_fila(id_fila_adm)
            if dados:
                caminho_log = dados.get("caminho_log")
        except Exception:
            pass

        try:
            notificar_falha(
                etapa="SAIDA",
                erro=e,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
                script_path=__file__,
                contexto_extra=f"id_fila_adm={id_fila_adm}",
            )
        except Exception:
            pass

        _emitir_json(
            {
                "status": "FALHA",
                "id_fila_adm": id_fila_adm,
                "id_adm": None,
                "modalidade": None,
                "link_drive": None,
                "etapas": {
                    "drive": "ERRO",
                    "email": "ERRO",
                    "planilha": "ERRO",
                    "fechamento": "ERRO",
                },
                "metricas": None,
                "observacao": f"Excecao geral: {type(e).__name__}: {e}",
            }
        )
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        _emitir_json(
            {
                "status": "FALHA",
                "id_fila_adm": None,
                "id_adm": None,
                "modalidade": None,
                "link_drive": None,
                "etapas": {
                    "drive": "ERRO",
                    "email": "ERRO",
                    "planilha": "ERRO",
                    "fechamento": "ERRO",
                },
                "metricas": None,
                "observacao": f"Toplevel: {type(e).__name__}: {e}",
            }
        )
        sys.exit(1)