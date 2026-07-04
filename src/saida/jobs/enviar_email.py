import base64
import glob as _glob
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from googleapiclient.discovery import build

from shared.google_auth import criar_servico_sheets
from shared.log import log_info, log_erro
from shared.sql_funcoes import (
    obter_dados_adm_por_fila,
    listar_cotas_nao_encontradas,
)
from saida.lib.db import get_conn
from config.modalidades import label_email


MESES_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Marco",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}


def _formatar_mes_extenso(mes_ref: int) -> str:
    mes = mes_ref % 100
    ano = mes_ref // 100
    return f"{MESES_PT.get(mes, '?')}/{ano}"


def _get_gmail_service():
    sheets_service = criar_servico_sheets()
    creds = sheets_service._http.credentials
    return build("gmail", "v1", credentials=creds)


def _buscar_metricas_lote(id_fila_adm: int):
    """
    Calcula as métricas AO VIVO direto de tbl_fila_cotas, considerando
    apenas a ÚLTIMA tentativa de cada cota (maior id_cota por grupo+cota).

    Motivo duplo:
      1. O email roda ANTES do fechar_lote_adm, então os contadores
         cacheados em tbl_fila_adm ainda estão em 0.
      2. Com retry via DB podem existir até 3 registros por cota; o email
         deve contar somente o resultado final (ex.: falhou na tentativa 1
         mas baixou na tentativa 2 → conta 1 BAIXADO, não 1 FALHA + 1 BAIXADO).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ultima_tentativa AS (
                    SELECT DISTINCT ON (grupo, cota)
                        status,
                        parcelas_atraso
                    FROM tbl_fila_cotas
                    WHERE id_fila_adm = %s
                    ORDER BY grupo, cota, id_cota DESC
                )
                SELECT
                    COALESCE(SUM(CASE WHEN ut.status = 'BAIXADO'     THEN 1 ELSE 0 END), 0) AS cotas_baixadas,
                    COALESCE(SUM(CASE WHEN ut.status = 'NAO_BAIXADO' THEN 1 ELSE 0 END), 0) AS cotas_nao_baixadas,
                    COALESCE(SUM(CASE WHEN ut.status = 'FALHA'       THEN 1 ELSE 0 END), 0) AS cotas_falha,
                    COALESCE(SUM(CASE WHEN ut.status = 'ADIANTADO'   THEN 1 ELSE 0 END), 0) AS cotas_adiantadas,
                    COALESCE(SUM(CASE WHEN ut.status = 'BAIXADO'
                                      AND ut.parcelas_atraso > 0     THEN 1 ELSE 0 END), 0) AS cotas_com_atraso,
                    f.link_drive,
                    a.email
                FROM ultima_tentativa ut
                CROSS JOIN tbl_fila_adm f
                JOIN tbl_adm a ON a.id_adm = f.id_adm
                WHERE f.id_fila_adm = %s
                GROUP BY f.id_fila_adm, f.link_drive, a.email
                """,
                (id_fila_adm, id_fila_adm),
            )
            return cur.fetchone()


def _buscar_excluidas_lote(id_fila_adm: int) -> list:
    """
    Retorna as cotas NAO_BAIXADO por badge 'Excluído' do lote.
    Detectadas pela observação que contém 'excluída no AVAPRO' (gravada pelo worker).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (grupo, cota)
                    nome_cliente, grupo, cota
                FROM tbl_fila_cotas
                WHERE id_fila_adm = %s
                  AND status = 'NAO_BAIXADO'
                  AND (
                      observacao ILIKE '%%excluída no AVAPRO%%'
                   OR observacao ILIKE '%%excluida no AVAPRO%%'
                  )
                ORDER BY grupo, cota, id_cota DESC
                """,
                (id_fila_adm,),
            )
            return cur.fetchall() or []


def _criar_excluidos_txt(caminho_base: str, excluidas: list) -> Path | None:
    """
    Cria Relatório/excluidos.txt com uma linha por cota excluída:
        {nome_cliente} - Grupo: {grupo} - Cota: {cota}

    Retorna o Path do arquivo criado, ou None se não houver excluídas.
    """
    if not excluidas or not caminho_base:
        return None
    pasta = Path(caminho_base) / "Relatório"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / "excluidos.txt"
    linhas = [
        f"{(row[0] or '').strip()} - Grupo: {row[1]} - Cota: {row[2]}"
        for row in excluidas
    ]
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return destino


