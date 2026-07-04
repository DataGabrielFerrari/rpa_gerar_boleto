"""
LOGIN no AVAPRO. Chamado UMA UNICA VEZ antes do loop de cotas.

Argumentos:
  argv[1] = id_fila_adm (int)

Saida (stdout): JSON unica linha
{
  "status": "SUCESSO|FALHA",
  "observacao": str
}

Comportamento em sucesso:
- Mata todo Edge antigo (taskkill /F)
- Sobe Edge novo com --remote-debugging-port=9222
- Conecta Playwright via CDP
- Faz fill em Matricula/Senha e clica Entrar
- Confirma sucesso pela URL (saiu de /login)
- Navega para "Meus Clientes"
- NAO fecha o browser - deixa pronto para o main.py por cota consumir

Comportamento em falha grave (credenciais invalidas, popup de erro, timeout):
- Tira screenshot na pasta evidencias/FALHA_LOGIN do lote
- Marca a fila como FALHA no banco
- Envia email via notificar_falha (log + script + screenshot)
- Limpa Edge para nao deixar processo zumbi
- Retorna FALHA pro orquestrador

Adaptado de C:\\rpa_ofertar_lance\\processamento\\login.py. Diferencas chave:
  - URL alvo: avapro/login (igual)
  - Pos-login: navega para "Meus Clientes" (no ofertar_lance era "Ofertar Lance")
  - Usa shared.sql_funcoes (em vez de db.db / db.funcoes)
  - Usa log_info/log_erro funcionais (em vez de Logger class)
"""

import os
import sys
import json
import time
import random
import subprocess
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# Forca stdout/stderr em UTF-8 para evitar mojibake quando o terminal
# Windows captura a saida (default cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # src/processamento/jobs
PROCESSAMENTO_DIR = os.path.dirname(CURRENT_DIR)           # src/processamento
SRC_DIR = os.path.dirname(PROCESSAMENTO_DIR)               # src
ROOT_DIR = os.path.dirname(SRC_DIR)                        # raiz do repo

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(ROOT_DIR, ".env")
    load_dotenv(ENV_PATH, override=True)
except Exception:
    pass

import requests
from playwright.sync_api import sync_playwright, expect

from shared.log import log_info, log_erro
from shared.notificador import notificar_falha
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    obter_credenciais_adm_por_fila,
    obter_url_avapro,
    finalizar_fila_adm,
)


URL_PADRAO = "https://avapro.ademicon.com.br/login"
PORTA_DEBUG = 9222


# ============================================================
# DELAYS HUMANOS
# ============================================================

def _pausa_humana(page, minimo_ms: int = 300, maximo_ms: int = 800) -> None:
    """Pausa com duracao aleatoria para simular comportamento humano."""
    ms = random.randint(minimo_ms, maximo_ms)
    page.wait_for_timeout(ms)


# ============================================================
# EXCEPTION ESPECIALIZADA
# ============================================================

class LoginGraveError(Exception):
    """
    Erro grave de login (credenciais invalidas, popup de erro, timeout).
    Carrega o caminho do screenshot capturado na hora do erro para anexar
    no email de falha.
    """
    def __init__(self, mensagem: str, caminho_print: Optional[str] = None):
        super().__init__(mensagem)
        self.caminho_print = caminho_print


# ============================================================
# HELPERS DE SAIDA / LOG
# ============================================================

def _emitir_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _payload(status: str, observacao: str) -> dict:
    return {"status": status, "observacao": observacao}


def _log(caminho_log: Optional[str], id_fila_adm: int, acao: str, detalhe: str = ""):
    """Log seguro - nunca levanta excecao."""
    if not caminho_log:
        return
    try:
        log_info(caminho_log, "LOGIN", id_fila_adm, acao, detalhe)
    except Exception:
        pass


def _log_err(caminho_log: Optional[str], id_fila_adm: int, acao: str, detalhe: str = ""):
    if not caminho_log:
        return
    try:
        log_erro(caminho_log, "LOGIN", id_fila_adm, acao, detalhe)
    except Exception:
        pass


