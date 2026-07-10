"""
Operacoes Playwright especificas do portal AVAPRO.

Fluxo do worker (por cliente, suportando boletos unificados):
  pesquisar -> entrar no cliente -> listar todas as cotas na tela ->
  casar com o banco -> (por cota casada) expandir 'Mostrar mais' p/ ler
  vencimento (modalidade) e marcar o checkbox -> clicar Emitir boleto ->
  modal de selecao de parcelas -> selecionar Em atraso + mes ref por cota ->
  Continuar -> aguardar PDF.
"""

import re
import time
import random
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Any, Dict


def _pausa_humana(page, minimo_ms: int = 200, maximo_ms: int = 600) -> None:
    """Pausa aleatoria curta para simular comportamento humano."""
    page.wait_for_timeout(random.randint(minimo_ms, maximo_ms))

try:
    from entrada.lib.vencimento import calcular_vencimento as _calcular_vencimento
except ImportError:
    _calcular_vencimento = None


# ============================================================
# Helpers de normalizacao
# ============================================================

def _so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _remover_acentos(texto: str) -> str:
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in t if not unicodedata.combining(c))


def gerar_variacoes_busca(grupo: str, cota: str, nome_cliente: str) -> List[str]:
    """
    Strings a digitar no campo de busca, em ORDEM de prioridade.

    Forma 1: "grupo cota" sem zero-pad (AVAPRO matcha melhor). Ex.: '1625 695'.
    Forma 2: "grupo\\cota" com zero-pad - ultima cartada com '\\'.
    Forma 3: nome completo do cliente conforme planilha (ultima tentativa).
             Se retornar mais de um resultado (RES_MUITOS), nao seleciona nenhum.
    """
    g_digit = _so_digitos(grupo)
    c_digit = _so_digitos(cota)

    g_sem_zero = g_digit.lstrip("0") or g_digit or "0"
    c_sem_zero = c_digit.lstrip("0") or c_digit or "0"

    g_zfill = g_digit.zfill(6) if g_digit else ""
    c_zfill = c_digit.zfill(4) if c_digit else ""

    variacoes: List[str] = []
    if g_sem_zero and c_sem_zero:
        variacoes.append(f"{g_sem_zero} {c_sem_zero}")
    if g_zfill and c_zfill:
        variacoes.append(f"{g_zfill}\\{c_zfill}")
    # Ultima tentativa: nome completo do cliente da planilha.
    # Se houver mais de um resultado, _entrar_via_busca trata como RES_MUITOS
    # e nao seleciona nenhum — sem risco de entrar no cliente errado.
    nome_limpo = (nome_cliente or "").strip()
    if nome_limpo:
        variacoes.append(nome_limpo)
    return variacoes


# ============================================================
# Pesquisa
# ============================================================

RES_UM = "UM"
RES_ZERO = "ZERO"
RES_MUITOS = "MUITOS"
RES_TIMEOUT = "TIMEOUT"


def _input_busca(page):
    return page.locator(
        "input[placeholder*='Buscar por grupo, cota, cliente, documento']"
    ).first


def fechar_modal_clara(page) -> bool:
    """
    Fecha o modal de apresentacao da Clara se estiver aberto.
    Tenta primeiro via aria-label exato, depois via aria-label parcial
    (ignora acentos) — tudo Playwright/Python, sem JavaScript.
    Retorna True se fechou, False se nao estava aberto.
    """
    # Tenta via Playwright (aria-label com e sem acento)
    for selector in [
        "button[aria-label='Fechar apresentação da Clara']",
        "button[aria-label='Fechar apresentacao da Clara']",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1500):
                btn.click(force=True, timeout=5000)
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass

    # Fallback (Python): localiza pelo aria-label parcial (ignora acentos/caixa)
    # e dispara o clique com dispatch_event — sem rolar a pagina.
    try:
        candidatos = page.locator(
            "button[aria-label*='Clara' i][aria-label*='Fechar' i], "
            "button[aria-label*='Clara' i][aria-label*='fechar' i]"
        )
        for _i in range(candidatos.count()):
            _b = candidatos.nth(_i)
            try:
                if _b.is_visible():
                    _b.dispatch_event("click")
                    page.wait_for_timeout(600)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def limpar_busca(page) -> None:
    try:
        inp = _input_busca(page)
        inp.click(timeout=5000)
        inp.fill("")
        page.wait_for_timeout(200)
    except Exception:
        pass


def digitar_busca(page, termo: str) -> None:
    """
    Digita o termo + aguarda o debounce do AVAPRO.

    Estrategia de digitacao rapida e robusta:
      1. fill("") limpa o campo instantaneamente.
      2. Digita o termo caracter a caracter com type() — mais natural para a
         SPA do AVAPRO (dispara onChange a cada tecla, como um humano).
      3. Aguarda 1000ms de debounce — suficiente para a SPA filtrar.
         O aguardar_resultado_pesquisa() estabiliza por mais ~600ms depois.

    Anterior: fill() + 3000ms wait → lento.
    Atual:    type() char a char + 1000ms → 3x mais rapido e igualmente robusto.
    """
    # Fecha modal da Clara se estiver bloqueando a tela
    fechar_modal_clara(page)
    inp = _input_busca(page)
    inp.wait_for(state="visible", timeout=30000)
    inp.click(timeout=10000)
    # Limpa com fill (rapido) e entao digita char a char para disparar onChange
    inp.fill("")
    _pausa_humana(page, 100, 300)
    inp.type(termo, delay=random.randint(35, 80))   # delay variavel entre teclas: mais natural
    # Debounce: aguarda a SPA aplicar o filtro.
    page.wait_for_timeout(1000)


def aguardar_resultado_pesquisa(
    page,
    timeout_s: int = 30,
    estabilizacoes_necessarias: int = 5,
) -> Tuple[str, Optional[List[Any]]]:
    """
    Espera ate `timeout_s` segundos por UM dos dois sinais (o que estabilizar
    primeiro vence):
      - 1+ cards visiveis e estaveis por N ciclos -> RES_UM / RES_MUITOS
      - 'Nenhum cliente encontrado' visivel e estavel por N ciclos -> RES_ZERO

    Por que estabilizar AMBOS os sinais:
    Sem isso, o codigo pegava o estado transitorio que a SPA exibe durante
    a troca de filtro - a lista some por uns ms antes do resultado novo
    chegar, e nesse intervalo o 'Nenhum cliente encontrado' pode piscar
    momentaneamente. Resultado: o worker dava ZERO rapido demais e ja partia
    pra forma com '\\' sem nem esperar o filtro real terminar. Agora cada
    sinal precisa de N ciclos (200ms cada -> ~600ms) de presenca estavel,
    o que filtra os flashes transitorios.

    Card count == 0 ESTAVEL nao conta como ZERO - so o texto explicito
    'Nenhum cliente encontrado' fecha a decisao. Ausencia de cards e
    ambigua (pode ser carregamento lento); o texto e definitivo. Se nada
    estabilizar em `timeout_s` segundos, retorna RES_TIMEOUT.
    """
    nenhum_loc = page.locator("p:has-text('Nenhum cliente encontrado')").first
    cards_loc = page.locator("#clientes-list-scroll a[href^='/meus-clientes/']")

    inicio = time.time()
    qtd_anterior = -1
    cards_estaveis = 0
    nenhum_estaveis = 0

    while time.time() - inicio < timeout_s:
        # --- Sinal 1: 'Nenhum cliente encontrado' visivel e estavel ---
        try:
            nenhum_visivel = (
                nenhum_loc.count() > 0 and nenhum_loc.first.is_visible()
            )
        except Exception:
            nenhum_visivel = False

        if nenhum_visivel:
            nenhum_estaveis += 1
            if nenhum_estaveis >= estabilizacoes_necessarias:
                return RES_ZERO, None
        else:
            nenhum_estaveis = 0

        # --- Sinal 2: 1+ cards visiveis e contagem estavel ---
        try:
            qtd = cards_loc.count()
        except Exception:
            qtd = 0

        if qtd > 0 and qtd == qtd_anterior:
            cards_estaveis += 1
            if cards_estaveis >= estabilizacoes_necessarias:
                if qtd == 1:
                    return RES_UM, [cards_loc.first]
                return RES_MUITOS, [cards_loc.nth(i) for i in range(qtd)]
        else:
            cards_estaveis = 0
            qtd_anterior = qtd

        page.wait_for_timeout(200)

    return RES_TIMEOUT, None


def entrar_no_cliente(page, anchor) -> None:
    """Clica no anchor do cliente e espera a URL mudar p/ o detalhe."""
    url_antes = ""
    try:
        url_antes = (page.url or "")
    except Exception:
        pass

    anchor.click(timeout=10000)

    inicio = time.time()
    while time.time() - inicio < 10:
        try:
            url_agora = page.url or ""
        except Exception:
            url_agora = ""
        if url_agora and url_agora != url_antes:
            break
        page.wait_for_timeout(200)

    page.wait_for_load_state("domcontentloaded")
    # Aguarda o skeleton sumir (Angular hidratando apos domcontentloaded).
    # Timeout reduzido para 5s: listar_cotas_na_pagina tem estabilizacao
    # propria e aguarda os cards renderizarem, tornando espera longa desnecessaria.
    try:
        page.locator("[class*='animate-pulse']").first.wait_for(
            state="hidden", timeout=5000
        )
    except Exception:
        pass


# ============================================================
# Cotas na pagina do cliente
# ============================================================

_RE_GRUPO_COTA = re.compile(r"Grupo\s+(\d+)\s*\|\s*Cota\s+(\d+)", re.IGNORECASE)


