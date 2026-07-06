"""
Helpers de conexao via CDP (Chrome DevTools Protocol) ao Edge ja
aberto pelo login.py. O login deixa o Edge rodando com
--remote-debugging-port=9222 e nao fecha o browser - o worker
conecta aqui sem reabrir nada.

Funcoes expostas:
  - conectar_ao_edge(playwright) -> (browser, context)
  - achar_aba_avapro(context) -> page
  - garantir_url_meus_clientes(page) -> None
"""

import time
from pathlib import Path
from typing import Optional, Tuple

import requests


PORTA_DEBUG = 9222
URL_AVAPRO_BASE = "https://avapro.ademicon.com.br"
URL_MEUS_CLIENTES = f"{URL_AVAPRO_BASE}/meus-clientes"


def cdp_disponivel(porta: int = PORTA_DEBUG, timeout: int = 5) -> bool:
    """Checa se o Edge esta aceitando CDP na porta - usado antes de conectar."""
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{porta}/json/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def conectar_ao_edge(playwright, porta: int = PORTA_DEBUG):
    """
    Conecta o Playwright ao Edge ja aberto pelo login.
    Retorna (browser, context).

    Apos conectar, configura o Edge via CDP para salvar downloads
    diretamente na pasta ~/Downloads do usuario, SEM interceptacao
    do Playwright. Sem isso, o Playwright salva o arquivo como UUID/
    hash em pasta temporaria (visivel no painel de downloads do Edge
    como arquivo suspeito). Com Browser.setDownloadBehavior(allow),
    o Edge baixa normalmente e o worker monitora ~/Downloads para
    detectar o PDF novo e mover para o destino final.
    """
    if not cdp_disponivel(porta, timeout=10):
        raise RuntimeError(
            f"CDP nao disponivel na porta {porta} - o Edge foi fechado? "
            f"Rode o login.py de novo antes do worker."
        )

    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{porta}")
    if not browser.contexts:
        raise RuntimeError("Nenhum contexto encontrado no Edge - sessao morreu?")

    # Configura o Edge para salvar downloads na pasta real do usuario.
    # Browser.setDownloadBehavior e um comando de nivel de browser:
    # persiste enquanto a sessao estiver aberta, sem precisar repetir
    # a cada navegacao ou troca de aba.
    try:
        pasta_downloads = str(Path.home() / "Downloads")
        cdp = browser.new_browser_cdp_session()
        cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": pasta_downloads,
            "eventsEnabled": False,
        })
    except Exception:
        # Se o comando nao for suportado nesta versao do Edge,
        # o download ainda funciona — so pode aparecer como UUID.
        pass

    return browser, browser.contexts[0]


def achar_aba_avapro(context):
    """
    Encontra a aba do AVAPRO entre as paginas do contexto e FECHA todas
    as demais — garante que so UMA aba fique aberta, independente do
    que o login.py deixou para tras.

    Prioridade para escolha:
      1) Aba ja em /meus-clientes (raiz, sem id)
      2) Aba em qualquer URL avapro.ademicon.com.br
      3) Primeira aba nao-blank
      4) Primeira aba qualquer
      5) Cria nova
    """
    paginas = list(context.pages)
    escolhida = None

    # Prioridade 1: ja em /meus-clientes
    for p in paginas:
        try:
            url = (p.url or "").lower()
            if _esta_na_listagem(url):
                escolhida = p
                break
        except Exception:
            continue

    # Prioridade 2: qualquer AVAPRO
    if escolhida is None:
        for p in paginas:
            try:
                url = (p.url or "").lower()
                if "avapro.ademicon.com.br" in url:
                    escolhida = p
                    break
            except Exception:
                continue

    # Prioridade 3: primeira nao-blank
    if escolhida is None:
        for p in paginas:
            try:
                url = (p.url or "").lower().strip()
                if url and url not in (
                    "about:blank", "edge://newtab/", "chrome://newtab/"
                ):
                    escolhida = p
                    break
            except Exception:
                continue

    # Prioridade 4: primeira qualquer
    if escolhida is None and paginas:
        escolhida = paginas[0]

    # Prioridade 5: cria nova
    if escolhida is None:
        return context.new_page()

    # Fecha TODAS as abas extras — so a escolhida sobrevive
    for p in list(context.pages):
        if p is escolhida:
            continue
        try:
            p.close()
        except Exception:
            pass

    return escolhida


def _esta_na_listagem(url: str) -> bool:
    """
    URL alvo: 'https://avapro.ademicon.com.br/meus-clientes' (com ou sem /).
    NAO aceita '/meus-clientes/12345' (detalhe do cliente) - se aceitasse,
    a cota seguinte tentaria pesquisar dentro do detalhe da cota anterior
    (que nao tem campo de busca) e estouraria timeout.
    """
    if not url:
        return False
    u = url.split("?")[0].split("#")[0].rstrip("/").lower()
    return u.endswith("/meus-clientes")


def fechar_modal_clara(page) -> bool:
    """
    Fecha o modal 'A Clara chegou para todos os consultores!' se estiver aberto.
    Aparece apos o login (ou em qualquer pagina) e bloqueia a interacao.

    Estrategia: procura o botao de fechar pelo aria-label especifico.
    Retorna True se fechou, False se nao estava aberto.
    """
    seletores = [
        "button[aria-label='Fechar apresentação da Clara']",
        "button[aria-label*='Fechar']",
        "button[aria-label*='Clara']",
    ]
    for sel in seletores:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


def garantir_url_meus_clientes(page, timeout_ms: int = 20000) -> None:
    """
    Garante que a aba esta na LISTAGEM /meus-clientes (raiz, sem id).
    Se estiver no detalhe de um cliente (/meus-clientes/12345), navega
    de volta. Espera o input de busca aparecer antes de retornar.
    Fecha o modal da Clara se estiver aberto.
    Fecha abas extras que o AVAPRO possa ter aberto durante o processamento.
    """
    # Fecha abas extras abertas pelo AVAPRO (ex: ao clicar em links com target="_blank")
    try:
        context = page.context
        for p in list(context.pages):
            if p is page:
                continue
            try:
                p.close()
            except Exception:
                pass
    except Exception:
        pass

    # Fecha o modal da Clara antes de qualquer navegacao (pode aparecer
    # logo apos o login ou ao entrar em /meus-clientes pela primeira vez).
    fechar_modal_clara(page)

    try:
        url = page.url or ""
    except Exception:
        url = ""

    if not _esta_na_listagem(url):
        page.goto(URL_MEUS_CLIENTES, wait_until="domcontentloaded", timeout=timeout_ms)
        # Tenta fechar o modal novamente caso apareça após a navegação
        fechar_modal_clara(page)

    # Espera o campo de busca aparecer (indica que a SPA renderizou)
    try:
        page.locator(
            "input[placeholder*='Buscar por grupo, cota, cliente, documento']"
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        # Ultima tentativa: fechar modal que pode estar bloqueando o campo
        fechar_modal_clara(page)
        page.wait_for_timeout(1000)