# ============================================================
# CONTROLE DO EDGE
# ============================================================

def _existe_edge_rodando() -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        saida = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
        return "msedge.exe" in saida
    except Exception:
        return False


def _esperar_edge_morrer(timeout: int = 15) -> bool:
    inicio = time.time()
    while time.time() - inicio < timeout:
        if not _existe_edge_rodando():
            return True
        time.sleep(0.5)
    return False


def _matar_edge_total():
    """
    Fecha TODO o Edge: janelas normais, abas, popups, processos filhos.
    Necessario para garantir que ao subir com --remote-debugging-port=9222
    o Edge realmente abra um perfil limpo com CDP exposto.
    ATENCAO: fecha qualquer Edge aberto na maquina.
    """
    comandos = [
        ["taskkill", "/F", "/T", "/IM", "msedge.exe"],
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process msedge -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
    ]

    for cmd in comandos:
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    if not _esperar_edge_morrer(timeout=15):
        raise RuntimeError(
            "Nao consegui encerrar totalmente o Microsoft Edge antes do login."
        )

    time.sleep(2)


def _esperar_cdp(porta: int = PORTA_DEBUG, timeout: int = 20) -> bool:
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{porta}/json/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _resolver_edge_path() -> str:
    candidatos = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidatos:
        if os.path.exists(c):
            return c
    raise FileNotFoundError("msedge.exe nao encontrado nos caminhos padrao")


# ============================================================
# CONSULTAS AO BANCO
# ============================================================

def _obter_credenciais_para_login(id_fila_adm: int) -> Tuple[str, str]:
    cred = obter_credenciais_adm_por_fila(id_fila_adm)
    if not cred:
        raise ValueError(
            f"Nao consegui obter credenciais para id_fila_adm={id_fila_adm}"
        )

    # obter_credenciais_adm_por_fila retorna Dict[str, Any] no shared deste repo
    matricula = ""
    senha = ""
    if hasattr(cred, "keys"):
        matricula = str(cred.get("matricula") or "").strip()
        senha = str(cred.get("senha") or "").strip()
    else:
        try:
            matricula = str(cred[2] or "").strip()
            senha = str(cred[3] or "").strip()
        except Exception:
            pass

    if not matricula or not senha:
        raise ValueError(
            f"Matricula ou senha vazias para id_fila_adm={id_fila_adm}"
        )

    return matricula, senha


def _get_dados_lote(id_fila_adm: int) -> Optional[dict]:
    try:
        return obter_dados_adm_por_fila(id_fila_adm)
    except Exception:
        return None


def _get_caminho_log_e_base(id_fila_adm: int) -> Tuple[Optional[str], Optional[str]]:
    dados = _get_dados_lote(id_fila_adm)
    if not dados:
        return None, None
    if hasattr(dados, "keys"):
        return dados.get("caminho_log"), dados.get("caminho_base")
    return None, None


# ============================================================
# SCREENSHOT / EVIDENCIA
# ============================================================

def _garantir_pasta_evidencias(caminho_base: Optional[str]) -> Path:
    if caminho_base:
        pasta = Path(caminho_base) / "Evidencias" / "FALHA_LOGIN"
    else:
        pasta = Path(ROOT_DIR) / "Lotes" / "FALHA_LOGIN"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _capturar_print(page, caminho_base: Optional[str], prefixo: str) -> Optional[str]:
    try:
        pasta = _garantir_pasta_evidencias(caminho_base)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        caminho = pasta / f"{prefixo}_{ts}.png"
        page.screenshot(path=str(caminho), full_page=True)
        return str(caminho)
    except Exception:
        return None


# ============================================================
# NAVEGACAO / LOGIN
# ============================================================

