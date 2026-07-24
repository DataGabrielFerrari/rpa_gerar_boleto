"""
Geracao de nomes de arquivos e pastas para o worker AVAPRO.

Boleto de cota unica:
  Boleto {AT:02d}AT - {grupo:06d}-{cota:04d} {Nome do Cliente}.pdf
  Ex.: Boleto 02AT - 001625-0695 Ana Flavia Martinenghi.pdf

Boleto unificado (mais de uma cota do mesmo cliente no mesmo boleto):
  Boleto Unificado {Nome do Cliente}.pdf
  Ex.: Boleto Unificado Joao Carlos Muniz da Cunha Filho.pdf

O nome do cliente vai em Title Case (capitalizacao profissional, com
conectores 'de/da/do/das/dos/e' em minusculo) e com acentos preservados -
nada de CAIXA ALTA gritando.

Pastas de evidencia por cota:
  {caminho_base}/Evidencias/FALHAS/{Nome Cliente}_{grupo}_{cota}/
  {caminho_base}/Evidencias/ADIANTADOS/{Nome Cliente}_{grupo}_{cota}/

O sufixo {grupo}_{cota} usa zfill (6 e 4 digitos) e e unico por cota
no AVAPRO - garante que nao colida com outras cotas do mesmo cliente,
mesmo em lotes que envolvem boletos unificados ou multiplas cotas.
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional


# Caracteres invalidos em nome de arquivo no Windows: \ / : * ? " < > |
_RE_INVALIDOS = re.compile(r'[\\/:*?"<>|]')
_RE_ESPACOS = re.compile(r"\s+")

# Conectores que ficam em minusculo no meio do nome (estilo brasileiro).
_CONECTORES = {
    "de", "da", "do", "das", "dos", "e", "di", "du",
    "del", "la", "las", "los", "van", "von",
}


def remover_acentos(texto: str) -> str:
    """'Nao Baixado' preservando o resto. (Usado em comparacoes, nao no nome.)"""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in t if not unicodedata.combining(c))


def sanitizar_nome(nome: str) -> str:
    """
    Remove caracteres invalidos de nome de arquivo no Windows e colapsa
    multiplos espacos. NAO altera case nem remove acentos.
    """
    if not nome:
        return "Sem Nome"
    s = _RE_INVALIDOS.sub("_", str(nome))
    s = _RE_ESPACOS.sub(" ", s).strip()
    return s or "Sem Nome"


def title_case_nome(nome: str) -> str:
    """
    Title Case profissional, preservando acentos:
      'OSMIR DE ARAÚJO LUZ'  -> 'Osmir de Araújo Luz'
      'joao carlos da cunha' -> 'Joao Carlos da Cunha'
      'GRAPHICAR LOCADORA DE VEICULOS LTDA' -> 'Graphicar Locadora de Veiculos Ltda'

    Conectores (de, da, do, e, ...) ficam minusculos, exceto se forem a
    primeira palavra. Mantem os acentos como vieram.
    """
    if not nome:
        return ""
    palavras = re.split(r"\s+", str(nome).strip())
    saida = []
    for i, w in enumerate(palavras):
        if not w:
            continue
        wl = w.lower()
        if i > 0 and wl in _CONECTORES:
            saida.append(wl)
        else:
            saida.append(wl[:1].upper() + wl[1:])
    return " ".join(saida)


def normalizar_grupo(grupo) -> str:
    """Zero-pad para 6 digitos: '1625' -> '001625'."""
    s = re.sub(r"\D", "", str(grupo or ""))
    return s.zfill(6) if s else "000000"


def normalizar_cota(cota) -> str:
    """Zero-pad para 4 digitos: '695' -> '0695'."""
    s = re.sub(r"\D", "", str(cota or ""))
    return s.zfill(4) if s else "0000"


def nome_cliente_para_arquivo(nome: str) -> str:
    """
    Nome do cliente pronto para nome de arquivo: MAIUSCULO + caracteres
    invalidos saneados.
    """
    s = sanitizar_nome((nome or "").strip().upper())
    return s


def situacao_atraso_texto(parcelas_atraso: int) -> str:
    """
    Texto de situacao de atraso para nome de arquivo (mesmo padrao do
    rpa_gerar_boleto):
      0    -> 'Nenhuma parcela em atraso'
      1    -> '1 parcela em atraso'
      2    -> '2 parcelas em atraso'
      3+   -> 'Verificar Diluição! N parcelas em atraso'
    """
    n = max(0, int(parcelas_atraso or 0))
    if n == 0:
        return "Nenhuma parcela em atraso"
    if n == 1:
        return "1 parcela em atraso"
    if n == 2:
        return "2 parcelas em atraso"
    return f"Verificar Diluição! {n} parcelas em atraso"


def nome_arquivo_boleto(
    grupo: str,
    cota: str,
    nome_cliente: str,
    meses_parcelas: list,
    extensao: str = "pdf",
) -> str:
    """
    Formato identico ao rpa_gerar_boleto:
      '{meses} {grupo} {cota}-00 {NOME CLIENTE}.pdf'

    Exemplos:
      'Junho 001644 0351-00 LUIZ APARECIDO ARAUJO NASCIMENTO.pdf'
      'Maio Junho 001644 0351-00 LUIZ APARECIDO ARAUJO NASCIMENTO.pdf'

    - meses_parcelas: lista de nomes de mes em ordem cronologica (ex: ['Maio', 'Junho'])
    - grupo: 6 digitos com zero-pad
    - cota: 4 digitos com zero-pad + sufixo '-00'
    - nome_cliente: em MAIUSCULO (sanitizado)
    """
    g = normalizar_grupo(grupo)
    c = normalizar_cota(cota)
    nome = sanitizar_nome(str(nome_cliente or "").strip().upper())
    meses_txt = " ".join(meses_parcelas).strip() if meses_parcelas else ""
    base = sanitizar_nome(f"{meses_txt} {g} {c}-00 {nome}".strip())
    return f"{base}.{extensao.lstrip('.')}"


def nome_arquivo_boleto_unificado(
    nome_cliente: str,
    extensao: str = "pdf",
) -> str:
    """
    Mais de uma cota -> 'Boleto Unificado Joao Carlos Muniz da Cunha Filho.pdf'.
    """
    nome = nome_cliente_para_arquivo(nome_cliente)
    base = sanitizar_nome(f"Boleto Unificado {nome}")
    return f"{base}.{extensao.lstrip('.')}"


def destino_sem_colisao(pasta, nome_arquivo: str) -> Path:
    """
    Retorna um Path dentro de `pasta` que ainda nao existe. Se
    `nome_arquivo` ja existir, adiciona sufixo ' (2)', ' (3)' etc.
    Evita sobrescrever boletos (ex.: 2 unificados do mesmo cliente em
    pastas CPF e CNPJ distintas, ou reexecucao parcial).
    """
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / nome_arquivo
    if not destino.exists():
        return destino

    stem = destino.stem
    suf = destino.suffix
    i = 2
    while True:
        cand = pasta / f"{stem} ({i}){suf}"
        if not cand.exists():
            return cand
        i += 1


# ============================================================
# Pastas
# ============================================================

def pasta_falhas_root(caminho_base: Optional[str]) -> Path:
    """ROOT/Lotes/.../fila_X/Evidencias/FALHAS ou fallback no projeto."""
    if caminho_base:
        return Path(caminho_base) / "Evidencias" / "FALHAS"
    raiz_projeto = Path(__file__).resolve().parents[3]
    return raiz_projeto / "Lotes" / "FALHAS"


def pasta_nao_baixados_root(caminho_base: Optional[str]) -> Path:
    """ROOT/Lotes/.../fila_X/Evidencias/NAO_BAIXADOS ou fallback no projeto."""
    if caminho_base:
        return Path(caminho_base) / "Evidencias" / "NAO_BAIXADOS"
    raiz_projeto = Path(__file__).resolve().parents[3]
    return raiz_projeto / "Lotes" / "NAO_BAIXADOS"


def _slug_grupo_cota(grupo, cota) -> str:
    """
    Normaliza grupo+cota para sufixo de nome de pasta: GGGGGG_CCCC.
    Aceita string, int ou None. Mantem zeros a esquerda (6 e 4 digitos).
    """
    g = re.sub(r"\D", "", str(grupo or "")).zfill(6)
    c = re.sub(r"\D", "", str(cota or "")).zfill(4)
    return f"{g}_{c}"


def pasta_falha_cota(
    caminho_base: Optional[str],
    nome_cliente: str,
    grupo,
    cota,
) -> Path:
    """
    Caminho da pasta de falhas da cota: {Nome Cliente}_{grupo}_{cota}/

    O sufixo grupo_cota e unico por cota no AVAPRO - nao colide com
    outras cotas do mesmo cliente (boletos unificados) nem entre
    lotes diferentes do mesmo cliente.

    NAO cria a pasta - apenas calcula o caminho. A criacao e PREGUICOSA:
    so acontece quando uma evidencia real e salva (_print_falha no worker).
    Isso evita pastas FALHA vazias para cotas que processaram com sucesso.
    """
    sufixo = _slug_grupo_cota(grupo, cota)
    # Nome truncado em 40 chars: evita estourar MAX_PATH (260) do Windows
    # com nomes de empresa gigantes (o sufixo grupo_cota mantem a unicidade)
    nome_pasta = f"{nome_cliente_para_arquivo(nome_cliente)[:40].rstrip()}_{sufixo}"
    return pasta_falhas_root(caminho_base) / nome_pasta


def pasta_nao_baixado_cota(
    caminho_base: Optional[str],
    nome_cliente: str,
    grupo,
    cota,
) -> Path:
    """
    Caminho da pasta de evidencias de cotas NAO_BAIXADO:
        {caminho_base}/Evidencias/NAO_BAIXADOS/{Nome Cliente}_{grupo}_{cota}/

    NAO_BAIXADO = cota encontrada mas sem boleto emitivel (cliente nao
    localizado na busca, modalidade errada, valor zero, toast definitivo...).
    Separado de FALHAS (erros tecnicos/retriable) para facilitar analise.

    NAO cria a pasta — criacao preguicosa no momento do save da evidencia.
    """
    sufixo = _slug_grupo_cota(grupo, cota)
    # Nome truncado em 40 chars: evita estourar MAX_PATH (260) do Windows
    nome_pasta = f"{nome_cliente_para_arquivo(nome_cliente)[:40].rstrip()}_{sufixo}"
    return pasta_nao_baixados_root(caminho_base) / nome_pasta


def pasta_atrasados_nao_emitidos_cota(
    caminho_base: Optional[str],
    nome_cliente: str,
    grupo,
    cota,
) -> Path:
    """
    Cotas de ADMs com selecionar_atraso=FALSE que so tinham parcela(s) em
    atraso (sem parcela do mes ref) e por isso NAO tiveram boleto emitido:
        {caminho_base}/Evidencias/NAO_BAIXADOS/Atrasados nao emitidos/{Nome}_{g}_{c}/

    Subpasta dedicada dentro de NAO_BAIXADOS para separar esses casos dos
    demais NAO_BAIXADOS (modalidade errada, valor zero, etc.).
    NAO cria a pasta — criacao preguicosa no momento do save da evidencia.
    """
    sufixo = _slug_grupo_cota(grupo, cota)
    nome_pasta = f"{nome_cliente_para_arquivo(nome_cliente)[:40].rstrip()}_{sufixo}"
    return pasta_nao_baixados_root(caminho_base) / "Atrasados nao emitidos" / nome_pasta


def pasta_boletos(
    caminho_base: str,
    nome_consultor: Optional[str] = None,
) -> Path:
    """
    Pasta destino dos boletos emitidos.

    Sem nome_consultor (compatibilidade legada):
        {caminho_base}/Boletos/
    Com nome_consultor (padrao do rpa_gerar_boleto antigo):
        {caminho_base}/Boletos/{Nome do Consultor}/

    O nome do consultor passa pelo mesmo `nome_cliente_para_arquivo`
    pra sanitizar acentos e caracteres invalidos do Windows. Se vier
    vazio/None, salva direto em Boletos/ (fallback seguro).

    A pasta e criada se nao existir.
    """
    pasta = Path(caminho_base) / "Boletos"
    consultor_limpo = (nome_consultor or "").strip()
    if consultor_limpo:
        sub = nome_cliente_para_arquivo(consultor_limpo)
        if sub:
            pasta = pasta / sub
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def pasta_adiantados_root(caminho_base: Optional[str]) -> Path:
    """
    ROOT/Lotes/.../fila_X/Evidencias/ADIANTADOS.

    Mesmo nivel de Boletos e FALHAS. Reservada para os screenshots do
    toast 'Nao existem cobrancas disponiveis para a cota' quando o
    cliente esta adiantado (sem parcela emitivel).

    NAO cria a pasta aqui - criacao preguicosa (so na hora de salvar
    o primeiro print) para nao deixar pasta ADIANTADOS vazia em lotes
    sem nenhuma cota adiantada.
    """
    if caminho_base:
        return Path(caminho_base) / "Evidencias" / "ADIANTADOS"
    raiz_projeto = Path(__file__).resolve().parents[3]
    return raiz_projeto / "Lotes" / "ADIANTADOS"


def pasta_cotas_nao_localizadas_planilha(caminho_base: Optional[str]) -> Path:
    """
    Pasta onde são salvos os screenshots das telas do AVAPRO que contêm
    cotas presentes no sistema mas ausentes na planilha do ADM.

    Caminho: {caminho_base}/Evidencias_Cotas_Faltantes/

    A pasta é criada preguiçosamente no momento em que o primeiro
    screenshot for salvo — não aparece em lotes sem divergências.
    """
    if caminho_base:
        return Path(caminho_base) / "Evidencias_Cotas_Faltantes"
    raiz_projeto = Path(__file__).resolve().parents[3]
    return raiz_projeto / "Lotes" / "Evidencias_Cotas_Faltantes"


def pasta_adiantado_cota(
    caminho_base: Optional[str],
    nome_cliente: str,
    grupo,
    cota,
) -> Path:
    """
    Caminho da pasta de ADIANTADOS de uma cota especifica:
        {Nome Cliente}_{grupo}_{cota}/

    O sufixo grupo_cota e unico por cota - nao colide entre lotes nem
    entre cotas do mesmo cliente em boletos unificados.

    NAO cria a pasta - apenas calcula o caminho. Quem chamar deve usar
    `_print_adiantado` no worker, que faz a criacao preguicosa antes de
    salvar o screenshot.
    """
    sufixo = _slug_grupo_cota(grupo, cota)
    nome_pasta = f"{nome_cliente_para_arquivo(nome_cliente)}_{sufixo}"
    return pasta_adiantados_root(caminho_base) / nome_pasta


def pasta_analisar_diferenca(caminho_base: Optional[str]) -> Path:
    """
    Pasta para screenshots de cotas com duas parcelas de mesmo vencimento
    (diferenca de valor — robô selecionou a de maior valor automaticamente):

        {caminho_base}/Evidencias/analisar_diferenca/

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    base = Path(caminho_base) if caminho_base else Path.cwd()
    return base / "Evidencias" / "analisar_diferenca"


