"""
Reenvia o email de resumo para um lote ja processado.

Uso:
    python reenviar_email.py <id_fila_adm>

Exemplo:
    python reenviar_email.py 259
"""

import sys
import os
from dotenv import load_dotenv

# Carrega variaveis do .env na raiz do projeto
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Garante que os modulos do projeto sejam encontrados
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from saida.jobs.enviar_email import enviar_email_lote


def main():
    if len(sys.argv) != 2:
        print("Uso: python reenviar_email.py <id_fila_adm>")
        sys.exit(1)

    try:
        id_fila_adm = int(sys.argv[1])
    except ValueError:
        print(f"Erro: '{sys.argv[1]}' nao e um numero valido.")
        sys.exit(1)

    print(f"Reenviando email do lote {id_fila_adm}...")
    enviar_email_lote(id_fila_adm)
    print("Email enviado com sucesso.")


if __name__ == "__main__":
    main()