def _achar_pagina_login(context, url: str, caminho_log: Optional[str], id_fila_adm: int):
    """
    Garante que reste apenas UMA aba no contexto, na URL alvo.

    Quando o Edge sobe com --remote-debugging-port=9222 [URL], as vezes
    ele cria uma aba about:blank ou edge://newtab/ extra antes da aba do
    AVAPRO terminar de carregar. Este helper:

      1) Espera 2s para o Edge terminar de criar as abas iniciais
      2) Identifica a melhor candidata por prioridade
      3) Garante que a aba esta na URL alvo
      4) Fecha todas as outras abas
      5) Retorna a aba unica sobrevivente
    """
    time.sleep(2)
    paginas = list(context.pages)
    page_escolhida = None

    # Prioridade 1: aba ja na URL de login do AVAPRO
    for p in paginas:
        try:
            if "avapro.ademicon.com.br/login" in (p.url or "").lower():
                page_escolhida = p
                break
        except Exception:
            continue

    # Prioridade 2: aba em qualquer URL do dominio AVAPRO
    if page_escolhida is None:
        for p in paginas:
            try:
                if "avapro.ademicon.com.br" in (p.url or "").lower():
                    page_escolhida = p
                    break
            except Exception:
                continue

    # Prioridade 3: primeira aba nao-blank
    if page_escolhida is None:
        for p in paginas:
            try:
                cur = (p.url or "").lower().strip()
                if cur and cur not in (
                    "about:blank", "edge://newtab/", "chrome://newtab/"
                ):
                    page_escolhida = p
                    break
            except Exception:
                continue

    # Prioridade 4: primeira aba qualquer
    if page_escolhida is None and paginas:
        page_escolhida = paginas[0]

    # Prioridade 5: cria nova se contexto vazio
    if page_escolhida is None:
        page_escolhida = context.new_page()

    # Garante que a aba escolhida esta na URL correta
    try:
        url_atual = (page_escolhida.url or "").lower()
    except Exception:
        url_atual = ""

    if "avapro.ademicon.com.br" not in url_atual:
        try:
            page_escolhida.goto(url, wait_until="load", timeout=30000)
        except Exception:
            pass

    # Fecha todas as outras abas
    for p in list(context.pages):
        if p is page_escolhida:
            continue
        try:
            url_fechar = ""
            try:
                url_fechar = (p.url or "")
            except Exception:
                pass
            p.close()
            _log(caminho_log, id_fila_adm, "Aba extra fechada", f"url={url_fechar}")
        except Exception as e:
            _log(caminho_log, id_fila_adm, "Falha ao fechar aba extra", f"{e}")

    return page_escolhida


def _detectar_mensagem_erro_login(page) -> Optional[str]:
    """
    Procura na pagina por mensagem de erro de credenciais que o AVAPRO
    exibe inline apos o clique em Entrar. Busca por texto (case-insensitive
    com/sem acento). Retorna o texto encontrado ou None.
    """
    candidatos = [
        "Usuário ou senha inválida",
        "Usuário ou senha invalida",
        "Usuario ou senha inválida",
        "Usuario ou senha invalida",
        "usuário ou senha inválida",
        "usuario ou senha invalida",
    ]

    for texto in candidatos:
        try:
            loc = page.get_by_text(texto, exact=False)
            if loc.count() > 0:
                try:
                    real = (loc.first.inner_text() or "").strip()
                    return real or texto
                except Exception:
                    return texto
        except Exception:
            continue

    return None


