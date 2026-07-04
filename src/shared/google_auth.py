import os
import sys
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
]


def _paths():
    """Retorna (cred_dir, client_secret, token_path) baseado na localizacao do arquivo."""
    base_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    cred_dir = os.path.join(base_dir, "credentials")
    client_secret = os.path.join(cred_dir, "client_secret.json")
    token_path = os.path.join(cred_dir, "token.json")
    return cred_dir, client_secret, token_path


def _salvar_creds(creds, cred_dir, token_path):
    os.makedirs(cred_dir, exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _rodar_flow_interativo(client_secret, cred_dir, token_path):
    """
    Roda o OAuth flow abrindo o browser local.
    So funciona em maquinas com interface grafica disponivel.
    Em VM headless, vai falhar com erro claro.
    """
    print(
        "[GOOGLE_AUTH] Token invalido/expirado. Iniciando re-autenticacao interativa.",
        flush=True,
    )
    print(
        "[GOOGLE_AUTH] Vai abrir um browser para login. Se estiver em VM sem GUI, "
        "rode 'src/shared/reautenticar.py' a partir da sua maquina local com o "
        "client_secret.json e copie o token.json gerado para a VM.",
        flush=True,
    )
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0)
    _salvar_creds(creds, cred_dir, token_path)
    return creds


def obter_credenciais(forcar_reauth: bool = False):
    """
    Obtem credenciais OAuth validas. Estrategia:
      1. Carrega token.json se existir
      2. Se nao tem ou e invalido, tenta refresh
      3. Se refresh falhar (token revogado/expirado pra sempre), deleta o
         token.json e tenta o flow interativo
      4. Se forcar_reauth=True, pula tudo e vai direto pro flow interativo
    """
    cred_dir, client_secret, token_path = _paths()

    if not os.path.exists(client_secret):
        raise FileNotFoundError(
            f"client_secret.json nao encontrado em: {client_secret}. "
            "Baixe o arquivo OAuth do Google Cloud Console."
        )

    if forcar_reauth:
        if os.path.exists(token_path):
            try:
                os.remove(token_path)
                print(f"[GOOGLE_AUTH] Token antigo removido: {token_path}", flush=True)
            except Exception as e:
                print(f"[GOOGLE_AUTH] Aviso: nao consegui remover token antigo: {e}", flush=True)
        return _rodar_flow_interativo(client_secret, cred_dir, token_path)

    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(
                f"[GOOGLE_AUTH] token.json corrompido ({e}). Vai re-autenticar.",
                flush=True,
            )
            creds = None

    if creds and creds.valid:
        return creds

    # Tenta refresh
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _salvar_creds(creds, cred_dir, token_path)
            return creds
        except RefreshError as e:
            print(
                f"[GOOGLE_AUTH] Refresh falhou (token revogado/expirado): {e}",
                flush=True,
            )
            print(
                "[GOOGLE_AUTH] Causas comuns: app OAuth no modo 'Testing' "
                "(token expira em 7 dias), inatividade > 6 meses, ou acesso "
                "revogado manualmente. Vai tentar re-autenticacao interativa.",
                flush=True,
            )
            # Apaga o token quebrado
            try:
                os.remove(token_path)
            except Exception:
                pass
            # creds nao serve mais, vai cair no flow abaixo
            creds = None

    # Sem creds validas e sem refresh possivel: precisa do flow interativo
    return _rodar_flow_interativo(client_secret, cred_dir, token_path)


def criar_servico_sheets():
    """Mantem assinatura compativel com codigo existente."""
    creds = obter_credenciais()
    return build("sheets", "v4", credentials=creds)


if __name__ == "__main__":
    # Permite rodar 'python -m shared.google_auth' pra forcar re-auth
    print("[GOOGLE_AUTH] Forcando re-autenticacao manual.")
    obter_credenciais(forcar_reauth=True)
    print("[GOOGLE_AUTH] OK. token.json regenerado.")