def listar_cotas_na_pagina(
    page, timeout_ms: int = 60000
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Le TODOS os cards de cota visiveis na pagina do cliente, SEM expandir
    'Mostrar mais'. Cada card tem um <p> 'Grupo XXXXXX | Cota YYYY'.

    O AVAPRO as vezes exibe a MESMA cota (mesmo grupo/cota/contrato) em
    dois ou mais cards - um defeito visual deles. Aqui deduplicamos por
    (grupo, cota) e tambem reportamos quais cotas vieram repetidas, para
    o worker registrar um aviso no log (rastreabilidade).

    Retorna uma tupla (cotas, duplicadas):
      cotas: lista de dicts {'grupo':'001663','cota':'0614',
                             'grupo_raw':'001663','cota_raw':'614'}
             (deduplicada; grupo/cota com zfill 6/4).
      duplicadas: lista de 'GGGGGG/CCCC' que apareceram em 2+ cards.
    """
    # Aguarda o primeiro card aparecer
    try:
        page.locator(
            "p.text-base.font-medium:has-text('Grupo')"
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass

    def _ler_cards():
        try:
            headers = page.locator("p.text-base.font-medium").all()
        except Exception:
            return [], {}
        cotas_local: List[Dict[str, str]] = []
        contagem_local: Dict[tuple, int] = {}
        for h in headers:
            txt = _texto_seguro(h, 1500)
            if not txt:
                continue
            m = _RE_GRUPO_COTA.search(txt)
            if not m:
                continue
            grupo_raw = m.group(1)
            cota_raw = m.group(2)
            g6 = _so_digitos(grupo_raw).zfill(6)
            c4 = _so_digitos(cota_raw).zfill(4)
            chave = (g6, c4)
            contagem_local[chave] = contagem_local.get(chave, 0) + 1
            if contagem_local[chave] > 1:
                continue
            cotas_local.append({
                "grupo": g6,
                "cota": c4,
                "grupo_raw": grupo_raw,
                "cota_raw": cota_raw,
            })
        return cotas_local, contagem_local

    # Estabilizacao: aguarda a contagem de cards parar de crescer
    # (maximo 5 ciclos de 400ms = 2s apos o primeiro card aparecer).
    ESTAB_NECESSARIAS = 3
    INTERVALO_MS = 400
    estab = 0
    qtd_anterior = -1
    cotas: List[Dict[str, str]] = []
    contagem: Dict[tuple, int] = {}
    for _ in range(10):
        cotas, contagem = _ler_cards()
        qtd_atual = len(cotas)
        if qtd_atual == qtd_anterior and qtd_atual > 0:
            estab += 1
            if estab >= ESTAB_NECESSARIAS:
                break
        else:
            estab = 0
        qtd_anterior = qtd_atual
        page.wait_for_timeout(INTERVALO_MS)

    duplicadas = [
        f"{g}/{c}" for (g, c), n in contagem.items() if n > 1
    ]
    return cotas, duplicadas


def localizar_card_cota(page, grupo: str, cota: str, timeout_ms: int = 15000):
    """Retorna o locator do <div> raiz do card 'Grupo X | Cota Y'."""
    g_zfill = _so_digitos(grupo).zfill(6)
    c_digit = _so_digitos(cota)
    c_sem_zero = c_digit.lstrip("0") or "0"

    padroes = [
        f"Grupo {g_zfill} | Cota {c_sem_zero}",
        f"Grupo {g_zfill} | Cota {c_digit.zfill(4)}",
        f"Grupo {g_zfill} | Cota {c_digit}",
    ]

    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        for padrao in padroes:
            try:
                loc = page.locator(
                    f"p.text-base.font-medium:has-text('{padrao}')"
                ).first
                if loc.count() > 0:
                    return loc.locator(
                        "xpath=ancestor::div[contains(@class,'flex')][1]"
                    )
            except Exception:
                continue
        page.wait_for_timeout(300)

    raise RuntimeError(
        f"Card da cota nao encontrado: grupo={g_zfill} cota={c_digit}"
    )


def verificar_badge_excluido(card_root) -> bool:
    """
    Retorna True se o card da cota tiver o badge 'Excluído' (ou 'Excluido')
    visivel, indicando que a cota foi excluida no AVAPRO.
    """
    seletores_badge = [
        "span:has-text('Excluído')",
        "span:has-text('Excluido')",
        "span:text-matches('Exclu[ií]do', 'i')",
        ":has-text('Excluído')",
        ":has-text('Excluido')",
    ]
    for sel in seletores_badge:
        try:
            loc = card_root.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def verificar_badge_desistente(card_root) -> bool:
    """
    Retorna True se o card da cota tiver o badge 'Desistente' visivel,
    indicando que o cliente desistiu do consorcio no AVAPRO.

    HTML do badge:
      <span class="inline-flex items-center ... capitalize">desistente</span>

    O card_root deve ser o locator retornado por localizar_card_cota().
    """
    seletores_badge = [
        "span:has-text('Desistente')",
        "span:has-text('desistente')",
        "span:text-matches('desistente', 'i')",
        ":has-text('Desistente')",
    ]
    for sel in seletores_badge:
        try:
            loc = card_root.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def clicar_mostrar_mais(page, card_root) -> None:
    """
    Clica no 'Mostrar mais' DESTE card (botao e irmao do <p> dentro do
    mesmo container flex-col). Nao usa fallback page-wide para nao abrir
    o card errado quando o cliente tem varias cotas.
    """
    candidatos = [
        card_root.locator("button:has-text('Mostrar mais')").first,
        card_root.locator(
            "xpath=following-sibling::*//button[contains(., 'Mostrar mais')]"
        ).first,
        card_root.locator(
            "xpath=ancestor::div[1]//button[contains(., 'Mostrar mais')]"
        ).first,
    ]
    for btn in candidatos:
        try:
            if btn.count() > 0 and btn.is_visible():
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=5000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue
    raise RuntimeError("Botao 'Mostrar mais' nao encontrado para o card")


# Seletores que indicam que ALGUM modal/overlay da cota esta aberto.
_SEL_MODAL_ABERTO = [
    "[role='dialog']",
    "i.fa-close", "i.fa-times",
    "[class*='fa-close']", "[class*='fa-times']",
]

# Botoes/icones de fechar, em ordem de preferencia.
_SEL_FECHAR = [
    "button:has(i[class*='fa-close'])",
    "button:has(i[class*='fa-times'])",
    "[role='dialog'] button:has(svg path[d='M6 18 18 6M6 6l12 12'])",
    "button[aria-label*='Close' i]",
    "button[aria-label*='Fechar' i]",
    "i.fa-close", "i.fa-times",
    "[class*='fa-close']", "[class*='fa-times']",
]


def _modal_aberto(page) -> bool:
    for sel in _SEL_MODAL_ABERTO:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def fechar_modal(page) -> None:
    """
    Fecha o modal de detalhes da cota (aberto pelo 'Mostrar mais').

    O AVAPRO usa markups diferentes para o botao de fechar conforme a
    tela: o modal 'Plano de venda / Saldo acumulado' usa um icone
    FontAwesome <i class="fa-duotone fa-close ...">, enquanto outras
    telas usam <svg> com path de X ou button[aria-label='Fechar'].

    Estrategia (para ate o modal sumir):
      1) Escape
      2) clica cada candidato de botao/icone de fechar (scoped ao dialog)
      3) JS click forcado no botao X dentro do dialog (ignora interceptacao)
      4) ultimo recurso: Escape de novo + clique fora do modal

    Inofensivo se nao houver modal aberto (retorna rapido).
    """
    if not _modal_aberto(page):
        return

    # 1) Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    if not _modal_aberto(page):
        return

    # 2) Tenta cada candidato de fechar (seletores scopados ao dialog primeiro)
    _sels_scopados = [
        "[role='dialog'] button:has(i[class*='fa-close'])",
        "[role='dialog'] button:has(i[class*='fa-times'])",
        "[role='dialog'] button:has(svg path[d='M6 18 18 6M6 6l12 12'])",
        "[role='dialog'] button[aria-label*='Close' i]",
        "[role='dialog'] button[aria-label*='Fechar' i]",
        "[role='dialog'] button[class*='text-gray']",
    ] + _SEL_FECHAR
    for sel in _sels_scopados:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            loc.click(timeout=2500)
            page.wait_for_timeout(300)
            if not _modal_aberto(page):
                return
            # As vezes o alvo clicavel e o ancestral <button> do <i>
            try:
                botao = loc.locator("xpath=ancestor-or-self::button[1]").first
                if botao.count() > 0 and botao.is_visible():
                    botao.click(timeout=2000)
                    page.wait_for_timeout(300)
                    if not _modal_aberto(page):
                        return
            except Exception:
                pass
        except Exception:
            continue

    # 3) Clique forcado no botao X via dispatch_event (Python) — ignora
    #    elementos interceptando o clique, sem rolar a pagina.
    _sels_x = [
        "[role='dialog'] button[class*='text-gray']",
        "[role='dialog'] button:has(i[class*='fa-close'])",
        "[role='dialog'] i[class*='fa-close']",
        "[role='dialog'] i[class*='fa-times']",
    ]
    for _sel_x in _sels_x:
        try:
            _el = page.locator(_sel_x).first
            if _el.count() == 0:
                continue
            _el.dispatch_event("click")
            page.wait_for_timeout(400)
            if not _modal_aberto(page):
                return
        except Exception:
            continue

    # 4) Ultimo recurso: Escape extra + clique no canto (fora do modal)
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:
            pass
        if not _modal_aberto(page):
            return
    try:
        page.mouse.click(8, 8)
        page.wait_for_timeout(300)
    except Exception:
        pass


def modal_aberto(page) -> bool:
    """Versao publica de _modal_aberto — usada pelo worker para verificar pos-fechar."""
    return _modal_aberto(page)


# ============================================================
# Parsing
# ============================================================

def _parse_int_pt(texto: str) -> Optional[int]:
    """'000' -> 0, '2' -> 2, '-' -> None, '  001 ' -> 1."""
    if texto is None:
        return None
    s = str(texto).strip()
    if not s or s == "-":
        return None
    s = re.sub(r"\D", "", s)
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def _parse_data_pt(texto: str) -> Optional[datetime]:
    """'08/06/2026' -> datetime."""
    if not texto:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(texto))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d/%m/%Y")
    except Exception:
        return None


def _texto_seguro(loc, timeout_ms: int = 1500) -> str:
    try:
        t = loc.inner_text(timeout=timeout_ms)
        return (t or "").strip()
    except Exception:
        return ""


def ler_dados_cota_expandida(page) -> Dict[str, Any]:
    """
    Le os dados do 'Mostrar mais' / modal 'Saldo acumulado' da cota:
      - assembleia_atual, vencimento_str, vencimento_dt
      - parcelas_atraso (Unidade da linha 'Atraso(s)')
      - parcela_atual   (Unidade da linha 'Vl. da parcela')

    Se houver um [role='dialog'] aberto, le DENTRO dele (evita ler a tabela
    de outra cota quando o conteudo eventualmente abre inline).
    """
    dados: Dict[str, Any] = {
        "assembleia_atual": None,
        "vencimento_str": None,
        "vencimento_dt": None,
        "parcelas_atraso": None,
        "parcela_atual": None,
    }

    # Escopo: dialog se aberto, senao a pagina inteira.
    scope = page
    try:
        dlg = page.locator("[role='dialog']").first
        if dlg.count() > 0 and dlg.is_visible():
            scope = dlg
    except Exception:
        scope = page

    # Espera a tabela renderizar.
    try:
        scope.locator(
            "table tr:has(td:text-matches('Atraso|Vl', 'i'))"
        ).first.wait_for(state="visible", timeout=4000)
    except Exception:
        pass

    # Assembleia + Vencimento via data-slot.
    try:
        labels = scope.locator("span[data-slot='data-item-label']").all()
    except Exception:
        labels = []

    for label_loc in labels:
        try:
            txt = _texto_seguro(label_loc, 1500)
            if not txt:
                continue
            valor_loc = label_loc.locator(
                "xpath=following-sibling::span[@data-slot='data-item-value'][1]"
            )
            if valor_loc.count() == 0:
                valor_loc = label_loc.locator("xpath=following-sibling::span[1]")
            valor = _texto_seguro(valor_loc, 1500)
            if "Assembleia" in txt and dados["assembleia_atual"] is None:
                dados["assembleia_atual"] = valor
            elif "Vencimento" in txt and dados["vencimento_str"] is None:
                dados["vencimento_str"] = valor
                dados["vencimento_dt"] = _parse_data_pt(valor)
        except Exception:
            continue

    # Tabela: itera <tr> e classifica pelo texto do 1o <td>.
    try:
        rows = scope.locator("table tr").all()
    except Exception:
        rows = []

    for row in rows:
        try:
            tds = row.locator("td").all()
            if len(tds) < 2:
                continue
            label = _texto_seguro(tds[0], 1500)
            if not label:
                continue
            unidade = _texto_seguro(tds[1], 1500)
            label_norm = label.lower()
            if dados["parcelas_atraso"] is None and label_norm.startswith("atraso"):
                dados["parcelas_atraso"] = _parse_int_pt(unidade)
            elif (
                dados["parcela_atual"] is None
                and "vl" in label_norm
                and "parcela" in label_norm
            ):
                dados["parcela_atual"] = _parse_int_pt(unidade)
        except Exception:
            continue

    return dados


def classificar_modalidade_por_vencimento(vencimento_dt) -> Optional[str]:
    """
    Dada a data de vencimento da cota (datetime/date lido apos 'Mostrar mais'),
    retorna a modalidade a que ela pertence:

      - dia < 15  -> 'MOTORS'  (base dia 7 + proximo dia util; max ~dia 14)
      - dia >= 15 -> 'IMOVEL'  (base dia 15 + proximo dia util; min dia 15)

    None se nao foi possivel determinar (vencimento_dt vazio/sem .day).

    A regra usa o mesmo corte de dia 15 que `config.modalidades.deve_pular_dia`
    - faixas de MOTORS (~7-14) e IMOVEL (~15-22) nao se sobrepoem mesmo
    considerando rolagem por feriado/FDS, entao a divisao em 15 e segura.

    Uso: filtrar cotas da tela do AVAPRO que pertencem a OUTRA modalidade
    do mesmo cliente - evita inserir como 'cota nao encontrada' uma cota
    de imovel quando o lote em execucao e motors (ou vice-versa), porque
    a outra esta na planilha da outra modalidade, so em outra aba.
    """
    if vencimento_dt is None:
        return None
    try:
        dia = int(vencimento_dt.day)
    except Exception:
        return None
    return "MOTORS" if dia < 15 else "IMOVEL"


# ============================================================
# Selecao da cota + emissao do boleto
# ============================================================

def marcar_checkbox_cota(page, grupo: str, cota: str, log_fn=None) -> None:
    """
    Marca o checkbox da cota. aria-label='Selecionar cota XXXXXX YYY'
    (grupo zfill 6 + cota com/sem zero-pad). scroll + espera visivel.

    `log_fn(acao, detalhe)` opcional: recebe cada evento (estado do checkbox
    antes/depois de cada clique) para gravacao no log do lote.
    """
    def _lg(acao: str, detalhe: str = "") -> None:
        if log_fn:
            try:
                log_fn(acao, detalhe)
            except Exception:
                pass

    raw = str(cota or "").strip()
    digit = _so_digitos(grupo)
    g_zfill = digit.zfill(6) if digit else "000000"

    c_digit = _so_digitos(cota)
    c_sem_zero = c_digit.lstrip("0") or (c_digit or "0")
    c_zfill_4 = c_digit.zfill(4) if c_digit else "0000"

    seletores = [
        f"button[role='checkbox'][aria-label='Selecionar cota {g_zfill} {c_sem_zero}']",
        f"button[role='checkbox'][aria-label='Selecionar cota {g_zfill} {c_zfill_4}']",
        f"button[role='checkbox'][aria-label='Selecionar cota {g_zfill} {raw}']",
        f"button[role='checkbox'][aria-label*='{g_zfill}'][aria-label*='{c_sem_zero}']",
        f"button[role='checkbox'][aria-label*='{g_zfill}'][aria-label*='{c_zfill_4}']",
    ]

    ultimo_erro: Optional[str] = None

    for sel in seletores:
        try:
            cb = page.locator(sel).first
            if cb.count() == 0:
                continue
            try:
                cb.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            cb.wait_for(state="visible", timeout=5000)
            state = cb.get_attribute("data-state") or ""
            _lg(
                "[CHECKBOX] Checkbox da cota localizado",
                f"grupo={g_zfill} cota={c_digit} seletor={sel!r} "
                f"estado_antes=data-state={state!r}",
            )
            if state == "checked":
                _lg(
                    "[CHECKBOX] Checkbox ja estava marcado — nenhum clique necessario",
                    f"grupo={g_zfill} cota={c_digit}",
                )
                return  # ja marcado, ok

            # Tenta clicar ate 3x, verificando data-state apos cada clique
            state_pos = state
            for _tentativa in range(1, 4):
                cb.click(timeout=5000)
                page.wait_for_timeout(400)
                state_pos = cb.get_attribute("data-state") or ""
                _lg(
                    f"[CHECKBOX] Clique {_tentativa}/3 no checkbox",
                    f"grupo={g_zfill} cota={c_digit} "
                    f"estado_apos_clique=data-state={state_pos!r}",
                )
                if state_pos == "checked":
                    _lg(
                        "[CHECKBOX] Checkbox confirmado como MARCADO",
                        f"grupo={g_zfill} cota={c_digit} tentativa={_tentativa}",
                    )
                    return  # confirmado como marcado
                page.wait_for_timeout(300)

            # 3 cliques sem sucesso — tenta o proximo seletor
            ultimo_erro = (
                f"checkbox encontrado mas nao ficou checked "
                f"apos 3 cliques (data-state={state_pos!r})"
            )
            _lg(
                "[CHECKBOX] 3 cliques sem confirmar marcado — tentando proximo seletor",
                f"grupo={g_zfill} cota={c_digit} seletor={sel!r} "
                f"data-state_final={state_pos!r}",
            )
            continue
        except Exception as e:
            ultimo_erro = str(e)
            continue

    _lg(
        "[CHECKBOX] FALHA: checkbox nao encontrado/nao marcavel em nenhum seletor",
        f"grupo={g_zfill} cota={c_digit} seletores_testados={len(seletores)} "
        f"ultimo_erro={ultimo_erro!r}",
    )
    raise RuntimeError(
        f"Checkbox da cota nao encontrado ou nao marcavel: grupo={g_zfill} cota={c_digit} "
        f"(tentou {len(seletores)} seletores; ultimo_erro={ultimo_erro!r})"
    )


def detectar_toast_sem_cobrancas(page, timeout_s: int = 3) -> Optional[str]:
    """
    Apos o clique em 'Emitir boleto', se a cota nao tem cobrancas
    disponiveis (cliente adiantado/sem parcela elegivel), o AVAPRO
    dispara DOIS toasts vermelhos. O segundo carrega o motivo:

        Grupo 001707 | Cota 1109: Nao existem cobrancas disponiveis
        para a cota.

    HTML aproximado:
        <div data-title="" class="">Grupo ... | Cota ...: Nao existem
        cobrancas disponiveis para a cota.</div>

    Esse toast some em ~5 segundos. Pra capturar a tempo usamos
    `wait_for(state="visible")` do Playwright, que reage ao DOM via
    MutationObserver (responde em milissegundos), e nao poll manual.

    O seletor usa regex Playwright (`text=/.../i`) case-insensitive
    com classes de caractere `[aã]` e `[çc]` pra cobrir variantes de
    acento.

    Retorna o texto exato do toast (util pra log/observacao) ou None
    se nao apareceu dentro do timeout.
    """
    seletor = "text=/n[aã]o existem cobran[çc]as/i"
    loc = page.locator(seletor).first
    try:
        loc.wait_for(state="visible", timeout=timeout_s * 1000)
    except Exception:
        return None

    try:
        texto = (loc.inner_text() or "").strip()
        return texto or "Nao existem cobrancas disponiveis para a cota"
    except Exception:
        return "Nao existem cobrancas disponiveis para a cota"


def clicar_baixar_documentos_emitir_boleto(page, log_fn=None) -> None:
    """
    Clica direto no botao 'Emitir boleto' (botao primario do card,
    fundo vermelho/primary). Nao usa mais o dropdown 'Baixar documentos'.

    `log_fn(acao, detalhe)` opcional: registra estado do botao (visivel/
    habilitado) e o seletor usado, para auditoria no log do lote.
    """
    def _lg(acao: str, detalhe: str = "") -> None:
        if log_fn:
            try:
                log_fn(acao, detalhe)
            except Exception:
                pass

    seletores = [
        "button[data-slot='button']:has-text('Emitir boleto')",
        "button.rounded-full:has-text('Emitir boleto')",
        "button:has-text('Emitir boleto')",
    ]
    ultimo_erro = None
    for sel in seletores:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            btn.scroll_into_view_if_needed(timeout=2000)
            btn.wait_for(state="visible", timeout=8000)
            try:
                _habilitado = btn.is_enabled(timeout=1000)
            except Exception:
                _habilitado = None
            _lg(
                "[BOTAO] 'Emitir boleto' antes do clique",
                f"seletor={sel!r} visivel=sim "
                f"habilitado={'sim' if _habilitado else ('nao' if _habilitado is False else 'indeterminado')}",
            )
            btn.click(timeout=8000)
            _lg("[BOTAO] 'Emitir boleto' clicado com sucesso", f"seletor={sel!r}")
            return
        except Exception as e:
            ultimo_erro = e
            _lg(
                "[BOTAO] Tentativa de clicar 'Emitir boleto' falhou — proximo seletor",
                f"seletor={sel!r} erro={type(e).__name__}: {e}",
            )
            continue
    _lg(
        "[BOTAO] FALHA: 'Emitir boleto' nao encontrado em nenhum seletor",
        f"seletores_testados={len(seletores)} ultimo_erro={ultimo_erro!r}",
    )
    raise RuntimeError("Botao 'Emitir boleto' nao encontrado na pagina")


# ============================================================
# Monitoramento da pasta Downloads (Windows)
# ============================================================
#
# Em vez de usar page.expect_download(), monitoramos a pasta Downloads
# do usuario antes/depois do clique em "Emitir boleto" e movemos o PDF
# novo pro destino final. Motivos:
#
#   - page.expect_download() em alguns cenarios deixava o PDF salvo
#     com nome bruto (UUID/hash) na pasta de Downloads, sem renomear.
#   - Clique normal + leitura da pasta e mais "natural" - o site nao
#     consegue distinguir esse fluxo de um download manual humano.
#   - Funciona bem com qualquer extensao que o AVAPRO mande no futuro.

def downloads_dir() -> Path:
    """
    Pasta de Downloads do usuario Windows.
    Padrao: %USERPROFILE%\\Downloads. O Edge respeita essa pasta
    quando o usuario nao alterou a configuracao "Local" no edge://settings/downloads.
    """
    return Path.home() / "Downloads"


def snapshot_pdfs_downloads(pasta: Optional[Path] = None) -> set:
    """
    Retorna set com os paths (str) de TODOS os PDFs ja existentes na
    pasta Downloads no momento da chamada. Usado como baseline pra
    detectar arquivos novos depois do clique em "Emitir boleto".

    Inclui apenas arquivos .pdf - ignora os temporarios do Edge (UUID
    sem extensao), que so existem durante o download em andamento.
    """
    pasta = pasta or downloads_dir()
    try:
        return {str(p) for p in pasta.glob("*.pdf")}
    except Exception:
        return set()


def detectar_detalhes_nao_encontrados(page, timeout_ms: int = 3000) -> bool:
    """
    Retorna True se o dialogo 'Detalhes da cota nao encontrados.' estiver visivel.
    Aparece quando 'Mostrar mais' e clicado e o AVAPRO nao consegue carregar
    os dados do contrato.
    """
    seletores = [
        "p:text('Detalhes da cota não encontrados.')",
        "p:text-matches('Detalhes da cota n.o encontrados', 'i')",
    ]
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=timeout_ms)
                if loc.is_visible():
                    return True
        except Exception:
            continue
    return False


def fechar_dialog_detalhes_nao_encontrados(page) -> bool:
    """
    Clica no botao 'Fechar' do dialogo 'Detalhes da cota nao encontrados.'.
    Retorna True se clicou com sucesso.
    """
    seletores = [
        "button:has-text('Fechar')",
        "[data-react-aria-pressable='true']:has-text('Fechar')",
    ]
    for sel in seletores:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    return False


def verificar_imoveis_no_card(page, card_root) -> bool:
    """
    Verifica se o card da cota contem o icone/texto 'Imoveis' (icone casa SVG
    + span.text-sm). Usado quando 'Mostrar mais' retorna 'Detalhes nao encontrados'
    para inferir modalidade IMOVEL.
    """
    try:
        # Span com texto "Imóveis" dentro do card
        for sel in [
            "span.text-sm:has-text('Imóveis')",
            "span.text-sm:has-text('Imoveis')",
            ".text-sm:has-text('Imóveis')",
        ]:
            loc = card_root.locator(sel).first
            if loc.count() > 0:
                return True
        # Fallback: qualquer texto "imóvel"/"imóveis" no card
        txt = _texto_seguro(card_root, 2000).lower()
        if "imóvel" in txt or "imovel" in txt or "imóveis" in txt or "imoveis" in txt:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------
# Modal de selecao de parcelas (novo fluxo apos "Emitir boleto")
# ---------------------------------------------------------------

_RE_GRUPO_COTA_MODAL = re.compile(r"Grupo\s+(\d+)\s*\|\s*Cota\s+(\d+)", re.IGNORECASE)


def _card_ja_expandido(card_div) -> bool:
    """
    Retorna True se o card do modal ja esta expandido (tabela de parcelas visivel).
    Previne clicar no header e fechar um card que ja estava aberto.
    """
    try:
        tabela = card_div.locator("table").first
        if tabela.count() > 0 and tabela.is_visible():
            return True
    except Exception:
        pass
    # Fallback: checa se existe ao menos uma linha de parcela
    try:
        row = card_div.locator("tr").first
        if row.count() > 0 and row.is_visible():
            return True
    except Exception:
        pass
    return False


def _expandir_card_modal(page, card_div) -> bool:
    """
    Expande o card de uma cota no modal de selecao de parcelas.
    Se o card JA estiver expandido (tabela visivel), retorna True sem clicar
    — evita o bug de fechar o card que o modal abre expandido por padrao.
    """
    # Se ja esta expandido nao faz nada
    if _card_ja_expandido(card_div):
        return True

    candidatos = [
        card_div.locator("button.min-w-0.flex-1").first,
        card_div.locator("button[type='button'].min-w-0").first,
        card_div.locator("button:has(p.text-base)").first,
        card_div.locator("button:has(.lucide-chevron-down)").first,
        card_div.locator("button:has(svg[class*='chevron-down'])").first,
    ]
    for btn in candidatos:
        try:
            if btn.count() > 0 and btn.is_visible():
                # dispatch_event: expande sem rolar a pagina de fundo (scroll fantasma)
                btn.dispatch_event("click")
                page.wait_for_timeout(200)
                if _card_ja_expandido(card_div):
                    return True
                # Fallback: clique normal (scroll só do container)
                btn.scroll_into_view_if_needed(timeout=2000)
                btn.click(timeout=4000)
                page.wait_for_timeout(200)
                # Confirma que expandiu de fato
                if _card_ja_expandido(card_div):
                    return True
        except Exception:
            continue
    return False


def _ler_parcelas_do_card(page, card_div):
    """
    Le as linhas de parcelas de um card expandido no modal.
    Retorna lista de dicts com:
      checkbox_loc, numero, vencimento_str, vencimento_dt, status, em_atraso.

    Tenta primeiro dentro do card_div; se a tabela nao for encontrada ali,
    sobe um nivel (pai) pra cobrir casos onde o conteudo expandido e sibling
    do header do card na arvore DOM.
    """
    resultado = []

    # Determina o escopo de busca: card_div primeiro, pai como fallback.
    escopos = [card_div]
    try:
        pai = card_div.locator("xpath=..").first
        if pai.count() > 0:
            escopos.append(pai)
    except Exception:
        pass

    rows = []
    for escopo in escopos:
        try:
            candidatos = escopo.locator("table tr").all()
            if candidatos:
                rows = candidatos
                break
        except Exception:
            continue

    if not rows:
        return resultado

    # Estrutura real das colunas no modal AVAPRO:
    #   tds[0] = checkbox  (button[role='checkbox'])
    #   tds[1] = Parcela   (ex: "38/220")
    #   tds[2] = Vencimento (ex: "15/05/2026")
    #   tds[3] = Juros/multa
    #   tds[4] = Valor da parcela
    #   tds[5] = Status    (badge "Em atraso" / "Futura")
    for row in rows:
        try:
            tds = row.locator("td").all()
            if len(tds) < 5:
                continue
            # Checkbox esta na primeira celula (pode estar dentro de span[tooltip-trigger])
            checkbox = row.locator("button[role='checkbox'][data-slot='checkbox']").first
            if checkbox.count() == 0:
                checkbox = row.locator("input[type='checkbox'], button[role='checkbox']").first
            if checkbox.count() == 0:
                continue
            numero = _texto_seguro(tds[1], 1000)  # Parcela
            if not numero or not re.search(r"\d", numero):
                continue
            venc_str = _texto_seguro(tds[2], 1000)  # Vencimento
            venc_dt = _parse_data_pt(venc_str)
            valor_str = _texto_seguro(tds[4], 1000)  # Valor da parcela
            # Converte "R$ 358,31" -> 358.31 ou "-R$ 0,33" -> -0.33
            # O sinal negativo deve ser preservado: entradas de credito/ajuste
            # nao devem ser selecionadas (ver logica em selecionar_parcelas_no_modal).
            try:
                _e_negativo = "-" in valor_str
                valor_num = float(
                    re.sub(r"[^\d,]", "", valor_str).replace(",", ".")
                )
                if _e_negativo:
                    valor_num = -valor_num
            except Exception:
                valor_num = 0.0
            status_txt = _texto_seguro(tds[5] if len(tds) > 5 else tds[-1], 1000)
            em_atraso = bool(re.search(r"em\s+atraso", status_txt, re.IGNORECASE))
            resultado.append({
                "checkbox_loc": checkbox,
                "numero": numero,
                "vencimento_str": venc_str,
                "vencimento_dt": venc_dt,
                "valor_str": valor_str,
                "valor_num": valor_num,
                "status": status_txt,
                "em_atraso": em_atraso,
            })
        except Exception:
            continue
    return resultado


def _clicar_checkbox_parcela(page, checkbox_loc) -> bool:
    """
    Marca o checkbox de uma parcela no modal. Retorna True se marcou.
    Retorna False imediatamente se o elemento estiver desabilitado
    (parcelas 'Futura' tem disabled='' e data-disabled='' no HTML).

    Verifica apos cada clique se o checkbox ficou marcado. Se o clique
    desmarcou (toggle indesejado), clica de novo. Ate 3 tentativas.
    """
    def _esta_marcado():
        try:
            state = checkbox_loc.get_attribute("data-state") or ""
            aria = checkbox_loc.get_attribute("aria-checked") or ""
            return state == "checked" or aria == "true"
        except Exception:
            return False

    try:
        # Nao tenta clicar em checkbox desabilitado
        disabled = checkbox_loc.get_attribute("disabled")
        data_disabled = checkbox_loc.get_attribute("data-disabled")
        if disabled is not None or data_disabled is not None:
            return False

        if _esta_marcado():
            return True  # Ja marcado

        for _tent in range(3):
            try:
                if _tent == 0:
                    # Tentativa 1 (PRINCIPAL): dispatch_event('click') — dispara o
                    # clique direto no checkbox, SEM rolar a pagina e sem risco de
                    # o clique cair no fundo (causa do scroll fantasma com muitas
                    # cotas). Puro Playwright/Python.
                    checkbox_loc.dispatch_event("click")
                elif _tent == 1:
                    # Tentativa 2: clique normal (com scroll só do container).
                    checkbox_loc.scroll_into_view_if_needed(timeout=2000)
                    checkbox_loc.click(timeout=4000)
                else:
                    # Tentativa 3: force=True ignora sobreposicao de elementos.
                    checkbox_loc.click(timeout=4000, force=True)
            except Exception:
                page.wait_for_timeout(300)
                continue
            page.wait_for_timeout(300)
            if _esta_marcado():
                return True
            # Clique nao surtiu efeito — aguarda mais antes de tentar de novo
            page.wait_for_timeout(400)

        return False  # Nao conseguiu marcar em 3 tentativas
    except Exception:
        return False


def detectar_e_fechar_erro_parcelas(page, timeout_ms: int = 3000) -> Optional[str]:
    """
    Detecta o dialog de erro 'Nao foi possivel carregar as parcelas para
    emissao do boleto.' que o AVAPRO exibe (em vez do modal de selecao)
    quando o servidor retorna 400 ou falha ao carregar as parcelas.

    HTML aproximado:
      <h2>Emitir boleto</h2>
      <p class="text-sm text-muted-foreground">
        Nao foi possivel carregar as parcelas para emissao do boleto.
      </p>
      <button>Tentar novamente</button>
      <button>Fechar</button>

    Se detectado, clica em 'Fechar' e retorna o texto da mensagem de erro.
    Retorna None se o dialog nao estiver presente.
    """
    # Estrategia: varre todos os <p> visiveis e normaliza o texto para
    # comparacao sem acentos. Mais robusto que text-matches que nao lida
    # bem com acentos no Playwright.
    # NAO fecha o dialog aqui — quem chama deve tirar o print primeiro,
    # depois chamar fechar_dialog_erro_parcelas(page).
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            for loc in page.locator("p").all():
                try:
                    if not loc.is_visible():
                        continue
                    txt = (loc.inner_text(timeout=500) or "").strip()
                    if not txt:
                        continue
                    txt_norm = _remover_acentos(txt).lower()
                    if "nao foi possivel carregar as parcelas" in txt_norm:
                        return txt
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.3)
    return None


def fechar_dialog_erro_parcelas(page) -> None:
    """
    Fecha o dialog de erro 'Nao foi possivel carregar as parcelas'
    clicando em 'Fechar'. Deve ser chamado APOS tirar o screenshot.
    """
    for btn_txt in ["Fechar", "fechar", "Close"]:
        try:
            btn = page.locator(f"button:has-text('{btn_txt}')").first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _alterar_data_base_modal(
    page, dia: int, mes: int, ano: int, campo: str = "venc_boleto"
) -> Optional[datetime]:
    """
    Abre um dos calendarios do modal de selecao de parcelas e seleciona
    a data dia/mes/ano informada.

    campo:
      'venc_boleto'    -> calendario 'Venc. boleto' (ultimo campo do modal)
      'base_pendencia' -> calendario 'Base pendencia' (penultimo campo)

    Fluxo:
      1. Clica no botao do campo de data escolhido.
      2. Aguarda o popover do calendario aparecer.
      3. Navega para o mes/ano correto (botoes < e >).
      4. Clica no botao do dia desejado.

    Retorna o datetime selecionado se conseguiu, None caso contrario.
    """
    _MESES_PT_CAL = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
        "abril": 4, "maio": 5, "junho": 6, "julho": 7,
        "agosto": 8, "setembro": 9, "outubro": 10,
        "novembro": 11, "dezembro": 12,
    }
    try:
        # --- Clica no botao "Venc. boleto" (calendario da direita no modal) ---
        # O modal tem 4 campos: Grupo e cota | Segmento e status | Base pendencia | Venc. boleto
        # Os tres ultimos sao popover-triggers. Precisamos clicar especificamente em
        # "Venc. boleto" (o ultimo). Estrategias em ordem de prioridade:
        #   1) Busca o <p> "Venc. boleto" e pega o proximo button no DOM.
        #   2) Div pai que contem o label "Venc. boleto" + button dentro.
        #   3) Ultimo [data-slot='popover-trigger'] button da pagina.
        # --- Clica no botao do campo de data escolhido ---
        # Define a data de vencimento do boleto para o dia correto do mes ref.
        # Se nenhuma parcela aparecer na data base atual ("Base pendencia"), o
        # worker trata a cota como ADIANTADO (nao ha debito no mes corrente).
        _btn_data = None
        if campo == "base_pendencia":
            # 'Base pendencia' e o penultimo campo de data do modal.
            # Sem acento no match ('pend') para cobrir 'pendencia'/'pendência'.
            _seletores_campo = [
                # Estrategia 1: xpath a partir do label
                "xpath=//p[contains(normalize-space(.), 'ase pend') or "
                "contains(normalize-space(.), 'ase Pend')]/following::button[1]",
                # Estrategia 2: div pai com label
                "div:has(p:text-matches('base\\s*pend', 'i')) button",
            ]
        else:
            _seletores_campo = [
                # Estrategia 1: xpath a partir do label
                "xpath=//p[contains(normalize-space(.), 'Venc. boleto') or "
                "contains(normalize-space(.), 'Venc boleto')]/following::button[1]",
                # Estrategia 2: div pai com label
                "div:has(p:has-text('Venc. boleto')) button",
                "div:has(p:text-matches('Venc\\.?\\s*boleto', 'i')) button",
            ]
        for _sel in _seletores_campo:
            try:
                _loc = page.locator(_sel).first
                if _loc.count() > 0 and _loc.is_visible():
                    _btn_data = _loc
                    break
            except Exception:
                continue
        # Fallback pela posicao dos popover-triggers:
        #   Venc. boleto = ultimo | Base pendencia = penultimo
        if _btn_data is None:
            _triggers = page.locator("[data-slot='popover-trigger'] button")
            if campo == "base_pendencia":
                _qtd = _triggers.count()
                if _qtd >= 2:
                    _btn_data = _triggers.nth(_qtd - 2)
                else:
                    return None
            else:
                _btn_data = _triggers.last
        _btn_data.wait_for(state="visible", timeout=5000)
        _btn_data.click(timeout=5000)
        page.wait_for_timeout(200)

        # --- Aguarda o calendario aparecer ---
        # O calendario e um popover Radix UI — conteudo no popper wrapper
        _cal_wrapper = page.locator(
            "[data-radix-popper-content-wrapper]"
        ).first
        _cal_wrapper.wait_for(state="visible", timeout=5000)

        # --- Le mes/ano atual do cabecalho do calendario ---
        def _ler_mes_ano():
            try:
                _txt = _cal_wrapper.inner_text(timeout=2000).lower()
                for _nome, _num in _MESES_PT_CAL.items():
                    if _nome in _txt:
                        _m = re.search(r"\b(20\d{2})\b", _txt)
                        if _m:
                            return _num, int(_m.group(1))
            except Exception:
                pass
            return None, None

        # Botoes de navegacao do calendario (chevron-left e chevron-right)
        _btn_prev = _cal_wrapper.locator(
            "button:has(svg[class*='chevron-left']), button:has(svg path[d*='m15 18'])"
        ).first
        _btn_next = _cal_wrapper.locator(
            "button:has(svg[class*='chevron-right']), button:has(svg path[d*='m9 18'])"
        ).first

        # --- Navega para o mes/ano correto ---
        for _ in range(24):
            _mes_cal, _ano_cal = _ler_mes_ano()
            if _mes_cal is None or _ano_cal is None:
                break
            if _mes_cal == mes and _ano_cal == ano:
                break
            _data_cal = _ano_cal * 12 + _mes_cal
            _data_alvo = ano * 12 + mes
            if _data_alvo > _data_cal:
                _btn_next.click(timeout=3000)
            else:
                _btn_prev.click(timeout=3000)
            page.wait_for_timeout(150)

        # --- Clica no dia desejado ---
        # Botoes de dia: texto exatamente igual ao numero (ex: "15")
        # Filtra enabled (nao disabled) para nao clicar em dias de outro mes
        _btn_dia = _cal_wrapper.locator(
            f"button:text-is('{dia}'):not([disabled]):not([data-disabled])"
        ).first
        if _btn_dia.count() == 0:
            _btn_dia = _cal_wrapper.locator(
                f"button:text-is('{dia}')"
            ).first
        _btn_dia.wait_for(state="visible", timeout=3000)
        _btn_dia.click(timeout=3000)
        page.wait_for_timeout(400)

        # --- VERIFICA que o campo passou a refletir a data escolhida ---
        # Sem isso, uma falha silenciosa deixava o campo em "Hoje" e o robo
        # seguia selecionando parcelas com a data errada.
        try:
            _txt_btn = (_btn_data.inner_text(timeout=2000) or "").strip().lower()
        except Exception:
            _txt_btn = ""
        _dd = f"{dia:02d}"
        _mm = f"{mes:02d}"
        _confirmou = (
            (_dd in _txt_btn and _mm in _txt_btn)
            or (f"{dia}/{mes}" in _txt_btn)
            or (str(ano) in _txt_btn and _mm in _txt_btn)
        )
        if not _confirmou:
            # Campo nao refletiu a data -> considera falha (quem chama retenta)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        return datetime(ano, mes, dia)

    except Exception:
        # Fecha o calendario se ainda estiver aberto (tecla Escape)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def _parse_valor_card(texto: str):
    """
    Extrai o valor do rodape do card do modal, ex:
      'Valor: R$ 337,30'  /  'Valor: -R$ 337,30'  /  'Valor: R$ -337,30'

    Robusto a:
      - &nbsp; (\\xa0) entre 'R$' e o numero
      - letras/simbolos entre o sinal '-' e os digitos (ex: '-R$ 337,30')
      - sinal negativo em qualquer posicao antes do numero

    Retorna (valor_str, negativo):
      valor_str: ex '-R$ 337,30' (None se nenhum numero encontrado)
      negativo:  True se o valor e negativo
    """
    t = (texto or "").replace("\xa0", " ").replace("−", "-")
    # Isola a parte apos 'Valor' (se presente)
    if "Valor" in t:
        t = t.split("Valor", 1)[1]
        if ":" in t:
            t = t.split(":", 1)[1]
    m = re.search(r"(\d[\d\.]*,\d{2})", t)
    if not m:
        return None, False
    # Negativo se houver '-' em QUALQUER ponto antes dos digitos
    # (cobre '-R$ 337,30', 'R$ -337,30', '- R$ 337,30') ou logo apos.
    antes = t[:m.start()]
    depois = t[m.end():m.end() + 2]
    negativo = ("-" in antes) or depois.strip().startswith("-")
    valor_str = ("-" if negativo else "") + "R$ " + m.group(1)
    return valor_str, negativo


def selecionar_parcelas_no_modal(
    page,
    data_ref: Optional[datetime] = None,
    modalidade: str = "IMOVEL",
    cotas_bloqueadas: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Trata o modal 'Selecione as parcelas para emissao do boleto'.

    Para cada card de cota no modal:
      1. Expande o card.
      2. Se 'Detalhes da cota nao encontrados.' aparecer → fechar + checar imoveis.
      3. Le as parcelas da tabela.
      4. Seleciona: todas 'Em atraso' + parcela do mes ref.
         Nao seleciona parcelas futuras.
      5. Se parcela do mes ref NAO encontrada → cota marcada como adiantada.

    `cotas_bloqueadas`: conjunto de (grupo_zfill6, cota_zfill4) que JA estao
    BAIXADAS num boleto anterior. O AVAPRO pode reexibi-las no modal de
    unificacao; para essas NAO selecionamos parcela nenhuma (evita dupla
    emissao). Elas sao devolvidas em resultado['cotas_bloqueadas_baixadas']
    para o worker registrar a FALHA com observacao especifica.

    Retorna:
      {
        'por_cota': {(grupo, cota): {'parcelas_atraso', 'mes_ref_encontrado',
                                     'sem_detalhes', 'imoveis'}},
        'adiantados_modal': [(grupo, cota), ...],
        'cotas_bloqueadas_baixadas': [(grupo, cota), ...],
        'total_selecionadas': int,
        'pode_continuar': bool,
        'erro': str|None,
        'erro_retriable': bool,  # True = problema de servidor (tentar de novo)
      }
    """
    cotas_bloqueadas = cotas_bloqueadas or set()
    if data_ref is None:
        data_ref = datetime.now()
    mes_ref = data_ref.month
    ano_ref = data_ref.year

    _MESES_EXTENSO = {
        1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril",
        5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
        9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
    }

    resultado: Dict[str, Any] = {
        "por_cota": {},
        "adiantados_modal": [],
        "cotas_bloqueadas_baixadas": [],  # cotas ja BAIXADAS reexibidas no modal
        "total_selecionadas": 0,
        "pode_continuar": False,
        "erro": None,
        "meses_parcelas": [],  # meses por extenso das parcelas selecionadas (ordem cronologica)
        "vencimento_esperado_dt": None,  # data calculada para o venc. boleto (pula fds/feriados)
        "base_pendencia_dt": None,  # data selecionada na 'Base pendencia' (= venc. boleto)
        "obs_adiantados": {},  # {(grupo, cota): observacao especifica do adiantamento}
    }

    try:
        page.locator(
            "h2:text-matches('Selecione as parcelas', 'i')"
        ).first.wait_for(state="visible", timeout=30000)  # 30 segundos
    except Exception as e:
        # Antes de retornar erro generico, checa se o AVAPRO exibiu o dialog
        # de erro "Nao foi possivel carregar as parcelas" (erro 400 do servidor).
        # NAO fecha o dialog aqui — quem chama tira o print primeiro.
        _dialog_err = detectar_e_fechar_erro_parcelas(page, timeout_ms=3000)
        if _dialog_err:
            resultado["erro"] = f"Dialog de erro ao carregar parcelas: {_dialog_err}"
            resultado["erro_retriable"] = True
            resultado["dialog_erro_aberto"] = True  # sinaliza: dialog ainda esta na tela
        else:
            resultado["erro"] = f"Modal de selecao de parcelas nao apareceu apos 30 segundos: {e}"
            resultado["modal_timeout"] = True  # timeout puro — sem dialog de erro
        return resultado

    # --- Altera "Venc. boleto" e "Base pendencia" para o dia de vencimento ---
    # SEMPRE faz isso ao abrir o modal: usa calcular_vencimento (pula fds/feriados),
    # igual ao Newcon. Garante que parcelas do mes ref apareçam como disponiveis
    # mesmo que hoje seja antes do dia de vencimento.
    # A "Base pendencia" recebe A MESMA DATA do vencimento para que parcelas
    # de outros meses NAO aparecam na lista do modal.
    # PASSO OBRIGATORIO: definir "Venc. boleto" (e "Base pendencia") ANTES de
    # ler/selecionar as parcelas. Se a data nao for aplicada, ABORTA como
    # retriable em vez de emitir boleto com "Venc. boleto: Hoje".
    if _calcular_vencimento is None:
        resultado["erro"] = (
            "calcular_vencimento indisponivel (import falhou) — nao da para "
            "definir a data de vencimento do boleto com seguranca"
        )
        resultado["erro_retriable"] = True
        return resultado

    try:
        _mes_ref_yyyymm = ano_ref * 100 + mes_ref
        _venc_correto = _calcular_vencimento(_mes_ref_yyyymm, modalidade)
    except Exception as _e_venc:
        resultado["erro"] = f"Falha ao calcular vencimento: {_e_venc}"
        resultado["erro_retriable"] = True
        return resultado

    # 1) Venc. boleto — obrigatorio, ate 3 tentativas com verificacao
    _dt_selecionada = None
    for _tent_venc in range(3):
        _dt_selecionada = _alterar_data_base_modal(
            page, _venc_correto.day, _venc_correto.month, _venc_correto.year,
            campo="venc_boleto",
        )
        if _dt_selecionada is not None:
            break
        page.wait_for_timeout(400)
    if _dt_selecionada is None:
        resultado["erro"] = (
            "Nao consegui definir 'Venc. boleto' para "
            f"{_venc_correto.strftime('%d/%m/%Y')} apos 3 tentativas"
        )
        resultado["erro_retriable"] = True
        return resultado
    resultado["vencimento_esperado_dt"] = _dt_selecionada
    page.wait_for_timeout(300)

    # 2) Base pendencia = mesma data do vencimento (evita parcelas de outros
    #    meses). NAO-FATAL: se o dia ja passou o calendario pode bloquear datas
    #    passadas; nesse caso segue com a base atual.
    _dt_base = None
    for _tent_base in range(2):
        _dt_base = _alterar_data_base_modal(
            page, _venc_correto.day, _venc_correto.month, _venc_correto.year,
            campo="base_pendencia",
        )
        if _dt_base is not None:
            break
        page.wait_for_timeout(400)
    if _dt_base is not None:
        resultado["base_pendencia_dt"] = _dt_base
    page.wait_for_timeout(300)

    # --- Detecta "Nenhuma parcela disponivel para pagamento na data selecionada" ---
    # Faz polling direto no <p> alvo — sem esperar "Atualizando valores..." sumir.
    # Se a mensagem aparecer a qualquer momento (mesmo durante o loading),
    # ja e suficiente para concluir ADIANTADO.
    # Tenta ate 20x com 200ms entre elas (ate 4s no total).
    # Seletores da mensagem GLOBAL do modal (texto completo, com
    # "...para pagamento na data selecionada"). NAO usar o prefixo curto
    # aqui: cada CARD tambem pode exibir "Nenhuma parcela disponível"
    # individualmente, e isso NAO significa que o modal inteiro esta vazio —
    # essas cotas sao tratadas como ADIANTADO no loop de cards e a leitura
    # continua nas demais.
    _SEL_SEM_PARCELAS = [
        "p:text-matches('Nenhuma parcela dispon[ií]vel para pagamento', 'i')",
        "text=/Nenhuma parcela dispon[ií]vel para pagamento na data selecionada/i",
    ]
    # Versao curta (prefixo) — usada SOMENTE quando nao ha nenhum card no
    # modal (sem risco de confundir com a mensagem por card).
    _SEL_SEM_PARCELAS_CURTO = [
        "p.text-sm.text-muted-foreground:text-matches('Nenhuma parcela dispon', 'i')",
        "p:text-matches('Nenhuma parcela dispon', 'i')",
    ]

    def _verificar_msg_sem_parcelas(incluir_curto: bool = False) -> bool:
        _sels = list(_SEL_SEM_PARCELAS)
        if incluir_curto:
            _sels += _SEL_SEM_PARCELAS_CURTO
        for _s in _sels:
            try:
                _loc = page.locator(_s).first
                if _loc.count() > 0 and _loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    _tem_msg_sem_parcelas = False
    for _chk in range(20):
        if _verificar_msg_sem_parcelas():
            _tem_msg_sem_parcelas = True
            break
        page.wait_for_timeout(200)

    if _tem_msg_sem_parcelas:
        resultado["nenhuma_parcela_disponivel"] = True
        # Tenta capturar grupo/cota do titulo do modal para o email de alerta
        try:
            _titulos = page.locator(
                "p.text-base.font-semibold"
            ).all()
            for _t in _titulos:
                _txt = _texto_seguro(_t, 2000)
                _m = _RE_GRUPO_COTA_MODAL.search(_txt)
                if _m:
                    _g = _so_digitos(_m.group(1)).zfill(6)
                    _c = _so_digitos(_m.group(2)).zfill(4)
                    resultado["adiantados_modal"].append((_g, _c))
                    resultado.setdefault("cotas_sem_parcelas", []).append((_g, _c))
        except Exception:
            pass
        # Se nao encontrou nenhuma cota no titulo, ainda marca nenhuma_parcela_disponivel
        # para que o worker envie o email de alerta
        resultado["total_selecionadas"] = 0
        resultado["pode_continuar"] = False
        return resultado

    cards_sel = "div.rounded-xl.border:has(p.text-base.font-semibold)"
    try:
        cards = page.locator(cards_sel).all()
    except Exception:
        cards = []

    if not cards:
        # Antes de reportar erro, verifica novamente se a mensagem "Nenhuma parcela
        # disponivel" apareceu (pode ter surgido apos a mudanca de data base).
        # Evita classificar como NAO_BAIXADO o que e ADIANTADO.
        # Sem cards no modal, a versao curta da mensagem tambem vale como global.
        if _verificar_msg_sem_parcelas(incluir_curto=True):
            resultado["nenhuma_parcela_disponivel"] = True
            resultado["total_selecionadas"] = 0
            resultado["pode_continuar"] = False
            return resultado
        resultado["erro"] = "Nenhum card de cota encontrado no modal"
        return resultado

    total_selecionadas = 0

    # Cotas com valor NEGATIVO no card: as parcelas foram desmarcadas e a
    # varredura global pos-loop NAO deve remarca-las.
    _cotas_excluidas_sweep: set = set()

    for card in cards:
        grupo_card, cota_card = None, None
        try:
            titulo_txt = _texto_seguro(
                card.locator("p.text-base.font-semibold").first, 2000
            )
            m = _RE_GRUPO_COTA_MODAL.search(titulo_txt)
            if m:
                grupo_card = _so_digitos(m.group(1)).zfill(6)
                cota_card  = _so_digitos(m.group(2)).zfill(4)
        except Exception:
            pass

        chave = (grupo_card, cota_card) if grupo_card else None

        # GUARD: cota ja BAIXADA num boleto anterior reapareceu no modal de
        # unificacao. NAO seleciona parcela nenhuma dela (evita dupla emissao).
        # Devolve a chave para o worker registrar a FALHA especifica.
        if chave and chave in cotas_bloqueadas:
            resultado["cotas_bloqueadas_baixadas"].append(chave)
            if chave not in _cotas_excluidas_sweep:
                _cotas_excluidas_sweep.add(chave)  # tambem exclui da varredura global
            continue

        expandiu = _expandir_card_modal(page, card)
        if not expandiu:
            if chave:
                resultado["por_cota"][chave] = {
                    "parcelas_atraso": 0,
                    "mes_ref_encontrado": False,
                    "sem_detalhes": False,
                    "imoveis": False,
                }
            continue

        page.wait_for_timeout(200)

        sem_detalhes = False
        imoveis_flag = False
        try:
            det_loc = card.locator(
                "p:text-matches('Detalhes da cota', 'i')"
            ).first
            if det_loc.count() == 0:
                det_loc = page.locator(
                    "p:text-matches('Detalhes da cota n.o encontrados', 'i')"
                ).first
            if det_loc.count() > 0 and det_loc.is_visible():
                sem_detalhes = True
                fechar_dialog_detalhes_nao_encontrados(page)
                page.wait_for_timeout(300)
                imoveis_flag = verificar_imoveis_no_card(page, card)
        except Exception:
            pass

        if sem_detalhes:
            if chave:
                resultado["por_cota"][chave] = {
                    "parcelas_atraso": 0,
                    "mes_ref_encontrado": False,
                    "sem_detalhes": True,
                    "imoveis": imoveis_flag,
                }
            continue

        # --- Card sem parcelas: "Nenhuma parcela disponível" DENTRO do card ---
        # A cota nao tem debito na data base selecionada -> ADIANTADO.
        # Marca e continua lendo os demais cards (unifica com os disponiveis).
        sem_parcelas_card = False
        try:
            _sp_loc = card.locator(
                "p:text-matches('Nenhuma parcela dispon', 'i')"
            ).first
            if _sp_loc.count() > 0 and _sp_loc.is_visible():
                sem_parcelas_card = True
        except Exception:
            pass

        if sem_parcelas_card:
            if chave:
                resultado["por_cota"][chave] = {
                    "parcelas_atraso": 0,
                    "mes_ref_encontrado": False,
                    "sem_detalhes": False,
                    "imoveis": False,
                }
                resultado["adiantados_modal"].append(chave)
                resultado.setdefault("cotas_sem_parcelas", []).append(chave)
                resultado["obs_adiantados"][chave] = (
                    "Nenhuma parcela disponível (cliente adiantado)"
                )
            continue

        parcelas = _ler_parcelas_do_card(page, card)

        mes_ref_encontrado = False
        parcelas_atraso_count = 0
        cota_selecionadas_count = 0
        # Coleta (vdt, nome_mes) das parcelas selecionadas para ordem cronologica
        _selecionadas_parcelas: List[tuple] = []

        # Lista de parcelas que devem ser selecionadas (para verificacao pos-loop)
        _devem_ser_selecionadas: List[dict] = []

        # Regra de dia de vencimento por modalidade:
        #   IMOVEL : vencimento.day >= 15
        #   MOTORS : vencimento.day <  15
        # Parcelas cujo dia nao bate com a modalidade sao ignoradas
        # (pertencem ao segmento errado — ex: cota Motors no lote Imovel).
        _modalidade_upper = (modalidade or "").upper()

        for p in parcelas:
            vdt = p.get("vencimento_dt")

            # Filtro de dia de vencimento por modalidade
            if vdt is not None:
                _dia_venc = vdt.day
                if _modalidade_upper == "IMOVEL" and _dia_venc < 15:
                    continue  # dia de Motors — ignora neste lote Imovel
                if _modalidade_upper == "MOTORS" and _dia_venc >= 15:
                    continue  # dia de Imovel — ignora neste lote Motors

            # Parcela futura alem do mes ref -> nunca selecionar
            e_futura = (
                vdt is not None
                and (vdt.year > ano_ref or (vdt.year == ano_ref and vdt.month > mes_ref))
            )
            if e_futura:
                continue

            e_mes_ref = (
                vdt is not None
                and vdt.month == mes_ref
                and vdt.year == ano_ref
            )

            # Atraso determinado PELA DATA (vencimento anterior ao mes ref),
            # NAO pelo status "Em atraso" do site. Igual ao rpa_gerar_boleto.
            e_atraso_data = (
                vdt is not None
                and (vdt.year < ano_ref or (vdt.year == ano_ref and vdt.month < mes_ref))
            )

            deve_selecionar = e_atraso_data or e_mes_ref

            if deve_selecionar:
                ok = _clicar_checkbox_parcela(page, p["checkbox_loc"])
                # Guarda a parcela + se ja foi contada com sucesso no loop principal
                _devem_ser_selecionadas.append({
                    **p,
                    "e_mes_ref": e_mes_ref,
                    "e_atraso_data": e_atraso_data,
                    "contada": ok,  # True = ja foi contada abaixo; False = falhou
                })
                if ok:
                    total_selecionadas += 1
                    cota_selecionadas_count += 1
                    # Conta mes_ref e atraso SO se o clique funcionou
                    if e_mes_ref:
                        mes_ref_encontrado = True
                    if e_atraso_data:
                        parcelas_atraso_count += 1
                    if vdt is not None:
                        _selecionadas_parcelas.append((vdt, _MESES_EXTENSO.get(vdt.month, str(vdt.month))))

        # --- Verificacao pos-loop: garante que todos os checkboxes que devem
        # estar marcados realmente estao (aria-checked="true" / data-state="checked").
        # Se algum nao estiver, tenta clicar de novo ate 3 vezes com espera maior.
        if _devem_ser_selecionadas:
            page.wait_for_timeout(300)
            for _p_ver in _devem_ser_selecionadas:
                _cb = _p_ver["checkbox_loc"]
                try:
                    _state = _cb.get_attribute("data-state") or ""
                    _aria  = _cb.get_attribute("aria-checked") or ""
                    _ja_ok = _state == "checked" or _aria == "true"
                except Exception:
                    _ja_ok = False
                if not _ja_ok:
                    # Checkbox nao ficou marcado na primeira passagem — retenta
                    for _rv in range(3):
                        try:
                            # dispatch_event: sem scroll da pagina (evita scroll fantasma)
                            _cb.dispatch_event("click")
                            page.wait_for_timeout(400)
                            _state2 = _cb.get_attribute("data-state") or ""
                            _aria2  = _cb.get_attribute("aria-checked") or ""
                            if _state2 == "checked" or _aria2 == "true":
                                # Contabiliza SOMENTE se nao havia sido contado
                                # no loop principal (evita dupla contagem)
                                if not _p_ver.get("contada"):
                                    _vdt2 = _p_ver.get("vencimento_dt")
                                    total_selecionadas += 1
                                    cota_selecionadas_count += 1
                                    if _p_ver.get("e_mes_ref"):
                                        mes_ref_encontrado = True
                                    if _p_ver.get("e_atraso_data"):
                                        parcelas_atraso_count += 1
                                    if _vdt2 is not None:
                                        _selecionadas_parcelas.append(
                                            (_vdt2, _MESES_EXTENSO.get(_vdt2.month, str(_vdt2.month)))
                                        )
                                break
                        except Exception:
                            page.wait_for_timeout(300)
                            continue

        # --- Verificacao de VALOR NEGATIVO do card ---
        # Apos selecionar as parcelas, le o rodape "Valor: R$ X" do card.
        # Valor negativo = credito (cliente adiantado). Desmarca as parcelas
        # deste card (para o valor negativo NAO entrar no boleto unificado
        # com as outras cotas) e marca a cota como ADIANTADO com o valor
        # a pagar na observacao.
        _valor_card_str = None
        _valor_card_negativo = False
        try:
            page.wait_for_timeout(300)  # rodape atualiza apos a selecao
            _span_valor = card.locator("span:has-text('Valor:')").last
            if _span_valor.count() > 0 and _span_valor.is_visible():
                _valor_card_str, _valor_card_negativo = _parse_valor_card(
                    _texto_seguro(_span_valor, 500)
                )
        except Exception:
            pass

        if _valor_card_negativo:
            # Desmarca TODOS os checkboxes marcados deste card (incluindo o
            # "Selecionar todas as parcelas", que desmarca as linhas juntas).
            try:
                for _cb_neg in card.locator("button[role='checkbox']").all():
                    try:
                        _st_neg = _cb_neg.get_attribute("data-state") or ""
                        _ar_neg = _cb_neg.get_attribute("aria-checked") or ""
                        if _st_neg != "checked" and _ar_neg != "true":
                            continue
                        # dispatch_event: desmarca sem rolar a pagina (sem scroll fantasma)
                        _cb_neg.dispatch_event("click")
                        page.wait_for_timeout(200)
                        # Verifica; retenta com force se ainda marcado
                        _st_neg2 = _cb_neg.get_attribute("data-state") or ""
                        _ar_neg2 = _cb_neg.get_attribute("aria-checked") or ""
                        if _st_neg2 == "checked" or _ar_neg2 == "true":
                            _cb_neg.click(timeout=3000, force=True)
                            page.wait_for_timeout(200)
                    except Exception:
                        continue
            except Exception:
                pass

            # Estorna do total o que havia sido contado para este card
            total_selecionadas -= cota_selecionadas_count
            if total_selecionadas < 0:
                total_selecionadas = 0

            if chave:
                resultado["por_cota"][chave] = {
                    "parcelas_atraso": 0,
                    "mes_ref_encontrado": False,
                    "sem_detalhes": False,
                    "imoveis": False,
                }
                resultado["adiantados_modal"].append(chave)
                resultado["obs_adiantados"][chave] = (
                    f"Valor a pagar negativo no modal: {_valor_card_str} "
                    f"(cliente adiantado)"
                )
                _cotas_excluidas_sweep.add(chave)
            continue

        # Ordena por data (cronologico) e coleta nomes dos meses sem duplicar
        _selecionadas_parcelas.sort(key=lambda x: x[0])
        _meses_vistos: set = set()
        for _vdt, _nome_mes in _selecionadas_parcelas:
            _chave_mes = (_vdt.year, _vdt.month)
            if _chave_mes not in _meses_vistos:
                _meses_vistos.add(_chave_mes)
                resultado["meses_parcelas"].append(_nome_mes)

        if chave:
            resultado["por_cota"][chave] = {
                "parcelas_atraso": parcelas_atraso_count,
                "mes_ref_encontrado": mes_ref_encontrado,
                "sem_detalhes": False,
                "imoveis": False,
            }
            # Adiantado = nenhuma parcela foi selecionavel para esta cota
            # (nem atraso nem mes ref — ex: cliente adiantado, tudo Futura/disabled)
            if cota_selecionadas_count == 0:
                resultado["adiantados_modal"].append(chave)

    # --- Varredura global pos-loop: seleciona TODOS os checkboxes nao marcados
    # e nao desabilitados que ainda existam no modal.
    #
    # Objetivo: garantir que parcelas pre-selecionadas pelo AVAPRO (ou que
    # escaparam do loop de cards por serem "Futura" alem do mes_ref) sejam
    # incluidas. Sem isso, o AVAPRO pode exibir -R$ 0,10 ja marcado e R$ 352,21
    # desmarcado — o footer fica negativo e a cota vai erroneamente para ADIANTADO.
    #
    # Estrategia: 3 passagens com 300ms de espera entre elas para cobrir o caso
    # de checkboxes que ainda nao renderizaram na primeira passagem.
    _MAX_PASSAGENS_GLOBAL = 3
    for _passagem in range(_MAX_PASSAGENS_GLOBAL):
        try:
            _todos_cb = page.locator('button[role="checkbox"]').all()
        except Exception:
            break
        _algum_clicou = False
        for _cb_g in _todos_cb:
            try:
                # Pula desabilitados
                if (_cb_g.get_attribute("disabled") is not None
                        or _cb_g.get_attribute("data-disabled") is not None):
                    continue
                # Pula ja marcados
                _state_g = _cb_g.get_attribute("data-state") or ""
                _aria_g  = _cb_g.get_attribute("aria-checked") or ""
                if _state_g == "checked" or _aria_g == "true":
                    continue
                # Pula checkboxes de cards marcados como ADIANTADO por valor
                # negativo — foram desmarcados de proposito e NAO devem ser
                # remarcados pela varredura global.
                if _cotas_excluidas_sweep:
                    try:
                        _tit_neg = _cb_g.locator(
                            "xpath=ancestor::div[contains(@class,'rounded-xl')][1]"
                            "//p[contains(@class,'font-semibold')]"
                        ).first
                        _m_neg = _RE_GRUPO_COTA_MODAL.search(
                            _texto_seguro(_tit_neg, 500)
                        )
                        if _m_neg:
                            _ch_neg = (
                                _so_digitos(_m_neg.group(1)).zfill(6),
                                _so_digitos(_m_neg.group(2)).zfill(4),
                            )
                            if _ch_neg in _cotas_excluidas_sweep:
                                continue
                    except Exception:
                        pass
                # Filtro de dia por modalidade: le a data da linha pai (td Vencimento)
                # para nao selecionar parcelas do segmento errado na varredura global.
                try:
                    _venc_td = _cb_g.locator(
                        "xpath=ancestor::tr[1]/td[3]"
                    ).first
                    _venc_txt_g = _texto_seguro(_venc_td, 500)
                    _vdt_g = _parse_data_pt(_venc_txt_g)
                    if _vdt_g is not None:
                        if _modalidade_upper == "IMOVEL" and _vdt_g.day < 15:
                            continue
                        if _modalidade_upper == "MOTORS" and _vdt_g.day >= 15:
                            continue
                except Exception:
                    pass
                # Clica e verifica — dispatch_event nao rola a pagina (evita scroll fantasma)
                _cb_g.dispatch_event("click")
                page.wait_for_timeout(300)
                _state_g2 = _cb_g.get_attribute("data-state") or ""
                _aria_g2  = _cb_g.get_attribute("aria-checked") or ""
                if _state_g2 == "checked" or _aria_g2 == "true":
                    total_selecionadas += 1
                    _algum_clicou = True
                else:
                    # Tentativa com force=True
                    _cb_g.click(timeout=3000, force=True)
                    page.wait_for_timeout(300)
                    _state_g3 = _cb_g.get_attribute("data-state") or ""
                    _aria_g3  = _cb_g.get_attribute("aria-checked") or ""
                    if _state_g3 == "checked" or _aria_g3 == "true":
                        total_selecionadas += 1
                        _algum_clicou = True
            except Exception:
                continue
        if not _algum_clicou:
            break  # Nenhum checkbox novo — nao precisa de mais passagens
        page.wait_for_timeout(300)

    resultado["total_selecionadas"] = total_selecionadas
    resultado["pode_continuar"] = total_selecionadas > 0
    return resultado