def _esperar_resultado_login(
    page_alvo,
    caminho_base: Optional[str],
    timeout_s: int = 15,
) -> str:
    """
    Decide se o login foi sucesso ou falha grave.

    Apos clicar em Entrar, fica em loop por ate timeout_s segundos
    procurando uma destas condicoes:

      1) Saiu da URL /login -> SUCESSO
      2) Apareceu texto "Usuario ou senha invalida" -> FALHA grave
      3) Timeout sem nenhum dos dois -> FALHA grave

    Em qualquer FALHA, tira screenshot e levanta LoginGraveError.
    """
    try:
        page_alvo.get_by_role("button", name="Entrar").click()
    except Exception as e:
        caminho_print = _capturar_print(page_alvo, caminho_base, "LOGIN_BOTAO_ENTRAR")
        raise LoginGraveError(
            f"Nao consegui clicar no botao Entrar: {e}",
            caminho_print,
        )

    inicio = time.time()
    while time.time() - inicio < timeout_s:
        try:
            url_atual = (page_alvo.url or "").lower()
        except Exception:
            url_atual = ""

        if url_atual and "avapro.ademicon.com.br/login" not in url_atual:
            return "Login realizado com sucesso"

        msg_erro = _detectar_mensagem_erro_login(page_alvo)
        if msg_erro:
            page_alvo.wait_for_timeout(300)
            caminho_print = _capturar_print(
                page_alvo, caminho_base, "LOGIN_CREDENCIAIS_INVALIDAS"
            )
            raise LoginGraveError(msg_erro, caminho_print)

        page_alvo.wait_for_timeout(300)

    caminho_print = _capturar_print(page_alvo, caminho_base, "LOGIN_TIMEOUT")
    raise LoginGraveError(
        "Login nao confirmou sucesso apos o clique (timeout sem mensagem de erro)",
        caminho_print,
    )


def _clicar_x_pos_login_se_existir(
    page,
    caminho_log: Optional[str],
    id_fila_adm: int,
    tentativas: int = 4,
    pausa_entre_ms: int = 1500,
) -> None:
    """
    Fecha qualquer modal/popup que o AVAPRO exibe apos o login
    (ex: popup "A Clara chegou para todos os consultores!", avisos de
    manutencao, banners de novidades, etc.).

    Estrategia multicamadas para maxima robustez:
      - Tenta fechar em loop por 'tentativas' rodadas (padrao 4 x 1,5s = ~6s)
      - Em cada rodada percorre varios seletores candidatos em ordem de
        especificidade, do mais especifico ao mais generico
      - Para em cuanto nenhum popup estiver visivel
      - Nunca levanta excecao — falha silenciosa, o fluxo segue

    Seletores cobertos:
      1. data-slot="close" / data-slot="dismiss" (Dialog/Alert do AVAPRO)
      2. aria-label contendo "fechar", "close" ou "dismiss"
      3. SVG path do X classico Heroicons (M6 18 18 6 / M6 6l12 12)
      4. SVG viewBox="0 0 24 ..." com path M6 (generalizacao)
      5. Botao com role=button dentro de [role=dialog] ou .modal
      6. Botao visivel de 1 filho (icone X sem texto) dentro de overlay
    """
    # Seletores em ordem de preferencia — o primeiro que encontrar visivel
    # e clicado; a rodada avanca e tentamos novamente ate nao haver mais.
    _SELETORES = [
        # 1. data-slot especifico do componente interno do AVAPRO
        'button[data-slot="close"]',
        'button[data-slot="dismiss"]',
        # 2. aria-label — exato e por substring (cobre "Fechar apresentação da Clara" etc.)
        'button[aria-label="Fechar"]',
        'button[aria-label="fechar"]',
        'button[aria-label*="Fechar"]',
        'button[aria-label*="fechar"]',
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button[aria-label*="Close"]',
        'button[aria-label*="close"]',
        'button[aria-label="Dismiss"]',
        'button[aria-label="dismiss"]',
        # 3. SVG path exato do X classico Heroicons (outline 24px)
        'button:has(svg path[d="M6 18 18 6M6 6l12 12"])',
        # 4. Variante compacta do mesmo path (algumas libs omitem o M6)
        'button:has(svg path[d*="18 6M6 6l12 12"])',
        # 5. X generica: qualquer path de SVG que comece com M6 18
        'button:has(svg path[d^="M6 18"])',
        # 6. Botao X dentro de dialog/modal (qualquer filho SVG)
        '[role="dialog"] button:has(svg)',
        '.modal button:has(svg)',
        # 7. Ultimo recurso: primeiro botao visivel com apenas 1 filho (icone)
        #    dentro de um container com overlay/modal no nome da classe
        '[class*="modal"] button',
        '[class*="overlay"] button',
        '[class*="dialog"] button',
    ]

    for rodada in range(tentativas):
        fechou = False
        for seletor in _SELETORES:
            try:
                loc = page.locator(seletor).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=2000)
                    _log(
                        caminho_log,
                        id_fila_adm,
                        "Popup pos-login fechado",
                        f"rodada={rodada + 1} seletor={seletor!r}",
                    )
                    fechou = True
                    # Aguarda a animacao de saida antes de checar de novo
                    page.wait_for_timeout(600)
                    break
            except Exception:
                continue

        if not fechou:
            # Nenhum popup detectado nesta rodada — pode ter terminado
            _log(
                caminho_log,
                id_fila_adm,
                "Sem popup pos-login detectado",
                f"rodada={rodada + 1}",
            )
            return

        # Fechou um popup — aguarda um pouco e verifica se surgiu outro
        page.wait_for_timeout(pausa_entre_ms)

    # Esgotou tentativas — segue o fluxo normalmente
    _log(caminho_log, id_fila_adm, "Fim das tentativas de fechar popup pos-login")