def _encontrar_zip_lote(caminho_base: str) -> Path | None:
    """
    Localiza o arquivo .zip principal do lote na raiz de caminho_base.
    O nome segue o padrao: {nome_adm}_{modalidade}_{mes}.zip
    Retorna o Path do zip mais recente, ou None se nao encontrar.
    """
    if not caminho_base:
        return None
    try:
        zips = sorted(
            Path(caminho_base).glob("*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return zips[0] if zips else None
    except Exception:
        return None


def _falhar_email(caminho_log: str, id_fila_adm: int, acao: str, detalhe: str) -> None:
    log_erro(caminho_log, "EMAIL", id_fila_adm, acao, detalhe)
    raise RuntimeError(detalhe)


def enviar_email_lote(id_fila_adm: int) -> None:
    dados = obter_dados_adm_por_fila(id_fila_adm)
    if not dados:
        raise RuntimeError(f"Lote {id_fila_adm} nao encontrado")

    caminho_log = dados["caminho_log"]
    caminho_base = dados.get("caminho_base")
    nome_adm = dados["nome"]
    modalidade = dados["modalidade"]
    mes_ref = int(dados["mes_ref"])

    metricas = _buscar_metricas_lote(id_fila_adm)
    if not metricas:
        _falhar_email(
            caminho_log,
            id_fila_adm,
            "Buscar metricas",
            "Lote nao encontrado em tbl_fila_adm",
        )

    (
        cotas_baixadas,
        cotas_nao_baixadas,
        cotas_falha,
        cotas_adiantadas,
        cotas_com_atraso,
        link_drive,
        email_destino,
    ) = metricas

    if not link_drive:
        _falhar_email(
            caminho_log,
            id_fila_adm,
            "Validar link",
            "link_drive vazio",
        )

    if not email_destino:
        _falhar_email(
            caminho_log,
            id_fila_adm,
            "Validar email",
            f"ADM sem email: {nome_adm}",
        )

    mes_ext = _formatar_mes_extenso(mes_ref)
    label = label_email(modalidade)

    # Cotas excluídas no AVAPRO: gera excluidos.txt em Relatório/ se houver.
    excluidas = _buscar_excluidas_lote(id_fila_adm)
    caminho_excluidos = _criar_excluidos_txt(caminho_base, excluidas)

    # Zip do lote (Boletos + Evidencias + FALHAS) gerado pelo Drive antes do email.
    caminho_zip = _encontrar_zip_lote(caminho_base)

    nao_encontrados = listar_cotas_nao_encontradas(id_fila_adm)

    if nao_encontrados:
        linhas_txt = []
        linhas_tr = []

        for ne in nao_encontrados:
            nome_cli = (ne["nome_cliente"] or "").strip() or "(sem nome)"
            linhas_txt.append(
                f"  - {nome_cli} | Grupo: {ne['grupo']} | Cota: {ne['cota']}"
            )
            linhas_tr.append(
                f"""
                <tr>
                  <td style="padding:10px 14px; border-bottom:1px solid #F0F0F0; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#212121;">
                    <strong style="color:#000000;">{nome_cli}</strong>
                  </td>
                  <td style="padding:10px 14px; border-bottom:1px solid #F0F0F0; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#616161; white-space:nowrap; text-align:right;">
                    Grupo <strong style="color:#212121;">{ne['grupo']}</strong> · Cota <strong style="color:#212121;">{ne['cota']}</strong>
                  </td>
                </tr>"""
            )

        secao_txt = (
            "\nCotas registradas no sistema e nao localizadas na planilha:\n"
            + "\n".join(linhas_txt)
            + "\n"
        )
        secao_html = f"""
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:700; color:#B71C1C; text-transform:uppercase; letter-spacing:1.2px; padding:6px 0 10px 0;">
                Cotas no sistema não localizadas na planilha
              </div>
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#FAFAFA; border:1px solid #EEEEEE; border-radius:6px;">
                {''.join(linhas_tr)}
              </table>
            </td>
          </tr>
        """
    else:
        secao_txt = "\nNenhuma divergencia entre sistema e planilha.\n"
        secao_html = """
          <tr>
            <td style="padding:8px 32px 0 32px;">
              <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:13px; color:#616161; padding:14px 16px; background-color:#FAFAFA; border:1px solid #EEEEEE; border-radius:6px;">
                Nenhuma divergência entre sistema e planilha.
              </div>
            </td>
          </tr>
        """

    assunto = f"{label} — {nome_adm} · {mes_ext}"

    corpo_txt = f"""Resumo de Processamento — {label}
Mes de vencimento: {mes_ext}
Administrador: {nome_adm}

Ola {nome_adm},

Segue o resultado do processamento dos boletos:

  Baixados ........... {cotas_baixadas}
  Nao baixados ....... {cotas_nao_baixadas}
  Falhas ............. {cotas_falha}
  Adiantados ......... {cotas_adiantadas}
  Com atraso ......... {cotas_com_atraso}
{secao_txt}
Acesse a pasta no Google Drive:
{link_drive}

—
Este e-mail foi gerado automaticamente pelo sistema de processamento.
""".strip()

    corpo_html = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Resumo de Processamento</title>
<!--[if mso]>
<style type="text/css">
table, td {{ border-collapse: collapse; mso-line-height-rule: exactly; }}
</style>
<![endif]-->
</head>
<body style="margin:0; padding:0; background-color:#EEEEEE; font-family:'Segoe UI', Arial, sans-serif;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#EEEEEE">
    <tr>
      <td align="center" style="padding:32px 12px;">

        <table role="presentation" width="640" border="0" cellspacing="0" cellpadding="0" bgcolor="#FFFFFF" style="border-collapse:collapse; max-width:640px; border-radius:8px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.08);">

          <tr>
            <td bgcolor="#8B0000" style="background-color:#8B0000; height:4px; line-height:4px; font-size:0;">&nbsp;</td>
          </tr>

          <tr>
            <td bgcolor="#B71C1C" style="padding:28px 32px; background-color:#B71C1C;">
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="left" valign="middle" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:700; letter-spacing:2px; text-transform:uppercase;">
                    {label.upper()}
                  </td>
                  <td align="right" valign="middle" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:600; letter-spacing:1px; text-transform:uppercase;">
                    {mes_ext}
                  </td>
                </tr>
                <tr>
                  <td colspan="2" align="left" style="color:#FFFFFF; font-family:'Segoe UI',Arial,sans-serif; font-size:24px; font-weight:700; line-height:1.3; padding-top:10px;">
                    Resumo de Processamento
                  </td>
                </tr>
                <tr>
                  <td colspan="2" align="left" style="color:#FFCDD2; font-family:'Segoe UI',Arial,sans-serif; font-size:13px; padding-top:4px;">
                    {nome_adm} &middot; {mes_ext}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:28px 32px 4px 32px; font-family:'Segoe UI',Arial,sans-serif; color:#212121; font-size:15px; line-height:1.65;">
              Olá <strong>{nome_adm}</strong>, segue o resultado consolidado do processamento dos boletos referentes a <strong>{mes_ext}</strong>.
            </td>
          </tr>

          <tr>
            <td style="padding:24px 32px 8px 32px;">
              <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="border:1px solid #EEEEEE; border-radius:8px;">
                <tr>
                  <td width="20%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:26px; font-weight:700; color:#2E7D32; line-height:1;">{cotas_baixadas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Baixados</div>
                  </td>
                  <td width="20%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:26px; font-weight:700; color:#E65100; line-height:1;">{cotas_nao_baixadas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Não baixados</div>
                  </td>
                  <td width="20%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:26px; font-weight:700; color:#B71C1C; line-height:1;">{cotas_falha}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Falhas</div>
                  </td>
                  <td width="20%" align="center" valign="middle" style="padding:18px 4px; border-right:1px solid #EEEEEE;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:26px; font-weight:700; color:#1565C0; line-height:1;">{cotas_adiantadas}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Adiantados</div>
                  </td>
                  <td width="20%" align="center" valign="middle" style="padding:18px 4px;">
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:26px; font-weight:700; color:#6A1A6A; line-height:1;">{cotas_com_atraso}</div>
                    <div style="font-family:'Segoe UI',Arial,sans-serif; font-size:10px; color:#616161; text-transform:uppercase; letter-spacing:1.2px; margin-top:6px; font-weight:700;">Com atraso</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {secao_html}

          <tr>
            <td align="center" style="padding:32px 32px 12px 32px;">
              <!--[if mso]>
              <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{link_drive}" style="height:46px; v-text-anchor:middle; width:280px;" arcsize="13%" stroke="f" fillcolor="#2E7D32">
                <w:anchorlock/>
                <center style="color:#ffffff; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700; letter-spacing:0.5px;">ACESSAR PASTA NO DRIVE</center>
              </v:roundrect>
              <![endif]-->
              <!--[if !mso]><!-->
              <a href="{link_drive}" target="_blank" style="background-color:#2E7D32; border-radius:6px; color:#FFFFFF; display:inline-block; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700; line-height:46px; text-align:center; text-decoration:none; padding:0 36px; -webkit-text-size-adjust:none; letter-spacing:0.5px;">ACESSAR PASTA NO DRIVE</a>
              <!--<![endif]-->
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:0 32px 28px 32px; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; color:#9E9E9E; line-height:1.6;">
              Link alternativo:
              <a href="{link_drive}" target="_blank" style="color:#2E7D32; text-decoration:underline; word-break:break-all;">{link_drive}</a>
            </td>
          </tr>

          <tr>
            <td bgcolor="#FAFAFA" style="padding:18px 32px; border-top:1px solid #EEEEEE; font-family:'Segoe UI',Arial,sans-serif; font-size:11px; color:#9E9E9E; line-height:1.6; text-align:center;">
              Este e-mail foi gerado automaticamente pelo sistema de processamento.
            </td>
          </tr>

        </table>

      </td>
    </tr>
  </table>
</body>
</html>
"""

    try:
        service = _get_gmail_service()

        # Estrutura MIME:
        #   multipart/mixed          ← envelope externo (suporta anexos)
        #   ├── multipart/alternative
        #   │   ├── text/plain
        #   │   └── text/html
        #   └── text/plain (excluidos.txt)  ← só se houver cotas excluídas
        msg = MIMEMultipart("mixed")
        msg["to"] = email_destino
        msg["subject"] = assunto

        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(corpo_txt, "plain", "utf-8"))
        msg_alt.attach(MIMEText(corpo_html, "html", "utf-8"))
        msg.attach(msg_alt)

        if caminho_excluidos and caminho_excluidos.exists():
            conteudo_excl = caminho_excluidos.read_text(encoding="utf-8")
            parte_excl = MIMEText(conteudo_excl, "plain", "utf-8")
            parte_excl.add_header(
                "Content-Disposition", "attachment", filename="excluidos.txt"
            )
            msg.attach(parte_excl)

        # Anexa o zip do lote (Boletos + Evidencias + FALHAS)
        # Limite seguro: 20 MB (Gmail rejeita acima de 25 MB com overhead MIME)
        _LIMITE_ZIP_BYTES = 20 * 1024 * 1024
        _zip_anexado = False
        _zip_omitido_tamanho = False
        if caminho_zip and caminho_zip.exists():
            tamanho_zip = caminho_zip.stat().st_size
            if tamanho_zip > _LIMITE_ZIP_BYTES:
                _zip_omitido_tamanho = True
                log_info(
                    caminho_log, "EMAIL", id_fila_adm,
                    "Anexar zip",
                    f"ZIP omitido por tamanho ({tamanho_zip/1024/1024:.1f} MB > 20 MB) "
                    f"— arquivos disponiveis no Drive: {link_drive}"
                )
            else:
                try:
                    with open(caminho_zip, "rb") as _f_zip:
                        parte_zip = MIMEApplication(_f_zip.read(), _subtype="zip")
                    parte_zip.add_header(
                        "Content-Disposition", "attachment", filename=caminho_zip.name
                    )
                    msg.attach(parte_zip)
                    _zip_anexado = True
                except Exception as _e_zip:
                    log_erro(
                        caminho_log, "EMAIL", id_fila_adm,
                        "Anexar zip", f"{type(_e_zip).__name__}: {_e_zip}"
                    )

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

        log_info(
            caminho_log,
            "EMAIL",
            id_fila_adm,
            "Enviar email",
            f"destino={email_destino} "
            f"nao_baixados={cotas_nao_baixadas} falhas={cotas_falha} "
            f"nao_encontrados={len(nao_encontrados)} "
            f"excluidas={len(excluidas)} "
            f"anexo_excluidos={'sim' if caminho_excluidos else 'nao'} "
            f"anexo_zip={'sim' if _zip_anexado else ('omitido_tamanho' if _zip_omitido_tamanho else 'nao')} "
            f"zip={caminho_zip.name if caminho_zip else 'nenhum'}",
        )
    except Exception as e:
        log_erro(
            caminho_log,
            "EMAIL",
            id_fila_adm,
            "Enviar email",
            f"{type(e).__name__}: {e}",
        )
        raise