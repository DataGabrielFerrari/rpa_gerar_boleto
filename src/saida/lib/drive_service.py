import os
import zipfile

from googleapiclient.http import MediaFileUpload
from googleapiclient.discovery import build

from shared.google_auth import criar_servico_sheets
from shared.log import log_info, log_erro
from shared.sql_funcoes import obter_dados_adm_por_fila, atualizar_link_drive_fila_adm


MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def mes_extenso(mes_ref: int) -> str:
    mes = mes_ref % 100
    if mes not in MESES_PT:
        raise ValueError(f"mes_ref invalido: {mes_ref}")
    return MESES_PT[mes]


def _adicionar_pasta_no_zip(zip_file: zipfile.ZipFile, pasta_origem: str, nome_raiz_no_zip: str) -> int:
    """
    Adiciona uma pasta inteira ao zip preservando a estrutura interna.
    Ex.:
      pasta_origem = C:\\...\\fila_18\\Boletos
      nome_raiz_no_zip = Boletos

    Retorna a quantidade de arquivos adicionados.
    """
    arquivos_adicionados = 0

    if not os.path.isdir(pasta_origem):
        return 0

    for root, _, files in os.walk(pasta_origem):
        for file in files:
            full_path = os.path.join(root, file)

            # caminho relativo dentro da própria pasta
            relative_path = os.path.relpath(full_path, pasta_origem)

            # caminho final dentro do zip
            arcname = os.path.join(nome_raiz_no_zip, relative_path)

            zip_file.write(full_path, arcname)
            arquivos_adicionados += 1

    return arquivos_adicionados


def zipar_boletos(caminho_lote: str, nome_adm: str, modalidade: str, mes_ref: int) -> str:
    """
    Gera um zip contendo:
      - Boletos/
      - Evidencias/
    """
    boletos_dir = os.path.join(caminho_lote, "Boletos")
    evidencias_dir = os.path.join(caminho_lote, "Evidencias")

    nome_zip = f"{nome_adm}_{modalidade}_{mes_extenso(mes_ref)}.zip"
    zip_path = os.path.join(caminho_lote, nome_zip)

    # remove zip antigo, se existir
    if os.path.exists(zip_path):
        os.remove(zip_path)

    total_arquivos = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        total_arquivos += _adicionar_pasta_no_zip(z, boletos_dir, "Boletos")
        total_arquivos += _adicionar_pasta_no_zip(z, evidencias_dir, "Evidencias")

    if total_arquivos == 0:
        raise RuntimeError(
            f"Nenhum arquivo encontrado para zipar em '{boletos_dir}' e '{evidencias_dir}'"
        )

    return zip_path


def criar_link_drive(zip_path: str, nome_zip: str, caminho_log: str, id_fila_adm: int) -> str:
    log_info(
        caminho_log=caminho_log,
        etapa="DRIVE",
        id_dado=id_fila_adm,
        acao="Autenticar Google",
        detalhe="Reusando credenciais do Sheets",
    )

    sheets_service = criar_servico_sheets()
    creds = sheets_service._http.credentials
    drive_service = build("drive", "v3", credentials=creds)

    media = MediaFileUpload(zip_path, resumable=True)
    file = drive_service.files().create(
        body={"name": nome_zip},
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    drive_service.permissions().create(
        fileId=file["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return file["webViewLink"]


def processar_drive_lote(id_fila_adm: int) -> str:
    """
    Zipa boletos + evidencias do lote e faz upload no Drive.
    Atualiza link_drive no banco.
    Retorna o link gerado.
    """
    dados = obter_dados_adm_por_fila(id_fila_adm)
    if not dados:
        raise RuntimeError(f"Lote {id_fila_adm} nao encontrado")

    caminho_lote = dados["caminho_base"]
    caminho_log = dados["caminho_log"]
    modalidade = dados["modalidade"]
    mes_ref = int(dados["mes_ref"])
    nome_adm = dados["nome"]

    if not caminho_lote:
        raise RuntimeError(f"Lote {id_fila_adm} sem caminho_base")

    log_info(
        caminho_log=caminho_log,
        etapa="DRIVE",
        id_dado=id_fila_adm,
        acao="Zipar lote",
        detalhe=f"adm={nome_adm} modalidade={modalidade} incluindo Boletos e Evidencias",
    )

    zip_path = zipar_boletos(caminho_lote, nome_adm, modalidade, mes_ref)
    nome_zip = os.path.basename(zip_path)

    log_info(
        caminho_log=caminho_log,
        etapa="DRIVE",
        id_dado=id_fila_adm,
        acao="Upload Drive",
        detalhe=f"arquivo={nome_zip}",
    )

    link = criar_link_drive(zip_path, nome_zip, caminho_log, id_fila_adm)

    atualizar_link_drive_fila_adm(id_fila_adm, link)

    log_info(
        caminho_log=caminho_log,
        etapa="DRIVE",
        id_dado=id_fila_adm,
        acao="Finalizar upload",
        detalhe="link_drive gravado",
    )

    return link