def _abrir_menu_meus_clientes(
    page,
    caminho_log: Optional[str],
    id_fila_adm: int,
) -> None:
    """
    Apos login bem sucedido, navega para o menu "Meus Clientes".
    Diferenca chave em relacao ao rpa_ofertar_lance que ia para
    "Ofertar Lance".

    Falha aqui NAO deve marcar lote como FALHA - so loga. O worker
    pode tentar navegar de novo se precisar.
    """
    try:
        _log(caminho_log, id_fila_adm, "Abrindo menu Meus Clientes")
        # Procura por link com href /meus-clientes (mais robusto que texto)
        link = page.locator("a[href='/meus-clientes']").first
        try:
            link.wait_for(state="visible", timeout=20000)
            link.click()
            return
        except Exception:
            pass

        # Fallback: clica pelo texto "Meus Clientes"
        page.locator("text=Meus Clientes").first.wait_for(
            state="visible", timeout=10000
        )
        page.locator("text=Meus Clientes").first.click()
    except Exception as e:
        _log_err(
            caminho_log, id_fila_adm,
            "Nao consegui abrir menu Meus Clientes (segue fluxo)",
            f"{e}",
        )


# ============================================================
# CONSEQUENCIAS DE FALHA
# ============================================================

def _marcar_lote_falha(
    id_fila_adm: int,
    observacao: str,
    caminho_log: Optional[str],
) -> None:
    try:
        finalizar_fila_adm(id_fila_adm, "FALHA", observacao)
        _log(caminho_log, id_fila_adm, "Lote marcado como FALHA", observacao)
    except Exception as e:
        _log_err(caminho_log, id_fila_adm, "Falha ao marcar lote como FALHA", f"{e}")


# ============================================================
# EXECUCAO
# ============================================================

