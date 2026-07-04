"""
notificar_pad.py
================

Wrapper CLI para o PAD (Power Automate Desktop) chamar quando algo falha
fora do escopo do Python — por exemplo:

  - PowerShell que chama um main.py nao conseguiu nem spawnar
  - F_Login (subfluxo PAD) falhou
  - UI Edge (clique em Salvar, popup do "Nome:") falhou
  - Query SQL executada diretamente pelo PAD falhou
  - Qualquer outro ponto onde o Python nao esta no controle

Reaproveita a funcao `notificar_falha` do shared/notificador.py para enviar
email via Gmail API com os mesmos anexos (log.txt do lote + este script).

USO PELO PAD (PowerShell)
-------------------------
$ProjectRoot = "C:\\rpa_gerar_boleto"
$PythonExe   = Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"
$ScriptPath  = Join-Path $ProjectRoot "src\\shared\\notificar_pad.py"

& $PythonExe $ScriptPath `
    --etapa "PROCESSAMENTO_POWERSHELL" `
    --mensagem "ExitCode=1 - Worker quebrou. Saida: ..." `
    --id-fila-adm %v_idFilaAdm% `
    --id-cota %v_idCota% `
    --contexto-extra "Tentativa %LoopIndex% de 3"

Para mensagens longas/multilinhas (ex: dump completo do OutputWorker), grava
em arquivo temporario e usa --mensagem-arquivo:

    $tmp = New-TemporaryFile
    Set-Content -Path $tmp -Value $OutputWorker -Encoding UTF8
    & $PythonExe $ScriptPath `
        --etapa "PROCESSAMENTO_POWERSHELL" `
        --mensagem-arquivo "$tmp" `
        --id-fila-adm %v_idFilaAdm%
    Remove-Item $tmp

EXIT CODES
----------
0 = email enviado com sucesso
1 = falha (parse de args, envio de email, etc.)
"""

import sys
import os
import argparse
import traceback

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o PAD
# captura a saida via PowerShell (default do Windows e cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Adiciona /src ao sys.path para permitir imports absolutos do projeto
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(SRC_DIR)

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Carrega .env da raiz do projeto (mesma estrategia dos mains)
try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(ROOT_DIR, ".env")
    load_dotenv(ENV_PATH, override=True)
except Exception as e_env:
    print(f"[NOTIFICAR_PAD] Aviso: nao consegui carregar .env: {e_env}", flush=True)

from shared.notificador import notificar_falha


class FalhaPAD(Exception):
    """
    Exception sintetica usada para representar erros vindos do PAD.
    A mensagem traz o conteudo capturado pelo PAD (ErroEntrada, ErroWorker,
    ScriptError ou descricao livre escrita no proprio fluxo).
    """
    pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Notificador de falha disparado pelo PAD (F_NotificarFalha). "
            "Envia email via Gmail API reaproveitando shared/notificador.py."
        )
    )

    parser.add_argument(
        "--etapa",
        required=True,
        help=(
            "Identificador da etapa onde ocorreu o erro. "
            "Exemplos: PROCESSAMENTO_POWERSHELL, F_LOGIN, UI_EDGE_SALVAR, "
            "DB_PAD, F_OBTER_FILA, ENTRADA_POWERSHELL, SAIDA_POWERSHELL."
        ),
    )

    grupo_msg = parser.add_mutually_exclusive_group(required=True)
    grupo_msg.add_argument(
        "--mensagem",
        help="Conteudo do erro como string (use para mensagens curtas).",
    )
    grupo_msg.add_argument(
        "--mensagem-arquivo",
        help=(
            "Caminho de um arquivo texto contendo o conteudo do erro. "
            "Use para mensagens longas/multilinhas (ex: dump do OutputWorker)."
        ),
    )

    parser.add_argument(
        "--id-fila-adm",
        type=int,
        default=None,
        help="ID do lote (tbl_fila_adm). Usado para lookup do caminho_log.",
    )
    parser.add_argument(
        "--id-cota",
        type=int,
        default=None,
        help="ID da cota (tbl_fila_cotas), quando aplicavel.",
    )
    parser.add_argument(
        "--caminho-log",
        default=None,
        help=(
            "Caminho do log.txt do lote. Se nao informado, tenta buscar via "
            "id-fila-adm na tbl_fila_adm."
        ),
    )
    parser.add_argument(
        "--contexto-extra",
        default=None,
        help="Texto livre com informacoes adicionais (ex: tentativa N de 3).",
    )
    parser.add_argument(
        "--script-path",
        default=None,
        help=(
            "Caminho de um script Python relacionado ao erro, anexado ao email. "
            "Default: o proprio notificar_pad.py."
        ),
    )
    parser.add_argument(
        "--email-destino",
        default=None,
        help="Sobrescreve o destinatario configurado no notificador.",
    )

    return parser.parse_args()


def _ler_mensagem(args: argparse.Namespace) -> str:
    if args.mensagem:
        return args.mensagem

    caminho = args.mensagem_arquivo
    try:
        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return (
            f"(Falha ao ler --mensagem-arquivo '{caminho}': {e})\n"
            f"Mensagem original indisponivel."
        )


def _lookup_caminho_log(id_fila_adm: int) -> str | None:
    """
    Busca o caminho_log do lote no banco. Falhas aqui nao sao fatais — o
    email ainda sai, so sem o anexo do log.txt.
    """
    try:
        # Tenta os modulos de DB ja existentes no projeto
        try:
            from saida.lib.db import get_conn
        except Exception:
            from entrada.lib.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT caminho_log FROM tbl_fila_adm WHERE id_fila_adm = %s",
                    (id_fila_adm,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception as e:
        print(
            f"[NOTIFICAR_PAD] Lookup do caminho_log falhou: {e}",
            flush=True,
        )

    return None


def main() -> int:
    try:
        args = _parse_args()
    except SystemExit as se:
        # argparse chamou sys.exit() (ex: -h ou erro de args)
        return int(se.code) if isinstance(se.code, int) else 1
    except Exception as e:
        print(f"[NOTIFICAR_PAD] Falha ao parsear args: {e}", flush=True)
        return 1

    mensagem = _ler_mensagem(args)

    caminho_log = args.caminho_log
    if not caminho_log and args.id_fila_adm is not None:
        caminho_log = _lookup_caminho_log(args.id_fila_adm)

    erro = FalhaPAD(mensagem)

    print(
        f"[NOTIFICAR_PAD] Disparando email | etapa={args.etapa} "
        f"id_fila_adm={args.id_fila_adm} id_cota={args.id_cota} "
        f"caminho_log={caminho_log}",
        flush=True,
    )

    enviado = notificar_falha(
        etapa=args.etapa,
        erro=erro,
        id_fila_adm=args.id_fila_adm,
        id_cota=args.id_cota,
        caminho_log=caminho_log,
        script_path=args.script_path or os.path.abspath(__file__),
        contexto_extra=args.contexto_extra,
        email_destino=args.email_destino,
        origem="PAD",
    )

    if enviado:
        print("[NOTIFICAR_PAD] Email enviado com sucesso.", flush=True)
        return 0

    print("[NOTIFICAR_PAD] Falha ao enviar email.", flush=True)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e_top:
        print(f"[NOTIFICAR_PAD] Falha catastrofica: {e_top}", flush=True)
        traceback.print_exc()
        sys.exit(1)
