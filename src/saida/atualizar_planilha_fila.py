"""
Script avulso: atualiza status e observacoes das cotas na planilha
correspondente a um id_fila_adm.

Uso:
    python atualizar_planilha_fila.py           # usa id_fila_adm=201
    python atualizar_planilha_fila.py 201
    python atualizar_planilha_fila.py 123

Saida: JSON linha unica
{
  "status": "SUCESSO|FALHA",
  "id_fila_adm": int,
  "metricas": { matched, updated_status, updated_obs, not_found, duplicated_keys },
  "observacao": str
}
"""

import os
import sys
import json
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- bootstrap de path / .env ----------
_SAIDA_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR   = os.path.dirname(_SAIDA_DIR)
_ROOT_DIR  = os.path.dirname(_SRC_DIR)

if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT_DIR, ".env"), override=True)
except ImportError:
    pass
# ----------------------------------------------

from saida.jobs.atualizar_planilha import atualizar_planilha_lote


ID_FILA_ADM_PADRAO = 201


def _emitir(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def main() -> int:
    # Aceita id_fila_adm via argv; padrao = 201
    if len(sys.argv) >= 2:
        try:
            id_fila_adm = int(sys.argv[1])
        except ValueError:
            _emitir({
                "status": "FALHA",
                "id_fila_adm": None,
                "metricas": None,
                "observacao": f"id_fila_adm invalido: {sys.argv[1]!r}",
            })
            return 1
    else:
        id_fila_adm = ID_FILA_ADM_PADRAO

    print(f"[INFO] Atualizando planilha para id_fila_adm={id_fila_adm} ...",
          file=sys.stderr, flush=True)

    try:
        metricas = atualizar_planilha_lote(id_fila_adm)
        payload = {
            "status": "SUCESSO",
            "id_fila_adm": id_fila_adm,
            "metricas": metricas,
            "observacao": "Planilha atualizada com sucesso",
        }
        _emitir(payload)
        print(
            f"[OK] matched={metricas['matched']}  "
            f"status={metricas['updated_status']}  "
            f"obs={metricas['updated_obs']}  "
            f"nao_encontrado={metricas['not_found']}  "
            f"duplicados={metricas['duplicated_keys']}",
            file=sys.stderr, flush=True,
        )
        return 0

    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        _emitir({
            "status": "FALHA",
            "id_fila_adm": id_fila_adm,
            "metricas": None,
            "observacao": f"{type(e).__name__}: {e}",
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
