# lib/db.py
import os
import sys
import time
import psycopg2
from contextlib import contextmanager


# Retry rapido: 4 tentativas em ~12s para falhas transientes normais.
_RETRY_BACKOFFS = (2.0, 4.0, 6.0)

# Retry longo: quando o banco cai de vez, espera ate 10 min antes de desistir.
# O orquestrador chama aguardar_banco_disponivel() nesses casos.
_AGUARDAR_BANCO_INTERVALO_S = 30    # checa a cada 30s
_AGUARDAR_BANCO_TIMEOUT_S   = 600   # desiste apos 10 min


def _abrir_conexao_uma_vez():
    """
    Abre uma conexao PostgreSQL nova usando variaveis do .env.
    Compativel com Aiven (SSL obrigatorio).
    """
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")

    if not host or not name or not user or not password:
        raise ValueError(
            "Faltam variaveis no .env: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD."
        )

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=name,
        user=user,
        password=password,
        sslmode="require",
        connect_timeout=10,
        application_name="rpa_gerar_boleto",
        # Fuso da sessao: todo NOW()/CURRENT_TIMESTAMP desta conexao
        # resolve em horario do Brasil, independente do fuso do servidor
        # ou da maquina (VM/local) que executa o RPA.
        options="-c timezone=America/Sao_Paulo",
    )


def _abrir_conexao():
    """
    _abrir_conexao_uma_vez com retry+backoff para tolerar:
      - 'timeout expired'   (TCP nao respondeu em 10s)
      - 'remaining connection slots' (pool da Aiven momentaneamente cheio)
      - falhas transientes de rede
    Levanta a ultima excecao se as tentativas se esgotarem.
    """
    ultima_exc = None
    for tentativa, espera in enumerate((0.0,) + _RETRY_BACKOFFS, start=1):
        if espera > 0:
            time.sleep(espera)
        try:
            return _abrir_conexao_uma_vez()
        except psycopg2.OperationalError as e:
            ultima_exc = e
            print(
                f"[DB] Falha ao conectar (tentativa {tentativa}): {e}",
                flush=True,
            )
    # esgotou as tentativas
    raise ultima_exc


def aguardar_banco_disponivel() -> bool:
    """
    Chamado pelo orquestrador quando detecta que o banco esta fora.
    Tenta reconectar a cada _AGUARDAR_BANCO_INTERVALO_S segundos por
    ate _AGUARDAR_BANCO_TIMEOUT_S segundos.

    Retorna True se o banco voltou, False se esgotou o tempo.
    Imprime aviso no stderr a cada tentativa para aparecer no terminal.
    """
    deadline = time.time() + _AGUARDAR_BANCO_TIMEOUT_S
    tentativa = 0
    while time.time() < deadline:
        tentativa += 1
        restante = int(deadline - time.time())
        print(
            f"[DB] Banco indisponivel — aguardando... "
            f"(tentativa {tentativa}, timeout em {restante}s)",
            file=sys.stderr, flush=True,
        )
        time.sleep(_AGUARDAR_BANCO_INTERVALO_S)
        try:
            conn = _abrir_conexao_uma_vez()
            conn.close()
            print("[DB] Banco voltou — retomando processamento.", file=sys.stderr, flush=True)
            return True
        except psycopg2.OperationalError:
            continue
    print(
        f"[DB] Banco nao voltou em {_AGUARDAR_BANCO_TIMEOUT_S}s — encerrando.",
        file=sys.stderr, flush=True,
    )
    return False


@contextmanager
def get_conn():
    """
    Context manager que GARANTE o fechamento da conexao no __exit__.

    IMPORTANTE: em psycopg2 o `with conn:` nativo controla a transacao
    (commit/rollback) mas NAO fecha a conexao. Sem este wrapper, cada
    chamada abre uma conexao que so e fechada quando o GC resolve coletar
    -> em loops longos (cota por cota) o pool da Aiven satura e aparece
    o erro "remaining connection slots are reserved for roles with the
    SUPERUSER attribute".
    """
    conn = _abrir_conexao()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