def _executar_login(id_fila_adm: int) -> str:
    caminho_log, caminho_base = _get_caminho_log_e_base(id_fila_adm)

    matricula, senha = _obter_credenciais_para_login(id_fila_adm)
    _log(caminho_log, id_fila_adm, "Credenciais obtidas", f"matricula={matricula}")

    url = obter_url_avapro() or URL_PADRAO
    _log(caminho_log, id_fila_adm, "URL alvo", url)

    edge_path = _resolver_edge_path()
    _log(caminho_log, id_fila_adm, "Edge encontrado", edge_path)

    _log(caminho_log, id_fila_adm, "Fechando Edge antigo")
    _matar_edge_total()

    _log(caminho_log, id_fila_adm, "Abrindo Edge limpo com CDP")
    subprocess.Popen(
        [
            edge_path,
            f"--remote-debugging-port={PORTA_DEBUG}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            # Suprime o "sinalizador de linha de comando sem suporte"
            "--test-type",
            "--start-maximized",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _esperar_cdp(PORTA_DEBUG, 20):
        raise RuntimeError(f"Edge nao abriu CDP na porta {PORTA_DEBUG}")

    _log(caminho_log, id_fila_adm, "CDP disponivel - conectando Playwright")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORTA_DEBUG}")
        if not browser.contexts:
            raise RuntimeError("Nenhum contexto encontrado no Edge")

        context = browser.contexts[0]

        # Mascara navigator.webdriver para reduzir deteccao de automacao.
        # Aplicado no contexto inteiro (vale para todas as abas/paginas).
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page_alvo = _achar_pagina_login(context, url, caminho_log, id_fila_adm)
        page_alvo.bring_to_front()
        page_alvo.wait_for_load_state("load")

        _log(caminho_log, id_fila_adm, "Aba do AVAPRO encontrada", page_alvo.url)

        if "avapro.ademicon.com.br" not in (page_alvo.url or "").lower():
            page_alvo.goto(url, wait_until="domcontentloaded", timeout=30000)

        inp_matricula = page_alvo.get_by_placeholder("Matrícula")
        inp_senha = page_alvo.get_by_placeholder("Senha")

        try:
            expect(inp_matricula).to_be_visible(timeout=30000)
            expect(inp_senha).to_be_visible(timeout=30000)
        except Exception as e:
            caminho_print = _capturar_print(
                page_alvo, caminho_base, "LOGIN_CAMPOS_NAO_APARECERAM"
            )
            raise LoginGraveError(
                f"Campos de matricula/senha nao apareceram: {e}",
                caminho_print,
            )

        _pausa_humana(page_alvo, 400, 900)
        inp_matricula.fill(matricula)
        _pausa_humana(page_alvo, 300, 700)
        inp_senha.fill(senha)
        _pausa_humana(page_alvo, 500, 1200)

        msg = _esperar_resultado_login(page_alvo, caminho_base, timeout_s=60)
        _log(caminho_log, id_fila_adm, "Resultado login", msg)

        # 1a rodada: fecha popups que aparecem logo apos o login
        _clicar_x_pos_login_se_existir(page_alvo, caminho_log, id_fila_adm)
        _abrir_menu_meus_clientes(page_alvo, caminho_log, id_fila_adm)
        # 2a rodada: fecha popups que possam ter aparecido durante/apos a
        # navegacao para Meus Clientes (o popup "Clara" as vezes surge aqui)
        _clicar_x_pos_login_se_existir(page_alvo, caminho_log, id_fila_adm)

        # Garante que so UMA aba esteja aberta no Edge.
        # O Edge as vezes abre uma aba extra (edge://newtab/ ou about:blank)
        # ao lado da aba do AVAPRO. Fechamos todas as que nao sao page_alvo.
        for _p in list(context.pages):
            if _p is page_alvo:
                continue
            try:
                _url_extra = ""
                try:
                    _url_extra = _p.url or ""
                except Exception:
                    pass
                _p.close()
                _log(caminho_log, id_fila_adm, "Aba extra fechada pos-login", f"url={_url_extra}")
            except Exception as _e_close:
                _log(caminho_log, id_fila_adm, "Aviso: nao consegui fechar aba extra", f"{_e_close}")

        # NAO fechar o browser - main.py por cota conecta via CDP
        return msg


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    if len(sys.argv) < 2:
        _emitir_json(_payload("FALHA", "argv[1] (id_fila_adm) ausente"))
        return 1

    try:
        id_fila_adm = int(str(sys.argv[1]).strip())
    except ValueError:
        _emitir_json(_payload("FALHA", f"id_fila_adm invalido: {sys.argv[1]!r}"))
        return 1

    caminho_log, _ = _get_caminho_log_e_base(id_fila_adm)
    _log(caminho_log, id_fila_adm, "Iniciando login AVAPRO")

    # Obtem matricula antecipadamente para incluir no email de falha grave.
    # Nao interrompe o fluxo se falhar aqui - apenas ficara em branco.
    matricula_para_email = ""
    try:
        cred_preview = obter_credenciais_adm_por_fila(id_fila_adm)
        if cred_preview:
            if hasattr(cred_preview, "keys"):
                matricula_para_email = str(
                    cred_preview.get("matricula") or ""
                ).strip()
            else:
                try:
                    matricula_para_email = str(cred_preview[2] or "").strip()
                except Exception:
                    pass
    except Exception:
        pass

    try:
        msg = _executar_login(id_fila_adm)
        _emitir_json(_payload("SUCESSO", msg))
        return 0

    except Exception as e:
        _stderr(traceback.format_exc())
        _log_err(caminho_log, id_fila_adm, "Erro grave no login", f"{type(e).__name__}: {e}")

        observacao = f"{type(e).__name__}: {e}"
        caminho_print = getattr(e, "caminho_print", None)

        # 1) marca o lote como FALHA no banco
        _marcar_lote_falha(id_fila_adm, observacao, caminho_log)

        # 2) envia ALERTA MAXIMO com screenshot anexado e matricula
        # Detecta se o erro e de credenciais invalidas para email mais especifico.
        _msg_erro_str = str(e).lower()
        _e_credencial_invalida = any(
            termo in _msg_erro_str
            for termo in (
                "usuario ou senha invalida", "usuario ou senha inválida",
                "usuário ou senha inválida", "usuário ou senha invalida",
                "invalida", "inválida",
            )
        )

        if _e_credencial_invalida:
            contexto = (
                f"╔══════════════════════════════════════════════════════╗\n"
                f"║   ⛔  USUÁRIO OU SENHA INVÁLIDOS  ⛔               ║\n"
                f"╚══════════════════════════════════════════════════════╝\n\n"
                f"O AVAPRO recusou o login com a mensagem:\n"
                f"  \"{e}\"\n\n"
                f"Matrícula utilizada : {matricula_para_email or '(nao obtida)'}\n"
                f"Senha               : (omitida por seguranca)\n"
                f"Screenshot          : {caminho_print or 'nao capturado'}\n\n"
                f"=== ACAO IMEDIATA NECESSARIA ===\n"
                f"1. Acesse o AVAPRO manualmente e tente logar com a matricula\n"
                f"   '{matricula_para_email or '?'}'\n"
                f"2. Se a senha estiver errada, redefina-a no AVAPRO\n"
                f"3. Atualize a senha no sistema/banco de dados do RPA\n"
                f"4. Reinicie o RPA apos corrigir as credenciais\n\n"
                f"O RPA esta PARADO ate que as credenciais sejam corrigidas."
            )
        else:
            contexto = (
                f"Matricula utilizada : {matricula_para_email or '(nao obtida)'}\n"
                f"Senha               : (omitida por seguranca)\n"
                f"Screenshot          : {caminho_print or 'nao capturado'}\n\n"
                f"=== ACAO IMEDIATA NECESSARIA ===\n"
                f"1. Verifique se o AVAPRO esta acessivel no navegador\n"
                f"2. Confirme as credenciais da matricula "
                f"'{matricula_para_email or '?'}' no sistema\n"
                f"3. Se necessario, redefina a senha no AVAPRO\n"
                f"4. Reinicie o RPA apos corrigir o problema\n\n"
                f"Erro tecnico: {type(e).__name__}: {e}"
            )

        try:
            notificar_falha(
                etapa="LOGIN - USUARIO INVALIDO" if _e_credencial_invalida else "LOGIN",
                erro=e,
                id_fila_adm=id_fila_adm,
                caminho_log=caminho_log,
                script_path=__file__,
                contexto_extra=contexto,
                nivel="ALERTA_MAXIMO",
                anexos_extras=[caminho_print] if caminho_print else None,
            )
        except Exception as e_notif:
            _stderr(f"[LOGIN] Falha ao enviar email de alerta maximo: {e_notif}")

        # 3) limpa Edge para nao deixar processo zumbi
        try:
            _matar_edge_total()
        except Exception:
            pass

        _emitir_json(_payload("FALHA", observacao))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        _stderr(traceback.format_exc())
        try:
            _emitir_json(_payload("FALHA", f"Toplevel: {type(e).__name__}: {e}"))
        except Exception:
            pass
        sys.exit(1)