def _ler_valor_total_footer(page) -> float:
    """
    Le o 'Valor total' exibido no footer do modal de selecao de parcelas.
    Retorna o valor numerico com sinal (ex: 375.54 ou -0.10) ou 0.0 se nao encontrado.

    HTML do footer:
      <p class="text-sm text-base-foreground">
        Valor total: <span class="font-semibold">R$&nbsp;375,54</span>
      </p>

    IMPORTANTE: preserva o sinal negativo. Valores negativos indicam que apenas
    parcelas de credito foram selecionadas (ex: -R$ 0,10), o que NAO deve ser
    tratado como valor positivo valido para prosseguir.
    """
    seletores = [
        "footer p:has-text('Valor total') span.font-semibold",
        "footer span.font-semibold",
        "p:has-text('Valor total') span.font-semibold",
        "p:has-text('Valor total') span",
    ]
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                txt = _texto_seguro(loc, 1000).replace("\xa0", "")
                # Preserva sinal negativo antes de remover outros chars
                negativo = "-" in txt
                num_str = re.sub(r"[^\d,]", "", txt).replace(",", ".")
                if num_str:
                    valor = float(num_str)
                    return -valor if negativo else valor
        except Exception:
            continue
    return 0.0


def ler_valor_total_footer_modal(page) -> Optional[float]:
    """
    Le o 'Valor total' do footer do modal de selecao de parcelas e retorna
    o valor numerico COM SINAL (positivo ou negativo), ou None se nao encontrado.

    Usado em main.py antes de clicar 'Gerar boleto' para detectar totais
    negativos (apenas creditos selecionados) sem precisar abrir o modal de Pagamento.
    """
    valor = _ler_valor_total_footer(page)
    # _ler_valor_total_footer retorna 0.0 quando nao encontra — distingue None de zero
    # tentando ler novamente via texto para checar se realmente esta la
    seletores = [
        "footer p:has-text('Valor total') span.font-semibold",
        "footer span.font-semibold",
        "p:has-text('Valor total') span.font-semibold",
        "p:has-text('Valor total') span",
    ]
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return valor  # encontrou o elemento → retorna o valor (pode ser 0.0)
        except Exception:
            continue
    return None  # elemento nao encontrado




