# db.py
import os
import psycopg2
from contextlib import contextmanager


def _load_env_if_needed():
    # se ja tem, nao faz nada
    if os.getenv("DB_HOST") and os.getenv("DB_NAME") and os.getenv("DB_USER") and os.getenv("DB_PASSWORD"):
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")

    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


_RETRY_BACKOFFS = (2.0, 4.0, 6.0)


def _abrir_conexao_uma_vez():
    _load_env_if_needed()

    missing = [k for k in ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"] if not os.getenv(k)]
    if missing:
        raise ValueError(f"Faltam variaveis no .env: {', '.join(missing)}")

    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode="require",
        connect_timeout=10,
        application_name="rpa_gerar_boleto",
        # Fuso da sessao: todo NOW()/CURRENT_TIMESTAMP desta conexao
        # resolve em horario do Brasil, independente do fuso do servidor
        # ou da maquina (VM/local) que executa o RPA.
        options="-c timezone=America/Sao_Paulo",
    )


def _abrir_conexao():
    """Connect com retry+backoff para timeouts e slot exhaustion transitorios."""
    import time
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
    raise ultima_exc


@contextmanager
def get_conn():
    """
    Context manager que GARANTE o fechamento da conexao no __exit__.

    Em psycopg2 o `with conn:` nativo controla a transacao (commit/rollback)
    mas NAO fecha a conexao. Sem este wrapper, cada chamada vaza um socket
    aberto contra o Postgres -> em loops longos o pool da Aiven satura e
    aparece "remaining connection slots are reserved for roles with the
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
