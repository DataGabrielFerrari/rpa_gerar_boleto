import os
import inspect
from datetime import datetime
from typing import Optional


def obter_origem() -> str:
    stack = inspect.stack()

    frame = None
    for item in stack:
        caminho = item.filename.replace("\\", "/")
        if not caminho.endswith("shared/log.py"):
            frame = item
            break

    if frame is None:
        frame = stack[1]

    caminho_completo = frame.filename.replace("\\", "/")

    if "/src/" in caminho_completo:
        caminho_relativo = caminho_completo.split("/src/", 1)[1]
    else:
        caminho_relativo = os.path.basename(caminho_completo)

    linha = frame.lineno
    return f"{caminho_relativo}:{linha}"


def obter_data_hora() -> str:
    # Milissegundos incluidos para ordenar eventos proximos (cliques, toasts)
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def caminho_log_fallback() -> str:
    """
    Retorna um caminho de log de fallback quando nenhum caminho_log
    foi informado.

    Precedencia:
      1) {LOTES_ROOT}\\log (variavel de ambiente do .env, se definida)
      2) {raiz_do_projeto}\\lotes\\log (projeto agora roda no C:, nao mais
         no Google Drive)
    """
    lotes_root = (os.environ.get("LOTES_ROOT") or "").strip()
    if lotes_root:
        pasta = os.path.join(lotes_root, "log")
    else:
        raiz_projeto = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        pasta = os.path.join(raiz_projeto, "lotes", "log")
    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, "log_geral.txt")


def criar_pasta_se_nao_existir(caminho: str) -> None:
    if not caminho:
        # Em vez de explodir, usa o fallback. Quem chamou recebe o caminho
        # de volta via escrever_log; aqui so garantimos que a pasta exista.
        caminho = caminho_log_fallback()

    pasta = os.path.dirname(caminho)

    if pasta:
        os.makedirs(pasta, exist_ok=True)


def formatar_linha_log(
    nivel: str,
    etapa: str,
    id_dado: Optional[int],
    acao: str,
    status: str,
    detalhe: str = ""
) -> str:
    data = obter_data_hora()
    origem = obter_origem()

    return (
        f"{data} | "
        f"{nivel.upper()} | "
        f"{etapa} | "
        f"{id_dado if id_dado is not None else '-'} | "
        f"{acao} | "
        f"{status} | "
        f"{origem} | "
        f"{detalhe}"
    )


def escrever_log(caminho_log: str, linha: str) -> None:
    # Se nao tem caminho, usa o fallback para nao perder a linha.
    if not caminho_log:
        caminho_log = caminho_log_fallback()

    # Imprime ao vivo no terminal (stderr) para acompanhamento em tempo real.
    try:
        import sys as _sys
        print(f"[LOG] {linha}", file=_sys.stderr, flush=True)
    except Exception:
        pass

    try:
        criar_pasta_se_nao_existir(caminho_log)

        with open(caminho_log, "a", encoding="utf-8") as arquivo:
            arquivo.write(linha + "\n")

    except Exception as e:
        # Nao re-raise: log nao pode quebrar o fluxo. So imprime no stdout
        # (que o PowerShell/PAD captura) e segue.
        print(f"[ERRO LOG] Falha ao escrever log em '{caminho_log}': {e}", flush=True)
        try:
            print(f"[LOG_FALLBACK] {linha}", flush=True)
        except Exception:
            pass


def registrar_log(
    caminho_log: str,
    nivel: str,
    etapa: str,
    id_dado: Optional[int],
    acao: str,
    status: str,
    detalhe: str = "",
) -> None:
    linha = formatar_linha_log(
        nivel=nivel,
        etapa=etapa,
        id_dado=id_dado,
        acao=acao,
        status=status,
        detalhe=detalhe
    )

    escrever_log(caminho_log, linha)


def log_info(
    caminho_log: str,
    etapa: str,
    id_dado: Optional[int],
    acao: str,
    detalhe: str = ""
) -> None:
    registrar_log(
        caminho_log=caminho_log,
        nivel="INFO",
        etapa=etapa,
        id_dado=id_dado,
        acao=acao,
        status="SUCESSO",
        detalhe=detalhe,
    )


def log_erro(
    caminho_log: str,
    etapa: str,
    id_dado: Optional[int],
    acao: str,
    detalhe: str = ""
) -> None:
    registrar_log(
        caminho_log=caminho_log,
        nivel="ERROR",
        etapa=etapa,
        id_dado=id_dado,
        acao=acao,
        status="FALHA",
        detalhe=detalhe,
    )