def ler_cotas_no_modal_parcelas(page, timeout_ms: int = 5000) -> set:
    """
    Le quais cotas aparecem como cards no modal de selecao de parcelas
    ("Selecione as parcelas para emissao do boleto").

    Retorna set de tuplas (grupo_zfill6, cota_zfill4).
    Usado para verificar se todas as cotas selecionadas na pagina do cliente
    realmente entraram no modal antes de processar as parcelas.
    """
    resultado: set = set()
    try:
        # Aguarda pelo menos um card aparecer
        page.locator(
            "p.text-base.font-semibold.text-base-foreground:has-text('Grupo')"
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass
    try:
        headers = page.locator(
            "p.text-base.font-semibold.text-base-foreground"
        ).all()
        for h in headers:
            txt = _texto_seguro(h, 1000)
            m = _RE_GRUPO_COTA.search(txt)
            if m:
                g6 = _so_digitos(m.group(1)).zfill(6)
                c4 = _so_digitos(m.group(2)).zfill(4)
                resultado.add((g6, c4))
    except Exception:
        pass
    return resultado


def cancelar_modal_parcelas(page) -> None:
    """
    Cancela o modal de selecao de parcelas clicando em 'Cancelar'.
    Inofensivo se o modal nao estiver aberto.
    """
    seletores = [
        "button:text-is('Cancelar')",
        "footer button:has-text('Cancelar')",
        "[role='dialog'] button:has-text('Cancelar')",
    ]
    for sel in seletores:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click(timeout=5000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue
    # Fallback: Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


def clicar_continuar_modal_parcelas(page) -> None:
    """
    Clica no botao de confirmacao do modal de selecao de parcelas.
    O AVAPRO usa 'Gerar boleto' (novo) ou 'Continuar' (antigo) — tenta ambos.
    So pode ser clicado quando houver ao menos uma parcela selecionada.

    Antes de clicar, verifica se o 'Valor total' no footer e maior que R$ 50,00.
    Se for menor ou igual a 50 (nenhuma parcela real selecionada, ou so creditos),
    levanta RuntimeError em vez de clicar e gerar boleto invalido.

    Para evitar o bug onde cliques normais caem no fundo da pagina (fora do modal)
    causando scroll indesejado, usa estrategia em camadas (tudo Playwright/Python):
      1. dispatch_event('click') — dispara o clique direto no botao, sem rolar a
         pagina e sem risco de cair no fundo
      2. clique posicional no centro exato do botao (bounding box)
      3. force=True (ignora sobreposicao de elementos)
    """
    # Verifica valor total no footer antes de clicar.
    # Tenta ate 8x com 400ms de espera — o footer pode demorar a atualizar apos marcar checkboxes.
    VALOR_MINIMO = 50.0
    valor_total = 0.0
    for _t in range(8):
        valor_total = _ler_valor_total_footer(page)
        if valor_total > VALOR_MINIMO:
            break
        page.wait_for_timeout(400)
    if valor_total <= VALOR_MINIMO:
        raise RuntimeError(
            f"Valor total do boleto e R$ {valor_total:.2f} (minimo R$ {VALOR_MINIMO:.2f}) "
            "— parcelas insuficientes ou apenas creditos selecionados; nao clica 'Gerar boleto'."
        )

    seletores = [
        "footer button:has-text('Gerar boleto'):not([disabled])",
        "footer button:has-text('Continuar'):not([disabled])",
        "button[data-slot='button']:has-text('Gerar boleto'):not([disabled])",
        "button:has-text('Gerar boleto'):not([disabled])",
        "button:has-text('Continuar'):not([disabled])",
    ]

    btn_encontrado = None
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=10000)
            btn_encontrado = loc
            break
        except Exception:
            continue

    if btn_encontrado is None:
        raise RuntimeError("Botao 'Gerar boleto'/'Continuar' nao encontrado no modal")

    # Garante que o botao esta no viewport, rolando APENAS o container do proprio
    # botao (o footer/modal), nunca o body — evita o "scroll fantasma" da tela de tras.
    try:
        btn_encontrado.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(150)
    except Exception:
        pass

    # Tentativa 1 (PRINCIPAL): dispatch_event('click') do Playwright.
    # Dispara o evento de clique diretamente no elemento EXATO do botao, sem
    # mover o mouse nem depender de coordenadas — assim o clique NUNCA cai no
    # fundo da pagina (causa do scroll fantasma). Puro Python/Playwright.
    try:
        btn_encontrado.dispatch_event("click")
        page.wait_for_timeout(300)
        return
    except Exception:
        pass

    # Tentativa 2: clique posicional no centro exato do botao (bounding box),
    # sem o auto-scroll do Playwright que pode acertar o fundo.
    try:
        box = btn_encontrado.bounding_box()
        if box:
            page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
            page.wait_for_timeout(300)
            return
    except Exception:
        pass

    # Tentativa 3: force=True — ignora elementos interceptando o clique.
    try:
        btn_encontrado.click(timeout=6000, force=True)
        return
    except Exception as e:
        raise RuntimeError(
            f"Botao 'Gerar boleto'/'Continuar' encontrado mas nao foi possivel clicar: {e}"
        )


def ler_dados_modal_pagamento(page, timeout_ms: int = 15000) -> Dict[str, Any]:
    """
    Le os dados do modal 'Pagamento' que aparece apos clicar 'Gerar boleto':
      - vencimento_str: ex '15/06/2026'
      - vencimento_dt:  datetime correspondente (ou None)
      - valor_str:      ex 'R$ 341,54'

    Aguarda o modal aparecer ate timeout_ms ms.
    Retorna dict com os campos acima (None se nao encontrou).
    """
    dados: Dict[str, Any] = {
        "vencimento_str": None,
        "vencimento_dt": None,
        "valor_str": None,
    }

    # Aguarda o modal "Pagamento" aparecer
    try:
        page.locator(
            "h2:text-matches('Pagamento', 'i'), "
            "h3:text-matches('Pagamento via Boleto', 'i')"
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass

    # --- Vencimento: "Data de vencimento: 15/06/2026" ---
    _sels_venc = [
        "p.text-sm.text-muted-foreground",
        "p:has-text('Data de vencimento')",
        "p:text-matches('Data de vencimento', 'i')",
    ]
    for _sel in _sels_venc:
        try:
            for _loc in page.locator(_sel).all():
                _txt = _texto_seguro(_loc, 1000)
                if "vencimento" in _txt.lower() or re.search(r"\d{2}/\d{2}/\d{4}", _txt):
                    dados["vencimento_str"] = _txt
                    dados["vencimento_dt"] = _parse_data_pt(_txt)
                    break
            if dados["vencimento_dt"] is not None:
                break
        except Exception:
            continue

    # --- Valor: "R$ 341,54" (paragrafo bold/2xl) ---
    _sels_valor = [
        "p.text-2xl.font-bold",
        "p:text-matches(r'R\\$', 'i').text-2xl",
        "p:has-text('R$')",
    ]
    for _sel in _sels_valor:
        try:
            for _loc in page.locator(_sel).all():
                _txt = _texto_seguro(_loc, 1000)
                _txt_clean = _txt.replace(" ", " ").strip()
                if re.search(r"R\$\s*[\d\.,]+", _txt_clean):
                    dados["valor_str"] = _txt_clean
                    break
            if dados["valor_str"]:
                break
        except Exception:
            continue

    return dados


def fechar_modal_pagamento(page) -> None:
    """
    Fecha o modal 'Pagamento' clicando no X (botao de fechar).
    Inofensivo se o modal nao estiver aberto.
    """
    _sels_fechar = [
        "button[aria-label*='Fechar' i]",
        "button[aria-label*='Close' i]",
        "button:has(svg path[d*='M6 18'])",   # lucide X icon
        "button:has(svg path[d*='18 6'])",
        "[role='dialog'] button:last-of-type",
    ]
    for _sel in _sels_fechar:
        try:
            _btn = page.locator(_sel).first
            if _btn.count() > 0 and _btn.is_visible():
                _btn.click(timeout=3000)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue
    # Fallback: Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def aguardar_e_clicar_baixar_resumo(page, timeout_ms: int = 15000) -> bool:
    """
    Apos clicar 'Gerar boleto'/'Continuar', o AVAPRO exibe um modal de
    resumo ('Pagamento' / 'Resumo da emissao de boleto') com um botao
    'Baixar' (icone download). Aguarda esse botao e clica nele.

    Aguarda qualquer candidato aparecer (polling rapido) dentro do
    timeout total — nao espera timeout_ms por cada seletor.

    Retorna True se clicou, False se o botao nao apareceu.
    """
    seletores = [
        "footer button:text-is('Baixar')",
        "[role='dialog'] button:text-is('Baixar')",
        "button:text-is('Baixar')",
        "footer button:has-text('Baixar')",
        "button[data-slot='button']:has-text('Baixar')",
    ]
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in seletores:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=200):
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(timeout=8000)
                    return True
            except Exception:
                continue
        page.wait_for_timeout(300)
    return False


def aguardar_pdf_novo_em_downloads(
    snapshot_antes: set,
    pasta: Optional[Path] = None,
    timeout_s: int = 60,
    estabilizar_ms: int = 400,
) -> Path:
    """
    Aguarda surgir um arquivo .pdf NOVO na pasta Downloads e retorna o Path.
    Levanta TimeoutError se nao surgir nada em timeout_s segundos.
    """
    pasta = pasta or downloads_dir()
    fim = time.time() + max(timeout_s, 5)

    while time.time() < fim:
        try:
            atuais = {str(p): p for p in pasta.glob("*.pdf")}
        except Exception:
            atuais = {}

        novos_paths = [atuais[k] for k in atuais.keys() - snapshot_antes]
        if novos_paths:
            novos_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidato = novos_paths[0]
            try:
                size1 = candidato.stat().st_size
            except OSError:
                time.sleep(0.1)
                continue
            time.sleep(estabilizar_ms / 1000)
            try:
                size2 = candidato.stat().st_size
            except OSError:
                time.sleep(0.1)
                continue
            if size1 > 0 and size1 == size2:
                return candidato

        time.sleep(0.3)

    raise TimeoutError(
        f"Nenhum PDF novo surgiu em Downloads em {timeout_s}s"
    )
