"""
reautenticar.py
================

Script utilitario para forcar re-autenticacao do Google OAuth quando o
token.json expirou ou foi revogado.

QUANDO USAR
-----------
- Erro 'invalid_grant: Token has been expired or revoked.'
- App OAuth no modo 'Testing' do Google Cloud Console (token vence em 7 dias)
- Inatividade > 6 meses
- Acesso revogado manualmente em myaccount.google.com

COMO USAR (na propria VM se tiver browser, ou na sua maquina local)
-------------------------------------------------------------------
    cd C:\\rpa_gerar_boleto_imovel
    .venv\\Scripts\\python.exe src\\shared\\reautenticar.py

Vai abrir o browser pedindo login Google. Depois de autorizar, o
token.json novo e salvo em credentials/.

SE A VM NAO TEM BROWSER
-----------------------
1. Rode este script na sua maquina local (com a mesma client_secret.json)
2. Copie o credentials/token.json gerado para a VM no mesmo caminho
3. Roda novamente o orquestrador
"""

import os
import sys

# Adiciona /src ao sys.path para permitir 'from shared.google_auth import ...'
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shared.google_auth import obter_credenciais


def main() -> int:
    print("=" * 60)
    print("RE-AUTENTICACAO GOOGLE OAUTH")
    print("=" * 60)
    print()
    print("Vou abrir um browser para voce autorizar o app.")
    print("Se ja existir um token.json, ele sera substituido.")
    print()

    try:
        creds = obter_credenciais(forcar_reauth=True)
    except FileNotFoundError as e:
        print(f"\nERRO: {e}")
        print("\nBaixe o client_secret.json em:")
        print("  Google Cloud Console -> APIs & Services -> Credentials")
        print("  -> OAuth 2.0 Client IDs -> Download JSON")
        print("E coloque em: credentials/client_secret.json")
        return 1
    except Exception as e:
        print(f"\nERRO inesperado: {e}")
        import traceback
        traceback.print_exc()
        return 1

    if creds and creds.valid:
        print()
        print("=" * 60)
        print("OK! Token regenerado com sucesso.")
        print("=" * 60)
        print()
        print("Verifica se os scopos abaixo cobrem o que voce precisa:")
        print(f"  Scopes: {creds.scopes}")
        print()
        print("Se rodou em outra maquina, copie o credentials/token.json")
        print("para a VM na mesma pasta.")
        return 0

    print("\nAlgo deu errado. creds nao retornou valido.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
