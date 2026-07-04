from typing import Dict, List, Optional, Tuple
from entrada.utils.texto_utils import normalizar
def mapear_indices_cabecalho(cabecalho: List[str]) -> Dict[str, int]:
    norm = [normalizar(h) for h in cabecalho]

    def achar(*nomes: str) -> Optional[int]:
        for n in nomes:
            nn = normalizar(n)
            if nn in norm:
                return norm.index(nn)
        return None

    idx_grupo = achar("GRUPO")
    idx_cota = achar("COTA")
    idx_consultor = achar("CONSULTOR", "NOME DA PASTA", "NOME_DA_PASTA", "PASTA")
    idx_boleto = achar("BOLETO", "STATUS")
    idx_cliente = achar("NOME DO CLIENTE", "NOME DE CLIENTE", "NOME_CLIENTE", "CLIENTE", "CONSORCIADO")
    idx_obs_boleto = achar("OBSERVAÇÃO BOLETO", "OBSERVACAO BOLETO")
    idx_pode_unificar = achar("PODE UNIFICAR", "PODE_UNIFICAR")
    idx_cpf_cnpj = achar("CPF\\CNPJ")

    faltando = []
    if idx_grupo is None:
        faltando.append("GRUPO")
    if idx_cota is None:
        faltando.append("COTA")
    if idx_boleto is None:
        faltando.append("BOLETO / STATUS")
    if idx_cliente is None:
        faltando.append("NOME DO CLIENTE")
    # A coluna CONSULTOR (ou NOME DA PASTA) e obrigatoria EXISTIR no cabecalho,
    # mas os valores podem ficar vazios (o leitor usa "Boletos" como padrao).
    if idx_consultor is None:
        faltando.append("CONSULTOR / NOME DA PASTA")

    if faltando:
        raise ValueError(f"Header faltando colunas: {', '.join(faltando)}. Header recebido: {cabecalho}")

    return {
        "grupo": idx_grupo,
        "cota": idx_cota,
        "consultor": idx_consultor,
        "boleto": idx_boleto,
        "cliente": idx_cliente,
        "obs_boleto": idx_obs_boleto,
        "pode_unificar": idx_pode_unificar,
        "cpf_cnpj": idx_cpf_cnpj
    }


def encontrar_cabecalho(linhas: List[List[str]], max_linhas_busca: int = 10) -> Tuple[int, Dict[str, int]]:
    limite = min(len(linhas), max_linhas_busca)

    for i in range(limite):
        linha = linhas[i]

        if not any(str(c).strip() for c in linha):
            continue

        try:
            indices = mapear_indices_cabecalho(linha)
            return i, indices
        except ValueError:
            continue

    raise ValueError(f"Nenhum cabeçalho válido encontrado nas primeiras {limite} linhas.")