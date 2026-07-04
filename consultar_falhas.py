"""
Consulta status e observacao das cotas que possuem observacao_LEIA.txt na pasta FALHAS.
"""
import os
import re
import sys
from pathlib import Path
from collections import Counter

# Adiciona o src ao path para usar o db.py do projeto
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "src" / "entrada"))
sys.path.insert(0, str(BASE / "src"))

from dotenv import load_dotenv
load_dotenv(BASE / "credentials" / ".env")

import psycopg2

PASTA_FALHAS = BASE / "lotes" / "Graziella - 6418_19" / "IMOVEL" / "fila_207" / "Evidencias" / "FALHAS"

# Coleta pares (grupo, cota) das pastas que tem o arquivo LEIA
pares = []
for txt in PASTA_FALHAS.rglob("observação_*_LEIA.txt"):
    m = re.search(r"observa[çc][aã]o_(\d+)_(\d+)_LEIA\.txt", txt.name, re.IGNORECASE)
    if m:
        g = m.group(1).zfill(6)
        c = m.group(2).zfill(4)
        pares.append((g, c))

pares = sorted(set(pares))
print(f"Cotas com arquivo LEIA.txt encontradas: {len(pares)}\n")

if not pares:
    print("Nenhuma cota encontrada.")
    sys.exit(0)

conn = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    sslmode="require",
    options="-c timezone=America/Sao_Paulo",
)

cur = conn.cursor()
cur.execute("""
    SELECT DISTINCT ON (grupo, cota)
        grupo, cota, status, observacao, id_cota
    FROM tbl_fila_cotas
    WHERE (grupo, cota) IN %s
    ORDER BY grupo, cota, id_cota DESC
""", (tuple(pares),))

rows = cur.fetchall()
conn.close()

# Pares sem resultado no banco
encontrados = {(r[0], r[1]) for r in rows}
sem_registro = [p for p in pares if p not in encontrados]

# Resumo por status
cnt = Counter(r[2] for r in rows)
print("=== RESUMO POR STATUS ===")
for st, n in sorted(cnt.items()):
    print(f"  {st}: {n}")
if sem_registro:
    print(f"  SEM REGISTRO NO BANCO: {len(sem_registro)}")

print(f"\n=== DETALHE ({len(rows)} cotas) ===")
print(f"{'GRUPO':<8} {'COTA':<6} {'STATUS':<12} OBSERVACAO")
print("-" * 100)
for grupo, cota, status, obs, id_cota in rows:
    obs_str = (obs or "").replace("\n", " ")[:80]
    print(f"{grupo:<8} {cota:<6} {status:<12} {obs_str}")

if sem_registro:
    print(f"\n=== SEM REGISTRO NO BANCO ({len(sem_registro)}) ===")
    for g, c in sem_registro:
        print(f"  {g} / {c}")