def pasta_excluidos(caminho_base: Optional[str]) -> Path:
    """
    Pasta de evidencias de cotas com badge 'Excluído' no AVAPRO:

        {caminho_base}/Evidencias/NAO_BAIXADOS/1 - Excluidos/

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    return pasta_nao_baixados_root(caminho_base) / "1 - Excluidos"


def pasta_desistentes(caminho_base: Optional[str]) -> Path:
    """
    Pasta de evidencias de cotas com badge 'Desistente' no AVAPRO:

        {caminho_base}/Evidencias/NAO_BAIXADOS/2 - Desistentes/

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    return pasta_nao_baixados_root(caminho_base) / "2 - Desistentes"


def pasta_cotas_nao_encontradas(caminho_base: Optional[str]) -> Path:
    """
    Pasta raiz para cotas que nao foram localizadas na busca do AVAPRO
    (busca retornou zero resultados, cota nao apareceu na tela, etc.):

        {caminho_base}/Evidencias/NAO_BAIXADOS/3 - Cotas não encontradas/

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    return pasta_nao_baixados_root(caminho_base) / "3 - Cotas não encontradas"


def pasta_cotas_nao_encontradas_cota(
    caminho_base: Optional[str],
    nome_cliente: str,
    grupo,
    cota,
) -> Path:
    """
    Subpasta por cliente dentro de 3 - Cotas não encontradas:

        {caminho_base}/Evidencias/NAO_BAIXADOS/3 - Cotas não encontradas/{Nome}_{grupo}_{cota}/

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    sufixo = _slug_grupo_cota(grupo, cota)
    nome_pasta = f"{nome_cliente_para_arquivo(nome_cliente)}_{sufixo}"
    return pasta_cotas_nao_encontradas(caminho_base) / nome_pasta


def pasta_verificar_adiantados(caminho_base: Optional[str]) -> Path:
    """
    Pasta para boletos adiantados que precisam de verificacao manual
    (quando o toast 'Nao existem cobrancas disponiveis' aparece apos
    clicar Emitir boleto):

        {caminho_base}/Evidencias/ADIANTADOS/verificar_adiantados/

    Diferente de pasta_adiantado_cota, esta pasta nao e por cota - e
    um repositorio centralizado de screenshots que requerem atencao manual.

    NAO cria a pasta aqui - criacao preguicosa no momento do save.
    """
    return pasta_adiantados_root(caminho_base) / "verificar_adiantados"
