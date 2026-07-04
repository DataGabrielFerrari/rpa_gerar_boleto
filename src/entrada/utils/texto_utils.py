import re
import unicodedata

def normalizar(texto: str) -> str:
    if texto is None:
        return ""
    t = str(texto).strip().lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\s+", "_", t)
    t = re.sub(r"[^a-z0-9_]", "", t)
    return t


def remover_acentos(texto: str) -> str:
    """
    Remove acentos preservando case e pontuacao.
    Ex.: 'Não Baixado' -> 'Nao Baixado'
    """
    if texto is None:
        return ""
    t = str(texto)
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def normalizar_status(texto: str) -> str:
    """
    Normaliza um status de planilha para comparacao com listas de
    status conhecidos (bloqueados, nao baixado, reexecutar, etc).

    Aplica:
      - upper case
      - remove acentos (NÃO -> NAO)
      - colapsa multiplos espacos em 1
      - strip
    """
    if not texto:
        return ""
    t = str(texto).upper().strip()
    t = remover_acentos(t)
    t = re.sub(r"\s+", " ", t)
    return t

def split_abas(nome_aba: str):
    abas = [a.strip() for a in (nome_aba or "").split(",") if a.strip()]
    if len(abas) < 1:
        raise ValueError("nome_aba precisa ter pelo menos 1 aba.")
    return abas