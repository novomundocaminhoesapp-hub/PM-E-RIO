import os
import time
from datetime import datetime, timedelta
import re
import urllib.parse
import traceback
import json
import unicodedata
import hashlib
import html
import io
import mimetypes
from google import genai

from flask import Flask, abort, redirect, render_template_string, request, session, url_for, jsonify, send_file
from werkzeug.exceptions import HTTPException
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as PdfImage, KeepInFrame, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.utils import ImageReader
import gspread

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "troque-esta-chave-em-producao")

MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"
}

NOMES_MODULOS = {
    "rio": "Telemetria RIO",
    "pm": "Plano de Manutenção",
    "valores": "Tabela de Valores",
    "informes": "Informes e Circulares",
    "fichatecnica": "Ficha Técnica",
    "argumentos": "Argumentos de Venda",
    "negocios": "Negócios em Andamento",
    "visitas": "Visitas e Acompanhamento",
    "pedidos": "Propostas a Clientes",
    "vendas": "Vendas Fechadas",
    "dashboard": "Dashboard Executivo",
    "camp_vw_prev": "Campanhas",
    "traton": "Simulador Traton"
}

escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def conectar_google_sheets():
    """
    Conecta à planilha "PM e RIO Novo".

    Em produção (Render), usa GOOGLE_CREDENTIALS quando configurada.
    Localmente, mantém o funcionamento usando credenciais.json.
    """
    global CACHE_PLANILHA_CLIENTE
    agora = time.time()
    if (
        CACHE_PLANILHA_CLIENTE["cliente"] is not None
        and agora - CACHE_PLANILHA_CLIENTE["timestamp"] < TEMPO_CACHE_CLIENTE_SEGS
    ):
        return CACHE_PLANILHA_CLIENTE["cliente"]

    if "GOOGLE_CREDENTIALS" in os.environ and os.environ["GOOGLE_CREDENTIALS"].strip():
        credenciais_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        credenciais = Credentials.from_service_account_info(
            credenciais_dict,
            scopes=escopos
        )
    else:
        credenciais = Credentials.from_service_account_file(
            "credenciais.json",
            scopes=escopos
        )

    cliente = gspread.authorize(credenciais)
    planilha = cliente.open("PM e RIO Novo")
    CACHE_PLANILHA_CLIENTE = {"cliente": planilha, "timestamp": agora}
    return planilha


def conectar_google_drive():
    credenciais_oauth = os.environ.get("GOOGLE_DRIVE_OAUTH_CREDENTIALS", "").strip()
    if credenciais_oauth:
        credenciais = OAuthCredentials.from_authorized_user_info(
            json.loads(credenciais_oauth), scopes=escopos
        )
    elif "GOOGLE_CREDENTIALS" in os.environ and os.environ["GOOGLE_CREDENTIALS"].strip():
        credenciais = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=escopos
        )
    else:
        credenciais = Credentials.from_service_account_file(
            "credenciais.json", scopes=escopos
        )
    return build("drive", "v3", credentials=credenciais)


def salvar_comprovante_static(arquivo):
    nome_original = secure_filename(os.path.basename(str(arquivo.filename or "comprovante"))) or "comprovante"
    nome_arquivo = f"{time.time_ns()}_{nome_original}"
    pasta_uploads = os.path.join(app.root_path, "static", "uploads")
    os.makedirs(pasta_uploads, exist_ok=True)
    arquivo.stream.seek(0)
    arquivo.save(os.path.join(pasta_uploads, nome_arquivo))
    return f"/static/uploads/{nome_arquivo}"


def nomear_comprovante_venda(cliente, produto, chassis, nome_original):
    componentes = []
    for valor, limite in ((cliente, 40), (produto, 60), (chassis, 40)):
        componente = secure_filename(str(valor or "").strip())[:limite]
        componentes.append(componente or "nao_informado")

    extensao = os.path.splitext(secure_filename(os.path.basename(str(nome_original or ""))))[1].lower()
    return f"{'_'.join(componentes)}_{time.time_ns()}{extensao}"


def subir_comprovante_google_drive(arquivo, permitir_fallback_local=True, nome_arquivo=None):
    """Envia comprovantes para a pasta de vendas no Drive."""
    try:
        service = conectar_google_drive()
        folder_id = os.environ.get("GOOGLE_DRIVE_UPLOAD_VENDAS_FOLDER_ID", "").strip()
        if not folder_id:
            resposta = service.files().list(
                q="name = 'Upload_Vendas' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="files(id, name)",
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            pastas = resposta.get("files", [])
            if len(pastas) != 1:
                raise RuntimeError(
                    "A pasta Upload_Vendas não foi encontrada de forma única."
                )
            folder_id = pastas[0]["id"]

        pasta_upload = service.files().get(
            fileId=folder_id,
            fields="id, name, mimeType, driveId",
            supportsAllDrives=True,
        ).execute()
        if pasta_upload.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError("O ID configurado para Upload_Vendas não é uma pasta do Google Drive.")
        if (
            not pasta_upload.get("driveId")
            and not os.environ.get("GOOGLE_DRIVE_OAUTH_CREDENTIALS", "").strip()
        ):
            raise RuntimeError(
                "Upload_Vendas está no Meu Drive. Contas de serviço não têm cota de armazenamento; "
                "configure GOOGLE_DRIVE_OAUTH_CREDENTIALS com OAuth de um usuário Google "
                "ou mova a pasta para uma Unidade compartilhada."
            )

        nome_original = secure_filename(os.path.basename(str(arquivo.filename or "comprovante"))) or "comprovante"
        nome_arquivo = secure_filename(os.path.basename(str(nome_arquivo or ""))) or f"{time.time_ns()}_{nome_original}"
        arquivo.stream.seek(0)
        media = MediaIoBaseUpload(
            arquivo.stream,
            mimetype=arquivo.mimetype or "application/octet-stream",
            resumable=True,
        )
        enviado = service.files().create(
            body={"name": nome_arquivo, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        return enviado.get("webViewLink") or f"https://drive.google.com/open?id={enviado['id']}"
    except Exception as erro:
        if not permitir_fallback_local:
            raise
        print(f"Upload no Drive indisponível; salvando comprovante em static/uploads: {erro}")
        return salvar_comprovante_static(arquivo)


def extrair_id_arquivo_drive(link):
    texto = str(link or "").strip()
    match = re.search(r"/file/d/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)", texto)
    return next((grupo for grupo in match.groups() if grupo), "") if match else ""


def url_comprovante_no_app(link):
    texto = str(link or "").strip()
    if texto.startswith("/static/uploads/"):
        caminho = os.path.realpath(os.path.join(app.root_path, texto.lstrip("/")))
        pasta_uploads = os.path.realpath(os.path.join(app.root_path, "static", "uploads"))
        if os.path.commonpath([caminho, pasta_uploads]) == pasta_uploads and os.path.isfile(caminho):
            return texto
        return ""
    file_id = extrair_id_arquivo_drive(texto)
    return f"/comprovante-drive/{file_id}" if file_id else ""


def migrar_comprovantes_static(aba_vendas):
    """Migra para o Drive links locais cujos arquivos ainda existem no servidor."""
    linhas = aba_vendas.get_all_values()
    if len(linhas) < 2:
        return 0

    cabecalhos = [normalizar_chave_planilha(valor) for valor in linhas[0]]
    colunas_anexos = [
        (indice, cabecalho)
        for indice, cabecalho in enumerate(cabecalhos)
        if cabecalho in ("anexo 1", "anexo 2", "anexo 3")
    ]
    pasta_uploads = os.path.realpath(os.path.join(app.root_path, "static", "uploads"))
    migrados = 0

    for numero_linha, linha in enumerate(linhas[1:], start=2):
        for indice_coluna, _ in colunas_anexos:
            valor = linha[indice_coluna].strip() if len(linha) > indice_coluna else ""
            if not valor.startswith("/static/uploads/"):
                continue

            caminho = os.path.realpath(os.path.join(app.root_path, valor.lstrip("/")))
            if os.path.commonpath([caminho, pasta_uploads]) != pasta_uploads or not os.path.isfile(caminho):
                continue

            try:
                registro = {
                    cabecalho: linha[indice]
                    for indice, cabecalho in enumerate(cabecalhos)
                    if indice < len(linha)
                }
                produto = str(
                    registro.get("produto")
                    or " / ".join(
                        valor for valor in (
                            registro.get("p manutencao"),
                            registro.get("rio"),
                        ) if valor
                    )
                ).strip()
                nome_arquivo = nomear_comprovante_venda(
                    registro.get("cliente", ""),
                    produto,
                    registro.get("chassis", "") or registro.get("chassi", ""),
                    os.path.basename(caminho),
                )
                with open(caminho, "rb") as arquivo_local:
                    arquivo = FileStorage(
                        stream=arquivo_local,
                        filename=os.path.basename(caminho),
                        content_type=mimetypes.guess_type(caminho)[0] or "application/octet-stream",
                    )
                    link_drive = subir_comprovante_google_drive(
                        arquivo,
                        permitir_fallback_local=False,
                        nome_arquivo=nome_arquivo,
                    )
                aba_vendas.update_cell(numero_linha, indice_coluna + 1, link_drive)
                migrados += 1
            except Exception as erro:
                print(f"Erro ao migrar comprovante da linha {numero_linha}: {erro}")

    if migrados:
        invalidar_cache_ab_as("Vendas_PM")
    return migrados


@app.route("/comprovante-drive/<file_id>")
def servir_comprovante_drive(file_id):
    if not session.get("logado"):
        abort(401)

    try:
        service = conectar_google_drive()
        metadados = service.files().get(
            fileId=file_id, fields="mimeType", supportsAllDrives=True
        ).execute()
        resposta = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        conteudo = io.BytesIO()
        downloader = MediaIoBaseDownload(conteudo, resposta)
        concluido = False
        while not concluido:
            _, concluido = downloader.next_chunk()
        conteudo.seek(0)
        return send_file(
            conteudo,
            mimetype=metadados.get("mimeType", "application/octet-stream"),
            max_age=300,
        )
    except Exception as erro:
        print(f"Erro ao carregar comprovante do Drive {file_id}: {erro}")
        abort(404)


@app.route("/pedido-arquivo/<file_id>")
def servir_arquivo_pedido(file_id):
    if not session.get("logado") or not session.get("perm_pedidos"):
        abort(404)

    try:
        planilha = conectar_google_sheets()
        email_vendedor = str(session.get("email_usuario", "") or "")
        pedidos = listar_pedidos_vendedor(planilha, email_vendedor)
        pedido = next(
            (
                item for item in pedidos
                if str(item.get("DRIVE_FILE_ID", "")).strip() == file_id
            ),
            None,
        )
        if pedido is None:
            abort(404)

        conteudo, mime_type = baixar_arquivo_drive(file_id)
        return send_file(
            io.BytesIO(conteudo),
            mimetype=mime_type,
            as_attachment=False,
            download_name=(
                secure_filename(str(pedido.get("NOME_ARQUIVO", "Pedido.pdf")))
                or "Pedido.pdf"
            ),
            max_age=0,
        )
    except HTTPException:
        raise
    except Exception as erro:
        print(f"Erro ao abrir arquivo do pedido {file_id}: {erro}")
        abort(404)

CACHE_IA = {
    "contexto_sistema": "",
    "timestamp": 0,
    "registros": None,
    "planilha_id": "",
}
TEMPO_CACHE_SEGUNDOS = 600  # 10 minutos de cache da IA

# 👇 ADICIONE ESTE BLOCO AQUI 👇
CACHE_PLANILHAS = {
    "dados": {},
    "timestamps": {}
}
TEMPO_CACHE_PLANILHA_SEGS = 600  # 10 minutos por aba
CACHE_PLANILHA_CLIENTE = {"cliente": None, "timestamp": 0}
TEMPO_CACHE_CLIENTE_SEGS = 300
TEMPO_CACHE_ABAS = {
    "Vendas_PM": 600,
    "Negocios_PM": 600,
    "Informes": 600,
}
CACHE_DRIVE = {"conteudo": {}, "mapa": {}, "timestamp": 0}
TEMPO_CACHE_DRIVE_SEGS = 600
CACHE_ATUALIZACOES_ABAS = {
    "hashes": {},
    "eventos": [],
    "atualizacoes": [],
    "timestamp": 0,
}


# =====================================================================
# REGRAS DE COMISSÃO — FONTE ÚNICA DO CÁLCULO
# =====================================================================
# A tela de comissão, KPIs e gráficos devem usar exatamente estas mesmas
# regras. Isso evita que cada parte do sistema faça um cálculo diferente.
COMISSAO_APM_PM = 250.0
COMISSAO_APM_RIO = 150.0
COMISSAO_VENDEDOR_PM = 200.0
COMISSAO_VENDEDOR_RIO = 150.0


def normalizar_texto_comissao(valor):
    """Normaliza texto para classificação de produto/modelo."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.strip().lower()


def parse_quantidade_comissao(valor):
    """Converte quantidades da planilha sem transformar 1,5 em 15."""
    if valor is None:
        return 1
    texto = str(valor).strip()
    if not texto:
        return 1

    texto = texto.replace(" ", "")
    # Formatos comuns: 1 / 1,0 / 1.0 / 1,5 / 1.500,00 / 1,500.00
    try:
        if "," in texto and "." in texto:
            # Decide pelo separador decimal usando o último separador.
            if texto.rfind(",") > texto.rfind("."):
                texto_num = texto.replace(".", "").replace(",", ".")
            else:
                texto_num = texto.replace(",", "")
            valor_num = float(texto_num)
        elif "," in texto:
            partes = texto.split(",")
            if len(partes[-1]) <= 2:
                valor_num = float(texto.replace(".", "").replace(",", "."))
            else:
                valor_num = float(texto.replace(",", ""))
        elif "." in texto:
            partes = texto.split(".")
            if len(partes) == 2 and len(partes[-1]) <= 2:
                valor_num = float(texto)
            else:
                valor_num = float(texto.replace(".", ""))
        else:
            valor_num = float(re.sub(r"[^0-9-]", "", texto) or "1")

        if valor_num <= 0:
            return 1
        return int(round(valor_num))
    except Exception:
        numeros = re.sub(r"[^0-9]", "", texto)
        return max(1, int(numeros)) if numeros else 1


def parse_data_comissao(valor):
    """Converte datas comuns do Google Sheets/planilha para datetime.
    Aceita dd/mm/aaaa, dd-mm-aaaa, aaaa-mm-dd, ISO com hora e números serial.
    """
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None

    # Datas ISO / Google Sheets com horário
    candidatos = [
        "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y",
        "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    ]
    for fmt in candidatos:
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            pass

    # ISO 8601 com T/Z ou fração de segundos
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass

    # Remove horário de strings como 22/09/2026 00:00:00
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", texto)
    if m:
        d, mo, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a < 100:
            a += 2000
        try:
            return datetime(a, mo, d)
        except ValueError:
            pass

    # Google Sheets pode retornar número serial de data.
    if re.fullmatch(r"\d+(?:\.\d+)?", texto):
        try:
            numero = float(texto)
            if 20000 <= numero <= 80000:
                from datetime import timedelta
                return datetime(1899, 12, 30) + timedelta(days=numero)
        except Exception:
            pass
    return None


def chave_venda_pm(registro):
    """Gera uma chave estável para evitar importar a mesma venda duas vezes."""
    produto = str(registro.get("PRODUTO", "")).strip()
    plano = str(
        registro.get("P. MANUTENÇÃO", "")
        or registro.get("P. MANUTENCAO", "")
        or registro.get("PLANO DE MANUTENÇÃO", "")
        or registro.get("PLANO", "")
    ).strip()
    rio = str(registro.get("RIO", "")).strip()
    if not plano and not rio and produto:
        partes_produto = produto.split(" / ", 1)
        plano = partes_produto[0].strip()
        rio = partes_produto[1].strip() if len(partes_produto) > 1 else ""
    elif not produto:
        produto = f"{plano} / {rio}".strip(" /")

    data = parse_data_comissao(
        registro.get("DATA DA VENDA") or registro.get("DATA", "")
    )
    data_chave = data.strftime("%Y-%m-%d") if data else str(
        registro.get("DATA DA VENDA") or registro.get("DATA", "")
    ).strip()

    return tuple(
        normalizar_texto_comissao(valor)
        for valor in (
            registro.get("CLIENTE", ""),
            plano or produto,
            rio,
            data_chave,
            registro.get("MODELO", ""),
            registro.get("VENDEDOR", ""),
        )
    )


def montar_linha_venda_pm(registro, cabecalhos):
    aliases = {
        "cliente": "CLIENTE",
        "produto": "PRODUTO",
        "numero do contrato": "CONTRATO",
        "numero contrato": "CONTRATO",
        "num do contrato": "CONTRATO",
        "num contrato": "CONTRATO",
        "no do contrato": "CONTRATO",
        "no contrato": "CONTRATO",
        "n do contrato": "CONTRATO",
        "n contrato": "CONTRATO",
        "contrato": "CONTRATO",
        "contrato n": "CONTRATO",
        "p manutencao": "PLANO",
        "p. manutencao": "PLANO",
        "plano de manutencao": "PLANO",
        "plano manutencao": "PLANO",
        "plano": "PLANO",
        "rio": "RIO",
        "data da venda": "DATA",
        "data": "DATA",
        "modelo": "MODELO",
        "quantidade": "QUANTIDADE",
        "vendedor": "VENDEDOR",
        "placa": "PLACA",
        "chassis": "CHASSIS",
        "chassi": "CHASSIS",
        "anexo 1": "ANEXO 1",
        "anexo 2": "ANEXO 2",
        "anexo 3": "ANEXO 3",
    }
    plano = str(
        registro.get("P. MANUTENÇÃO", "")
        or registro.get("PLANO DE MANUTENÇÃO", "")
        or registro.get("PLANO", "")
    ).strip()
    rio = str(registro.get("RIO", "")).strip()
    produto = str(registro.get("PRODUTO", "")).strip()
    if not plano and not rio and produto:
        partes_produto = produto.split(" / ", 1)
        plano = partes_produto[0].strip()
        rio = partes_produto[1].strip() if len(partes_produto) > 1 else ""
    if not produto:
        produto = " / ".join(valor for valor in (plano, rio) if valor)

    valores = {
        "CLIENTE": str(registro.get("CLIENTE", "")).strip(),
        "PRODUTO": produto,
        "CONTRATO": str(registro.get("CONTRATO", "") or obter_numero_contrato(registro)).strip(),
        "PLANO": plano,
        "RIO": rio,
        "DATA": str(registro.get("DATA DA VENDA") or registro.get("DATA", "")).strip(),
        "MODELO": str(registro.get("MODELO", "")).strip(),
        "QUANTIDADE": str(registro.get("QUANTIDADE", "1") or "1").strip(),
        "VENDEDOR": str(registro.get("VENDEDOR", "")).strip(),
        "PLACA": str(registro.get("PLACA", "")).strip(),
        "CHASSIS": str(registro.get("CHASSIS", "") or registro.get("CHASSI", "")).strip(),
        "ANEXO 1": str(registro.get("ANEXO 1", "")).strip(),
        "ANEXO 2": str(registro.get("ANEXO 2", "")).strip(),
        "ANEXO 3": str(registro.get("ANEXO 3", "")).strip(),
    }
    return [
        valores.get(aliases.get(normalizar_chave_planilha(cabecalho), ""), "")
        for cabecalho in cabecalhos
    ]


def normalizar_chave_planilha(valor):
    texto = normalizar_texto_comissao(valor)
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def normalizar_chassi(valor):
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return re.sub(r"[^A-Z0-9]", "", texto.upper())


def normalizar_identidade_comercial(valor):
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return re.sub(r"[^A-Z0-9]", "", texto.upper())


def usuario_eh_gestao(perfil):
    return str(perfil or "").strip().upper() in {"ADM", "DIRETOR", "GERENTE"}


def registro_pertence_ao_usuario(registro, nome_usuario):
    nome_normalizado = normalizar_identidade_comercial(nome_usuario)
    if not nome_normalizado:
        return False

    proprietarios = [
        normalizar_identidade_comercial(valor)
        for cabecalho, valor in registro.items()
        if normalizar_chave_planilha(cabecalho) in {"vendedor", "consultor"}
        and str(valor or "").strip()
    ]
    return bool(proprietarios) and all(
        proprietario == nome_normalizado for proprietario in proprietarios
    )


def registro_planilha_pertence_ao_usuario(aba, numero_linha, nome_usuario):
    if numero_linha <= 1:
        return False
    cabecalhos = aba.row_values(1)
    valores = aba.row_values(numero_linha)
    registro = {
        str(cabecalho).strip(): valores[indice]
        for indice, cabecalho in enumerate(cabecalhos)
        if cabecalho and indice < len(valores)
    }
    return registro_pertence_ao_usuario(registro, nome_usuario)


def garantir_colunas_venda_pm(aba_vendas):
    cabecalhos = aba_vendas.row_values(1)
    if not cabecalhos:
        cabecalhos = [
            "CLIENTE", "P. MANUTENÇÃO", "RIO", "DATA DA VENDA", "MODELO",
            "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "Nº DO CONTRATO",
            "CHASSIS",
        ]
        aba_vendas.append_row(cabecalhos)
        return cabecalhos

    colunas_existentes = {normalizar_chave_planilha(nome) for nome in cabecalhos}
    novas_colunas = [
        nome for nome in ("CHASSIS",)
        if normalizar_chave_planilha(nome) not in colunas_existentes
    ]
    if novas_colunas:
        colunas_necessarias = len(cabecalhos) + len(novas_colunas)
        if colunas_necessarias > aba_vendas.col_count:
            aba_vendas.add_cols(colunas_necessarias - aba_vendas.col_count)
        for nome in novas_colunas:
            cabecalhos.append(nome)
            aba_vendas.update_cell(1, len(cabecalhos), nome)
    return cabecalhos


def obter_numero_contrato(registro):
    cabecalhos_contrato = {
        "numero do contrato", "numero contrato", "num do contrato", "num contrato",
        "no do contrato", "no contrato", "n do contrato", "n contrato",
        "contrato", "contrato n",
    }
    for cabecalho, valor in registro.items():
        if normalizar_chave_planilha(cabecalho) in cabecalhos_contrato:
            return str(valor or "").strip()
    return ""


def separar_produto_venda(registro):
    plano = str(
        registro.get("P. MANUTENÇÃO", "")
        or registro.get("P. MANUTENCAO", "")
        or registro.get("PLANO DE MANUTENÇÃO", "")
        or registro.get("PLANO", "")
        or registro.get("PLANO MANUTENCAO", "")
    ).strip()
    rio = str(registro.get("RIO", "")).strip()
    produto = str(registro.get("PRODUTO", "")).strip()

    if produto and (not plano or not rio):
        partes = produto.split(" / ", 1)
        if len(partes) == 2:
            plano = plano or partes[0].strip()
            rio = rio or partes[1].strip()

    return plano, rio


def sincronizar_negocio_fechado(aba_vendas, negocio, chaves_existentes=None):
    """Registra um negócio fechado em Vendas_PM, sem duplicar a venda.

    Retorna True quando uma nova venda foi criada e False quando ela já existia.
    Esta função concentra a gravação usada tanto pelo fechamento manual quanto
    pela sincronização retroativa do módulo de Vendas.
    """
    if chaves_existentes is None:
        registros_vendas = obter_registros_seguros(aba_vendas)
        chaves_existentes = {chave_venda_pm(registro) for registro in registros_vendas}

    chave = chave_venda_pm(negocio)
    if chave in chaves_existentes:
        return False

    cabecalhos = garantir_colunas_venda_pm(aba_vendas)

    aba_vendas.append_row(montar_linha_venda_pm(negocio, cabecalhos))
    invalidar_cache_ab_as("Vendas_PM")
    chaves_existentes.add(chave)
    return True


def mover_negocio_fechado_para_vendas(aba_negocios, aba_vendas, index_linha, negocio, chaves_existentes=None):
    """Move um negócio fechado de Negocios_PM para Vendas_PM.

    A origem só é excluída depois que a venda é confirmada em Vendas_PM.
    Isso evita perder o negócio se houver erro na gravação da venda.
    """
    criado = sincronizar_negocio_fechado(aba_vendas, negocio, chaves_existentes)

    # Se já estava em Vendas_PM, ainda assim o objetivo do status Fechado é
    # retirar o registro da fila de negócios em andamento.
    if index_linha and int(index_linha) > 1:
        aba_negocios.delete_rows(int(index_linha))
        invalidar_cache_ab_as("Negocios_PM")

    return criado


def detectar_produtos_comissao(produto, modelo, registro=None):
    """Identifica PM/RIO usando todas as colunas relevantes da venda.
    Evita que o relatório fique zerado quando a planilha usa nomes diferentes
    para a descrição do produto.
    """
    registro = registro or {}
    campos = [
        produto, modelo,
        registro.get("PRODUTO", ""),
        registro.get("PLANO DE MANUTENÇÃO", ""),
        registro.get("P. MANUTENÇÃO", ""),
        registro.get("PLANO", ""),
        registro.get("RIO", ""),
        registro.get("TELEMETRIA RIO", ""),
        registro.get("SERVIÇO", ""),
    ]
    textos = [normalizar_texto_comissao(x) for x in campos if str(x or "").strip()]
    texto = " | ".join(textos)

    # RIO deve ser identificado antes por seus próprios campos ou palavras.
    rio_coluna = normalizar_texto_comissao(registro.get("RIO", ""))
    is_rio = bool(rio_coluna and rio_coluna not in ("-", "nenhum", "nao")) or any(t in texto for t in (
        "rio", "telemetria", "diagnostico", "diagnostico remoto",
        "conectividade rio", "rio remote", "performance", "geo",
        "analise de eficiencia"
    ))
    # PM: PREV/MAX/PLUS ou plano/manutenção; também aceita nomes de coluna.
    is_pm = any(t in texto for t in (
        "prev", "max", "plus", "plano de manutencao", "plano",
        "manutencao", "manutenção"
    ))
    return is_pm, is_rio


def calcular_comissoes_venda(produto, modelo, quantidade, registro=None):
    """Calcula APM e vendedor para uma venda, em um único ponto do sistema."""
    qtd = parse_quantidade_comissao(quantidade)
    is_pm, is_rio = detectar_produtos_comissao(produto, modelo, registro)

    vendedor_pm_unit = COMISSAO_VENDEDOR_PM if is_pm else 0.0
    vendedor_rio_unit = COMISSAO_VENDEDOR_RIO if is_rio else 0.0

    return {
        "qtd": qtd,
        "is_pm": is_pm,
        "is_rio": is_rio,
        "apm_pm": COMISSAO_APM_PM * qtd if is_pm else 0.0,
        "apm_rio": COMISSAO_APM_RIO * qtd if is_rio else 0.0,
        "vendedor_pm": vendedor_pm_unit * qtd if is_pm else 0.0,
        "vendedor_rio": vendedor_rio_unit * qtd if is_rio else 0.0,
    }

def criar_cliente_gemini():
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        for caminho_env in (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
            os.path.join(os.getcwd(), ".env"),
        ):
            try:
                if not os.path.exists(caminho_env):
                    continue
                with open(caminho_env, "r", encoding="utf-8") as f:
                    for linha in f:
                        linha = linha.strip()
                        if "=" not in linha or linha.startswith("#"):
                            continue
                        chave, valor = linha.split("=", 1)
                        valor = valor.strip().strip('"').strip("'")
                        if chave.strip() in ("GEMINI_API_KEY", "GOOGLE_API_KEY") and valor:
                            api_key = valor
                            break
                if api_key:
                    break
            except Exception:
                pass
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY não configurada no ambiente ou .env.")
    return genai.Client(api_key=api_key)


def aba_sensivel_para_ia(nome_aba):
    palavras = set(normalizar_chave_planilha(nome_aba).split())
    nomes_sensiveis = {
        "usuario", "usuarios", "user", "users", "log", "logs", "login",
        "logins", "acesso", "acessos", "credencial", "credenciais",
    }
    return bool(palavras.intersection(nomes_sensiveis))


def campo_sensivel_para_ia(nome_campo):
    campo = normalizar_chave_planilha(nome_campo)
    palavras_sensiveis = {
        "senha", "password", "cpf", "cnpj", "email", "token", "secret",
        "segredo", "credencial", "usuario", "user", "login", "telefone",
        "celular", "rg", "endereco", "cep", "nascimento",
    }
    frases_sensiveis = {"e mail", "api key", "chave api", "access token", "refresh token"}
    palavras = set(campo.split())
    return bool(palavras.intersection(palavras_sensiveis)) or any(
        frase in campo for frase in frases_sensiveis
    )


def link_planilha_google(url, planilha_id=""):
    url_normalizada = str(url or "").lower()
    return (
        "docs.google.com/spreadsheets" in url_normalizada
        or bool(planilha_id and planilha_id in url_normalizada)
    )


def carregar_indice_planilha_ia(planilha):
    registros_ia = []
    links_planilha = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
    planilha_id = str(getattr(planilha, "id", "") or "")

    for aba in planilha.worksheets():
        nome_aba = str(aba.title or "").strip()
        if not nome_aba or aba_sensivel_para_ia(nome_aba):
            continue

        try:
            registros = obter_registros_com_cache(planilha, nome_aba)
        except Exception as erro:
            print(f"IA: falha ao indexar aba {nome_aba}: {erro}")
            continue

        for registro in registros:
            campos = []
            links = set()
            for nome_campo, valor in registro.items():
                if campo_sensivel_para_ia(nome_campo):
                    continue
                texto_valor = str(valor or "").strip()
                if not texto_valor:
                    continue

                def preservar_link_existente(match):
                    url = match.group(0).rstrip(".,;:!?)\"]}")
                    if link_planilha_google(url, planilha_id):
                        return "[link da planilha omitido]"
                    links.add(url)
                    return url

                texto_valor = links_planilha.sub(preservar_link_existente, texto_valor)
                campos.append(f"{nome_campo}: {texto_valor}")

            if campos:
                texto_registro = f"Aba {nome_aba} | " + " | ".join(campos)
                registros_ia.append({
                    "texto": texto_registro,
                    "busca": normalizar_texto_comissao(texto_registro),
                    "links": links,
                })

    return registros_ia, planilha_id


def selecionar_contexto_ia(registros, pergunta, limite_linhas=50, limite_caracteres=18000):
    stopwords = {
        "com", "que", "qual", "quais", "como", "para", "por", "uma", "uns",
        "umas", "dos", "das", "mais", "menos", "meu", "minha", "seu", "sua",
        "tem", "ter", "ser", "está", "esta", "são", "sao", "sobre", "entre",
        "onde", "quando", "isso", "essa", "esse", "favor", "pode", "me", "de",
        "do", "da", "no", "na", "em", "os", "as", "um", "ao", "aos", "e",
        "ou", "se", "eu", "ele", "ela", "voce", "vocês", "voces",
    }
    termos = {
        termo for termo in re.findall(r"[a-z0-9]{2,}", normalizar_texto_comissao(pergunta))
        if termo not in stopwords
    }
    if not termos:
        return "", set()

    encontrados = []
    for indice, registro in enumerate(registros):
        score = sum(termo in registro["busca"] for termo in termos)
        if score:
            encontrados.append((score, indice, registro))
    encontrados.sort(key=lambda item: (-item[0], item[1]))

    linhas = []
    links_permitidos = set()
    tamanho = 0
    for _, _, registro in encontrados[:limite_linhas]:
        if tamanho + len(registro["texto"]) > limite_caracteres:
            continue
        linhas.append(registro["texto"])
        links_permitidos.update(registro["links"])
        tamanho += len(registro["texto"])

    return "\n".join(linhas), links_permitidos


def filtrar_links_resposta_ia(texto, links_permitidos, planilha_id=""):
    padrao_url = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

    def validar_url(match):
        url_original = match.group(0)
        url = url_original.rstrip(".,;:!?)\"]}")
        sufixo = url_original[len(url):]
        if link_planilha_google(url, planilha_id):
            return "[link da planilha não compartilhado]" + sufixo
        if url not in links_permitidos:
            return "[link não encontrado nos dados consultados]" + sufixo
        return url_original

    return padrao_url.sub(validar_url, str(texto or ""))


def validar_cpf(cpf_input):
    cpf = re.sub(r'\D', '', str(cpf_input))
    if len(cpf) < 11:
        cpf = cpf.zfill(11)
        
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
        
    for i in range(9, 11):
        soma = sum(int(cpf[num]) * ((i + 1) - num) for num in range(0, i))
        digito = ((soma * 10) % 11) % 10
        if digito != int(cpf[i]):
            return False
            
    return True
    return True

def validar_cnpj(cnpj_input):
    cnpj = re.sub(r"\D", "", str(cnpj_input))
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    for tamanho, pesos in (
        (12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
        (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
    ):
        soma = sum(
            int(digito) * peso
            for digito, peso in zip(cnpj[:tamanho], pesos)
        )
        resto = soma % 11
        digito_verificador = 0 if resto < 2 else 11 - resto
        if digito_verificador != int(cnpj[tamanho]):
            return False

    return True


def validar_documento_cliente(documento):
    documento = str(documento or "").strip()
    if not re.fullmatch(r"[\d./\-\s]+", documento):
        return False

    digitos = re.sub(r"\D", "", documento)
    if len(digitos) == 11:
        return validar_cpf(digitos)
    if len(digitos) == 14:
        return validar_cnpj(digitos)
    return False


def formatar_telefone_br(telefone):
    digitos = re.sub(r"\D", "", str(telefone or ""))[:11]
    if len(digitos) not in {10, 11}:
        return str(telefone or "").strip()
    tamanho_numero = 5 if len(digitos) == 11 else 4
    return (
        f"({digitos[:2]}) "
        f"{digitos[2:2 + tamanho_numero]}-{digitos[2 + tamanho_numero:]}"
    )


CACHE_LOGIN_DADOS = {"dados": {}, "timestamp": 0, "usuario": ""}
TEMPO_CACHE_LOGIN_SEGS = 600

def carregar_dados_login():
    """
    Pré-carrega as abas usadas pelo Dashboard/IA em uma única conexão.
    Depois do primeiro carregamento, as requisições reutilizam o cache.
    """
    global CACHE_LOGIN_DADOS
    agora = time.time()
    usuario = str(session.get("usuario", "") or session.get("email", "") or "")

    if (
        CACHE_LOGIN_DADOS["dados"]
        and CACHE_LOGIN_DADOS["usuario"] == usuario
        and agora - CACHE_LOGIN_DADOS["timestamp"] < TEMPO_CACHE_LOGIN_SEGS
    ):
        return CACHE_LOGIN_DADOS["dados"]

    planilha = conectar_google_sheets()
    abas = ["PM", "RIO", "PM_Precos", "Promocao VW", "Informes", "Argumentos", "Modelos", "Usuarios",
            "Negocios_PM", "Vendas_PM"]

    dados = {}
    for nome_aba in abas:
        if nome_aba == "Promocao VW":
            try:
                chave_aba_promocao = normalizar_chave_manutencao(nome_aba)
                aba_promocao = next(
                    (
                        aba
                        for aba in planilha.worksheets()
                        if normalizar_chave_manutencao(aba.title)
                        == chave_aba_promocao
                    ),
                    None,
                )
                if aba_promocao is None:
                    print(
                        "Aba opcional da campanha VW não encontrada na planilha "
                        "'PM e RIO Novo'; o quadro da campanha ficará oculto."
                    )
                    dados[nome_aba] = []
                else:
                    dados[nome_aba] = obter_registros_com_cache(
                        planilha,
                        aba_promocao.title,
                    )
            except Exception as e:
                print(f"Erro ao carregar aba opcional da campanha VW: {e}")
                dados[nome_aba] = []
            continue

        try:
            dados[nome_aba] = obter_registros_com_cache(planilha, nome_aba)
        except Exception as e:
            print(f"⚠️ Pré-carga da aba {nome_aba}: {e}")
            dados[nome_aba] = []

    CACHE_LOGIN_DADOS = {
        "dados": dados,
        "timestamp": agora,
        "usuario": usuario,
    }
    return dados


def obter_registros_com_cache(planilha, nome_aba, ttl=None, falhar_em_erro=False):
    """Lê uma aba com cache independente por aba."""
    global CACHE_PLANILHAS
    agora = time.time()
    ttl = TEMPO_CACHE_ABAS.get(nome_aba, TEMPO_CACHE_PLANILHA_SEGS) if ttl is None else ttl
    timestamp = CACHE_PLANILHAS["timestamps"].get(nome_aba, 0)

    if (nome_aba not in CACHE_PLANILHAS["dados"]) or (agora - timestamp > ttl):
        inicio_leitura = time.perf_counter()
        try:
            aba = planilha.worksheet(nome_aba)
            registros = obter_registros_seguros(aba)
            CACHE_PLANILHAS["dados"][nome_aba] = registros
            CACHE_PLANILHAS["timestamps"][nome_aba] = agora
            duracao_ms = (time.perf_counter() - inicio_leitura) * 1000
            print(f"Cache Sheets miss: aba={nome_aba} linhas={len(registros)} duracao_ms={duracao_ms:.0f}")
        except Exception as e:
            duracao_ms = (time.perf_counter() - inicio_leitura) * 1000
            print(f"Erro ao carregar aba {nome_aba} duracao_ms={duracao_ms:.0f}: {e}")
            if falhar_em_erro:
                raise
            return []
    return CACHE_PLANILHAS["dados"].get(nome_aba, [])


def invalidar_cache_ab_as(*nomes_abas):
    global CACHE_LOGIN_DADOS
    for nome_aba in nomes_abas:
        CACHE_PLANILHAS["dados"].pop(nome_aba, None)
        CACHE_PLANILHAS["timestamps"].pop(nome_aba, None)
    CACHE_LOGIN_DADOS = {"dados": {}, "timestamp": 0, "usuario": ""}
    if nomes_abas:
        CACHE_IA["contexto_sistema"] = ""
        CACHE_IA["timestamp"] = 0
        CACHE_IA["registros"] = None
        CACHE_IA["planilha_id"] = ""


def obter_registros_seguros(aba):
    linhas = aba.get_all_values()
    return registros_de_linhas_planilha(linhas)


def registros_de_linhas_planilha(linhas):
    if not linhas or len(linhas) <= 1:
        return []
    cabecalhos = linhas[0]
    dados = []
    for linha in linhas[1:]:
        item_dict = {}
        for i, valor_celula in enumerate(linha):
            if i < len(cabecalhos) and str(cabecalhos[i]).strip():
                nome_coluna = str(cabecalhos[i]).strip()
                if nome_coluna in item_dict:
                    idx = 1
                    while f"{nome_coluna}_{idx}" in item_dict:
                        idx += 1
                    nome_coluna = f"{nome_coluna}_{idx}"
                item_dict[nome_coluna] = valor_celula
        if any(str(v).strip() for v in item_dict.values()):
            dados.append(item_dict)
    return dados


CABECALHOS_PEDIDOS = [
    "ID_PEDIDO", "DATA_PEDIDO", "VENDEDOR", "TELEFONE_VENDEDOR",
    "CELULAR_VENDEDOR", "EMAIL_VENDEDOR", "CLIENTE", "DOCUMENTO_CLIENTE", "TELEFONE_CLIENTE",
    "EMAIL_CLIENTE", "CIDADE", "UF", "MODELO", "SEGMENTO", "TIPO", "CATEGORIA",
    "QUANTIDADE", "VALOR_UNITARIO", "VALOR_TOTAL", "PLANO_MANUTENCAO",
    "RIO", "ANO_MODELO", "CABINE", "MOTOR", "TRANSMISSAO", "PBT",
    "ENTRE_EIXOS", "COMBUSTIVEL", "INFORMACOES_COMPLEMENTARES", "GARANTIA",
    "ASSISTENCIA", "CONDICOES_PM", "MODALIDADE_FATURAMENTO", "FATURANTE",
    "CNPJ_FATURANTE", "PAGAMENTO", "DGA", "COD_FINAME", "PAC", "CLASSIFICACAO_FISCAL",
    "LOCAL_ENTREGA", "PRAZO_ENTREGA", "DETALHES", "VALIDADE", "LINK_FICHA_TECNICA",
    "IMAGEM_MODELO_ID",
    "NOME_ARQUIVO", "DRIVE_FILE_ID", "TECNOLOGIA", "SEGMENTO_FICHA",
    "TECNO", "POTENCIA", "SISTEMA_INJECAO",
]
ABA_PEDIDOS_FEITOS = "Pedidos_feitos"
ALIASES_CABECALHOS_PEDIDOS = {
    "DATA_PEDIDO": ("DATA",),
    "VENDEDOR": ("CONSULTOR",),
    "TELEFONE_VENDEDOR": ("TELEFONE CONSULTOR",),
    "CELULAR_VENDEDOR": ("CELULAR CONSULTOR",),
    "EMAIL_VENDEDOR": ("EMAIL CONSULTOR",),
    "DOCUMENTO_CLIENTE": ("CNPJ",),
    "ANO_MODELO": ("FAB/MODELO",),
    "INFORMACOES_COMPLEMENTARES": (
        "CONDICOES COMPLEMENTARES",
        "INFORMACOES COMPLEMENTARES",
    ),
    "PLANO_MANUTENCAO": ("PLANO DE MANUTENCAO", "PLANO MANUTENCAO"),
    "RIO": ("TELEMETRIA RIO",),
    "ASSISTENCIA": ("CHAMEVOLKS", "CHAME VOLKS"),
    "CONDICOES_PM": ("VOLKSTOTAL", "VOLKS TOTAL"),
    "VALOR_UNITARIO": ("VALOR UNITAR",),
    "VALOR_TOTAL": ("VALOR TOTAL",),
    "MODALIDADE_FATURAMENTO": ("MOD. FAT.", "MODALIDADE FATURAMENTO"),
    "FATURANTE": ("FATURAMENTO",),
    "CLASSIFICACAO_FISCAL": ("CLASS. FISCAL",),
    "COD_FINAME": ("COD FINAME",),
    "PAC": ("PAC Nº",),
    "LOCAL_ENTREGA": ("ENTREGA",),
    "PRAZO_ENTREGA": ("PRAZO", "PRAZO DE ENTREGA"),
    "DETALHES": ("OBSERVACOES", "OBSERVACOES DO PEDIDO"),
    "VALIDADE": ("PROPOSTA VALIDA ATE",),
}


def obter_pasta_upload_pedidos(service):
    folder_id = os.environ.get("GOOGLE_DRIVE_UPLOAD_PEDIDOS_FOLDER_ID", "").strip()
    if folder_id:
        pasta = service.files().get(
            fileId=folder_id,
            fields="id, name, mimeType, driveId",
            supportsAllDrives=True,
        ).execute()
        if pasta.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(
                "GOOGLE_DRIVE_UPLOAD_PEDIDOS_FOLDER_ID não aponta para uma pasta."
            )
    else:
        resposta = service.files().list(
            q="name = 'Upload_Pedidos' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id, name, mimeType, driveId)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        pastas = resposta.get("files", [])
        if len(pastas) != 1:
            raise RuntimeError(
                "A pasta Upload_Pedidos não foi encontrada de forma única. "
                "Configure GOOGLE_DRIVE_UPLOAD_PEDIDOS_FOLDER_ID com o ID da pasta compartilhada."
            )
        pasta = pastas[0]

    if (
        not pasta.get("driveId")
        and not os.environ.get("GOOGLE_DRIVE_OAUTH_CREDENTIALS", "").strip()
    ):
        raise RuntimeError(
            "Upload_Pedidos está no Meu Drive. Configure GOOGLE_DRIVE_OAUTH_CREDENTIALS "
            "com OAuth de um usuário Google que tenha acesso à pasta; contas de serviço "
            "não têm cota para criar arquivos no Meu Drive."
        )
    return pasta["id"]


def baixar_arquivo_drive(file_id, service=None):
    service = service or conectar_google_drive()
    metadados = service.files().get(
        fileId=file_id,
        fields="mimeType",
        supportsAllDrives=True,
    ).execute()
    resposta = service.files().get_media(
        fileId=file_id,
        supportsAllDrives=True,
    )
    conteudo = io.BytesIO()
    downloader = MediaIoBaseDownload(conteudo, resposta)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    return conteudo.getvalue(), metadados.get("mimeType", "application/octet-stream")


def gerar_pdf_pedido(dados_pedido, modelo_pedido, imagem_drive_id=""):
    from xml.sax.saxutils import escape as xml_escape

    buffer = io.BytesIO()
    documento = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=8 * mm,
        leftMargin=8 * mm,
        topMargin=7 * mm,
        bottomMargin=7 * mm,
        title=f"Proposta {dados_pedido.get('ID_PEDIDO', '')}",
        author=str(dados_pedido.get("VENDEDOR", "")),
    )
    estilos_base = getSampleStyleSheet()
    azul = colors.HexColor("#1e4778")
    texto = ParagraphStyle(
        "PedidoTexto",
        parent=estilos_base["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8,
        textColor=colors.HexColor("#1f2937"),
        spaceAfter=2,
    )
    menor = ParagraphStyle(
        "PedidoMenor",
        parent=texto,
        fontSize=6,
        leading=7,
        textColor=colors.HexColor("#64748b"),
    )
    validade_estilo = ParagraphStyle(
        "PedidoValidade",
        parent=menor,
        fontName="Helvetica-Bold",
        alignment=TA_CENTER,
        textColor=azul,
    )
    assinatura_estilo = ParagraphStyle(
        "PedidoAssinatura",
        parent=menor,
        alignment=TA_CENTER,
    )
    modelo_estilo = ParagraphStyle(
        "PedidoModelo",
        parent=texto,
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=12,
        textColor=azul,
    )
    secao_estilo = ParagraphStyle(
        "PedidoSecao",
        parent=texto,
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=8,
        textColor=colors.white,
    )
    total_estilo = ParagraphStyle(
        "PedidoTotal",
        parent=secao_estilo,
        alignment=TA_RIGHT,
    )
    rotulo_complementar_estilo = ParagraphStyle(
        "PedidoRotuloComplementar",
        parent=menor,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    def paragrafo(valor, estilo=texto):
        seguro = xml_escape(str(valor or "").strip()).replace("\n", "<br/>")
        return Paragraph(seguro or "—", estilo)

    def paragrafo_formatado(valor, estilo=texto):
        return Paragraph(str(valor or "—"), estilo)

    def dinheiro(valor):
        numero = converter_numero(valor)
        if numero is None:
            return "—"
        return f"R$ {numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def obter_valor_pedido(chave):
        valor = dados_pedido.get(chave, "")
        if str(valor or "").strip():
            return str(valor).strip()
        nomes_coluna = (chave, *ALIASES_CABECALHOS_PEDIDOS.get(chave, ()))
        nomes_normalizados = {
            normalizar_chave_planilha(nome)
            for nome in nomes_coluna
        }
        return next(
            (
                str(valor_coluna).strip()
                for nome_coluna, valor_coluna in dados_pedido.items()
                if normalizar_chave_planilha(nome_coluna) in nomes_normalizados
                and str(valor_coluna or "").strip()
            ),
            "",
        )

    validade = str(dados_pedido.get("VALIDADE", "") or "").strip()
    if validade:
        try:
            validade = datetime.strptime(validade, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass
    faixa_validade = Table(
        [[paragrafo_formatado(
            f"<b>Proposta válida até {xml_escape(validade or '—')}</b>",
            validade_estilo,
        )]],
        colWidths=[194 * mm],
    )
    faixa_validade.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef3f8")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    def secao(rotulo):
        tabela = Table([[paragrafo(rotulo, secao_estilo)]], colWidths=[194 * mm])
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), azul),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return [Spacer(1, 3), tabela, Spacer(1, 2)]

    def grade_campos(campos, colunas=2, omitir_vazios=False):
        def valor_campo(chave):
            valor = obter_valor_pedido(chave)
            if chave == "VALIDADE" and valor:
                try:
                    return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
                except ValueError:
                    pass
            return valor

        celulas = [
            paragrafo_formatado(
                f"<b>{xml_escape(rotulo)}:</b> "
                f"{xml_escape(valor_campo(chave)) or '—'}"
            )
            for chave, rotulo in campos
            if not omitir_vazios or valor_campo(chave)
        ]
        if not celulas:
            return ""
        linhas = []
        for inicio in range(0, len(celulas), colunas):
            linha = celulas[inicio:inicio + colunas]
            linha.extend([""] * (colunas - len(linha)))
            linhas.append(linha)
        tabela = Table(linhas, colWidths=[194 * mm / colunas] * colunas, hAlign="LEFT")
        tabela.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe3ed")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return tabela

    def grade_campos_com_rotulo_lateral(campos):
        linhas = [
            [
                paragrafo_formatado(
                    xml_escape(rotulo),
                    rotulo_complementar_estilo,
                ),
                paragrafo(obter_valor_pedido(chave)),
            ]
            for chave, rotulo in campos
        ]
        tabela = Table(
            linhas,
            colWidths=[25 * mm, 169 * mm],
            hAlign="LEFT",
        )
        tabela.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe3ed")),
            ("BACKGROUND", (0, 0), (0, -1), azul),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ]))
        return tabela

    data_pedido = str(dados_pedido.get("DATA_PEDIDO", "") or "")
    try:
        data_pedido = datetime.strptime(data_pedido, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        pass

    story = []
    logos_pdf = []
    for nome_logo, limite_largura, limite_altura in (
        ("logo2.png", 90 * mm, 36 * mm),
        ("VW_TRANS.png", 72 * mm, 30 * mm),
    ):
        caminho_logo = os.path.join(app.root_path, "static", nome_logo)
        leitor_logo = ImageReader(caminho_logo)
        largura_logo, altura_logo = leitor_logo.getSize()
        escala_logo = min(
            limite_largura / largura_logo,
            limite_altura / altura_logo,
        )
        logos_pdf.append(PdfImage(
            caminho_logo,
            width=largura_logo * escala_logo,
            height=altura_logo * escala_logo,
            mask="auto",
        ))
    linha_logos = Table(
        [[logos_pdf[0], "", logos_pdf[1]]],
        colWidths=[82 * mm, 30 * mm, 82 * mm],
        hAlign="LEFT",
    )
    linha_logos.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    cabecalho = Table(
        [[
            [paragrafo("Caminhões e Ônibus · Proposta sujeita à confirmação das condições comerciais", menor)],
            [paragrafo_formatado(f"<b>Data:</b> {xml_escape(data_pedido)}", menor)],
        ]],
        colWidths=[140 * mm, 54 * mm],
    )
    cabecalho.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 2, azul),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.extend([linha_logos, cabecalho, Spacer(1, 2)])
    story.extend(secao("CLIENTE"))
    story.append(grade_campos([
        ("CLIENTE", "Nome / Razão social"),
        ("DOCUMENTO_CLIENTE", "CPF / CNPJ"),
        ("TELEFONE_CLIENTE", "Telefone"),
        ("EMAIL_CLIENTE", "E-mail"),
        ("CIDADE", "Cidade"),
        ("UF", "Estado"),
    ], 3))
    story.extend(secao("VEÍCULO OFERTADO"))

    imagem_pdf = None
    if imagem_drive_id:
        imagem_bytes, mime_imagem = baixar_arquivo_drive(imagem_drive_id)
        if not mime_imagem.startswith("image/"):
            raise ValueError(
                f"O arquivo do modelo {imagem_drive_id} não é uma imagem "
                f"(tipo recebido: {mime_imagem})."
            )
        leitor = ImageReader(io.BytesIO(imagem_bytes))
        largura, altura = leitor.getSize()
        escala = min(32 * mm / largura, 25 * mm / altura)
        imagem_pdf = PdfImage(
            io.BytesIO(imagem_bytes),
            width=largura * escala,
            height=altura * escala,
        )

    identificacao_modelo = [
        paragrafo("MODELO SELECIONADO", menor),
        paragrafo(dados_pedido.get("MODELO", ""), modelo_estilo),
    ]
    cartoes_modelo = Table(
        [[
            paragrafo_formatado(
                f"<b>ANO / MODELO</b><br/>{xml_escape(str(dados_pedido.get('ANO_MODELO', '') or '—'))}",
                menor,
            ),
            paragrafo_formatado(
                f"<b>CABINE</b><br/>{xml_escape(str(dados_pedido.get('CABINE', '') or '—'))}",
                menor,
            ),
        ], [
            paragrafo_formatado(
                f"<b>TIPO</b><br/>{xml_escape(str(dados_pedido.get('TIPO', '') or '—'))}",
                menor,
            ),
            paragrafo_formatado(
                f"<b>CATEGORIA</b><br/>{xml_escape(str(dados_pedido.get('CATEGORIA', '') or '—'))}",
                menor,
            ),
        ]],
        colWidths=[75 * mm, 75 * mm] if imagem_pdf else [93 * mm, 93 * mm],
    )
    cartoes_modelo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    identificacao_modelo.extend((Spacer(1, 3), cartoes_modelo))
    campos_tecnicos = [
        ("TECNO", "Tecnologia do motor"),
        ("PBT", "PBT"),
        ("ENTRE_EIXOS", "Entre-eixos"),
        ("MOTOR", "Motor"),
        ("POTENCIA", "Potência"),
        ("TRANSMISSAO", "Transmissão"),
        ("SISTEMA_INJECAO", "Sistema de injeção"),
        ("COMBUSTIVEL", "Combustível"),
    ]
    valores_tecnicos = [
        (chave, rotulo, str(dados_pedido.get(chave, "") or "").strip())
        for chave, rotulo in campos_tecnicos
        if str(dados_pedido.get(chave, "") or "").strip()
    ]
    if valores_tecnicos:
        largura_detalhe = 150 if imagem_pdf else 186
        larguras_tecnicas = [
            largura_detalhe * 0.18 * mm,
            largura_detalhe * 0.32 * mm,
            largura_detalhe * 0.18 * mm,
            largura_detalhe * 0.32 * mm,
        ]
        linhas_tecnicas = []
        for inicio in range(0, len(valores_tecnicos), 2):
            linha_tecnica = []
            for _, rotulo, valor in valores_tecnicos[inicio:inicio + 2]:
                linha_tecnica.extend((
                    paragrafo_formatado(f"<b>{xml_escape(rotulo)}:</b>", menor),
                    paragrafo_formatado(xml_escape(valor), menor),
                ))
            linha_tecnica.extend([""] * (4 - len(linha_tecnica)))
            linhas_tecnicas.append(linha_tecnica)
        tabela_tecnica = Table(
            linhas_tecnicas,
            colWidths=larguras_tecnicas,
            hAlign="LEFT",
        )
        tabela_tecnica.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe3ed")),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
        identificacao_modelo.extend((Spacer(1, 4), tabela_tecnica))
    if imagem_pdf:
        bloco_modelo = Table(
            [[imagem_pdf, identificacao_modelo]],
            colWidths=[36 * mm, 158 * mm],
        )
    else:
        bloco_modelo = Table([[identificacao_modelo]], colWidths=[194 * mm])
    bloco_modelo.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(bloco_modelo)
    story.append(Spacer(1, 2))
    story.extend(secao("PLANOS E INFORMAÇÕES COMPLEMENTARES"))
    campos_opcionais_complementares = [
        (chave, rotulo)
        for chave, rotulo in (
            ("PLANO_MANUTENCAO", "Plano de manutenção"),
            ("RIO", "Telemetria RIO"),
        )
        if obter_valor_pedido(chave)
    ]
    tabela_opcionais = grade_campos(
        campos_opcionais_complementares,
        colunas=2,
        omitir_vazios=True,
    )
    if tabela_opcionais:
        story.append(tabela_opcionais)
    story.append(grade_campos_com_rotulo_lateral([
        ("GARANTIA", "Garantia"),
        ("ASSISTENCIA", "Chame Volks"),
        ("CONDICOES_PM", "VolksTotal"),
        ("INFORMACOES_COMPLEMENTARES", "Informações"),
    ]))
    story.extend(secao("VALORES E CONDIÇÕES DE FATURAMENTO"))
    story.append(grade_campos([
        ("QUANTIDADE", "Quantidade"),
        ("VALOR_UNITARIO", "Valor unitário"),
        ("MODALIDADE_FATURAMENTO", "Modalidade de faturamento"),
        ("FATURANTE", "Faturante"),
        ("CNPJ_FATURANTE", "CNPJ faturante"),
        ("PAGAMENTO", "Pagamento"),
        ("DGA", "DGA"),
        ("COD_FINAME", "Código FINAME"),
        ("PAC", "PAC nº"),
        ("CLASSIFICACAO_FISCAL", "Classificação fiscal"),
        ("LOCAL_ENTREGA", "Entrega"),
        ("PRAZO_ENTREGA", "Prazo de entrega"),
        ("VALIDADE", "Validade da proposta"),
    ], 3))
    total = Table(
        [[paragrafo("VALOR TOTAL", total_estilo), paragrafo(dinheiro(dados_pedido.get("VALOR_TOTAL")), total_estilo)]],
        colWidths=[150 * mm, 44 * mm],
    )
    total.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), azul),
        ("ALIGN", (0, 0), (0, 0), "RIGHT"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.extend([Spacer(1, 3), total])
    story.append(Spacer(1, 4))
    story.append(paragrafo_formatado(
        f"<b>Detalhes:</b><br/>{xml_escape(str(dados_pedido.get('DETALHES', '') or '—')).replace(chr(10), '<br/>')}",
        texto,
    ))
    story.append(Spacer(1, 5))
    contato_superintendente = (
        "<b>Ricardo Ricarte</b><br/>Superintendente<br/>"
        "(82) 99134-5112<br/>ricardo.ricarte@adtsa.com.br"
    )
    contato_cliente = (
        f"<b>{xml_escape(str(dados_pedido.get('CLIENTE', '') or 'Cliente'))}</b><br/>"
        f"{xml_escape(str(dados_pedido.get('DOCUMENTO_CLIENTE', '') or 'CNPJ não informado'))}"
        "<br/>Cliente"
    )
    telefone_vendedor = str(dados_pedido.get("TELEFONE_VENDEDOR", "") or "").strip()
    celular_vendedor = str(dados_pedido.get("CELULAR_VENDEDOR", "") or "").strip()
    contatos_vendedor = "<br/>".join(
        xml_escape(contato)
        for contato in dict.fromkeys((telefone_vendedor, celular_vendedor))
        if contato
    )
    contato_vendedor = (
        f"<b>{xml_escape(str(dados_pedido.get('VENDEDOR', '') or ''))}</b><br/>"
        "Consultor"
        f"{'<br/>' + contatos_vendedor if contatos_vendedor else ''}"
        f"{'<br/>' + xml_escape(str(dados_pedido.get('EMAIL_VENDEDOR', '') or '')) if dados_pedido.get('EMAIL_VENDEDOR') else ''}"
    )
    cliente_assinatura = Table(
        [[paragrafo_formatado(contato_cliente, assinatura_estilo)]],
        colWidths=[194 * mm],
        hAlign="CENTER",
    )
    cliente_assinatura.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    espaco_assinatura_cliente = Table(
        [[""]],
        colWidths=[62 * mm, 70 * mm, 62 * mm],
        rowHeights=[28],
    )
    espaco_assinatura_cliente.setStyle(TableStyle([
        ("LINEBELOW", (1, 0), (1, 0), 0.5, colors.HexColor("#94a3b8")),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    contatos = Table(
        [[
            paragrafo_formatado(contato_superintendente, assinatura_estilo),
            paragrafo_formatado(contato_vendedor, assinatura_estilo),
        ]],
        colWidths=[97 * mm] * 2,
        hAlign="CENTER",
    )
    contatos.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, azul),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    enderecos_unidades = Table(
        [[
            paragrafo_formatado(
                "<b>Unidade I</b><br/>Jaboatão – PE<br/>Br. 101 Sul, Km 82,9<br/>"
                "Prazeres - CEP 54.345-160<br/>(81) 2138-2300<br/>"
                "www.novomundocaminhoes.com.br",
                menor,
            ),
            paragrafo_formatado(
                "<b>Unidade II</b><br/>Maceió – AL<br/>Av. Lourival Melo Mota s/n<br/>"
                "Cidade Universitária - CEP 57.072-000<br/>(82) 3311-3700",
                menor,
            ),
            paragrafo_formatado(
                "<b>Unidade III</b><br/>Arapiraca – AL<br/>Rod AL 220, nº 2458 Km 68<br/>"
                "Senador Arnon Melo - CEP 57.315-745<br/>(82) 3482-5200<br/>"
                "*Imagens dos modelos meramente ilustrativas.",
                menor,
            ),
        ]],
        colWidths=[194 * mm / 3] * 3,
    )
    enderecos_unidades.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([
        Spacer(1, 5),
        faixa_validade,
        Spacer(1, 3),
        paragrafo_formatado("<b>De acordo:</b>", menor),
        espaco_assinatura_cliente,
        cliente_assinatura,
        contatos,
        Spacer(1, 3),
        enderecos_unidades,
    ])
    documento.build([
        KeepInFrame(documento.width, documento.height, story, mode="shrink")
    ])
    return buffer.getvalue()


def nome_arquivo_pedido(dados_pedido):
    vendedor = secure_filename(str(dados_pedido.get("VENDEDOR", "")).strip()) or "Vendedor"
    cliente = secure_filename(str(dados_pedido.get("CLIENTE", "")).strip()) or "Cliente"
    cnpj = secure_filename(str(dados_pedido.get("DOCUMENTO_CLIENTE", "")).strip()) or "Sem_CNPJ"
    modelo = secure_filename(str(dados_pedido.get("MODELO", "")).strip()) or "Modelo"
    total = converter_numero(dados_pedido.get("VALOR_TOTAL")) or 0
    nome = f"{vendedor}_{cliente}_{cnpj}_{modelo}_{total:.2f}_{dados_pedido.get('ID_PEDIDO', '')}"
    nome_seguro = secure_filename(nome)[:175].rstrip("._-")
    return f"{nome_seguro or 'Pedido'}.pdf"


def subir_pdf_pedido_drive(pdf_bytes, nome_arquivo):
    service = conectar_google_drive()
    folder_id = obter_pasta_upload_pedidos(service)
    enviado = service.files().create(
        body={"name": nome_arquivo, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            resumable=True,
        ),
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return enviado


def listar_pedidos_vendedor(planilha, email_vendedor):
    try:
        aba_pedidos = planilha.worksheet(ABA_PEDIDOS_FEITOS)
    except gspread.exceptions.WorksheetNotFound:
        return []

    registros = obter_registros_seguros(aba_pedidos)
    email_norm = str(email_vendedor or "").strip().casefold()
    if not email_norm:
        return []
    pedidos = []
    for registro in registros:
        registro = dict(registro)
        indices_cabecalhos = {
            normalizar_chave_planilha(cabecalho): valor
            for cabecalho, valor in registro.items()
        }
        for cabecalho, aliases in ALIASES_CABECALHOS_PEDIDOS.items():
            if str(registro.get(cabecalho, "")).strip():
                continue
            for nome_cabecalho in (cabecalho, *aliases):
                valor = indices_cabecalhos.get(
                    normalizar_chave_planilha(nome_cabecalho)
                )
                if str(valor or "").strip():
                    registro[cabecalho] = valor
                    break
        if str(registro.get("EMAIL_VENDEDOR", "")).strip().casefold() == email_norm:
            pedidos.append(registro)
    return list(reversed(pedidos))


@app.route("/proposta-pdf/<pedido_id>")
def gerar_pdf_proposta(pedido_id):
    if not session.get("logado") or not session.get("perm_pedidos"):
        abort(404)

    planilha = conectar_google_sheets()
    email_vendedor = str(session.get("email_usuario", "") or "")
    pedido = next(
        (
            item for item in listar_pedidos_vendedor(planilha, email_vendedor)
            if str(item.get("ID_PEDIDO", "")).strip() == pedido_id
        ),
        None,
    )
    if pedido is None:
        abort(404)

    return redirect(url_for(
        "acessar_modulo",
        nome_modulo="pedidos",
        editar=pedido_id,
        imprimir="1",
    ))


def salvar_pedido_na_planilha(planilha, dados_pedido):
    """Salva uma proposta na aba de pedidos feitos sem alterar as listas do formulário."""
    try:
        aba_pedidos = planilha.worksheet(ABA_PEDIDOS_FEITOS)
    except gspread.exceptions.WorksheetNotFound:
        aba_pedidos = planilha.add_worksheet(
            title=ABA_PEDIDOS_FEITOS,
            rows=1000,
            cols=len(CABECALHOS_PEDIDOS),
        )

    linhas = aba_pedidos.get_all_values()
    cabecalhos = linhas[0] if linhas and any(str(c).strip() for c in linhas[0]) else []
    if not cabecalhos:
        if aba_pedidos.col_count < len(CABECALHOS_PEDIDOS):
            aba_pedidos.add_cols(len(CABECALHOS_PEDIDOS) - aba_pedidos.col_count)
        aba_pedidos.update("A1", [CABECALHOS_PEDIDOS], value_input_option="RAW")
        cabecalhos = list(CABECALHOS_PEDIDOS)
    else:
        indices = {
            normalizar_chave_planilha(cabecalho): indice
            for indice, cabecalho in enumerate(cabecalhos)
            if str(cabecalho).strip()
        }
        colunas_necessarias = [
            cabecalho
            for cabecalho in CABECALHOS_PEDIDOS
            if not any(
                normalizar_chave_planilha(nome_alternativo) in indices
                for nome_alternativo in (
                    cabecalho,
                    *ALIASES_CABECALHOS_PEDIDOS.get(cabecalho, ()),
                )
            )
        ]
        colunas_finais = len(cabecalhos) + len(colunas_necessarias)
        if aba_pedidos.col_count < colunas_finais:
            aba_pedidos.add_cols(colunas_finais - aba_pedidos.col_count)
        for cabecalho in colunas_necessarias:
            aba_pedidos.update_cell(1, len(cabecalhos) + 1, cabecalho)
            cabecalhos.append(cabecalho)

    indices = {
        normalizar_chave_planilha(cabecalho): indice
        for indice, cabecalho in enumerate(cabecalhos)
        if str(cabecalho).strip()
    }
    linha = [""] * len(cabecalhos)
    for cabecalho, valor in dados_pedido.items():
        nomes_cabecalho = (
            cabecalho,
            *ALIASES_CABECALHOS_PEDIDOS.get(cabecalho, ()),
        )
        indice = next(
            (
                indices[normalizar_chave_planilha(nome)]
                for nome in nomes_cabecalho
                if normalizar_chave_planilha(nome) in indices
            ),
            None,
        )
        if indice is not None:
            linha[indice] = valor
    aba_pedidos.append_row(linha, value_input_option="RAW")


@app.route("/proposta-pdf-preview", methods=["POST"])
def gerar_pdf_previa_proposta():
    if not session.get("logado") or not session.get("perm_pedidos"):
        abort(404)

    valor_unitario_texto = re.sub(
        r"(?i)^\s*R\$\s*",
        "",
        str(request.form.get("valor_unitario", "") or ""),
    ).replace(" ", "")
    if "," in valor_unitario_texto:
        valor_unitario_texto = (
            valor_unitario_texto.replace(".", "").replace(",", ".")
        )
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", valor_unitario_texto):
        valor_unitario_texto = valor_unitario_texto.replace(".", "")

    quantidade = converter_numero(request.form.get("quantidade"))
    valor_unitario = converter_numero(valor_unitario_texto)
    if (
        not request.form.get("cliente", "").strip()
        or quantidade is None
        or quantidade <= 0
        or not quantidade.is_integer()
        or valor_unitario is None
        or valor_unitario <= 0
    ):
        return "Informe cliente, quantidade inteira e valor unitário válidos.", 400

    nomes_campos = {
        "DATA_PEDIDO": "data_pedido",
        "CLIENTE": "cliente",
        "DOCUMENTO_CLIENTE": "documento_cliente",
        "TELEFONE_CLIENTE": "telefone_cliente",
        "EMAIL_CLIENTE": "email_cliente",
        "CIDADE": "cidade",
        "UF": "uf",
        "MODELO": "modelo",
        "SEGMENTO": "segmento",
        "ANO_MODELO": "ano_modelo",
        "CABINE": "cabine",
        "TECNOLOGIA": "tecnologia",
        "TECNO": "tecnologia_motor",
        "SEGMENTO_FICHA": "segmento_ficha",
        "MOTOR": "motor",
        "POTENCIA": "potencia",
        "TRANSMISSAO": "transmissao",
        "SISTEMA_INJECAO": "sistema_injecao",
        "PBT": "pbt",
        "ENTRE_EIXOS": "entre_eixos",
        "COMBUSTIVEL": "combustivel",
        "PLANO_MANUTENCAO": "plano_manutencao",
        "RIO": "rio",
        "INFORMACOES_COMPLEMENTARES": "informacoes_complementares",
        "GARANTIA": "garantia",
        "ASSISTENCIA": "assistencia",
        "CONDICOES_PM": "condicoes_pm",
        "MODALIDADE_FATURAMENTO": "modalidade_faturamento",
        "FATURANTE": "faturante",
        "CNPJ_FATURANTE": "cnpj_faturante",
        "PAGAMENTO": "pagamento",
        "DGA": "dga",
        "COD_FINAME": "cod_finame",
        "PAC": "pac",
        "CLASSIFICACAO_FISCAL": "classificacao_fiscal",
        "LOCAL_ENTREGA": "local_entrega",
        "PRAZO_ENTREGA": "prazo_entrega",
        "VALIDADE": "validade",
        "DETALHES": "detalhes",
    }
    dados_pedido = {
        chave: str(request.form.get(campo, "") or "").strip()
        for chave, campo in nomes_campos.items()
    }
    dados_pedido.update({
        "ID_PEDIDO": str(
            request.form.get("id_pedido_edicao", "") or "NOVA PROPOSTA"
        ),
        "VENDEDOR": str(session.get("nome", "") or ""),
        "TELEFONE_VENDEDOR": str(session.get("telefone", "") or ""),
        "CELULAR_VENDEDOR": str(session.get("celular", "") or ""),
        "EMAIL_VENDEDOR": str(session.get("email_usuario", "") or ""),
        "TIPO": str(request.form.get("modelo_tipo", "") or ""),
        "CATEGORIA": str(request.form.get("modelo_categoria", "") or ""),
        "QUANTIDADE": int(quantidade),
        "VALOR_UNITARIO": valor_unitario,
        "VALOR_TOTAL": quantidade * valor_unitario,
        "LINK_FICHA_TECNICA": str(
            request.form.get("link_ficha_tecnica", "") or ""
        ),
    })

    try:
        pdf = gerar_pdf_pedido(
            dados_pedido,
            {},
            str(request.form.get("imagem_modelo_id", "") or ""),
        )
    except Exception:
        traceback.print_exc()
        return "Não foi possível gerar o PDF da proposta.", 500

    return send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=nome_arquivo_pedido(dados_pedido),
        max_age=0,
    )


def atualizar_pedido_na_planilha(planilha, dados_pedido, id_pedido):
    """Atualiza a linha de uma proposta existente sem alterar seu ID."""
    aba_pedidos = planilha.worksheet(ABA_PEDIDOS_FEITOS)
    linhas = aba_pedidos.get_all_values()
    if not linhas:
        raise LookupError(f"A aba {ABA_PEDIDOS_FEITOS} não contém propostas.")

    cabecalhos = linhas[0]
    coluna_imagem_modelo = normalizar_chave_planilha("IMAGEM_MODELO_ID")
    if not any(
        normalizar_chave_planilha(cabecalho) == coluna_imagem_modelo
        for cabecalho in cabecalhos
    ):
        if aba_pedidos.col_count < len(cabecalhos) + 1:
            aba_pedidos.add_cols(len(cabecalhos) + 1 - aba_pedidos.col_count)
        aba_pedidos.update_cell(1, len(cabecalhos) + 1, "IMAGEM_MODELO_ID")
        cabecalhos.append("IMAGEM_MODELO_ID")
    indices = {
        normalizar_chave_planilha(cabecalho): indice
        for indice, cabecalho in enumerate(cabecalhos)
        if str(cabecalho).strip()
    }
    indice_id = indices.get(normalizar_chave_planilha("ID_PEDIDO"))
    if indice_id is None:
        raise LookupError("A coluna ID_PEDIDO não foi encontrada na planilha.")

    linhas_correspondentes = [
        indice
        for indice, linha in enumerate(linhas[1:], start=1)
        if len(linha) > indice_id and str(linha[indice_id]).strip() == id_pedido
    ]
    if len(linhas_correspondentes) != 1:
        raise LookupError(
            f"Esperada uma única proposta com o ID {id_pedido}; "
            f"encontradas {len(linhas_correspondentes)}."
        )

    indice_linha = linhas_correspondentes[0]
    linha = list(linhas[indice_linha])
    linha.extend([""] * (len(cabecalhos) - len(linha)))
    for cabecalho, valor in dados_pedido.items():
        if cabecalho == "ID_PEDIDO" and str(valor).strip() != id_pedido:
            raise ValueError("O ID da proposta não pode ser alterado.")
        nomes_cabecalho = (
            cabecalho,
            *ALIASES_CABECALHOS_PEDIDOS.get(cabecalho, ()),
        )
        indice_coluna = next(
            (
                indices[normalizar_chave_planilha(nome)]
                for nome in nomes_cabecalho
                if normalizar_chave_planilha(nome) in indices
            ),
            None,
        )
        if indice_coluna is not None:
            linha[indice_coluna] = valor

    aba_pedidos.update(
        f"A{indice_linha + 1}",
        [linha],
        value_input_option="RAW",
    )


def obter_linhas_abas_em_lote(planilha, nomes_abas, worksheets=None):
    """Lê várias abas com uma única chamada values.batchGet."""
    abas_disponiveis = worksheets if worksheets is not None else planilha.worksheets()
    titulos_disponiveis = {aba.title for aba in abas_disponiveis}
    nomes_existentes = [nome for nome in nomes_abas if nome in titulos_disponiveis]
    resultado = {nome: [] for nome in nomes_abas}
    if not nomes_existentes:
        return resultado

    intervalos = [
        f"'{nome.replace(chr(39), chr(39) * 2)}'!A:ZZ"
        for nome in nomes_existentes
    ]
    resposta = planilha.values_batch_get(
        intervalos,
        params={"valueRenderOption": "FORMATTED_VALUE"},
    )
    faixas = resposta.get("valueRanges", [])
    for nome, faixa in zip(nomes_existentes, faixas):
        resultado[nome] = faixa.get("values", [])
    return resultado


def obter_conteudo_pastas_drive():
    """
    Lê os arquivos disponíveis no Google Drive usando as mesmas credenciais
    do sistema e monta um mapa:
        nome do arquivo -> link de visualização

    Retorna:
        (conteudo_pastas, mapa_drive)

    O segundo item é usado pelos módulos que precisam transformar o nome
    de uma Circular/Ficha Técnica em um link do Google Drive.
    """
    agora = time.time()
    if CACHE_DRIVE["timestamp"] and agora - CACHE_DRIVE["timestamp"] < TEMPO_CACHE_DRIVE_SEGS:
        return CACHE_DRIVE["conteudo"], CACHE_DRIVE["mapa"]

    mapa_drive = {}
    conteudo_pastas = {}

    try:
        if 'GOOGLE_CREDENTIALS' in os.environ:
            credenciais_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
            credenciais = Credentials.from_service_account_info(
                credenciais_dict,
                scopes=escopos
            )
        else:
            credenciais = Credentials.from_service_account_file(
                "credenciais.json",
                scopes=escopos
            )

        service = build('drive', 'v3', credentials=credenciais)

        # Busca todos os arquivos não excluídos aos quais a conta de serviço
        # tem acesso. Paginação evita perder arquivos quando há muitos itens.
        page_token = None

        while True:
            resposta = service.files().list(
                q="trashed = false",
                fields="nextPageToken, files(id, name, mimeType, webViewLink, parents)",
                pageSize=1000,
                pageToken=page_token
            ).execute()

            for arquivo in resposta.get("files", []):
                nome = str(arquivo.get("name", "")).strip()
                if not nome:
                    continue

                nome_chave = nome.lower()
                link = arquivo.get("webViewLink", "")

                # Para arquivos que não retornarem webViewLink, monta um
                # endereço padrão de visualização pelo ID.
                if not link and arquivo.get("id"):
                    link = f"https://drive.google.com/open?id={arquivo['id']}"

                if link:
                    mapa_drive[nome_chave] = link

                    # Também permite encontrar o arquivo sem a extensão.
                    if "." in nome:
                        nome_sem_extensao = nome.rsplit(".", 1)[0].strip().lower()
                        if nome_sem_extensao and nome_sem_extensao not in mapa_drive:
                            mapa_drive[nome_sem_extensao] = link

                pasta = "Raiz"
                if arquivo.get("parents"):
                    pasta = str(arquivo["parents"][0])

                conteudo_pastas.setdefault(pasta, []).append({
                    "id": arquivo.get("id", ""),
                    "nome": nome,
                    "mimeType": arquivo.get("mimeType", ""),
                    "link": link
                })

            page_token = resposta.get("nextPageToken")
            if not page_token:
                break

        CACHE_DRIVE["conteudo"] = conteudo_pastas
        CACHE_DRIVE["mapa"] = mapa_drive
        CACHE_DRIVE["timestamp"] = agora
        return conteudo_pastas, mapa_drive

    except Exception as e:
        print(f"Erro ao obter conteúdo das pastas do Drive: {e}")
        traceback.print_exc()

        # O sistema continua funcionando mesmo se o Drive estiver
        # temporariamente indisponível.
        return {}, {}

def importar_relatorios_drive_vendas():
    """
    Varre a pasta 'Rel_Vendas' no Google Drive ignorando arquivos já rotulados como [IMPORTADO],
    preserva os dados do relatório em Negocios_PM e marca o arquivo como importado
    somente depois que todos os registros válidos forem gravados ou identificados como duplicados.
    """
    try:
        service = conectar_google_drive()
        planilha = conectar_google_sheets()
        query_folder = "name = 'Rel_Vendas' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        folders_res = service.files().list(
            q=query_folder,
            fields="files(id, name)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = folders_res.get('files', [])

        if not folders:
            return "Pasta Rel_Vendas não encontrada no Drive."
        if len(folders) != 1:
            return "Há mais de uma pasta Rel_Vendas no Drive; a importação foi interrompida para evitar importar da pasta errada."

        folder_id = folders[0]['id']

        # Busca apenas arquivos que NÃO contêm '[IMPORTADO]' no nome.
        query_files = f"'{folder_id}' in parents and not name contains '[IMPORTADO]' and trashed = false"
        files = []
        page_token = None
        while True:
            files_res = service.files().list(
                q=query_files,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files.extend(
                arquivo
                for arquivo in files_res.get('files', [])
                if "[IMPORTADO]" not in str(arquivo.get("name", "")).upper()
            )
            page_token = files_res.get('nextPageToken')
            if not page_token:
                break

        arquivos_planilha = [
            arquivo for arquivo in files
            if os.path.splitext(arquivo.get("name", "") or "")[1].lower()
            in {".xls", ".xlsx", ".xlsm"}
            or arquivo.get("mimeType") == "application/vnd.google-apps.spreadsheet"
        ]

        if not arquivos_planilha:
            return "Nenhum arquivo novo para importar."

        # ============================================================
        # ABA NEGOCIOS_PM
        # ============================================================
        try:
            aba_negocios = planilha.worksheet("Negocios_PM")
        except gspread.exceptions.WorksheetNotFound:
            aba_negocios = planilha.add_worksheet(title="Negocios_PM", rows=1000, cols=11)
            aba_negocios.append_row([
                "TEMPERATURA",
                "DATA",
                "VENDEDOR",
                "CLIENTE",
                "MODELO",
                "CHASSIS",
                "PLANO DE MANUTENÇÃO",
                "RIO",
                "CONTATO DO CLIENTE",
                "TELEFONE",
                "COMENTÁRIOS"
            ])

        # Exige um cabeçalho de chassi para nunca importar sem conferir duplicidade.
        registros_existentes = aba_negocios.get_all_values()
        if not registros_existentes:
            return "A aba Negocios_PM está sem cabeçalho; importação interrompida para evitar duplicidade de chassi."

        cabecalhos_negocios = [
            normalizar_chave_planilha(cabecalho)
            for cabecalho in registros_existentes[0]
        ]
        indice_chassi = next(
            (
                indice for indice, cabecalho in enumerate(cabecalhos_negocios)
                if cabecalho in {"chassi", "chassis"}
                or "chassi" in cabecalho
            ),
            None,
        )
        if indice_chassi is None:
            return "A aba Negocios_PM não possui coluna CHASSIS/CHASSI; importação interrompida para evitar duplicidade."

        # Carrega os chassis existentes pela coluna identificada no cabeçalho,
        # sem depender de a coluna continuar na posição F.
        chaves_cadastradas = set()
        for r in registros_existentes[1:]:
            chassis_existente = normalizar_chassi(
                r[indice_chassi] if indice_chassi < len(r) else ""
            )
            if chassis_existente:
                chaves_cadastradas.add(f"CHASSIS:{chassis_existente}")
            else:
                valores_legados = [
                    r[indice] if indice < len(r) else ""
                    for indice in (1, 2, 3, 4)
                ]
                chave_legada = "_".join(
                    normalizar_chave_planilha(valor) for valor in valores_legados
                )
                chaves_cadastradas.add(f"REGISTRO:{chave_legada}")

        # ============================================================
        # MAPA DE VENDEDORES
        # ============================================================
        try:
            aba_usuarios = planilha.worksheet("Usuarios")
            regs_u = obter_registros_seguros(aba_usuarios)
            mapa_usuarios = {}
            for u in regs_u:
                nome_u = str(u.get("NOME", "")).strip()
                if nome_u:
                    mapa_usuarios[nome_u.upper()] = nome_u
                    partes = nome_u.upper().split()
                    if len(partes) > 1:
                        mapa_usuarios[f"{partes[0]} {partes[-1]}"] = nome_u
        except Exception:
            mapa_usuarios = {}

        def identificar_vendedor(nome_bruto):
            if not nome_bruto:
                return ""
            n_limpo = str(nome_bruto).strip().upper()

            if n_limpo in mapa_usuarios:
                return mapa_usuarios[n_limpo]

            for chave, real in mapa_usuarios.items():
                if chave in n_limpo or n_limpo in chave:
                    return real

                partes_chave = chave.split()
                if len(partes_chave) > 1:
                    if all(parte in n_limpo for parte in partes_chave):
                        return real

            return str(nome_bruto).strip()

        # ============================================================
        # MAPA DE MODELOS
        # ============================================================
        try:
            aba_modelos = planilha.worksheet("Modelos")
            regs_m = obter_registros_seguros(aba_modelos)
            mapa_modelos = {}
            for m in regs_m:
                mod_nome = str(m.get("MODELO", "") or m.get("NOME", "") or m.get("DESCRICAO", "")).strip()
                if mod_nome:
                    mapa_modelos[mod_nome.upper()] = mod_nome
        except Exception as erro_modelos:
            print(f"Erro ao carregar modelos para importar relatório: {erro_modelos}")
            mapa_modelos = {}

        def extrair_numeracoes_modelo(valor):
            texto = str(valor or "").upper()
            return {
                f"{match.group(1)}{match.group(2)}"
                for match in re.finditer(r"(?<!\d)(\d{1,2})[.\s]?(\d{3})(?!\d)", texto)
            }

        modelos_por_numeracao = {}
        for chave, nome_modelo in mapa_modelos.items():
            for numeracao in extrair_numeracoes_modelo(nome_modelo):
                modelos_por_numeracao.setdefault(numeracao, nome_modelo)

        def identificar_modelo(modelo_bruto):
            if modelo_bruto is None or not str(modelo_bruto).strip() or str(modelo_bruto).lower() == 'nan':
                return ""

            m_bruto_str = str(modelo_bruto).upper()

            for numeracao in extrair_numeracoes_modelo(m_bruto_str):
                modelo_cadastrado = modelos_por_numeracao.get(numeracao)
                if modelo_cadastrado:
                    return modelo_cadastrado

            m_limpo = m_bruto_str.replace(".", "").replace("-", "").replace(" ", "").replace("/", "")
            for chave, real in mapa_modelos.items():
                chave_limpa = chave.upper().replace(".", "").replace("-", "").replace(" ", "").replace("/", "")
                if m_limpo == chave_limpa or m_limpo in chave_limpa or chave_limpa in m_limpo:
                    return real

            import re
            padrao_num = re.search(r'\d{1,2}\.\d{3}|\d{5}', m_bruto_str)
            if padrao_num:
                num_encontrado = padrao_num.group(0).replace(".", "")
                for chave, real in mapa_modelos.items():
                    c_limpa = chave.upper().replace(".", "").replace("-", "").replace(" ", "")
                    if num_encontrado in c_limpa:
                        return real

            tokens = re.findall(r'\d{4,5}', m_bruto_str)
            for token in tokens:
                for chave, real in mapa_modelos.items():
                    if token in chave.replace(".", ""):
                        return real

            for chave, real in mapa_modelos.items():
                palavras_chave = [p for p in chave.upper().split() if len(p) > 2]
                if palavras_chave and all(p in m_bruto_str for p in palavras_chave):
                    return real

            return ""

        import io
        import pandas as pd

        importados_count = 0
        duplicados_count = 0
        arquivos_concluidos = 0
        erros_importacao = []
        avisos_modelos = []
        modelos_cadastrados = set(mapa_modelos.values())
        if not modelos_cadastrados:
            avisos_modelos.append(
                "A aba Modelos está vazia ou indisponível; os nomes originais foram preservados."
            )

        # ============================================================
        # PROCESSA OS RELATÓRIOS DO DRIVE
        # ============================================================
        for arquivo in arquivos_planilha:
            file_name = arquivo['name']
            file_id = arquivo['id']
            file_mime = arquivo.get("mimeType", "")

            try:
                if file_mime == "application/vnd.google-apps.spreadsheet":
                    conteudo_arquivo = service.files().export_media(
                        fileId=file_id,
                        mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ).execute()
                else:
                    conteudo_arquivo = service.files().get_media(
                        fileId=file_id,
                        supportsAllDrives=True,
                    ).execute()
                fh = io.BytesIO(conteudo_arquivo)
            except Exception as ex_download:
                print(f"Erro ao baixar arquivo {file_name}: {ex_download}")
                erros_importacao.append(f"{file_name}: falha no download")
                continue

            try:
                fh.seek(0)
                engine_excel = "xlrd" if os.path.splitext(file_name)[1].lower() == ".xls" else None
                df_rel = pd.read_excel(fh, engine=engine_excel)
            except Exception as ex_excel:
                print(f"Erro ao ler arquivo excel {file_name}: {ex_excel}")
                erros_importacao.append(f"{file_name}: formato Excel inválido")
                continue

            if df_rel is None or df_rel.empty:
                erros_importacao.append(f"{file_name}: relatório sem registros")
                continue

            colunas_relatorio = {
                normalizar_chave_planilha(coluna): coluna
                for coluna in df_rel.columns
            }

            def obter_valor_relatorio(linha, *nomes_coluna):
                for nome_coluna in nomes_coluna:
                    coluna = colunas_relatorio.get(normalizar_chave_planilha(nome_coluna))
                    if coluna is None:
                        continue
                    valor = linha.get(coluna)
                    if pd.isna(valor) or str(valor).strip().lower() in {"", "nan", "none"}:
                        continue
                    return valor
                return None

            linhas_arquivo = []
            chaves_arquivo = set()
            registros_invalidos = 0
            modelos_nao_cadastrados = []
            duplicados = 0

            for _, row in df_rel.iterrows():
                cliente_bruto = obter_valor_relatorio(row, "CLIENTE")
                modelo_bruto = obter_valor_relatorio(row, "MODELO", "MODELO NA MARCA")
                raw_data = obter_valor_relatorio(row, "DATA", "DATA DA VENDA")
                if cliente_bruto is None and modelo_bruto is None and raw_data is None:
                    continue
                if cliente_bruto is None or modelo_bruto is None or raw_data is None:
                    registros_invalidos += 1
                    continue

                data_convertida = pd.to_datetime(raw_data, errors="coerce", dayfirst=True)
                if pd.isna(data_convertida):
                    registros_invalidos += 1
                    continue
                data_venda = data_convertida.strftime("%d/%m/%Y")

                cliente = str(cliente_bruto).strip()
                vendedor_bruto = obter_valor_relatorio(row, "VENDEDOR")
                vendedor = identificar_vendedor(vendedor_bruto) if vendedor_bruto is not None else ""
                modelo = identificar_modelo(modelo_bruto)
                chassis_bruto = obter_valor_relatorio(row, "CHASSIS", "CHASSI")
                chassis = str(chassis_bruto).strip() if chassis_bruto is not None else ""
                if modelo not in modelos_cadastrados:
                    chassis_conferencia = chassis or "sem chassi"
                    modelos_nao_cadastrados.append(
                        f"{data_venda} | {cliente} | modelo original: {modelo} | chassi: {chassis_conferencia}"
                    )

                if chassis:
                    chave_unica = f"CHASSIS:{normalizar_chassi(chassis)}"
                else:
                    chave_unica = "REGISTRO:" + "_".join(
                        normalizar_chave_planilha(valor)
                        for valor in (data_venda, vendedor, cliente, modelo)
                    )

                if chave_unica in chaves_cadastradas or chave_unica in chaves_arquivo:
                    duplicados += 1
                    continue

                chaves_arquivo.add(chave_unica)
                linhas_arquivo.append([
                    "Frio",
                    data_venda,
                    vendedor,
                    cliente,
                    modelo,
                    chassis,
                ])

            if registros_invalidos:
                erros_importacao.append(
                    f"{file_name}: {registros_invalidos} linha(s) sem cliente, modelo ou data válida"
                )
                continue

            duplicados_count += duplicados
            if modelos_nao_cadastrados:
                avisos_modelos.extend(
                    f"{file_name}: {detalhe}"
                    for detalhe in modelos_nao_cadastrados
                )

            try:
                if linhas_arquivo:
                    aba_negocios.append_rows(
                        linhas_arquivo,
                        insert_data_option="INSERT_ROWS",
                    )
                    chaves_cadastradas.update(chaves_arquivo)
                    invalidar_cache_ab_as("Negocios_PM")
                    importados_count += len(linhas_arquivo)
            except Exception as ex_gravacao:
                print(f"Erro ao gravar negócios do arquivo {file_name}: {ex_gravacao}")
                erros_importacao.append(f"{file_name}: falha ao gravar na aba Negocios_PM")
                continue

            try:
                novo_nome = (
                    file_name
                    if file_name.upper().startswith("[IMPORTADO]")
                    else f"[IMPORTADO] {file_name}"
                )
                service.files().update(
                    fileId=file_id,
                    body={"name": novo_nome},
                    supportsAllDrives=True,
                ).execute()
                arquivos_concluidos += 1
            except Exception as ex_ren:
                print(f"Erro ao renomear arquivo no Drive: {ex_ren}")
                erros_importacao.append(f"{file_name}: registros gravados, mas não foi possível renomear")

        resumo = (
            f"Sincronização concluída! {importados_count} novos registros importados "
            f"em {arquivos_concluidos} arquivo(s); {duplicados_count} chassi(s)/registro(s) duplicado(s) ignorado(s)."
        )
        if erros_importacao:
            resumo += " Pendências: " + "; ".join(erros_importacao)
        if avisos_modelos:
            resumo += " Conferência manual: " + "; ".join(avisos_modelos)
        return resumo

    except Exception as e:
        import traceback
        print(f"Erro ao sincronizar relatórios do Drive: {e}")
        traceback.print_exc()
        return f"Erro ao sincronizar relatórios: {e}"

def registrar_log_acesso(nome_usuario, acao_texto="Login efetuado via Flask"):
    try:
        planilha = conectar_google_sheets()
        try:
            aba_logs = planilha.worksheet("LogsAcessos")
        except gspread.exceptions.WorksheetNotFound:
            aba_logs = planilha.add_worksheet(title="LogsAcessos", rows=1000, cols=4)
            aba_logs.append_row(["NOME", "DATA", "HORA", "AÇÃO"])
        
        agora = datetime.now()
        data_str = agora.strftime("%d/%m/%Y")
        hora_str = agora.strftime("%H:%M:%S")
        
        aba_logs.append_row([str(nome_usuario), data_str, hora_str, str(acao_texto)])
    except Exception as e:
        print(f"Erro ao registrar log de acesso: {e}")

def converter_para_embed(url):
    if not url:
        return ""
    url = str(url).strip()

    if "youtube.com/shorts/" in url:
        video_id = url.split("youtube.com/shorts/")[1].split("?")[0].split("&")[0]
        return f"https://www.youtube.com/embed/{video_id}?autoplay=1"
    elif "youtube.com/watch" in url:
        match = re.search(r"v=([a-zA-Z0-9_-]+)", url)
        if match:
            return f"https://www.youtube.com/embed/{match.group(1)}?autoplay=1"
    elif "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
        return f"https://www.youtube.com/embed/{video_id}?autoplay=1"
    elif "drive.google.com" in url:
        if "/view" in url:
            return url.replace("/view", "/preview")
        if not url.endswith("/preview"):
            return f"{url}/preview" if not url.endswith("/") else f"{url}preview"

    return url


def converter_numero(valor):
    """Converte números em formatos BR/US sem quebrar valores já numéricos."""
    if valor is None:
        return None
    s = str(valor).strip()
    if not s or s.lower() in {"nan", "none", "-"}:
        return None
    try:
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except (TypeError, ValueError):
        return None


FAMILIAS_POR_MODELO = {
    "DELIVERY": {"6170", "9180", "11180", "13180", "14180", "14210", "17210", "18210", "18260", "18320"},
    "CONSTELLATION": {"20480", "25480", "26260", "26320", "27260", "30320", "31320", "33480"},
    "METEOR": {"28480", "29530"},
}


def normalizar_chave_manutencao(valor):
    texto = unicodedata.normalize("NFKD", str(valor).upper())
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return re.sub(r"[^A-Z0-9]", "", texto)


def encontrar_coluna_por_indice(chaves, base, indice):
    base_normalizada = normalizar_chave_manutencao(base)
    for chave in chaves:
        chave_normalizada = normalizar_chave_manutencao(chave)
        if not chave_normalizada.startswith(base_normalizada):
            continue
        sufixo = chave_normalizada[len(base_normalizada):]
        if (indice == 0 and not sufixo) or sufixo == str(indice):
            return chave
    return None


def converter_intervalo_manutencao(valor):
    texto = re.sub(
        r"[^0-9,.-]",
        "",
        str("" if valor is None else valor).strip().lower(),
    )
    if not texto:
        return None
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", texto):
            texto = texto.replace(",", "")
        else:
            texto = texto.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", texto):
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except ValueError:
        return None


def identificar_familia_modelo(modelo, registros_modelos=None):
    modelo_norm = normalizar_chave_manutencao(modelo)
    if "DELIVERY" in modelo_norm or "EXPRESS" in modelo_norm:
        return "DELIVERY"
    if "CONSTELLATION" in modelo_norm:
        return "CONSTELLATION"
    if "METEOR" in modelo_norm:
        return "METEOR"

    for registro_modelo in registros_modelos or []:
        nome_modelo = str(
            registro_modelo.get("MODELO")
            or registro_modelo.get("NOME")
            or registro_modelo.get("DESCRICAO")
            or ""
        ).strip()
        nome_norm = normalizar_chave_manutencao(nome_modelo)
        if not nome_norm or not (nome_norm in modelo_norm or modelo_norm in nome_norm):
            continue
        metadados = normalizar_chave_manutencao(" ".join(
            str(registro_modelo.get(campo, "") or "")
            for campo in ("FAMILIA", "CATEGORIA", "TIPO", "DESCRICAO")
        ))
        for familia in ("DELIVERY", "CONSTELLATION", "METEOR", "EXPRESS"):
            if familia in metadados:
                return "DELIVERY" if familia == "EXPRESS" else familia

    for familia, modelos in FAMILIAS_POR_MODELO.items():
        if any(modelo_id in modelo_norm for modelo_id in modelos):
            return familia
    return ""


def identificar_grupo_manutencao(familia, km):
    if km is None or not familia:
        return "Não identificado"
    if familia == "DELIVERY":
        if km <= 3250:
            return "Severo"
        if km <= 6500:
            return "Misto"
        return "Rodoviário"
    if familia in ("CONSTELLATION", "METEOR"):
        if km <= 6500:
            return "Severo"
        if km <= 10000:
            return "Misto"
        return "Rodoviário"
    return "Não identificado"


INTERVALOS_REVISAO_MODELO = {
    "EXPRESS": {"RODOVIARIO": 30000, "MISTO": 20000, "SEVERO": 20000, "ESPECIAL": 500},
    "6170": {"RODOVIARIO": 30000, "MISTO": 20000, "SEVERO": 20000, "ESPECIAL": 500},
    "9180": {"RODOVIARIO": 40000, "MISTO": 30000, "SEVERO": 20000, "ESPECIAL": 500},
    "11180": {"RODOVIARIO": 40000, "MISTO": 30000, "SEVERO": 20000, "ESPECIAL": 500},
    "13180": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "14180": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "14210": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "17210": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "18210": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "18260": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "18320": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "25480": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "26260": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "26320": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "27260": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "30320": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "31320": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "33480": {"RODOVIARIO": 40000, "MISTO": 30000, "SEVERO": 20000, "ESPECIAL": 600},
    "28480": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "29530": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
}
INTERVALOS_REVISAO_FAMILIA = {
    "DELIVERY": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "CONSTELLATION": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
    "METEOR": {"RODOVIARIO": 50000, "MISTO": 40000, "SEVERO": 20000, "ESPECIAL": 600},
}


def obter_intervalo_revisao(modelo, familia, grupo, intervalo_horas=None):
    modelo_norm = normalizar_chave_manutencao(modelo)
    id_modelo = next(
        (
            modelo_id
            for modelos in FAMILIAS_POR_MODELO.values()
            for modelo_id in modelos
            if modelo_id in modelo_norm
        ),
        "",
    )
    if not id_modelo and "EXPRESS" in modelo_norm:
        id_modelo = "EXPRESS"

    regras_modelo = INTERVALOS_REVISAO_MODELO.get(
        id_modelo,
        INTERVALOS_REVISAO_FAMILIA.get(familia, {}),
    )
    intervalo = regras_modelo.get(normalizar_chave_manutencao(grupo))
    if normalizar_chave_manutencao(grupo) == "ESPECIAL" and intervalo is None:
        intervalo = intervalo_horas
    if intervalo is None:
        return "Não informado"

    unidade = "h" if normalizar_chave_manutencao(grupo) == "ESPECIAL" else "km"
    return f"A cada {intervalo:,.0f} {unidade}".replace(",", ".")


def obter_top3_planos_melhor_preco(registros, registros_modelos=None):
    """Seleciona os 3 modelos distintos de menor preço para cada plano."""

    def converter_intervalo(valor):
        return converter_intervalo_manutencao(valor)

    def encontrar_coluna_mensal(chaves, indice):
        return encontrar_coluna_por_indice(chaves, "VALOR MENSAL", indice)

    def encontrar_coluna_intervalo(chaves, unidade, indice):
        base = "KM" if unidade == "KM" else "HORA"
        return encontrar_coluna_por_indice(chaves, base, indice)

    candidatos = {"PREV": [], "MAX": [], "PLUS": []}
    configuracoes = (("PREV", 0), ("MAX", 1), ("PLUS", 2))

    for registro in registros:
        chaves = list(registro.keys())
        modelo = str(registro.get("MODELO") or registro.get("PRODUTO") or "").strip()
        if not modelo:
            continue

        periodo = str(registro.get("PERIODO", "")).strip() or "12"
        familia = identificar_familia_modelo(modelo, registros_modelos)
        for plano, indice_plano in configuracoes:
            for unidade, indice_valor in (("KM", indice_plano), ("HORA", indice_plano + 3)):
                coluna_valor = encontrar_coluna_mensal(chaves, indice_valor)
                coluna_intervalo = encontrar_coluna_intervalo(chaves, unidade, 0)
                if not coluna_valor:
                    continue

                valor = converter_numero(registro.get(coluna_valor))
                intervalo = converter_intervalo(registro.get(coluna_intervalo)) if coluna_intervalo else None
                if valor is None or valor <= 0 or (unidade == "HORA" and not intervalo):
                    continue

                grupo = "Especial" if unidade == "HORA" else identificar_grupo_manutencao(familia, intervalo)
                texto_intervalo = obter_intervalo_revisao(
                    modelo,
                    familia,
                    grupo,
                    intervalo_horas=intervalo if unidade == "HORA" else None,
                )

                candidatos[plano].append({
                    "tipo": plano,
                    "plano": f"Plano {plano}",
                    "modelo": modelo,
                    "valor": round(valor, 2),
                    "periodo": periodo,
                    "km": intervalo if unidade == "KM" else None,
                    "horas": intervalo if unidade == "HORA" else None,
                    "unidade": unidade,
                    "grupo_manutencao": grupo,
                    "intervalo_revisao": texto_intervalo,
                })

    resultado = []
    for plano in ("PREV", "MAX", "PLUS"):
        ordenados = sorted(
            candidatos[plano],
            key=lambda item: (item["valor"], str(item["modelo"]).upper()),
        )
        melhor_por_modelo = {}
        for item in ordenados:
            chave_modelo = normalizar_chave_manutencao(item["modelo"])
            melhor_por_modelo.setdefault(chave_modelo, item)
        resultado.extend(list(melhor_por_modelo.values())[:3])

    print(
        "Dashboard PM_Precos: "
        f"PREV={len([x for x in resultado if x['tipo']=='PREV'])}, "
        f"MAX={len([x for x in resultado if x['tipo']=='MAX'])}, "
        f"PLUS={len([x for x in resultado if x['tipo']=='PLUS'])}"
    )
    return resultado


def obter_precos_campanha_vw(registros, registros_modelos=None):
    """Monta os preços válidos da campanha VW, separados por plano."""
    resultado = []
    configuracoes = (("PREV", 0), ("MAX", 1))

    for registro in registros:
        chaves = list(registro.keys())
        modelo = str(
            registro.get("MODELO") or registro.get("PRODUTO") or ""
        ).strip()
        if not modelo:
            continue

        periodo = converter_numero(registro.get("PERIODO"))
        coluna_km = encontrar_coluna_por_indice(chaves, "KM", 0)
        km = converter_intervalo_manutencao(registro.get(coluna_km)) if coluna_km else None
        familia = identificar_familia_modelo(modelo, registros_modelos)
        grupo = identificar_grupo_manutencao(familia, km)
        intervalo_revisao = obter_intervalo_revisao(
            modelo,
            familia,
            grupo,
        )

        for plano, indice in configuracoes:
            coluna_valor = encontrar_coluna_por_indice(
                chaves,
                "VALOR MENSAL",
                indice,
            )
            valor = converter_numero(registro.get(coluna_valor)) if coluna_valor else None
            if valor is None or valor <= 0:
                continue

            resultado.append({
                "plano": f"Plano {plano}",
                "modelo": modelo,
                "valor": round(valor, 2),
                "periodo": periodo,
                "km": km,
                "grupo_manutencao": grupo,
                "intervalo_revisao": intervalo_revisao,
            })

    return resultado


def formatar_moeda(valor, manter_todos_decimais=False):
    if valor is None or str(valor).strip() in ["", "-"]:
        return "-"

    v_str = str(valor).strip()
    if "R$" in v_str or any(c.isalpha() for c in v_str):
        return v_str

    try:
        v_limpo = v_str.replace("R$", "").strip()
        if "." in v_limpo and "," in v_limpo:
            v_limpo = v_limpo.replace(".", "").replace(",", ".")
        elif "," in v_limpo:
            v_limpo = v_limpo.replace(",", ".")

        numero = float(v_limpo)

        if manter_todos_decimais:
            partes = v_limpo.split(".")
            casas = len(partes[1]) if len(partes) > 1 else 2
            if casas < 2:
                casas = 2
            formato_str = f"{{:,.{casas}f}}"
            s = formato_str.format(numero)
            s = s.replace(",", "X").replace(".", ",").replace("X", ".")
            return f"R$ {s}"
        else:
            s = f"{numero:,.2f}"
            s = s.replace(",", "X").replace(".", ",").replace("X", ".")
            return f"R$ {s}"
    except ValueError:
        return v_str


TEMPLATE_HTML = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <link rel="manifest" href="{{ url_for('static', filename='manifest.json') }}">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Sistema PM e RIO - Novo Mundo</title>
    
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="PM e RIO">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#002244">

    <!-- Script do Chart.js e Plugin de DataLabels -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>

    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
        
        body { 
            background: #f4f6f9;
            color: #333;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        .login-wrapper {
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 15px;
            background: #f4f6f9;
        }
        .card-login { 
            background: #ffffff; 
            padding: 30px 20px; 
            border-radius: 12px; 
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08); 
            width: 100%; 
            max-width: 380px; 
            text-align: center; 
            border-top: 4px solid #002244;
        }
        .logo-container { margin-bottom: 20px; display: flex; justify-content: center; }
        .logo { max-width: 200px; height: auto; }
        .input-group { text-align: left; margin-bottom: 15px; }
        label { display: block; font-weight: 600; font-size: 11px; color: #4a5568; margin-bottom: 5px; text-transform: uppercase; }
        input, select, textarea { width: 100%; padding: 12px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 16px; background-color: #f7fafc; color: #2d3748; }
        input:focus, select:focus, textarea:focus { border-color: #0066cc; background-color: #fff; outline: none; }
        button.btn-login { background-color: #002244; color: white; border: none; padding: 12px; width: 100%; border-radius: 6px; cursor: pointer; font-size: 15px; font-weight: 600; }
        .error { background-color: #fff5f5; color: #c53030; padding: 12px; border-radius: 6px; font-size: 13px; margin-bottom: 15px; border: 1px solid #feb2b2; line-height: 1.4; font-weight: 500; }
        .sucesso { background-color: #f0fff4; color: #276749; padding: 12px; border-radius: 6px; font-size: 13px; margin-bottom: 15px; border: 1px solid #9ae6b4; line-height: 1.4; font-weight: 500; }

        .topbar {
            height: 56px;
            background-color: #002244;
            color: #ffffff;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 100;
            box-shadow: 0 2px 6px rgba(0,0,0,0.15);
        }
        .topbar-left { display: flex; align-items: center; gap: 16px; }
        .menu-hamburger { background: none; border: none; color: #fff; font-size: 24px; cursor: pointer; padding: 4px; display: flex; align-items: center; }
        .topbar-title { font-size: 17px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
        .topbar-right button { background: none; border: none; color: #fff; font-size: 20px; cursor: pointer; }
        .notificacao-badge {
            display:inline-flex; align-items:center; justify-content:center;
            min-width:17px; height:17px; padding:0 4px; margin-left:-5px;
            border-radius:999px; background:#dc2626; color:#fff; font-size:10px; font-weight:800;
            vertical-align:top;
        }
        .painel-notificacoes {
            position:absolute; right:0; top:42px; width:min(390px, calc(100vw - 24px));
            max-height:430px; overflow:auto; background:#fff; color:#1f2937;
            border:1px solid #dbe3ec; border-radius:10px; box-shadow:0 12px 30px rgba(0,0,0,.18);
            padding:8px; z-index:5000;
        }
        .notificacao-item {
            display:block; padding:10px; border-radius:8px; text-decoration:none; color:#1f2937;
            border-bottom:1px solid #eef2f7;
        }
        .notificacao-item:hover { background:#f8fafc; }
        .notificacao-tipo { font-size:9px; font-weight:800; text-transform:uppercase; color:#2563eb; }
        .notificacao-titulo { font-size:12px; font-weight:800; margin-top:2px; }
        .notificacao-desc { font-size:11px; color:#64748b; margin-top:3px; line-height:1.35; }
        .notificacao-vazia { padding:18px 10px; text-align:center; color:#64748b; font-size:12px; }


        .drawer-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.5); z-index: 998; opacity: 0; visibility: hidden; transition: all 0.3s ease;
        }
        .drawer-overlay.active { opacity: 1; visibility: visible; }

        .drawer {
            position: fixed; top: 0; left: -310px; width: 280px; height: 100%;
            background: #ffffff; z-index: 999; transition: all 0.3s ease; overflow-y: auto;
            display: flex; flex-direction: column; box-shadow: 3px 0 15px rgba(0,0,0,0.15); border-right: 1px solid #e2e8f0;
        }
        .drawer.open { left: 0; }

        @media (min-width: 992px) {
            .menu-hamburger { display: none !important; }
            .drawer-overlay { display: none !important; }
            .drawer { left: 0 !important; box-shadow: none; z-index: 90; }
            .topbar { left: 280px; width: calc(100% - 280px); }
            .main-content { margin-left: 280px !important; max-width: 1200px !important; }
        }

        .drawer-header { background: #002244; color: white; padding: 22px 20px; text-align: center; border-bottom: 3px solid #0066cc; }
        .drawer-header img { max-width: 160px; height: auto; margin-bottom: 4px; }
        .drawer-profile { padding: 16px 20px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; display: flex; align-items: center; gap: 12px; }
        .avatar-box { width: 44px; height: 44px; border-radius: 50%; background: #002244; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: bold; flex-shrink: 0; }
        .user-details h3 { font-size: 14px; color: #002244; font-weight: 700; }
        .user-details p { font-size: 11px; color: #718096; }

        .drawer-menu { list-style: none; padding: 10px 0; margin: 0; flex-grow: 1; }
        .drawer-item a, .drawer-item button { display: flex; align-items: center; gap: 14px; padding: 14px 20px; text-decoration: none; color: #2d3748; font-size: 14px; font-weight: 600; border: none; background: none; width: 100%; text-align: left; cursor: pointer; transition: all 0.2s; }
        .drawer-item a:hover, .drawer-item.active a { background-color: #ebf8ff; color: #0066cc; border-left: 4px solid #0066cc; }
        .drawer-icon { font-size: 18px; width: 22px; text-align: center; color: #002244; }

        .submodulo-nav-container { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.03); }
        .submodulo-nav-label { font-size: 10px; font-weight: 700; color: #718096; text-transform: uppercase; margin-bottom: 6px; }
        .submodulo-nav-scroll { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; -webkit-overflow-scrolling: touch; }
        .submodulo-nav-scroll::-webkit-scrollbar { height: 4px; }
        .submodulo-nav-scroll::-webkit-scrollbar-thumb { background: #cbd5e0; border-radius: 4px; }

        .submodulo-pill { white-space: nowrap; padding: 8px 14px; background: #f7fafc; border: 1px solid #cbd5e0; border-radius: 20px; text-decoration: none; color: #2d3748; font-size: 13px; font-weight: 600; transition: all 0.2s; flex-shrink: 0; }
        .submodulo-pill:hover { background: #edf2f7; color: #002244; }
        .submodulo-pill.active { background: #002244; color: #ffffff; border-color: #002244; box-shadow: 0 2px 4px rgba(0,34,68,0.25); }

        .main-content { margin-top: 56px; padding: 16px; flex-grow: 1; width: 100%; margin-left: auto; margin-right: auto; }
        .submenus-grid { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        .submenu-btn { background: #ffffff; border: 1px solid #cbd5e0; border-left: 4px solid #002244; padding: 14px 16px; border-radius: 8px; text-decoration: none; color: #1a202c; font-weight: 600; font-size: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.02); display: flex; justify-content: space-between; align-items: center; transition: all 0.2s; }
        .submenu-btn:hover { background: #f7fafc; border-color: #0066cc; }
        .submenu-btn::after { content: '›'; font-size: 18px; color: #a0aec0; }

        .produto-detalhe-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; box-shadow: 0 2px 5px rgba(0,0,0,0.03); }
        .detalhe-linha { margin-bottom: 12px; border-bottom: 1px solid #edf2f7; padding-bottom: 10px; }
        .detalhe-label { font-size: 11px; font-weight: 700; color: #4a5568; text-transform: uppercase; margin-bottom: 3px; }
        .detalhe-valor { font-size: 14px; color: #1a202c; }
        .detalhe-produto-nome { font-size: 17px; font-weight: 700; color: #002244; }
        .detalhe-preco { font-size: 17px; font-weight: 700; color: #2f855a; }

        .acoes-produto { display: flex; gap: 8px; margin-top: 12px; }
        .btn-acao { padding: 6px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; text-align: center; text-decoration: none; display: inline-flex; justify-content: center; align-items: center; cursor: pointer; border: none; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        .btn-video { background-color: #002244; color: #ffffff; }
        .btn-video:hover { background-color: #001529; }
        .btn-whatsapp { background-color: #2f855a; color: #ffffff; }
        .btn-whatsapp:hover { background-color: #276749; }
        .btn-email { background-color: #2b6cb0; color: #ffffff; }
        .btn-email:hover { background-color: #2c5282; }
        
        .btn-pdf { background-color: #002244; color: #ffffff; }
        .btn-pdf:hover { background-color: #001529; }
        .btn-editar { background-color: #004080; color: #ffffff; }
        .btn-editar:hover { background-color: #002244; }
        .btn-excluir { background-color: #4a5568; color: #ffffff; }
        .btn-excluir:hover { background-color: #2d3748; }

        /* Dashboard / Gráficos Styles */
        .dashboard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 800px) { .dashboard-grid { grid-template-columns: 1fr; } }
        
        .chart-container { position: relative; height: 320px; width: 100%; background: #fff; padding: 10px; border-radius: 8px; border: 1px solid #e2e8f0; }
        .btn-graficos { background-color: #2b6cb0; color: #ffffff; }
        .btn-graficos:hover { background-color: #1a4971; }

        .btn-toggle-cobertura { background-color: #edf2f7; color: #2d3748; border: 1px solid #cbd5e0; padding: 10px 14px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; width: 100%; text-align: left; display: flex; justify-content: space-between; align-items: center; margin-top: 4px; }
        .btn-toggle-cobertura:hover { background-color: #e2e8f0; }
        .conteudo-cobertura { display: none; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-top: 6px; font-size: 13px; color: #2d3748; white-space: pre-line; }

        .grid-planos { display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }
        .card-plano { background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.02); border-left: 4px solid #002244; }
        .card-plano.max { border-left-color: #d69e2e; }
        .card-plano.plus { border-left-color: #2f855a; }

        .plano-titulo { font-size: 14px; font-weight: 700; color: #1a202c; margin-bottom: 10px; text-transform: uppercase; border-bottom: 1px solid #edf2f7; padding-bottom: 6px; }
        .plano-linha-tripla { display: flex; gap: 8px; margin-bottom: 8px; }
        .plano-linha-com-grupo { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-bottom: 8px; }
        @media (max-width: 600px) { .plano-linha-com-grupo { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        .plano-col { flex: 1; background: #f7fafc; padding: 8px 10px; border-radius: 6px; border: 1px solid #edf2f7; }

        .acoes-ficha-tecnica { display: flex; gap: 8px; margin-top: 8px; }
        .btn-acao-ficha { flex: 1; padding: 10px; border-radius: 6px; font-size: 13px; font-weight: 600; text-align: center; text-decoration: none; display: inline-block; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        .btn-abrir-pdf { background-color: #002244; color: #ffffff; }
        .btn-abrir-pdf:hover { background-color: #001529; }
        .btn-wpp-pdf { background-color: #2f855a; color: #ffffff; }
        .btn-wpp-pdf:hover { background-color: #276749; }

        .modal-video-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.85); z-index: 2000; justify-content: center; align-items: center; padding: 15px; }
        .modal-video-content { background: #000000; width: 100%; max-width: 720px; border-radius: 12px; overflow: hidden; position: relative; box-shadow: 0 10px 30px rgba(0,0,0,0.5); display: flex; flex-direction: column; }
        .modal-video-header { display: flex; justify-content: space-between; align-items: center; background: #002244; color: #ffffff; padding: 12px 16px; font-size: 14px; font-weight: 600; }
        .btn-fechar-modal { background: transparent; border: none; color: #ffffff; font-size: 24px; cursor: pointer; line-height: 1; padding: 0 4px; }
        .iframe-container { position: relative; width: 100%; padding-bottom: 56.25%; height: 0; background: #000; }
        .iframe-container iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: 0; }

        .img-comprovacao {
            width: 50px;
            height: 50px;
            object-fit: contain;
            display: block;
        }

        #secaoRelatorioPDF.relatorio-vendas #tabelaVendas th {
            text-align: center !important;
            vertical-align: middle !important;
        }
        #secaoRelatorioPDF.relatorio-vendas #tabelaVendas tbody tr:nth-child(odd) td {
            padding: 5px 4px !important;
            text-align: center !important;
            vertical-align: middle !important;
            line-height: 1.2;
            font-weight: 600;
        }

        @media print {
            body * { visibility: hidden; }
            #secaoRelatorioPDF, #secaoRelatorioPDF *, #secaoDashboard, #secaoDashboard * { visibility: visible; }
            #secaoRelatorioPDF, #secaoDashboard { position: absolute; left: 0; top: 0; width: 100%; margin: 0; padding: 15px; background: #fff; }
            .no-print { display: none !important; }
            .chart-container { page-break-inside: avoid; margin-bottom: 20px; height: 250px !important; }

            #secaoRelatorioPDF.relatorio-vendas { padding: 9px !important; }
            #secaoRelatorioPDF.relatorio-vendas * {
                font-size: 9px !important;
                line-height: 1.2 !important;
            }
            #secaoRelatorioPDF.relatorio-vendas h3 {
                font-size: 12px !important;
                line-height: 1.25 !important;
            }
            #secaoRelatorioPDF.relatorio-vendas h4 {
                font-size: 10px !important;
                line-height: 1.25 !important;
            }
            #secaoRelatorioPDF.relatorio-vendas p { font-size: 8px !important; }
            #secaoRelatorioPDF.relatorio-vendas #tabelaVendas th,
            #secaoRelatorioPDF.relatorio-vendas #tabelaVendas td {
                padding: 5px 4px !important;
                vertical-align: middle !important;
            }
            
            .img-comprovacao {
                width: 500px !important;
                height: auto !important;
                max-height: 700px !important;
                object-fit: contain !important;
                margin-top: 10px;
                border: 1px solid #999;
            }
        }

        #ai-float-btn {
            position: fixed; bottom: 24px; right: 24px; width: 60px; height: 60px;
            background: #002244; border-radius: 50%; display: flex; align-items: center;
            justify-content: center; box-shadow: 0 4px 15px rgba(0,34,68,0.4); cursor: pointer; z-index: 9999;
            transition: transform 0.3s ease;
        }
        #ai-float-btn:hover { transform: scale(1.08); }
        #ai-float-btn img { width: 35px; height: auto; object-fit: contain; }
        .ai-pulse-ring {
            position: absolute; width: 100%; height: 100%; border-radius: 50%;
            border: 2px solid #0066cc; animation: pulseAI 2s infinite;
        }
        @keyframes pulseAI {
            0% { transform: scale(1); opacity: 1; }
            100% { transform: scale(1.4); opacity: 0; }
        }
        .ai-chat-container {
            position: fixed; bottom: 95px; right: 24px; width: 350px; height: 480px;
            background: #ffffff; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            z-index: 9998; display: none; flex-direction: column; overflow: hidden; border: 1px solid #cbd5e0;
        }
        .ai-chat-header {
            background: #002244; color: white; padding: 10px 16px; font-weight: 600;
            display: flex; justify-content: space-between; align-items: center; font-size: 14px;
        }
        .ai-chat-body {
            flex-grow: 1; padding: 12px; overflow-y: auto; background: #f7fafc;
            display: flex; flex-direction: column; gap: 10px;
        }
        .ai-chat-footer {
            padding: 10px; background: #ffffff; border-top: 1px solid #e2e8f0; display: flex; gap: 8px; align-items: center;
        }
        .ai-chat-footer input {
            flex-grow: 1; padding: 8px 12px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 14px; background: #f7fafc;
        }
        .ai-msg { max-width: 85%; padding: 10px 12px; border-radius: 8px; font-size: 13px; line-height: 1.4; word-break: break-word; }
        .ai-msg.bot { background: #e2e8f0; color: #2d3748; align-self: flex-start; }
        .ai-msg.bot a { color: #0056b3; font-weight: 600; text-decoration: underline; }
        .ai-msg.user { background: #002244; color: #ffffff; align-self: flex-end; }
        .ai-typing span {
            height: 7px; width: 7px; float: left; margin: 0 2px; background-color: #90949c;
            border-radius: 50%; display: inline-block; animation: typing 1s infinite ease-in-out;
        }
        .ai-typing span:nth-of-type(2) { animation-delay: 0.2s; }
        .ai-typing span:nth-of-type(3) { animation-delay: 0.4s; }
        @keyframes typing {
            0% { transform: translateY(0); }
            50% { transform: translateY(-5px); }
            100% { transform: translateY(0); }
        }
    
        /* ============================================================
           Negócios: ações sempre visíveis mesmo quando a tabela é larga.
           A tabela continua horizontalmente rolável, mas a última coluna
           fica presa à direita da área visível.
           ============================================================ */
        .negocios-scroll-top {
            display: block;
            width: 100%;
            height: 18px;
            overflow-x: scroll !important;
            overflow-y: hidden;
            margin: 0 0 6px 0;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            background: #f1f5f9;
            scrollbar-width: auto;
            scrollbar-color: #64748b #e2e8f0;
        }
        .negocios-scroll-top::-webkit-scrollbar {
            height: 14px;
        }
        .negocios-scroll-top::-webkit-scrollbar-track {
            background: #e2e8f0;
            border-radius: 6px;
        }
        .negocios-scroll-top::-webkit-scrollbar-thumb {
            background: #64748b;
            border-radius: 6px;
            border: 2px solid #e2e8f0;
        }
        .negocios-scroll-top-inner {
            height: 1px;
            min-width: 1450px;
        }
        .negocios-tabela-wrap {
            overflow-x: scroll !important;
            overflow-y: visible;
            position: relative;
            width: 100%;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: auto;
            scrollbar-color: #64748b #e2e8f0;
        }
        .negocios-tabela-wrap::-webkit-scrollbar {
            height: 14px;
        }
        .negocios-tabela-wrap::-webkit-scrollbar-track {
            background: #e2e8f0;
        }
        .negocios-tabela-wrap::-webkit-scrollbar-thumb {
            background: #64748b;
            border-radius: 6px;
        }
        .negocios-tabela {
            width: 1450px !important;
            min-width: 1450px !important;
            border-collapse: separate;
            border-spacing: 0;
            font-size: 13px;
            text-align: left;
        }
        .negocios-tabela th,
        .negocios-tabela td {
            padding: 10px;
            border-bottom: 1px solid #edf2f7;
            vertical-align: middle;
        }
        .negocios-tabela th {
            white-space: nowrap;
        }
        .negocios-tabela td {
            max-width: 220px;
            overflow-wrap: anywhere;
        }
        .negocios-tabela .coluna-acoes-header,
        .negocios-tabela .coluna-acoes {
            position: sticky;
            right: 0;
            z-index: 5;
            min-width: 118px;
            width: 118px;
            background: #ffffff;
            box-shadow: -6px 0 10px rgba(15,23,42,.08);
            pointer-events: auto !important;
        }
        .negocios-tabela .coluna-acoes-header {
            background: #002244;
            color: #ffffff;
            text-align: center;
        }
        .negocios-tabela .coluna-acoes > div {
            display: flex !important;
            flex-direction: column;
            gap: 5px !important;
            align-items: stretch !important;
            justify-content: center !important;
        }
        .negocios-tabela .coluna-acoes .btn-acao {
            width: 100%;
            min-width: 94px;
            margin: 0;
            white-space: nowrap;
        }
        @media (max-width: 900px) {
            .negocios-tabela {
                width: 1450px !important;
                min-width: 1450px !important;
            }
            .negocios-tabela .coluna-acoes-header,
            .negocios-tabela .coluna-acoes {
                min-width: 105px;
                width: 105px;
            }
            .negocios-tabela .coluna-acoes .btn-acao {
                min-width: 82px;
                font-size: 11px;
                padding: 6px 5px;
            }
        }

</style>
    <script>
        function toggleDrawer() {
            var drawer = document.getElementById('drawerMenu');
            var overlay = document.getElementById('drawerOverlay');
            drawer.classList.toggle('open');
            overlay.classList.toggle('active');
        }

        function closeDrawer() {
            var drawer = document.getElementById('drawerMenu');
            var overlay = document.getElementById('drawerOverlay');
            drawer.classList.remove('open');
            overlay.classList.remove('active');
        }

        function toggleCobertura() {
            var conteudo = document.getElementById('cobertura-conteudo');
            var seta = document.getElementById('cobertura-seta');
            if (conteudo.style.display === 'block') {
                conteudo.style.display = 'none';
                seta.innerHTML = '▼';
            } else {
                conteudo.style.display = 'block';
                seta.innerHTML = '▲';
            }
        }

        function abrirVideoModal(urlEmbed) {
            var modal = document.getElementById('modalVideo');
            var iframe = document.getElementById('iframeVideo');
            iframe.src = urlEmbed;
            modal.style.display = 'flex';
        }

        function fecharVideoModal() {
            var modal = document.getElementById('modalVideo');
            var iframe = document.getElementById('iframeVideo');
            iframe.src = '';
            modal.style.display = 'none';
        }

        function abrirImagemModal(urlImagem) {
            var modal = document.getElementById('modalImagemAmpliada');
            var imgTag = document.getElementById('imgAmpliadaConteudo');
            if (!modal) {
                var divModal = document.createElement('div');
                divModal.id = 'modalImagemAmpliada';
                divModal.style.cssText = 'display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:3000; justify-content:center; align-items:center; padding:15px; cursor:pointer;';
                divModal.onclick = function() { this.style.display = 'none'; };
                divModal.innerHTML = '<div style="position:relative; max-width:90%; max-height:90%;" onclick="event.stopPropagation()"><button type="button" onclick="document.getElementById(\'modalImagemAmpliada\').style.display=\'none\'" style="position:absolute; top:-15px; right:-15px; background:#002244; color:#fff; border:none; border-radius:50%; width:32px; height:32px; font-size:18px; cursor:pointer; font-weight:bold;">&times;</button><img id="imgAmpliadaConteudo" src="" style="max-width:100%; max-height:85vh; border-radius:6px; box-shadow:0 5px 25px rgba(0,0,0,0.5); object-fit:contain; background:#fff;"></div>';
                document.body.appendChild(divModal);
                modal = document.getElementById('modalImagemAmpliada');
                imgTag = document.getElementById('imgAmpliadaConteudo');
            }
            imgTag.src = urlImagem;
            modal.style.display = 'flex';
        }

        function gerarPDFRelatorio() {
            var imagens = Array.from(document.querySelectorAll('#secaoRelatorioPDF img'));
            Promise.all(imagens.map(function(imagem) {
                if (imagem.complete) return Promise.resolve();
                return new Promise(function(resolve) {
                    imagem.addEventListener('load', resolve, { once: true });
                    imagem.addEventListener('error', resolve, { once: true });
                });
            })).then(function() {
                window.print();
            });
        }

        function alternarVisaoDashboard() {
            var secTabela = document.getElementById('secaoRelatorioPDF');
            var secDash = document.getElementById('secaoDashboard');
            var btnVisao = document.getElementById('btnAlternarVisao');

            if (secTabela.style.display !== 'none') {
                secTabela.style.display = 'none';
                secDash.style.display = 'block';
                btnVisao.innerHTML = '📋 Ver Tabela';
                btnVisao.style.backgroundColor = '#004080';
                if (typeof renderizarGraficos === 'function') renderizarGraficos();
            } else {
                secTabela.style.display = 'block';
                secDash.style.display = 'none';
                btnVisao.innerHTML = '📊 Ver Gráficos';
                btnVisao.style.backgroundColor = '#2b6cb0';
            }
        }

        function carregarParaEdicao(indexLinha, temp, data, vendedor, cliente, modelo, planoManutencao, rioVal, contato, telefone, comentarios) {
            document.getElementById('editIndexInput').value = indexLinha;
            document.getElementById('tituloFormCard').innerText = "✏️ Alterar Negociação (Linha " + indexLinha + ")";
            document.getElementById('btnSubmitForm').innerText = "Atualizar Negociação";
            document.getElementById('btnCancelarEdicao').style.display = "inline-block";

            document.querySelector('[name="temperatura"]').value = temp;
            document.querySelector('[name="data"]').value = data;
            document.querySelector('[name="vendedor"]').value = vendedor;
            document.querySelector('[name="cliente"]').value = cliente;
            document.querySelector('[name="modelo"]').value = modelo;
            document.querySelector('[name="plano_manutencao"]').value = planoManutencao;
            document.querySelector('[name="rio"]').value = rioVal;
            document.querySelector('[name="contato"]').value = contato;
            document.querySelector('[name="comentarios"]').value = comentarios;

            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function cancelarEdicao() {
            document.getElementById('editIndexInput').value = "";
            document.getElementById('tituloFormCard').innerText = "Registrar Nova Negociação";
            document.getElementById('btnSubmitForm').innerText = "Salvar Negociação";
            document.getElementById('btnCancelarEdicao').style.display = "none";
            
            document.querySelector('[name="temperatura"]').selectedIndex = 0;
            document.querySelector('[name="cliente"]').value = "";
            document.querySelector('[name="plano_manutencao"]').selectedIndex = 0;
            document.querySelector('[name="rio"]').selectedIndex = 0;
            document.querySelector('[name="contato"]').value = "";
            document.querySelector('[name="comentarios"]').value = "";
        }

        // Funções exclusivas do módulo Negócios em Andamento.
        // Mantidas no script global para evitar que o JavaScript seja impresso
        // como texto quando o conteúdo do módulo é montado dinamicamente.
        function carregarNegocioParaEdicao(indexLinha, temp, data, vendedor, cliente, modelo, chassis, planoManutencao, rioVal, contato, telefone, comentarios) {
            var edit = document.getElementById('editIndexInput');
            if (!edit) return;
            edit.value = indexLinha;

            var titulo = document.getElementById('tituloBotaoSanfona');
            if (titulo) titulo.innerText = '✏️ Alterar Negociação (Linha ' + indexLinha + ')';

            var btn = document.getElementById('btnSubmitForm');
            if (btn) btn.innerText = 'Atualizar Negociação';
            var cancelar = document.getElementById('btnCancelarEdicao');
            if (cancelar) cancelar.style.display = 'inline-block';

            var setVal = function(name, val) {
                var el = document.querySelector('#containerFormulario [name="' + name + '"]');
                if (el) el.value = val || '';
            };
            setVal('temperatura', temp);
            setVal('data', data);
            setVal('vendedor', vendedor);
            setVal('cliente', cliente);
            setVal('modelo', modelo);
            setVal('chassis', chassis);
            setVal('plano_manutencao', planoManutencao);
            setVal('rio', rioVal);
            setVal('contato', contato);
            setVal('telefone', telefone);
            setVal('comentarios', comentarios);

            var container = document.getElementById('containerFormulario');
            if (container) container.style.display = 'block';
            var icone = document.getElementById('iconeSanfona');
            if (icone) icone.innerHTML = '▼';
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function cancelarNegocioEdicao() {
            var edit = document.getElementById('editIndexInput');
            if (edit) edit.value = '';
            var titulo = document.getElementById('tituloBotaoSanfona');
            if (titulo) titulo.innerText = '➕ Registrar Nova Negociação';
            var btn = document.getElementById('btnSubmitForm');
            if (btn) btn.innerText = 'Salvar Nova Negociação';
            var cancelar = document.getElementById('btnCancelarEdicao');
            if (cancelar) cancelar.style.display = 'none';
            var container = document.getElementById('containerFormulario');
            if (container) container.style.display = 'none';
            var icone = document.getElementById('iconeSanfona');
            if (icone) icone.innerHTML = '▶';
        }

        function toggleFormularioNegocio() {
            var container = document.getElementById('containerFormulario');
            var icone = document.getElementById('iconeSanfona');
            if (!container) return;
            var aberto = container.style.display !== 'none';
            container.style.display = aberto ? 'none' : 'block';
            if (icone) icone.innerHTML = aberto ? '▶' : '▼';
        }

        function aplicarFiltrosNegocios() {
            var busca = document.getElementById('filtroBusca')?.value || '';
            var vend = document.getElementById('filtroVend')?.value || 'todos';
            var ano = document.getElementById('filtroAno')?.value || '';
            var periodo = document.getElementById('filtroPeriodo')?.value || 'anointeiro';
            window.location.href = '/modulo/negocios?busca=' + encodeURIComponent(busca) + '&vend=' + encodeURIComponent(vend) + '&ano=' + encodeURIComponent(ano) + '&periodo=' + encodeURIComponent(periodo);
        }

        function filtrarTempNegocios(temp) {
            var busca = document.getElementById('filtroBusca')?.value || '';
            var vend = document.getElementById('filtroVend')?.value || 'todos';
            var ano = document.getElementById('filtroAno')?.value || '';
            var periodo = document.getElementById('filtroPeriodo')?.value || 'anointeiro';
            window.location.href = '/modulo/negocios?busca=' + encodeURIComponent(busca) + '&vend=' + encodeURIComponent(vend) + '&ano=' + encodeURIComponent(ano) + '&periodo=' + encodeURIComponent(periodo) + '&temp=' + encodeURIComponent(temp);
        }

        function excluirNegocioAndamento(idx) {
            if (!confirm('Deseja realmente excluir este negócio?')) return;
            var form = document.createElement('form');
            form.method = 'POST';
            form.action = '/modulo/negocios';
            var acao = document.createElement('input');
            acao.type = 'hidden'; acao.name = 'acao_form'; acao.value = 'excluir';
            var indice = document.createElement('input');
            indice.type = 'hidden'; indice.name = 'index_linha'; indice.value = idx;
            form.appendChild(acao); form.appendChild(indice);
            document.body.appendChild(form);
            form.submit();
        }

        function carregarVendaParaEdicao(indexLinha, cliente, contrato, plano, rio, dataVenda, modelo, quantidade, vendedor) {
            document.getElementById('editVendaIndexInput').value = indexLinha;
            document.getElementById('tituloFormVendaCard').innerText = "✏️ Alterar Venda / Comprovação (Linha " + indexLinha + ")";
            document.getElementById('btnSubmitVendaForm').innerText = "Atualizar Venda";
            document.getElementById('btnCancelarEdicaoVenda').style.display = "inline-block";

            document.querySelector('[name="cliente"]').value = cliente;
            document.querySelector('[name="numero_contrato"]').value = contrato;
            document.querySelector('[name="plano_manutencao"]').value = plano;
            document.querySelector('[name="rio"]').value = rio;
            document.querySelector('[name="data_venda"]').value = dataVenda;
            document.querySelector('[name="modelo"]').value = modelo;
            document.querySelector('[name="quantidade"]').value = quantidade;
            document.querySelector('[name="vendedor"]').value = vendedor;

            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function cancelarEdicaoVenda() {
            document.getElementById('editVendaIndexInput').value = "";
            document.getElementById('tituloFormVendaCard').innerText = "Registrar Nova Venda / Comprovação";
            document.getElementById('btnSubmitVendaForm').innerText = "Salvar Venda";
            document.getElementById('btnCancelarEdicaoVenda').style.display = "none";

            document.querySelector('[name="cliente"]').value = "";
            document.querySelector('[name="numero_contrato"]').value = "";
            document.querySelector('[name="plano_manutencao"]').value = "";
            document.querySelector('[name="rio"]').value = "";
            document.querySelector('[name="modelo"]').value = "";
            document.querySelector('[name="quantidade"]').value = "";
        }

        function excluirNegocio(indexLinha, moduloDestino) {
            if (confirm("Tem certeza que deseja excluir este registro de negócio?")) {
                var form = document.createElement('form');
                form.method = 'POST';
                form.action = '/modulo/' + moduloDestino;

                var inputAcao = document.createElement('input');
                inputAcao.type = 'hidden';
                inputAcao.name = 'acao_form';
                inputAcao.value = 'excluir';
                form.appendChild(inputAcao);

                var inputIndex = document.createElement('input');
                inputIndex.type = 'hidden';
                inputIndex.name = 'index_linha';
                inputIndex.value = indexLinha;
                form.appendChild(inputIndex);

                document.body.appendChild(form);
                form.submit();
            }
        }

        function excluirVenda(indexLinha, moduloDestino) {
            if (confirm("Tem certeza que deseja excluir este registro de venda?")) {
                var form = document.createElement('form');
                form.method = 'POST';
                form.action = '/modulo/' + moduloDestino;

                var inputAcao = document.createElement('input');
                inputAcao.type = 'hidden';
                inputAcao.name = 'acao_form';
                inputAcao.value = 'excluir';
                form.appendChild(inputAcao);

                var inputIndex = document.createElement('input');
                inputIndex.type = 'hidden';
                inputIndex.name = 'index_linha';
                inputIndex.value = indexLinha;
                form.appendChild(inputIndex);

                document.body.appendChild(form);
                form.submit();
            }
        }

        function ordenarTabela(tableId, colIndex, tipo) {
            var table = document.getElementById(tableId);
            var tbody = table.tBodies[0];
            var rows = Array.from(tbody.querySelectorAll("tr"));
            if (rows.length <= 1) return;

            var currentDir = table.getAttribute("data-sort-dir") === "asc" ? "desc" : "asc";
            table.setAttribute("data-sort-dir", currentDir);

            rows.sort(function(rowA, rowB) {
                var cellA = rowA.children[colIndex].innerText.trim();
                var cellB = rowB.children[colIndex].innerText.trim();

                if (tipo === 'data') {
                    var dateA = parseDate(cellA);
                    var dateB = parseDate(cellB);
                    return currentDir === "asc" ? dateA - dateB : dateB - dateA;
                } else if (tipo === 'num') {
                    var numA = parseFloat(cellA.replace(/[^\d.-]/g, '')) || 0;
                    var numB = parseFloat(cellB.replace(/[^\d.-]/g, '')) || 0;
                    return currentDir === "asc" ? numA - numB : numB - numA;
                } else {
                    return currentDir === "asc" ? cellA.localeCompare(cellB) : cellB.localeCompare(cellA);
                }
            });

            rows.forEach(row => tbody.appendChild(row));
        }

        function parseDate(str) {
            var parts = str.split('/');
            if (parts.length === 3) {
                var year = parts[2].length === 2 ? '20' + parts[2] : parts[2];
                return new Date(year, parts[1] - 1, parts[0]);
            }
            return new Date(0);
        }


        function atualizarBadgeNotificacoes(lista) {
            var badge = document.getElementById('contadorNotificacoes');
            if (!badge) return;
            var idsLidos = JSON.parse(localStorage.getItem('pmrio_notificacoes_lidas') || '[]');
            var naoLidas = lista.filter(function(item) { return idsLidos.indexOf(item.id) === -1; }).length;
            badge.textContent = naoLidas > 99 ? '99+' : naoLidas;
            badge.style.display = naoLidas ? 'inline-flex' : 'none';
        }

        function renderizarNotificacoes(lista) {
            var painel = document.getElementById('painelNotificacoes');
            if (!painel) return;
            if (!lista.length) {
                painel.innerHTML = '<div class="notificacao-vazia">Nenhuma atualização encontrada.</div>';
                return;
            }
            painel.innerHTML = lista.map(function(item) {
                var href = item.link || '#';
                var alvo = (item.link && /^https?:\/\//i.test(item.link)) ? ' target="_blank" rel="noopener noreferrer"' : '';
                return '<a class="notificacao-item" href="' + href.replace(/"/g, '&quot;') + '"' + alvo +
                    ' onclick="marcarNotificacaoLida(\'' + String(item.id).replace(/'/g, "\\'") + '\')">' +
                    '<div class="notificacao-tipo">' + (item.tipo || 'Atualização') + '</div>' +
                    '<div class="notificacao-titulo">' + (item.titulo || 'Atualização disponível') + '</div>' +
                    '<div class="notificacao-desc">' + (item.descricao || '') + '</div>' +
                    '</a>';
            }).join('');
        }

        function marcarNotificacaoLida(id) {
            var ids = JSON.parse(localStorage.getItem('pmrio_notificacoes_lidas') || '[]');
            if (ids.indexOf(id) === -1) ids.push(id);
            localStorage.setItem('pmrio_notificacoes_lidas', JSON.stringify(ids.slice(-100)));
            carregarNotificacoes();
        }

        function carregarNotificacoes() {
            fetch('/api/atualizacoes?limite=12', {cache:'no-store'})
                .then(function(res) {
                    if (!res.ok) {
                        throw new Error('Não foi possível consultar atualizações (' + res.status + ').');
                    }
                    return res.json();
                })
                .then(function(data) {
                    var lista = data.atualizacoes || [];
                    window.__notificacoes = lista;
                    atualizarBadgeNotificacoes(lista);
                    renderizarNotificacoes(lista);
                })
                .catch(function() {
                    var painel = document.getElementById('painelNotificacoes');
                    if (painel) {
                        painel.innerHTML = '<div class="notificacao-vazia">Atualizações temporariamente indisponíveis. Tente novamente em alguns minutos.</div>';
                    }
                });
        }

        function toggleNotificacoes() {
            var painel = document.getElementById('painelNotificacoes');
            if (!painel) return;
            var aberto = painel.style.display === 'block';
            painel.style.display = aberto ? 'none' : 'block';
            if (!aberto) carregarNotificacoes();
        }

        document.addEventListener('click', function(event) {
            var painel = document.getElementById('painelNotificacoes');
            var botao = document.getElementById('btnNotificacoes');
            if (painel && botao && painel.style.display === 'block' &&
                !painel.contains(event.target) && !botao.contains(event.target)) {
                painel.style.display = 'none';
            }
        });

        // Primeira verificação rápida e depois a cada 5 minutos.
        document.addEventListener('DOMContentLoaded', function() {
            carregarNotificacoes();
            window.setInterval(carregarNotificacoes, 300000);

            var barraSuperiorNegocios = document.getElementById('barraScrollNegocios');
            var conteudoBarraNegocios = document.getElementById('barraScrollNegociosInner');
            var areaTabelaNegocios = document.getElementById('negociosTabelaWrapPrincipal');
            var tabelaNegocios = document.getElementById('tabelaNegociosPrincipal');

            if (barraSuperiorNegocios && conteudoBarraNegocios && areaTabelaNegocios && tabelaNegocios) {
                var sincronizandoRolagemNegocios = false;
                var ajustarLarguraBarraNegocios = function() {
                    conteudoBarraNegocios.style.width = tabelaNegocios.scrollWidth + 'px';
                };
                var sincronizarRolagemNegocios = function(origem, destino) {
                    if (sincronizandoRolagemNegocios) return;
                    sincronizandoRolagemNegocios = true;
                    destino.scrollLeft = origem.scrollLeft;
                    sincronizandoRolagemNegocios = false;
                };

                ajustarLarguraBarraNegocios();
                barraSuperiorNegocios.addEventListener('scroll', function() {
                    sincronizarRolagemNegocios(barraSuperiorNegocios, areaTabelaNegocios);
                });
                areaTabelaNegocios.addEventListener('scroll', function() {
                    sincronizarRolagemNegocios(areaTabelaNegocios, barraSuperiorNegocios);
                });
                window.addEventListener('resize', ajustarLarguraBarraNegocios);
            }
        });

        function forcarAtualizacao() {
            var url = window.location.pathname + window.location.search;
            var separador = url.indexOf('?') !== -1 ? '&' : '?';
            window.location.href = url + separador + '_t=' + new Date().getTime();
        }

        function toggleAIChat() {
            var modal = document.getElementById('ai-chat-modal');
            modal.style.display = modal.style.display === 'flex' ? 'none' : 'flex';
        }

        function limparCacheIA() {
            fetch('/api/limpar-cache', { method: 'POST' })
            .then(res => res.json()).then(data => {
                alert(data.mensagem || "Cache limpo com sucesso!");
            }).catch(() => alert("Erro ao limpar cache."));
        }

        function formatarLinksTexto(texto) {
            if (!texto) return "";
            var textoLimpo = texto.replace(/<\/?[^>]+(>|$)/g, "");
            var expUrl = /(\b(https?|ftp|file):\/\/[-a-zA-Z0-9+&@#\/%?=~_|!:,.;]*[-a-zA-Z0-9+&@#\/%=~_|])/ig;
            return textoLimpo.replace(expUrl, function(match) {
                return '<a href="' + match + '" target="_blank" rel="noopener noreferrer">' + match + '</a>';
            });
        }

        function ouvirVoz() {
            if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
                alert("Seu navegador não suporta reconhecimento de voz.");
                return;
            }
            var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            var recognition = new SpeechRecognition();
            recognition.lang = 'pt-BR';
            var btnMic = document.getElementById('btn-mic');
            btnMic.style.background = '#feb2b2';
            recognition.onresult = function(event) {
                var textoFalado = event.results[0][0].transcript;
                document.getElementById('ai-user-input').value = textoFalado;
                btnMic.style.background = '#edf2f7';
                enviarMensagemIA();
            };
            recognition.onerror = function() { btnMic.style.background = '#edf2f7'; };
            recognition.start();
        }

        function enviarMensagemIA(mensagemPersonalizada) {
            var input = document.getElementById('ai-user-input');
            var mensagem = mensagemPersonalizada || input.value.trim();
            if (!mensagem) return;
            var chatBody = document.getElementById('ai-chat-messages');
            
            chatBody.innerHTML += `<div class="ai-msg user">${mensagem}</div>`;
            if (!mensagemPersonalizada) input.value = '';
            chatBody.scrollTop = chatBody.scrollHeight;

            var idDigitando = 'typing-' + Date.now();
            chatBody.innerHTML += `<div id="${idDigitando}" class="ai-msg bot ai-typing"><span></span><span></span><span></span></div>`;
            chatBody.scrollTop = chatBody.scrollHeight;

            fetch('/api/chat-ia', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mensagem: mensagem })
            })
            .then(response => response.json().then(data => {
                var elemTyping = document.getElementById(idDigitando);
                if (elemTyping) elemTyping.remove();

                var respostaBot = data.resposta || "Desculpe, ocorreu um erro.";
                var respostaFormatada = formatarLinksTexto(respostaBot);
                chatBody.innerHTML += `<div class="ai-msg bot">${respostaFormatada}</div>`;
                chatBody.scrollTop = chatBody.scrollHeight;
            }))
            .catch(error => {
                var elemTyping = document.getElementById(idDigitando);
                if (elemTyping) elemTyping.remove();
                chatBody.innerHTML += `<div class="ai-msg bot">Erro de conexão ou resposta inválida do servidor de IA.</div>`;
                chatBody.scrollTop = chatBody.scrollHeight;
            });
        }
    </script>
</head>
<body>

    {% if not session.get('logado') %}
        <div class="login-wrapper">
            <div class="card-login">
                <div class="logo-container">
                    <img src="{{ url_for('static', filename='logo2.png') }}" alt="Logo Novo Mundo" class="logo">
                </div>
                
                {% if modulo_reset %}
                    <h2 style="font-size: 18px; color: #002244; margin-bottom: 15px;">Redefinir Senha</h2>
                    {% if erro %}
                        <div class="error">{{ erro }}</div>
                    {% endif %}
                    <form method="POST">
                        <input type="hidden" name="acao" value="redefinir">
                        <div class="input-group">
                            <label>E-mail Corporativo</label>
                            <input type="email" name="email" value="{{ email_tentativa }}" readonly style="background-color: #edf2f7;">
                        </div>
                        <div class="input-group">
                            <label>CPF (apenas números)</label>
                            <input type="text" name="cpf" placeholder="Digite seu CPF (11 dígitos)" maxlength="14" required autofocus>
                        </div>
                        <div class="input-group">
                            <label>Nova Senha</label>
                            <input type="password" name="nova_senha" placeholder="Nova Senha" required>
                        </div>
                        <button type="submit" class="btn-login">Salvar Nova Senha</button>
                    </form>
                {% else %}
                    <h2 style="font-size: 18px; color: #002244; margin-bottom: 15px;">Acesso Corporativo</h2>
                    {% if erro %}
                        <div class="error">{{ erro }}</div>
                    {% endif %}
                    {% if sucesso %}
                        <div class="sucesso">{{ sucesso }}</div>
                    {% endif %}
                    <form method="POST">
                        <input type="hidden" name="acao" value="login">
                        <div class="input-group">
                            <label>E-mail Corporativo</label>
                            <input type="email" name="email" value="{{ email_tentativa }}" placeholder="seu.email@novomundo.com" required autocapitalize="none">
                        </div>
                        <div class="input-group">
                            <label>Senha</label>
                            <input type="password" name="senha" placeholder="••••••••" required>
                        </div>
                        <button type="submit" class="btn-login">Entrar no Sistema</button>
                    </form>
                {% endif %}
            </div>
        </div>
    {% else %}
        <header class="topbar">
            <div class="topbar-left">
                <button class="menu-hamburger" onclick="toggleDrawer()">☰</button>
                <div class="topbar-title">{{ modulo_titulo }}</div>
            </div>
            <div class="topbar-right" style="display:flex;align-items:center;gap:8px;position:relative;">
                <button type="button" id="btnNotificacoes" onclick="toggleNotificacoes()" title="Atualizações" aria-label="Atualizações">
                    🔔 <span id="contadorNotificacoes" class="notificacao-badge" style="display:none;">0</span>
                </button>
                <button type="button" onclick="forcarAtualizacao()" title="Atualizar">↻</button>
                <div id="painelNotificacoes" class="painel-notificacoes" style="display:none;"></div>
            </div>
        </header>

        <div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>

        <aside class="drawer" id="drawerMenu">
            <div class="drawer-header">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Novo Mundo">
            </div>

            <div class="drawer-profile">
                <div class="avatar-box">👤</div>
                <div class="user-details">
                    <h3>{{ session.get('nome', 'Usuário') }}</h3>
                    <p>{{ session.get('perfil', 'Colaborador') }}</p>
                </div>
            </div>

            <ul class="drawer-menu">
                {% if session.get('perm_rio') %}
                <li class="drawer-item {% if modulo_ativo == 'rio' %}active{% endif %}">
                    <a href="/modulo/rio" onclick="closeDrawer()"><span class="drawer-icon">📡</span> Telemetria RIO</a>
                </li>
                {% endif %}
                {% if session.get('perm_pm') %}
                <li class="drawer-item {% if modulo_ativo == 'pm' %}active{% endif %}">
                    <a href="/modulo/pm" onclick="closeDrawer()"><span class="drawer-icon">🛠</span> Plano de Manutenção</a>
                </li>
                {% endif %}
                {% if session.get('perm_valores') %}
                <li class="drawer-item {% if modulo_ativo == 'valores' %}active{% endif %}">
                    <a href="/modulo/valores" onclick="closeDrawer()"><span class="drawer-icon">💲</span> Tabela de Valores</a>
                </li>
                {% endif %}
                {% if session.get('perm_informes') %}
                <li class="drawer-item {% if modulo_ativo == 'informes' %}active{% endif %}">
                    <a href="/modulo/informes" onclick="closeDrawer()"><span class="drawer-icon">📢</span> Informes e Circulares</a>
                </li>
                {% endif %}
                {% if session.get('perm_fichatecnica') %}
                <li class="drawer-item {% if modulo_ativo == 'fichatecnica' %}active{% endif %}">
                    <a href="/modulo/fichatecnica" onclick="closeDrawer()"><span class="drawer-icon">📋</span> Ficha Técnica</a>
                </li>
                {% endif %}
                {% if session.get('perm_pedidos') %}
                <li class="drawer-item {% if modulo_ativo == 'pedidos' %}active{% endif %}">
                    <a href="/modulo/pedidos" onclick="closeDrawer()"><span class="drawer-icon">🧾</span> Propostas a Clientes</a>
                </li>
                {% endif %}
                {% if session.get('perm_argumentos') %}
                <li class="drawer-item {% if modulo_ativo == 'argumentos' %}active{% endif %}" style="border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-bottom: 4px;">
                    <a href="/modulo/argumentos" onclick="closeDrawer()"><span class="drawer-icon">💡</span> Argumentos de Venda</a>
                </li>
                {% endif %}
                {% if session.get('perm_negocios') %}
                <li class="drawer-item {% if modulo_ativo == 'negocios' %}active{% endif %}">
                    <a href="/modulo/negocios?sincronizar=1" onclick="closeDrawer()"><span class="drawer-icon">🤝</span> Negócios em Andamento</a>
                </li>
                {% endif %}
                {% if session.get('perm_visitas') %}
                <li class="drawer-item {% if modulo_ativo == 'visitas' %}active{% endif %}">
                    <a href="/modulo/visitas" onclick="closeDrawer()"><span class="drawer-icon">📍</span> Visitas e Acompanhamento</a>
                </li>
                {% endif %}
                {% if session.get('perm_vendas') %}
                <li class="drawer-item {% if modulo_ativo == 'vendas' %}active{% endif %}">
                    <a href="/modulo/vendas" onclick="closeDrawer()"><span class="drawer-icon">💰</span> Vendas Fechadas</a>
                </li>
                {% endif %}
                
                {% if session.get('perm_dashboard') %}
                <li class="drawer-item {% if modulo_ativo == 'dashboard' %}active{% endif %}">
                    <a href="/modulo/dashboard" onclick="closeDrawer()"><span class="drawer-icon">📊</span> Dashboard</a>
                </li>
                {% endif %}

                {% if session.get('perm_camp_vw_prev') %}
                <li class="drawer-item {% if modulo_ativo == 'camp_vw_prev' %}active{% endif %}">
                    <a href="/modulo/camp_vw_prev" onclick="closeDrawer()"><span class="drawer-icon">🚛</span> Campanha VW PREV</a>
                </li>
                {% endif %}

                {% if session.get('perm_traton') %}
                <li class="drawer-item {% if modulo_ativo == 'traton' %}active{% endif %}">
                    <a href="/modulo/traton" onclick="closeDrawer()" style="display: flex; justify-content: center; align-items: center; padding: 12px 10px;">
                        <img src="{{ url_for('static', filename='traton.jpg') }}?v=2" alt="Traton" style="max-height: 24px; width: auto; object-fit: contain;">
                    </a>
                </li>
                {% endif %}

                <li class="drawer-item" style="margin-top: 10px; border-top: 1px solid #edf2f7;">
                    <button type="button" onclick="limparCacheIA()" style="color: #2b6cb0;"><span class="drawer-icon">🔄</span> Atualizar Base IA</button>
                </li>
                <li class="drawer-item" style="border-top: 1px solid #edf2f7;">
                    <form action="/logout" method="POST" style="margin: 0; width: 100%;">
                        <button type="submit"><span class="drawer-icon">🚪</span> Sair da Conta</button>
                    </form>
                </li>
            </ul>
        </aside>
      
        
        <main class="main-content">
            {% if conteudo_modulo %}
                {{ conteudo_modulo | safe }}
            {% else %}
                <div style="display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 60vh; text-align: center; padding: 20px;">
                    <img src="{{ url_for('static', filename='logo.png') }}" alt="Novo Mundo" style="max-width: 200px; width: 100%; height: auto; margin-bottom: 15px;">
                    <p style="font-size: 15px; font-weight: 500; color: #4a5568; margin: 0;">Selecione um dos módulos no menu para começar.</p>
                </div>
            {% endif %}
        </main>

        <div id="modalVideo" class="modal-video-overlay" onclick="fecharVideoModal()">
            <div class="modal-video-content" onclick="event.stopPropagation()">
                <div class="modal-video-header">
                    <span>Vídeo Explicativo</span>
                    <button type="button" class="btn-fechar-modal" onclick="fecharVideoModal()">&times;</button>
                </div>
                <div class="iframe-container">
                    <iframe id="iframeVideo" src="" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
                </div>
            </div>
        </div>

        <div id="ai-float-btn" onclick="toggleAIChat()" title="Assistente Novo Mundo">
            <img src="{{ url_for('static', filename='logo.png') }}" alt="IA">
            <span class="ai-pulse-ring"></span>
        </div>

        <div id="ai-chat-modal" class="ai-chat-container">
            <div class="ai-chat-header">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-size: 18px;">🤖</span>
                    <span>Assistente Novo Mundo</span>
                </div>
                <button type="button" onclick="toggleAIChat()" style="background:none; border:none; color:white; font-size:20px; cursor:pointer;">&times;</button>
            </div>
            
            <div id="ai-chat-messages" class="ai-chat-body">
                <div class="ai-msg bot">
                    Olá! Sou o Assistente Novo Mundo Caminhões e Ônibus. Como posso ajudar você hoje?
                </div>
            </div>

            <div class="ai-chat-footer">
                <button type="button" id="btn-mic" onclick="ouvirVoz()" title="Falar por Voz" style="background:#edf2f7; border:none; border-radius:50%; width:38px; height:38px; cursor:pointer; font-size:16px;">🎙️</button>
                <input type="text" id="ai-user-input" placeholder="Digite sua dúvida..." onkeypress="if(event.key === 'Enter') enviarMensagemIA()">
                <button type="button" onclick="enviarMensagemIA()" style="background:#002244; color:white; border:none; border-radius:6px; padding:0 14px; cursor:pointer; font-weight:650;">Enviar</button>
            </div>
        </div>
    {% endif %}

</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def login():
    erro = None
    sucesso = None
    modulo_reset = False
    email_tentativa = session.get("email_bloqueado", "")

    if session.get("tentativas_erro", 0) >= 3:
        modulo_reset = True

    if request.method == "POST":
        acao = request.form.get("acao", "login")

        if acao == "login":
            input_email = request.form.get("email", "").strip().lower()
            input_senha = request.form.get("senha")
            session["email_bloqueado"] = input_email
            email_tentativa = input_email

            try:
                planilha = conectar_google_sheets()
                aba_usuarios = planilha.worksheet("Usuarios")
                usuarios = obter_registros_seguros(aba_usuarios)

                usuario_encontrado = None

                for u in usuarios:
                    if str(u.get("EMAIL", "")).strip().lower() == input_email:
                        usuario_encontrado = u
                        break

                if usuario_encontrado and str(usuario_encontrado.get("SENHA", "")) == input_senha:
                    session.pop("tentativas_erro", None)
                    session.pop("email_bloqueado", None)

                    session["logado"] = True
                    session["nome"] = usuario_encontrado.get("NOME")
                    session["perfil"] = usuario_encontrado.get("PERFIL")
                    session["email_usuario"] = usuario_encontrado.get("EMAIL")
                    session["telefone"] = usuario_encontrado.get("TELEFONE", "")
                    session["celular"] = usuario_encontrado.get("CELULAR", "")
                    
                    def normalize_key(k):
                        return unicodedata.normalize('NFKD', str(k)).encode('ASCII', 'ignore').decode('utf-8').strip().upper()

                    user_norm_keys = {normalize_key(k): str(v).strip().upper() for k, v in usuario_encontrado.items()}

                    def tem_permissao(chaves_alvo):
                        for chave in chaves_alvo:
                            chaves_possiveis = [chave] + [f"{chave}_{i}" for i in range(1, 10)]
                            for c in chaves_possiveis:
                                val = user_norm_keys.get(c, "")
                                if val in ["X", "SIM", "S", "V", "TRUE", "1", "OK"]:
                                    return True
                        return False

                    session["perm_rio"] = tem_permissao(["TELEMETRIA RIO", "RIO"])
                    session["perm_pm"] = tem_permissao(["PLANO DE MANUTENCAO", "PLANO DE MANUTENÇAO", "PM"])
                    session["perm_valores"] = tem_permissao(["TABELAS DE VALORES", "TABELA DE VALORES", "VALORES"])
                    session["perm_informes"] = tem_permissao(["INFORME E CIRCULARES", "INFORME E CIRCULAR", "INFORMES"])
                    session["perm_fichatecnica"] = tem_permissao(["FICHA TECNICA", "FICHATECNICA"])
                    session["perm_argumentos"] = tem_permissao(["ARGUMENTOS DE VENDA", "ARGUMENTOS"])
                    session["perm_visitas"] = tem_permissao(["VISITAS"])
                    session["perm_pedidos"] = tem_permissao([
                        "PEDIDOS_FEITOS",
                        "PEDIDOS FEITOS",
                        "FORMULARIO_PEDIDO",
                        "FORMULARIO PEDIDO",
                    ])
                    session["perm_camp_vw_prev"] = tem_permissao(["CAMPANHAS", "CAMPANHA", "CAMPANHA VW PREV", "CAMP", "PREV"])
                    session["perm_dashboard"] = tem_permissao(["DASHBORD", "DASHBOARD", "DASH"])
                    session["perm_traton"] = tem_permissao(["TRATON", "SIMULADOR TRATON"])
                    session["perm_negocios"] = tem_permissao(["NEGOCIOS EM ANDAMENTO", "NEGOCIOS"])
                    
                    val_vendas = False
                    for k_norm, v_val in user_norm_keys.items():
                        if "VENDAS" in k_norm and "LOC" not in k_norm and "CON" not in k_norm:
                            if v_val in ["X", "SIM", "S", "V", "TRUE", "1", "OK"]:
                                val_vendas = True
                                break
                    session["perm_vendas"] = val_vendas or tem_permissao(["VENDAS"])

                    session["perm_traton"] = tem_permissao(["TRATON", "SIMULADOR", "SIMULADOR TRATON"])
                    session.pop("historico_ia", None)
                    
                    registrar_log_acesso(usuario_encontrado.get("NOME"), "Login efetuado via Flask")

                    modulos_permitidos_pos_login = [
                        (modulo, session.get(permissao, False))
                        for modulo, permissao in (
                            ("dashboard", "perm_dashboard"),
                            ("rio", "perm_rio"),
                            ("pm", "perm_pm"),
                            ("valores", "perm_valores"),
                            ("informes", "perm_informes"),
                            ("fichatecnica", "perm_fichatecnica"),
                            ("argumentos", "perm_argumentos"),
                            ("negocios", "perm_negocios"),
                            ("visitas", "perm_visitas"),
                            ("pedidos", "perm_pedidos"),
                            ("vendas", "perm_vendas"),
                            ("camp_vw_prev", "perm_camp_vw_prev"),
                            ("traton", "perm_traton"),
                        )
                        if session.get(permissao, False)
                    ]
                    if modulos_permitidos_pos_login:
                        return redirect(url_for(
                            "acessar_modulo",
                            nome_modulo=modulos_permitidos_pos_login[0][0],
                        ))

                    session.clear()
                    erro = "Login válido, mas nenhum módulo está autorizado para este usuário. Solicite acesso ao administrador."
                else:
                    tentativas = session.get("tentativas_erro", 0) + 1
                    session["tentativas_erro"] = tentativas

                    if tentativas >= 3:
                        modulo_reset = True
                        erro = "Você excedeu 3 tentativas incorretas. Confirme seu CPF abaixo para cadastrar uma nova senha."
                    else:
                        restantes = 3 - tentativas
                        erro = f"E-mail ou Senha incorretos. Você tem mais {restantes} tentativa(s) antes do bloqueio."
            except Exception as e:
                erro = f"Erro ao conectar com a planilha: {e}"

        elif acao == "redefinir":
            input_email = session.get("email_bloqueado", "").strip().lower()
            input_cpf_raw = re.sub(r'\D', '', request.form.get("cpf", "").strip())
            
            input_cpf = input_cpf_raw.zfill(11) if len(input_cpf_raw) < 11 else input_cpf_raw
            nova_senha = request.form.get("nova_senha", "").strip()

            if not validar_cpf(input_cpf):
                modulo_reset = True
                erro = "O CPF digitado é inválido. Digite os 11 números corretamente."
            else:
                try:
                    planilha = conectar_google_sheets()
                    aba_usuarios = planilha.worksheet("Usuarios")
                    
                    linhas = aba_usuarios.get_all_values()

                    if not linhas:
                        modulo_reset = True
                        erro = "Aba de usuários está vazia."
                    else:
                        cabecalhos = [h.upper().strip() for h in linhas[0]]
                        idx_email = cabecalhos.index("EMAIL") if "EMAIL" in cabecalhos else None
                        idx_cpf = cabecalhos.index("CPF") if "CPF" in cabecalhos else None
                        idx_senha = cabecalhos.index("SENHA") if "SENHA" in cabecalhos else None

                        if idx_cpf is None or idx_senha is None or idx_email is None:
                            modulo_reset = True
                            erro = "Erro de configuração: Colunas EMAIL, CPF ou SENHA não encontradas na planilha."
                        else:
                            linha_encontrada = None

                            for idx_linha, linha in enumerate(linhas[1:], start=2):
                                email_planilha = str(linha[idx_email]).strip().lower() if len(linha) > idx_email else ""
                                cpf_planilha_raw = re.sub(r'\D', '', str(linha[idx_cpf]).strip()) if len(linha) > idx_cpf else ""
                                cpf_planilha = cpf_planilha_raw.zfill(11) if len(cpf_planilha_raw) < 11 and cpf_planilha_raw else cpf_planilha_raw

                                if email_planilha == input_email and (cpf_planilha == input_cpf or cpf_planilha_raw == input_cpf_raw):
                                    linha_encontrada = idx_linha
                                    break

                            if linha_encontrada:
                                try:
                                    aba_usuarios.update_cell(linha_encontrada, idx_senha + 1, str(nova_senha))
                                except Exception:
                                    aba_usuarios.update(f"{chr(65 + idx_senha)}{linha_encontrada}", [[str(nova_senha)]])
                                
                                session.pop("tentativas_erro", None)
                                session.pop("email_bloqueado", None)
                                sucesso = "Senha redefinida com sucesso! Faça login com a sua nova senha."
                                modulo_reset = False
                            else:
                                modulo_reset = True
                                erro = "CPF não confere com o e-mail informado. Verifique os dados digitados."
                except Exception as e:
                    modulo_reset = True
                    erro = f"Erro ao atualizar a senha no Google Sheets: {e}"

    return render_template_string(
        TEMPLATE_HTML,
        erro=erro,
        sucesso=sucesso,
        modulo_reset=modulo_reset,
        email_tentativa=email_tentativa
    )

@app.route("/modulo/<nome_modulo>", methods=["GET", "POST"])
def acessar_modulo(nome_modulo):
    if not session.get("logado"):
        return redirect(url_for("login"))

    permissoes_map = {
        "rio": session.get("perm_rio", False),
        "pm": session.get("perm_pm", False),
        "valores": session.get("perm_valores", False),
        "informes": session.get("perm_informes", False),
        "fichatecnica": session.get("perm_fichatecnica", False),
        "argumentos": session.get("perm_argumentos", False),
        "negocios": session.get("perm_negocios", False),
        "visitas": session.get("perm_visitas", False),
        "pedidos": session.get("perm_pedidos", False),
        "vendas": session.get("perm_vendas", False),
        "dashboard": session.get("perm_dashboard", False),
        "camp_vw_prev": session.get("perm_camp_vw_prev", False),
        "traton": session.get("perm_traton", False)
    }

    if not permissoes_map.get(nome_modulo, False):
        return render_template_string(
            TEMPLATE_HTML,
            conteudo_modulo='<div style="padding: 20px; color: #c53030; background: #fff5f5; border-radius: 8px; border: 1px solid #feb2b2;"><h3>Acesso Negado</h3><p>Você não possui permissão para acessar este módulo.</p></div>',
            modulo_ativo="",
            modulo_titulo="Acesso Restrito"
        )

    conteudo = ""
    modulo_titulo = NOMES_MODULOS.get(nome_modulo, "Início")
    mapa_drive = {}
    if nome_modulo in {"informes", "fichatecnica"}:
        _, mapa_drive = obter_conteudo_pastas_drive()
    nome_usuario_logado = session.get('nome', 'Usuário')

    # ============================================================
    # CAMPANHA VW PREV
    # Usa os mesmos negócios ativos filtrados pelo vendedor logado
    # no módulo "Visitas e Acompanhamento" e lê as regras da aba
    # "Regras_Camp_VW" diretamente do Google Sheets.
    # ============================================================
    if nome_modulo == "camp_vw_prev":
        global CACHE_CAMPANHAS
        if 'CACHE_CAMPANHAS' not in globals():
            CACHE_CAMPANHAS = {}

        try:
            planilha = conectar_google_sheets()

            # Garante que a aba Campanhas_VW existe
            abas_planilha = planilha.worksheets()
            aba_campanhas_vw = next(
                (aba for aba in abas_planilha if aba.title == "Campanhas_VW"),
                None,
            )
            if aba_campanhas_vw is None:
                aba_campanhas_vw = planilha.add_worksheet(title="Campanhas_VW", rows=1000, cols=10)
                aba_campanhas_vw.append_row(["DATA", "CIRCULAR", "CONSULTOR", "EMPRESA", "CHASSIS", "MODELOS", "G MANUTENÇÃO", "PLANO DE MANUTENÇÃO", "RIO", "STATUS"])
                abas_planilha.append(aba_campanhas_vw)

            sucesso_msg = None
            erro_msg = None

            perfil_usuario = str(session.get("perfil", "")).strip().upper()
            usuario_logado = str(session.get("nome", "")).strip().upper()
            is_adm = perfil_usuario in ["ADM", "DIRETOR", "GERENTE"]

            def chave_campanha(valor):
                valor = unicodedata.normalize("NFKD", str(valor or ""))
                valor = "".join(c for c in valor if not unicodedata.combining(c))
                return re.sub(r"[^A-Z0-9]+", "", valor.upper())

            def chave_registro_campanha(registro):
                chassis_registro = next(
                    (
                        valor for cabecalho, valor in registro.items()
                        if chave_campanha(cabecalho) in {"CHASSI", "CHASSIS"}
                        and str(valor or "").strip()
                    ),
                    "",
                )
                chassis_normalizado = chave_campanha(chassis_registro)
                if chassis_normalizado:
                    return f"CH:{chassis_normalizado}"

                chave_sem_chassis = "|".join(
                    chave_campanha(registro.get(campo, ""))
                    for campo in ("DATA", "CONSULTOR", "EMPRESA", "MODELOS", "CIRCULAR")
                )
                return f"REG:{chave_sem_chassis}" if chave_sem_chassis.strip("|") else ""

            # --------------------------------------------------------
            # Tratamento de POST (Salvamento do Grupo)
            # --------------------------------------------------------
            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if acao_form == "salvar_grupo":
                    chassis_alvo = request.form.get("chassis", "").strip()
                    grupo_manutencao = request.form.get("grupo_manutencao", "").strip()
                    cliente_alvo = request.form.get("cliente", "").strip()
                    modelo_alvo = request.form.get("modelo", "").strip()
                    circular_alvo = request.form.get("circular", "").strip()
                    plano_manutencao_salvar = request.form.get("plano_manutencao", "").strip()
                    rio_salvar = request.form.get("rio", "").strip()
                    vendedor_reg = request.form.get("vendedor_reg", "").strip() or session.get("nome", "Usuário")

                    if grupo_manutencao in ["Rodoviário", "Misto", "Severo"]:
                        registros_camp_existentes = obter_registros_seguros(aba_campanhas_vw)
                        encontrado_idx = None
                        registro_encontrado = None
                        status_existente = "Aguardando Consultor"
                        vendedor_norm = chave_campanha(vendedor_reg)
                        cliente_norm = chave_campanha(cliente_alvo)

                        for idx_c, rc in enumerate(registros_camp_existentes, start=2):
                            ch_plan = chave_campanha(rc.get("CHASSIS", ""))
                            cli_plan = chave_campanha(rc.get("EMPRESA", ""))
                            vendedor_plan = chave_campanha(rc.get("CONSULTOR", ""))
                            chassis_norm = chave_campanha(chassis_alvo)
                            if chassis_norm and ch_plan == chassis_norm:
                                encontrado_idx = idx_c
                                registro_encontrado = rc
                                status_existente = str(rc.get("STATUS", "Aguardando Consultor")).strip() or "Aguardando Consultor"
                                break
                            elif not chassis_norm and cli_plan == cliente_norm and vendedor_plan == vendedor_norm:
                                encontrado_idx = idx_c
                                registro_encontrado = rc
                                status_existente = str(rc.get("STATUS", "Aguardando Consultor")).strip() or "Aguardando Consultor"
                                break

                        data_atual = (
                            str(registro_encontrado.get("DATA", "")).strip()
                            if registro_encontrado else ""
                        ) or datetime.now().strftime("%d/%m/%Y")

                        if is_adm:
                            status_solicitado = request.form.get("status", "").strip()
                            status_alvo = (
                                status_solicitado
                                if status_solicitado in {"Ativo", "Pendente", "Aguardando Consultor"}
                                else status_existente
                            )
                            nova_linha_campanha = [
                                data_atual, circular_alvo, vendedor_reg, cliente_alvo,
                                chassis_alvo, modelo_alvo, grupo_manutencao,
                                plano_manutencao_salvar, rio_salvar, status_alvo,
                            ]
                            if encontrado_idx:
                                aba_campanhas_vw.update(f"A{encontrado_idx}:J{encontrado_idx}", [nova_linha_campanha])
                            else:
                                aba_campanhas_vw.append_row(nova_linha_campanha)
                            sucesso_msg = "Grupo de manutenção e dados da campanha salvos."
                        else:
                            consultor_registro = chave_campanha(
                                registro_encontrado.get("CONSULTOR", "")
                            ) if registro_encontrado else ""
                            if not encontrado_idx or consultor_registro != chave_campanha(usuario_logado):
                                erro_msg = "Você só pode informar o grupo de manutenção dos seus próprios negócios da campanha."
                            else:
                                atualizacoes_consultor = [
                                    {"range": f"G{encontrado_idx}", "values": [[grupo_manutencao]]}
                                ]
                                if chave_campanha(status_existente) in {
                                    "AGUARDANDOCONSULTOR", "AGUARDANDO"
                                }:
                                    atualizacoes_consultor.append({
                                        "range": f"J{encontrado_idx}",
                                        "values": [["Pendente"]],
                                    })
                                aba_campanhas_vw.batch_update(atualizacoes_consultor)
                                sucesso_msg = (
                                    "Grupo de manutenção enviado para análise da gestão. "
                                    "Os demais dados e o status permanecem sob responsabilidade da gestão."
                                )

                        if CACHE_CAMPANHAS is not None:
                            CACHE_CAMPANHAS.clear()
                    else:
                        erro_msg = "Selecione um Grupo de Manutenção válido."

            # Recarrega regras e negócios para considerar alterações recentes
            # antes de sincronizar os candidatos para Campanhas_VW.
            dados_campanha = obter_linhas_abas_em_lote(
                planilha,
                ["Regras_Camp_VW", "Usuarios", "Negocios_PM", "Campanhas_VW"],
                worksheets=abas_planilha,
            )
            regras = registros_de_linhas_planilha(dados_campanha["Regras_Camp_VW"])
            registros_usuarios = registros_de_linhas_planilha(dados_campanha["Usuarios"])
            linhas_brutas_negocios = dados_campanha["Negocios_PM"]
            registros_salvos_camp = registros_de_linhas_planilha(dados_campanha["Campanhas_VW"])

            # --------------------------------------------------------
            # Carrega consultores com perfil CONSULTOR PE ou AL
            # --------------------------------------------------------
            lista_consultores = []
            for u in registros_usuarios:
                p_u = str(u.get("PERFIL", "")).strip().upper()
                n_u = str(u.get("NOME", "")).strip()
                if ("CONSULTOR PE" in p_u or "CONSULTOR AL" in p_u or "CONSULTOR" in p_u) and n_u:
                    if n_u not in lista_consultores:
                        lista_consultores.append(n_u)

            # --------------------------------------------------------
            # Parâmetros de Filtro
            # --------------------------------------------------------
            ano_atual_str = str(datetime.now().year)
            vend_selecionado = request.args.get("vend", "todos" if is_adm else session.get("nome", "")).strip().lower()
            ano_selecionado = request.args.get("ano", ano_atual_str).strip()
            periodo_selecionado = request.args.get("periodo", "todos").strip().lower()

            # Função inteligente que extrai Apenas a Raiz do Modelo (Ex: 29.530 4x4 -> 29530)
            def extrair_modelo_base(val):
                texto = str(val or "").strip()
                match = re.search(r'(\d{1,2}\.?\d{3})', texto)
                if match:
                    return re.sub(r'\D', '', match.group(1))
                return ""

            meses_campanha = {
                "JANEIRO": "01", "FEVEREIRO": "02", "MARCO": "03",
                "ABRIL": "04", "MAIO": "05", "JUNHO": "06",
                "JULHO": "07", "AGOSTO": "08", "SETEMBRO": "09",
                "OUTUBRO": "10", "NOVEMBRO": "11", "DEZEMBRO": "12",
            }

            def normalizar_mes_campanha(valor):
                texto = normalizar_chave_manutencao(valor)
                if texto.isdigit():
                    return texto.zfill(2) if 1 <= int(texto) <= 12 else ""
                return meses_campanha.get(texto, "")

            mapa_regras_base = {}
            for regra in regras:
                m_regra = str(regra.get("MODELOS", "") or regra.get("MODELO", "")).strip()
                m_base = extrair_modelo_base(m_regra)
                mes_regra = normalizar_mes_campanha(
                    regra.get("MÊS", "") or regra.get("MES", "")
                )
                if m_base and mes_regra:
                    mapa_regras_base.setdefault((mes_regra, m_base), []).append(regra)

            def encontrar_regra_cruzada(modelo_negocio, mes_numero=""):
                m_base_negocio = extrair_modelo_base(modelo_negocio)
                mes_regra = normalizar_mes_campanha(mes_numero)
                if not m_base_negocio or not mes_regra:
                    return None
                candidatas = mapa_regras_base.get((mes_regra, m_base_negocio), [])
                return candidatas[0] if candidatas else None

            def data_negocio_campanha(valor):
                texto = str(valor or "").strip()
                for formato in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(texto, formato)
                    except ValueError:
                        continue
                return None

            # --------------------------------------------------------
            # Negócios: cruzando estritamente com os modelos da Regra
            # --------------------------------------------------------
            registros_campanha = []
            anos_encontrados = set([ano_atual_str])
            chaves_campanhas_existentes = set()
            for registro_campanha in registros_salvos_camp:
                chave_existente = chave_registro_campanha(registro_campanha)
                if chave_existente:
                    chaves_campanhas_existentes.add(chave_existente)
            novas_linhas_campanha = []

            if len(linhas_brutas_negocios) > 1:
                cabecalhos = [str(c).upper().strip() for c in linhas_brutas_negocios[0]]

                for idx_linha, linha in enumerate(linhas_brutas_negocios[1:], start=2):
                    item_dict = {"_index_planilha": idx_linha}

                    for i, val in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i]:
                            item_dict[cabecalhos[i]] = val

                    vend_val = str(item_dict.get("VENDEDOR", "")).strip()
                    temp_val = str(item_dict.get("TEMPERATURA", "")).strip().lower()
                    modelo_val = str(item_dict.get("MODELO", "")).strip()
                    data_val = str(item_dict.get("DATA", "")).strip()

                    if temp_val in ["fechado", "perdida"]:
                        continue

                    data_obj = data_negocio_campanha(data_val)
                    ano_item = str(data_obj.year) if data_obj else ""
                    mes_item = f"{data_obj.month:02d}" if data_obj else ""

                    if ano_item:
                        anos_encontrados.add(ano_item)

                    regra_associada = encontrar_regra_cruzada(modelo_val, mes_item)
                    if regra_associada:
                        cliente_val = str(item_dict.get("CLIENTE", "")).strip()
                        chassis_val = str(item_dict.get("CHASSIS", "") or item_dict.get("CHASSI", "")).strip()
                        regra_circular = str(regra_associada.get("CIRCULAR", "")).strip()
                        data_campanha = data_obj.strftime("%d/%m/%Y") if data_obj else data_val
                        chave_chassis = chave_campanha(chassis_val)
                        chave_sem_chassis = "|".join(chave_campanha(valor) for valor in (
                            data_campanha, vend_val, cliente_val, modelo_val, regra_circular
                        ))
                        chave_nova = (
                            f"CH:{chave_chassis}"
                            if chave_chassis
                            else f"REG:{chave_sem_chassis}"
                        )

                        if chave_nova not in chaves_campanhas_existentes:
                            regra_prev = str(regra_associada.get("REGRA PREV", "")).strip()
                            regra_prev_max = str(regra_associada.get("REGRA PREV / MAX", "")).strip()
                            regra_rio = str(regra_associada.get("REGRA RIO", "")).strip()
                            plano_campanha = f"{regra_prev} / {regra_prev_max}".strip(" /")
                            novas_linhas_campanha.append([
                                data_campanha,
                                regra_circular,
                                vend_val,
                                cliente_val,
                                chassis_val,
                                modelo_val,
                                "",
                                plano_campanha,
                                regra_rio,
                                "Aguardando Consultor",
                            ])
                            chaves_campanhas_existentes.add(chave_nova)

                    if ano_selecionado != "todos" and ano_item and ano_item != ano_selecionado:
                        continue

                    if periodo_selecionado == "semestre1" and mes_item not in ["01","02","03","04","05","06"]:
                        continue
                    elif periodo_selecionado == "semestre2" and mes_item not in ["07","08","09","10","11","12"]:
                        continue
                    elif len(periodo_selecionado) == 2 and periodo_selecionado.isdigit() and mes_item != periodo_selecionado:
                        continue

                    if not regra_associada:
                        continue

                    if not is_adm and chave_campanha(vend_val) != chave_campanha(usuario_logado):
                        continue

                    if vend_selecionado != "todos" and vend_val.strip().lower() != vend_selecionado:
                        continue

                    item_dict["_regra"] = regra_associada
                    registros_campanha.append(item_dict)

            if novas_linhas_campanha:
                # Confere novamente diretamente na planilha antes de gravar,
                # bloqueando chassis que tenham sido incluídos desde a leitura inicial.
                registros_campanha_atualizados = registros_de_linhas_planilha(
                    aba_campanhas_vw.get_all_values()
                )
                chaves_campanhas_atualizadas = {
                    chave
                    for registro_atualizado in registros_campanha_atualizados
                    if (chave := chave_registro_campanha(registro_atualizado))
                }
                linhas_sem_duplicidade = []
                for linha_campanha in novas_linhas_campanha:
                    registro_novo = dict(zip(
                        (
                            "DATA", "CIRCULAR", "CONSULTOR", "EMPRESA", "CHASSIS",
                            "MODELOS", "G MANUTENÇÃO", "PLANO DE MANUTENÇÃO", "RIO", "STATUS",
                        ),
                        linha_campanha,
                    ))
                    chave_nova = chave_registro_campanha(registro_novo)
                    if chave_nova and chave_nova in chaves_campanhas_atualizadas:
                        continue
                    linhas_sem_duplicidade.append(linha_campanha)
                    if chave_nova:
                        chaves_campanhas_atualizadas.add(chave_nova)

                novas_linhas_campanha = linhas_sem_duplicidade

            if novas_linhas_campanha:
                aba_campanhas_vw.append_rows(
                    novas_linhas_campanha,
                    value_input_option="USER_ENTERED",
                    insert_data_option="INSERT_ROWS",
                )
                if CACHE_CAMPANHAS is not None:
                    CACHE_CAMPANHAS.clear()
                registros_salvos_camp = obter_registros_seguros(aba_campanhas_vw)
                sucesso_msg = (
                    f"{len(novas_linhas_campanha)} negócio(s) elegível(is) "
                    "copiado(s) para Campanhas_VW."
                )

            # Campanhas_VW é a fonte oficial dos dados e status exibidos.
            # Negocios_PM serve apenas para descobrir e inserir novos elegíveis.
            registros_campanha = []
            for registro_salvo in registros_salvos_camp:
                data_val = str(registro_salvo.get("DATA", "")).strip()
                data_obj = data_negocio_campanha(data_val)
                ano_item = str(data_obj.year) if data_obj else ""
                mes_item = f"{data_obj.month:02d}" if data_obj else ""

                if ano_item:
                    anos_encontrados.add(ano_item)
                if ano_selecionado != "todos" and ano_item and ano_item != ano_selecionado:
                    continue
                if periodo_selecionado == "semestre1" and mes_item not in {
                    "01", "02", "03", "04", "05", "06"
                }:
                    continue
                if periodo_selecionado == "semestre2" and mes_item not in {
                    "07", "08", "09", "10", "11", "12"
                }:
                    continue
                if (
                    len(periodo_selecionado) == 2
                    and periodo_selecionado.isdigit()
                    and mes_item != periodo_selecionado
                ):
                    continue

                consultor_salvo = str(registro_salvo.get("CONSULTOR", "")).strip()
                consultor_norm = chave_campanha(consultor_salvo)
                if not is_adm and consultor_norm != chave_campanha(usuario_logado):
                    continue
                if (
                    vend_selecionado != "todos"
                    and consultor_norm != chave_campanha(vend_selecionado)
                ):
                    continue

                modelo_salvo = str(
                    registro_salvo.get("MODELOS", "") or registro_salvo.get("MODELO", "")
                ).strip()
                item_campanha = dict(registro_salvo)
                item_campanha.update({
                    "CLIENTE": str(
                        registro_salvo.get("EMPRESA", "") or registro_salvo.get("CLIENTE", "")
                    ).strip(),
                    "MODELO": modelo_salvo,
                    "CHASSIS": str(
                        registro_salvo.get("CHASSIS", "") or registro_salvo.get("CHASSI", "")
                    ).strip(),
                    "VENDEDOR": consultor_salvo,
                    "_regra": encontrar_regra_cruzada(modelo_salvo, mes_item) or {},
                    "_mes_campanha": mes_item,
                })
                registros_campanha.append(item_campanha)

            # Os KPIs são calculados a partir dos mesmos registros persistidos
            # que aparecem na tabela e respeitam os filtros selecionados.
            kpis_status = {"Ativo": 0, "Pendente": 0, "Aguardando Consultor": 0, "Total": 0}

            for reg in registros_campanha:
                status_norm = chave_campanha(reg.get("STATUS", ""))
                if status_norm == "ATIVO":
                    kpis_status["Ativo"] += 1
                elif status_norm == "PENDENTE":
                    kpis_status["Pendente"] += 1
                else:
                    kpis_status["Aguardando Consultor"] += 1
                kpis_status["Total"] += 1

            kpis_status["Aguardando Consultor"] = max(
                0, len(registros_campanha) - kpis_status["Ativo"] - kpis_status["Pendente"]
            )

            # --------------------------------------------------------
            # Geração da Tabela Principal
            # --------------------------------------------------------
            linhas_campanha = ""
            for reg in registros_campanha:
                cliente = reg.get("CLIENTE", "")
                modelo = reg.get("MODELO", "")
                chassis = reg.get("CHASSIS", "") or reg.get("CHASSI", "")
                vendedor_item = reg.get("VENDEDOR", session.get('nome', ''))
                regra = reg.get("_regra") or {}

                circular = str(reg.get("CIRCULAR", "") or regra.get("CIRCULAR", "")).strip()
                link_circular = str(regra.get("LINK_CIRCULAR", "")).strip()
                mes_numero = reg.get("_mes_campanha", "")
                mes_campanha = str(
                    regra.get("MÊS", "") or regra.get("MES", "")
                    or next(
                        (nome for nome, numero in meses_campanha.items() if numero == mes_numero),
                        "",
                    )
                ).strip()
                regra_prev = str(regra.get("REGRA PREV", "")).strip()
                regra_rio = str(regra.get("REGRA RIO", "")).strip()
                regra_prev_max = str(regra.get("REGRA PREV / MAX", "")).strip()

                plano_manutencao_txt = str(
                    reg.get("PLANO DE MANUTENÇÃO", "")
                    or f"{regra_prev} / {regra_prev_max}".strip(" /")
                ).strip()
                rio_txt = str(reg.get("RIO", "") or regra_rio).strip()

                if not link_circular:
                    link_circular = f"https://drive.google.com/drive/search?q={urllib.parse.quote(circular)}"

                circular_html = f'<a href="{link_circular}" target="_blank" rel="noopener noreferrer" style="color: #0066cc; font-weight: 600;">{circular}</a>' if circular else "-"

                grupo_atual = str(reg.get("G MANUTENÇÃO", "")).strip()
                status_norm = chave_campanha(reg.get("STATUS", ""))
                status_atual = {
                    "ATIVO": "Ativo",
                    "PENDENTE": "Pendente",
                    "AGUARDANDOCONSULTOR": "Aguardando Consultor",
                    "AGUARDANDO": "Aguardando Consultor",
                }.get(status_norm, "Aguardando Consultor")

                if is_adm:
                    opcoes_grupo = f"""
                    <form method="POST" style="display: flex; gap: 4px; align-items: center; margin: 0; flex-wrap: wrap;">
                        <input type="hidden" name="acao_form" value="salvar_grupo">
                        <input type="hidden" name="chassis" value="{chassis}">
                        <input type="hidden" name="cliente" value="{cliente}">
                        <input type="hidden" name="modelo" value="{modelo}">
                        <input type="hidden" name="circular" value="{circular}">
                        <input type="hidden" name="plano_manutencao" value="{plano_manutencao_txt}">
                        <input type="hidden" name="rio" value="{rio_txt}">
                        <input type="hidden" name="vendedor_reg" value="{vendedor_item}">
                        <select name="grupo_manutencao" required style="padding: 6px; font-size: 11px; border-radius: 4px; border: 1px solid #cbd5e0; background: #fff;">
                            <option value="">Grupo...</option>
                            <option value="Rodoviário" {"selected" if grupo_atual == "Rodoviário" else ""}>Rodoviário</option>
                            <option value="Misto" {"selected" if grupo_atual == "Misto" else ""}>Misto</option>
                            <option value="Severo" {"selected" if grupo_atual == "Severo" else ""}>Severo</option>
                        </select>
                        <select name="status" required style="padding: 6px; font-size: 11px; border-radius: 4px; border: 1px solid #cbd5e0; background: #fff;">
                            <option value="Ativo" {"selected" if status_atual == "Ativo" else ""}>Ativo</option>
                            <option value="Pendente" {"selected" if status_atual == "Pendente" else ""}>Pendente</option>
                            <option value="Aguardando Consultor" {"selected" if status_atual == "Aguardando Consultor" else ""}>Aguardando Consultor</option>
                        </select>
                        <button type="submit" class="btn-acao btn-editar" style="padding: 6px 10px; font-size: 11px; background:#2b6cb0; border:none; color:white; border-radius:4px; cursor:pointer;">Salvar</button>
                    </form>
                    """
                else:
                    opcoes_grupo = f"""
                    <form method="POST" style="display: flex; gap: 4px; align-items: center; margin: 0; flex-wrap: wrap;">
                        <input type="hidden" name="acao_form" value="salvar_grupo">
                        <input type="hidden" name="chassis" value="{chassis}">
                        <input type="hidden" name="cliente" value="{cliente}">
                        <input type="hidden" name="modelo" value="{modelo}">
                        <input type="hidden" name="circular" value="{circular}">
                        <input type="hidden" name="plano_manutencao" value="{plano_manutencao_txt}">
                        <input type="hidden" name="rio" value="{rio_txt}">
                        <input type="hidden" name="vendedor_reg" value="{vendedor_item}">
                        <select name="grupo_manutencao" required style="padding: 6px; font-size: 11px; border-radius: 4px; border: 1px solid #cbd5e0; background: #fff;">
                            <option value="">Grupo...</option>
                            <option value="Rodoviário" {"selected" if grupo_atual == "Rodoviário" else ""}>Rodoviário</option>
                            <option value="Misto" {"selected" if grupo_atual == "Misto" else ""}>Misto</option>
                            <option value="Severo" {"selected" if grupo_atual == "Severo" else ""}>Severo</option>
                        </select>
                        <span style="font-size: 11px; padding: 6px 10px; background: #edf2f7; border-radius: 4px; color: #2d3748; font-weight:600;">Status: {status_atual}</span>
                        <button type="submit" class="btn-acao btn-editar" style="padding: 6px 10px; font-size: 11px; background:#2b6cb0; border:none; color:white; border-radius:4px; cursor:pointer;">Salvar</button>
                    </form>
                    """

                linhas_campanha += f"""
                <tr>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;"><b>{cliente}</b></td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;">{modelo}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;">{chassis}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;">{vendedor_item}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;">{mes_campanha}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7; color: #2b6cb0; font-weight: 600;">{plano_manutencao_txt}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7; color: #2f855a; font-weight: 600;">{rio_txt if rio_txt else '-'}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7;">{circular_html}</td>
                    <td style="padding:12px 10px; border-bottom:1px solid #edf2f7; min-width: 280px;">{opcoes_grupo}</td>
                </tr>
                """

            if not linhas_campanha:
                linhas_campanha = '<tr><td colspan="9" style="padding:30px; text-align:center; color:#718096; font-size: 14px;">Nenhum negócio enquadrado nas regras da campanha para os filtros selecionados.</td></tr>'

            options_vend = '<option value="todos"' + (' selected' if vend_selecionado == 'todos' else '') + '>Todos os Vendedores</option>'
            for c in lista_consultores:
                sel_v = ' selected' if vend_selecionado == c.lower() else ''
                options_vend += f'<option value="{c}"{sel_v}>{c}</option>'

            options_ano = ""
            for a in sorted(list(anos_encontrados), reverse=True):
                sel_a = ' selected' if ano_selecionado == a else ''
                options_ano += f'<option value="{a}"{sel_a}>{a}</option>'

            meses_dict = {
                "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
                "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
                "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro"
            }
            options_periodo = f'<option value="todos" {"selected" if periodo_selecionado == "todos" else ""}>Todos os Meses / Semestres</option>'
            options_periodo += f'<option value="semestre1" {"selected" if periodo_selecionado == "semestre1" else ""}>1º Semestre</option>'
            options_periodo += f'<option value="semestre2" {"selected" if periodo_selecionado == "semestre2" else ""}>2º Semestre</option>'
            for m_num, m_nome in meses_dict.items():
                options_periodo += f'<option value="{m_num}" {"selected" if periodo_selecionado == m_num else ""}>{m_nome}</option>'

            # --------------------------------------------------------
            # Geração do Relatório PDF (Apenas Itens Salvos)
            # --------------------------------------------------------
            linhas_pdf = ""
            for r in registros_salvos_camp:
                consultor_salvo = str(r.get("CONSULTOR", "")).strip()
                if not is_adm and chave_campanha(consultor_salvo) != chave_campanha(usuario_logado):
                    continue
                
                linhas_pdf += f"""
                <tr>
                    <td style="padding:6px; border:1px solid #333;">{r.get('DATA', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('CIRCULAR', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{consultor_salvo}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('EMPRESA', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('CHASSIS', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('MODELOS', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('G MANUTENÇÃO', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('PLANO DE MANUTENÇÃO', '')}</td>
                    <td style="padding:6px; border:1px solid #333;">{r.get('RIO', '')}</td>
                    <td style="padding:6px; border:1px solid #333; font-weight:bold;">{r.get('STATUS', '')}</td>
                </tr>
                """

            if not linhas_pdf:
                linhas_pdf = '<tr><td colspan="10" style="text-align:center; padding:20px; border:1px solid #333;">Nenhum registro salvo ou enviado encontrado.</td></tr>'


            conteudo = f"""
            <div style="max-width: 1200px; margin: 0 auto; padding: 10px;">
                <h2 style="color:#002244; border-bottom:2px solid #edf2f7; padding-bottom:8px; margin-bottom:8px; font-size:20px;">Campanha VW PREV</h2>
                <p style="color:#4a5568; font-size:13px; margin-bottom:20px;">
                    Usuário logado: <b>{session.get('nome', 'Usuário')}</b>. Gerencie e acompanhe os modelos da campanha.
                </p>

                <!-- Área de Filtros Responsiva -->
                <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 20px; display: flex; flex-direction: column; gap: 15px;">
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px;">
                        {f'<select id="filtroVend" style="padding: 10px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 13px;" onchange="aplicarFiltrosCampanha()">{options_vend}</select>' if is_adm else ''}
                        <select id="filtroAno" style="padding: 10px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 13px;" onchange="aplicarFiltrosCampanha()">{options_ano}</select>
                        <select id="filtroPeriodo" style="padding: 10px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 13px;" onchange="aplicarFiltrosCampanha()">{options_periodo}</select>
                    </div>
                    <div style="display: flex; justify-content: flex-end;">
                        <!-- Botão Gerar PDF sem erro 404 - Usa a área oculta -->
                        <button onclick="window.print()" style="padding: 10px 20px; background: #c53030; color: white; border: none; border-radius: 6px; font-weight: bold; font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 5px;">
                            📄 Imprimir / Gerar PDF
                        </button>
                    </div>
                </div>

                <!-- KPIs Responsivos -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px;">
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:8px; padding:15px; border-left:4px solid #3182ce; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                        <div style="font-size:11px; color:#718096; font-weight:700; text-transform: uppercase;">Total a Trabalhar</div>
                        <div style="font-size:24px; font-weight:bold; color:#2d3748; margin-top: 5px;">{len(registros_campanha)}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:8px; padding:15px; border-left:4px solid #38a169; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                        <div style="font-size:11px; color:#38a169; font-weight:700; text-transform: uppercase;">Ativo</div>
                        <div style="font-size:24px; font-weight:bold; color:#276749; margin-top: 5px;">{kpis_status['Ativo']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:8px; padding:15px; border-left:4px solid #d69e2e; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                        <div style="font-size:11px; color:#d69e2e; font-weight:700; text-transform: uppercase;">Pendente</div>
                        <div style="font-size:24px; font-weight:bold; color:#b7791f; margin-top: 5px;">{kpis_status['Pendente']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:8px; padding:15px; border-left:4px solid #e53e3e; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                        <div style="font-size:11px; color:#e53e3e; font-weight:700; text-transform: uppercase;">Aguardando Consultor</div>
                        <div style="font-size:24px; font-weight:bold; color:#c53030; margin-top: 5px;">{kpis_status['Aguardando Consultor']}</div>
                    </div>
                </div>

                {f'<div style="background:#c6f6d5; color:#22543d; padding:12px; border-radius:6px; margin-bottom:15px; font-size:13px; font-weight:bold;">{sucesso_msg}</div>' if sucesso_msg else ''}
                {f'<div style="background:#fed7d7; color:#822727; padding:12px; border-radius:6px; margin-bottom:15px; font-size:13px; font-weight:bold;">{erro_msg}</div>' if erro_msg else ''}

                <!-- Tabela de Dados Responsiva -->
                <div style="background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom:20px; overflow: hidden;">
                    <div style="padding:15px; font-weight:700; color:#002244; border-bottom:1px solid #e2e8f0; background: #f8fafc;">
                        Modelos Enquadrados nas Regras da Campanha
                    </div>
                    <div style="overflow-x:auto;">
                        <table style="width:100%; border-collapse:collapse; font-size:12px; text-align:left;">
                            <thead>
                                <tr style="background:#002244; color:#fff;">
                                    <th style="padding:12px 10px;">Cliente</th>
                                    <th style="padding:12px 10px;">Modelo</th>
                                    <th style="padding:12px 10px;">Chassis</th>
                                    <th style="padding:12px 10px;">Consultor</th>
                                    <th style="padding:12px 10px;">Mês Campanha</th>
                                    <th style="padding:12px 10px;">Plano de Manutenção</th>
                                    <th style="padding:12px 10px;">RIO</th>
                                    <th style="padding:12px 10px;">Circular</th>
                                    <th style="padding:12px 10px;">Ação / Status</th>
                                </tr>
                            </thead>
                            <tbody>{linhas_campanha}</tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- ÁREA DE IMPRESSÃO OCULTA: APENAS OS REGISTROS SALVOS -->
            <div id="area-impressao-pdf" style="display: none;">
                <h2 style="color: #002244; text-align: center; font-family: Arial, sans-serif;">Relatório de Campanhas VW - Itens Salvos</h2>
                <p style="text-align: center; font-size: 12px; color: #666; font-family: Arial, sans-serif; margin-bottom: 20px;">
                    Gerado por: {session.get('nome', 'Usuário')} em {datetime.now().strftime('%d/%m/%Y %H:%M')}
                </p>
                <table style="width: 100%; border-collapse: collapse; font-size: 11px; font-family: Arial, sans-serif;">
                    <thead>
                        <tr style="background: #002244; color: white; text-align: left;">
                            <th style="padding: 8px; border: 1px solid #333;">Data</th>
                            <th style="padding: 8px; border: 1px solid #333;">Circular</th>
                            <th style="padding: 8px; border: 1px solid #333;">Consultor</th>
                            <th style="padding: 8px; border: 1px solid #333;">Empresa</th>
                            <th style="padding: 8px; border: 1px solid #333;">Chassis</th>
                            <th style="padding: 8px; border: 1px solid #333;">Modelos</th>
                            <th style="padding: 8px; border: 1px solid #333;">Grupo</th>
                            <th style="padding: 8px; border: 1px solid #333;">Plano</th>
                            <th style="padding: 8px; border: 1px solid #333;">RIO</th>
                            <th style="padding: 8px; border: 1px solid #333;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {linhas_pdf}
                    </tbody>
                </table>
            </div>

            <!-- Estilo CSS Inteligente para o PDF -->
            <style>
                @media print {{
                    body * {{ visibility: hidden !important; }}
                    #area-impressao-pdf, #area-impressao-pdf * {{ visibility: visible !important; }}
                    #area-impressao-pdf {{ 
                        display: block !important; 
                        position: absolute; 
                        left: 0; 
                        top: 0; 
                        width: 100%; 
                        padding: 20px;
                        background: #fff;
                    }}
                }}
            </style>

            <script>
                function aplicarFiltrosCampanha() {{
                    var vendSelect = document.getElementById('filtroVend');
                    var anoSelect = document.getElementById('filtroAno');
                    var periodoSelect = document.getElementById('filtroPeriodo');
                    var url = '/modulo/camp_vw_prev?ano=' + encodeURIComponent(anoSelect.value) + '&periodo=' + encodeURIComponent(periodoSelect.value);
                    if (vendSelect) {{
                        url += '&vend=' + encodeURIComponent(vendSelect.value);
                    }}
                    window.location.href = url;
                }}
            </script>
            """

        except Exception as e:
            traceback.print_exc()
            erro_campanha = str(e)
            if "429" in erro_campanha or "quota" in erro_campanha.lower():
                mensagem_campanha = (
                    "O Google Sheets atingiu o limite temporário de consultas. "
                    "A leitura da campanha agora usa uma consulta em lote; aguarde alguns minutos e tente novamente."
                )
            else:
                mensagem_campanha = f"Erro ao carregar Campanha VW PREV: {html.escape(erro_campanha)}"
            conteudo = f'<div style="color:#c53030; background:#fff5f5; padding:15px; border-radius:8px; margin: 15px;">{mensagem_campanha}</div>'

    elif nome_modulo == "dashboard":
        try:
            # ============================================================
            # DASHBOARD 2 NÍVEIS
            #   1) ADM / DIRETOR / GERENTE -> visão consolidada + filtro por consultor
            #   2) CONSULTOR               -> somente os próprios registros
            #
            # Fonte dos dados:
            #   Vendas_PM, Negocios_PM
            #
            # O dashboard não altera nenhuma planilha. Ele somente consolida
            # os dados existentes e aplica os filtros no servidor.
            # ============================================================
            planilha = conectar_google_sheets()

            perfil_usuario = str(session.get("perfil", "")).strip().upper()
            usuario_logado = str(session.get("nome", "")).strip()
            usuario_logado_norm = usuario_logado.upper()
            perfis_gestao = {"ADM", "DIRETOR", "GERENTE"}
            is_gestao = perfil_usuario in perfis_gestao

            def norm(v):
                txt = str(v if v is not None else "").strip()
                return unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII").upper()

            def parse_data(valor):
                if valor is None:
                    return None
                if hasattr(valor, "year") and hasattr(valor, "month"):
                    try:
                        return datetime(valor.year, valor.month, valor.day)
                    except Exception:
                        pass
                s = str(valor).strip()
                if not s or s.lower() in ("nan", "none", "-"):
                    return None
                formatos = (
                    "%d/%m/%Y", "%d/%m/%y",
                    "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
                    "%d-%m-%Y", "%d-%m-%y",
                    "%m/%d/%Y", "%m/%d/%y"
                )

                for fmt in formatos:
                    try:
                        return datetime.strptime(s.split(" ")[0] if " " in s and fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%m/%d/%Y", "%m/%d/%y") else s, fmt)
                    except Exception:
                        continue
                # Tentativa final para ISO com horário
                try:
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    return None

            def qtd_valor(v):
                s = str(v if v is not None else "").strip()
                if not s or s.lower() == "nan":
                    return 1
                # aceita "10", "10 un", "10 veículos"
                m = re.search(r"-?\d+(?:[.,]\d+)?", s.replace(".", "").replace(",", "."))
                if not m:
                    return 1
                try:
                    return max(0, int(float(m.group(0))))
                except Exception:
                    return 1

            def carregar_aba(nome):
                return obter_registros_com_cache(
                    planilha,
                    nome,
                    falhar_em_erro=True,
                )

            # Dashboard atual: somente Plano de Manutenção / RIO.
            # Lê Vendas_PM e Negocios_PM diretamente a cada abertura do dashboard.
            # Assim os KPIs sempre são recalculados sobre os dados atuais e,
            # principalmente, obedecem aos filtros de ano, período, consultor,
            # produto e estado aplicados logo abaixo.
            vendas_pm = carregar_aba("Vendas_PM")
            neg_pm = carregar_aba("Negocios_PM")

            # As tabelas auxiliares continuam usando o carregamento consolidado.
            dados_login = carregar_dados_login()
            usuarios = dados_login.get("Usuarios", [])
            pm_precos_dashboard = dados_login.get("PM_Precos", [])
            top3_planos = obter_top3_planos_melhor_preco(
                pm_precos_dashboard,
                dados_login.get("Modelos", []),
            )
            precos_campanha_vw = obter_precos_campanha_vw(
                dados_login.get("Promocao VW", []),
                dados_login.get("Modelos", []),
            )
            # Catálogo de produtos do dashboard:
            # - PM: coluna PRODUTO da aba PM
            # - RIO: coluna PRODUTO da aba RIO
            catalogo_pm = []
            catalogo_rio = []
            for r in carregar_aba("PM"):
                p = str(r.get("PRODUTO", "")).strip()
                if p and norm(p) != "PRODUTO" and p not in catalogo_pm:
                    catalogo_pm.append(p)
            for r in carregar_aba("RIO"):
                p = str(r.get("PRODUTO", "")).strip()
                if p and norm(p) != "PRODUTO" and p not in catalogo_rio:
                    catalogo_rio.append(p)
            catalogo_pm.sort(key=lambda x: norm(x))
            catalogo_rio.sort(key=lambda x: norm(x))

            # Mapa de consultores e região/UF.
            consultores = []
            mapa_regiao = {}
            for u in usuarios:
                nome_u = str(u.get("NOME", "")).strip()
                perfil_u = str(u.get("PERFIL", "")).strip()
                if not nome_u:
                    continue
                if "CONSULTOR" in norm(perfil_u):
                    if nome_u not in consultores:
                        consultores.append(nome_u)
                    pnorm = norm(perfil_u)
                    mapa_regiao[norm(nome_u)] = "AL" if re.search(r"\bAL\b", pnorm) else "PE"

            consultores.sort(key=lambda x: norm(x))

            # ------------------------------------------------------------
            # Filtros
            # ------------------------------------------------------------
            filtro_ano = request.args.get("ano", "").strip()
            filtro_mes = request.args.get("mes", "").strip()
            filtro_consultor = request.args.get("consultor", "").strip()
            filtro_produto = request.args.get("produto", "").strip()
            filtro_uf = request.args.get("uf", "").strip().upper()

            anos = set()

            def coletar_anos(registros):
                for r in registros:
                    dt = parse_data(r.get("DATA DA VENDA") or r.get("DATA"))
                    if dt:
                        anos.add(str(dt.year))

            coletar_anos(vendas_pm)
            coletar_anos(neg_pm)

            ano_atual = str(datetime.now().year)
            if not filtro_ano:
                filtro_ano = ano_atual if ano_atual in anos else (max(anos) if anos else ano_atual)

            # Consultor não pode escapar do próprio escopo.
            if not is_gestao:
                filtro_consultor = usuario_logado

            def passa_data(dt):
                if not dt:
                    return False
                if filtro_ano and str(dt.year) != filtro_ano:
                    return False
                if filtro_mes:
                    if filtro_mes == "S1" and dt.month > 6:
                        return False
                    if filtro_mes == "S2" and dt.month <= 6:
                        return False
                    if filtro_mes.isdigit() and dt.month != int(filtro_mes):
                        return False
                return True

            def passa_pessoa(vendedor):
                if filtro_consultor and norm(filtro_consultor) != "TODOS":
                    return norm(vendedor) == norm(filtro_consultor)
                return True

            def passa_uf(vendedor):
                if not filtro_uf or filtro_uf == "TODOS":
                    return True
                return mapa_regiao.get(norm(vendedor), "") == filtro_uf

            def passa_produto(produto, origem="", plano="", rio=""):
                if not filtro_produto or norm(filtro_produto) == "TODOS":
                    return True

                fp = norm(filtro_produto)

                # Filtros vindos do catálogo das abas PM/RIO.
                # O valor enviado pelo select tem prefixo PM| ou RIO|.
                if fp.startswith("PM|"):
                    alvo = norm(filtro_produto.split("|", 1)[1])
                    if origem == "PM":
                        return alvo in norm(produto) or alvo in norm(plano)
                    return False

                if fp.startswith("RIO|"):
                    alvo = norm(filtro_produto.split("|", 1)[1])
                    if origem == "PM":
                        return alvo in norm(produto) or alvo in norm(rio)
                    return False

                return fp in norm(produto) or fp in norm(plano) or fp in norm(rio)

            # ------------------------------------------------------------
            # 1. Carrega lista dinâmica de produtos RIO cadastrados na aba RIO
            # ------------------------------------------------------------
            catalogo_rio_lista = [norm(p) for p in catalogo_rio if p]
            # Termos chave padrão de fallback para segurança
            termos_rio_padrao = ["RIO", "DIAGNOSTICO", "PERFORMANCE", "BROKER", "GEO", "ANÁLISE", "ANALISE"]

            # ------------------------------------------------------------
            # 2. Normalização e Leitura das Vendas (Vendas_PM)
            # ------------------------------------------------------------
            fontes_vendas = (
                ("Plano de Manutenção / RIO", "PM", vendas_pm),
            )

            vendas = []
            for solucao, origem, registros in fontes_vendas:
                for r in registros:
                    vendedor = str(r.get("VENDEDOR", "")).strip()
                    dt = parse_data(r.get("DATA DA VENDA") or r.get("DATA"))
                    produto = str(r.get("PRODUTO", "")).strip()
                    
                    # Captura Plano (Coluna B) e RIO (Coluna C) com suporte a nomes antigos e novos
                    plano_manutencao = str(
                        r.get("P. MANUTENÇÃO", "")
                        or r.get("PLANO DE MANUTENÇÃO", "")
                        or r.get("PLANO", "")
                        or r.get("PLANO MANUTENCAO", "")
                    ).strip()

                    rio_val = str(r.get("RIO", "")).strip()
                    if not produto:
                        produto = " / ".join(
                            valor for valor in (plano_manutencao, rio_val) if valor
                        ) or "Não informado"

                    if not passa_pessoa(vendedor) or not passa_uf(vendedor):
                        continue
                    if not passa_data(dt):
                        continue
                    if not passa_produto(produto, origem=origem, plano=plano_manutencao, rio=rio_val):
                        continue

                    qtd = qtd_valor(r.get("QUANTIDADE", 1))
                    cliente = str(r.get("CLIENTE", "")).strip()
                    modelo = str(r.get("MODELO", "")).strip()
                    chassis = next(
                        (
                            str(valor).strip()
                            for cabecalho, valor in r.items()
                            if normalizar_chave_planilha(cabecalho) in {"chassis", "chassi"}
                            and str(valor or "").strip()
                        ),
                        "",
                    )
                    contrato = obter_numero_contrato(r)
                    
                    chave = (
                        norm(origem), norm(cliente), norm(produto),
                        norm(plano_manutencao), norm(rio_val),
                        norm(modelo), norm(vendedor),
                        dt.strftime("%Y-%m-%d") if dt else "",
                        norm(chassis), norm(contrato), qtd,
                    )
                    vendas.append({
                        "origem": origem,
                        "solucao": solucao,
                        "cliente": cliente,
                        "contrato": contrato,
                        "chassis": chassis,
                        "produto": produto,
                        "plano": plano_manutencao,
                        "rio": rio_val,
                        "modelo": modelo,
                        "qtd": qtd,
                        "vendedor": vendedor,
                        "data": dt,
                        "data_txt": str(r.get("DATA DA VENDA", "")).strip(),
                        "anexo": str(r.get("ANEXO 1", "")).strip(),
                        "chave": chave,
                    })

            # Dedupe de registros idênticos
            vendas_unicas = {}
            duplicatas = 0
            for v in vendas:
                if v["chave"] in vendas_unicas:
                    duplicatas += 1
                    continue
                vendas_unicas[v["chave"]] = v
            vendas = list(vendas_unicas.values())
            vendas.sort(key=lambda x: x["data"] or datetime.min, reverse=True)

            # ------------------------------------------------------------
            # 3. PLANOS E TELEMETRIA RIO (Cálculo dos KPIs Dinâmicos)
            # ------------------------------------------------------------
            total_planos_vendidos = 0
            total_unidades_planos = 0
            qtd_prev = 0
            qtd_max = 0
            qtd_plus = 0
            qtd_rio = 0
            por_tipo_rio = {}
            vendas_por_consultor_mes = {}

            for v in vendas:
                qtd_venda = v.get("qtd", 1)
                total_unidades_planos += qtd_venda

                texto_plano = norm(str(v.get("plano", "")))
                texto_rio = norm(str(v.get("rio", "")))
                texto_produto = norm(str(v.get("produto", "")))
                texto_anexo = norm(str(v.get("anexo", "")))
                
                texto_geral_plano = f"{texto_plano} {texto_produto}"
                texto_geral_rio = f"{texto_rio} {texto_produto} {texto_anexo}"

                tem_pm = (
                    texto_plano not in ("", "-", "NENHUM", "NAO", "N/A")
                    or any(modalidade in texto_geral_plano for modalidade in ("PREV", "MAX", "PLUS"))
                )

                # --- A. Contabilização do PLANO DE MANUTENÇÃO ---
                if "PREV" in texto_geral_plano:
                    qtd_prev += qtd_venda
                    total_planos_vendidos += qtd_venda
                elif "MAX" in texto_geral_plano:
                    qtd_max += qtd_venda
                    total_planos_vendidos += qtd_venda
                elif "PLUS" in texto_geral_plano:
                    qtd_plus += qtd_venda
                    total_planos_vendidos += qtd_venda

                # --- B. Contabilização DINÂMICA da TELEMETRIA RIO ---
                # Verifica se o texto da coluna RIO (ou Produto) coincide com
                # qualquer item da aba RIO ou qualquer palavra-chave da família RIO
                tem_rio = False
                tokens_rio = set(texto_geral_rio.split())

                if len(texto_rio) > 0 and texto_rio not in ["-", "NENHUM", "NAO", "NÃO"]:
                    tem_rio = True
                else:
                    # Checagem contra o catálogo da aba RIO
                    for prod_rio in catalogo_rio_lista:
                        tokens_produto_rio = {
                            token for token in re.findall(r"[A-Z0-9]+", prod_rio)
                            if token not in {"RIO", "TELEMETRIA", "PRODUTO"}
                        }
                        if tokens_produto_rio and tokens_produto_rio.issubset(tokens_rio):
                            tem_rio = True
                            break
                    
                    # Checagem contra termos de fallback (GEO, Broker, Performance, etc.)
                    if not tem_rio:
                        for termo in termos_rio_padrao:
                            if termo in texto_geral_rio:
                                tem_rio = True
                                break

                if tem_pm or tem_rio:
                    chave_consultor = norm(v.get("vendedor", ""))
                    dados_consultor = vendas_por_consultor_mes.setdefault(
                        chave_consultor,
                        {"nome": str(v.get("vendedor", "")).strip(), "meses": {}},
                    )
                    dados_mes = dados_consultor["meses"].setdefault(
                        v["data"].month, {"pm": 0, "rio": 0},
                    )
                    if tem_pm:
                        dados_mes["pm"] += qtd_venda
                    if tem_rio:
                        dados_mes["rio"] += qtd_venda

                if tem_rio:
                    qtd_rio += qtd_venda
                    tipos_catalogo = [
                        produto_rio
                        for produto_rio in catalogo_rio
                        if (tokens_produto_rio := {
                            token for token in re.findall(r"[A-Z0-9]+", norm(produto_rio))
                            if token not in {"RIO", "TELEMETRIA", "PRODUTO"}
                        })
                        and tokens_produto_rio.issubset(tokens_rio)
                    ]
                    if tipos_catalogo:
                        tipo_rio = max(tipos_catalogo, key=lambda produto: len(norm(produto)))
                    elif texto_rio and texto_rio not in {
                        "X", "SIM", "S", "TRUE", "1", "ATIVO", "CONTRATADO", "CONTRATADA"
                    }:
                        tipo_rio = str(v.get("rio", "")).strip()
                    else:
                        tipos_fallback = (
                            ("diagnostico", "Diagnóstico remoto"),
                            ("performance", "Performance"),
                            ("broker", "Broker"),
                            ("bloqueio", "Bloqueio"),
                            ("geo", "GEO"),
                            ("analise", "Análise de eficiência"),
                        )
                        tipo_rio = next(
                            (nome for termo, nome in tipos_fallback if termo in texto_geral_rio),
                            "RIO (tipo não identificado)",
                        )
                    por_tipo_rio[tipo_rio] = por_tipo_rio.get(tipo_rio, 0) + qtd_venda
            # A conversão NÃO usa mais o total de planos como denominador.
            # O denominador correto é calculado abaixo, depois da leitura
            # do pipeline, somando caminhões em andamento + fechados + perdidos.
            taxa_conversao_plano = 0.0


            # ------------------------------------------------------------
            # Normalização dos negócios/pipeline
            # ------------------------------------------------------------
            fontes_negocios = (
                ("PM / RIO", "PM", neg_pm),
            )

            negocios = []
            for solucao, origem, registros in fontes_negocios:
                for r in registros:
                    vendedor = str(r.get("VENDEDOR", "")).strip()
                    dt = parse_data(r.get("DATA"))
                    temperatura = str(r.get("TEMPERATURA", "")).strip()
                    cliente = str(r.get("CLIENTE", "")).strip()
                    modelo = str(r.get("MODELO", "")).strip()
                    plano = str(r.get("PLANO DE MANUTENÇÃO", "")).strip()
                    rio = str(r.get("RIO", "")).strip()
                    produto = " / ".join([x for x in (plano, rio) if x]) or solucao

                    if not passa_pessoa(vendedor) or not passa_uf(vendedor):
                        continue
                    if not passa_data(dt):
                        continue
                    if not passa_produto(produto, origem=origem, plano=plano, rio=rio):
                        continue

                    negocios.append({
                        "origem": origem,
                        "solucao": solucao,
                        "temperatura": temperatura,
                        "temperatura_norm": norm(temperatura),
                        "cliente": cliente,
                        "modelo": modelo,
                        "produto": produto,
                        "vendedor": vendedor,
                        "data": dt,
                        "data_txt": str(r.get("DATA", "")).strip(),
                        "chassis": str(r.get("CHASSIS", "")).strip(),
                        "comentarios": str(r.get("COMENTÁRIOS", "")).strip(),
                    })

            # ------------------------------------------------------------
            # CORREÇÃO DO RIO
            # ------------------------------------------------------------
            # Em alguns registros o RIO fica gravado somente na coluna
            # "RIO" da aba Negocios_PM, enquanto na Vendas_PM o produto
            # pode trazer apenas o plano. Para não perder essas vendas,
            # usamos também o campo RIO dos negócios FECHADOS.
            #
            # Não somamos novamente uma venda que já foi identificada na
            # Vendas_PM. A chave abaixo evita duplicidade.
            chaves_rio_vendas = set()
            for v in vendas:
                texto_v = norm(" ".join([
                    str(v.get("produto", "")),
                    str(v.get("plano", "")),
                    str(v.get("rio", "")),
                    str(v.get("anexo", ""))
                ]))
                if "RIO" in texto_v or "REMOTE DIAGNOSIS" in texto_v or "PERFORMANCE" in texto_v:
                    chave_rio = (
                        norm(v.get("cliente", "")),
                        norm(v.get("modelo", "")),
                        norm(v.get("vendedor", "")),
                        v.get("data").strftime("%Y-%m-%d") if v.get("data") else ""
                    )
                    chaves_rio_vendas.add(chave_rio)

            for n in negocios:
                if n.get("temperatura_norm") != "FECHADO":
                    continue

                texto_rio_negocio = norm(str(n.get("produto", "")))
                # O campo RIO já foi incorporado ao produto em negocios.
                # Aceitamos também a descrição completa do RIO.
                if not (
                    "RIO" in texto_rio_negocio
                    or "REMOTE DIAGNOSIS" in texto_rio_negocio
                    or "PERFORMANCE" in texto_rio_negocio
                ):
                    continue

                chave_rio = (
                    norm(n.get("cliente", "")),
                    norm(n.get("modelo", "")),
                    norm(n.get("vendedor", "")),
                    n.get("data").strftime("%Y-%m-%d") if n.get("data") else ""
                )

                if chave_rio not in chaves_rio_vendas:
                    qtd_rio += 1
                    chaves_rio_vendas.add(chave_rio)
                    dados_consultor = vendas_por_consultor_mes.setdefault(
                        norm(n.get("vendedor", "")),
                        {"nome": str(n.get("vendedor", "")).strip(), "meses": {}},
                    )
                    dados_mes = dados_consultor["meses"].setdefault(
                        n["data"].month, {"pm": 0, "rio": 0},
                    )
                    dados_mes["rio"] += 1

            # ------------------------------------------------------------
            # KPIs
            # ------------------------------------------------------------
            # Quantidade de planos vendidos: vem da aba Vendas_PM.
            # Não deve ser confundida com quantidade de caminhões vendidos.
            total_unidades_planos = sum(v["qtd"] for v in vendas)
            total_registros_venda = len(vendas)

            # Base de caminhões no período: negociações filtradas mais as
            # unidades registradas em Vendas_PM, também já filtradas.
            total_pipeline = len(negocios)

            fechados_pipeline = sum(
                1 for n in negocios
                if n["temperatura_norm"] == "FECHADO"
            )
            perdidos_pipeline = sum(
                1 for n in negocios
                if n["temperatura_norm"] == "PERDIDA"
            )
            ativos_pipeline = sum(
                1 for n in negocios
                if n["temperatura_norm"] not in ("FECHADO", "PERDIDA")
            )

            # As duas fontes já passaram pelos filtros de ano, período,
            # consultor, produto e estado antes de entrarem nesta soma.
            total_vendas_caminhao = total_pipeline + total_unidades_planos

            # Meta: 20% dos caminhões do contexto filtrado devem possuir
            # PREV, MAX ou PLUS.
            meta_planos_20 = total_vendas_caminhao * 0.20
            taxa_conversao_plano = (
                total_planos_vendidos / total_vendas_caminhao * 100
                if total_vendas_caminhao > 0 else 0.0
            )

            desfechos = fechados_pipeline + perdidos_pipeline
            taxa_fechamento = (fechados_pipeline / desfechos * 100) if desfechos else 0.0

            # Comissão do Dashboard usa as mesmas regras do módulo Vendas.
            qtd_pm = sum(
                v["qtd"] for v in vendas
                if detectar_produtos_comissao(
                    v.get("produto", ""),
                    v.get("modelo", ""),
                    {"P. MANUTENÇÃO": v.get("plano", ""), "RIO": v.get("rio", "")},
                )[0]
            )
            comissao_apm_pm = qtd_pm * COMISSAO_APM_PM
            comissao_apm_rio = qtd_rio * COMISSAO_APM_RIO
            comissao_vendedor_pm = qtd_pm * COMISSAO_VENDEDOR_PM
            comissao_vendedor_rio = qtd_rio * COMISSAO_VENDEDOR_RIO
            comissao_apm_total = comissao_apm_pm + comissao_apm_rio
            comissao_vendedor_total = comissao_vendedor_pm + comissao_vendedor_rio

            # ------------------------------------------------------------
            # Séries para gráficos
            # ------------------------------------------------------------
            meses = {str(i): {"nome": MESES_PT[i], "qtd": 0, "registros": 0} for i in range(1, 13)}
            for v in vendas:
                if v["data"]:
                    k = str(v["data"].month)
                    meses[k]["qtd"] += v["qtd"]
                    meses[k]["registros"] += 1

            por_consultor = {}
            for v in vendas:
                nome = v["vendedor"] or "Sem vendedor"
                por_consultor.setdefault(nome, {"qtd": 0, "registros": 0})
                por_consultor[nome]["qtd"] += v["qtd"]
                por_consultor[nome]["registros"] += 1

            por_solucao = {}
            for v in vendas:
                por_solucao.setdefault(v["solucao"], 0)
                por_solucao[v["solucao"]] += v["qtd"]

            por_produto = {}
            for v in vendas:
                por_produto.setdefault(v["produto"] or "Não informado", 0)
                por_produto[v["produto"] or "Não informado"] += v["qtd"]

            por_modelo = {}
            for v in vendas:
                por_modelo.setdefault(v["modelo"] or "Não informado", 0)
                por_modelo[v["modelo"] or "Não informado"] += v["qtd"]

            # Resumo comercial por modalidade de manutenção.
            por_modalidade = {"PREV": 0, "MAX": 0, "PLUS": 0}
            for v in vendas:
                texto_plano = norm(v.get("plano") or v.get("produto") or "")
                if "PREV" in texto_plano:
                    por_modalidade["PREV"] += v["qtd"]
                elif "MAX" in texto_plano:
                    por_modalidade["MAX"] += v["qtd"]
                elif "PLUS" in texto_plano:
                    por_modalidade["PLUS"] += v["qtd"]

            por_uf = {"PE": 0, "AL": 0, "Não identificado": 0}
            for v in vendas:
                uf = mapa_regiao.get(norm(v["vendedor"]), "Não identificado")
                por_uf[uf] = por_uf.get(uf, 0) + v["qtd"]

            por_temp = {
                "Super Quente": 0, "Quente": 0, "Morno": 0,
                "Frio": 0, "Perdida": 0, "Fechado": 0
            }
            for n in negocios:
                t = n["temperatura_norm"]
                if t == "SUPER QUENTE":
                    por_temp["Super Quente"] += 1
                elif t == "QUENTE":
                    por_temp["Quente"] += 1
                elif t == "MORNO":
                    por_temp["Morno"] += 1
                elif t == "FRIO":
                    por_temp["Frio"] += 1
                elif t == "PERDIDA":
                    por_temp["Perdida"] += 1
                elif t == "FECHADO":
                    por_temp["Fechado"] += 1

            # Clientes ativos há mais tempo, usando a data do negócio.
            hoje = datetime.now()
            aging = {"0-30 dias": 0, "31-60 dias": 0, "61-90 dias": 0, "+90 dias": 0}
            negocios_ativos = []
            for n in negocios:
                if n["temperatura_norm"] in ("FECHADO", "PERDIDA"):
                    continue
                negocios_ativos.append(n)
                if n["data"]:
                    dias = max(0, (hoje - n["data"]).days)
                    if dias <= 30:
                        aging["0-30 dias"] += 1
                    elif dias <= 60:
                        aging["31-60 dias"] += 1
                    elif dias <= 90:
                        aging["61-90 dias"] += 1
                    else:
                        aging["+90 dias"] += 1

            # ------------------------------------------------------------
            # Dados JSON enviados uma única vez para o navegador.
            # ------------------------------------------------------------
            dashboard_data = {
                "resumo": {
                    "unidades": total_unidades_planos,
                    "vendas": total_registros_venda,
                    "total_planos_vendidos": total_planos_vendidos,
                    "total_vendas_caminhao": total_vendas_caminhao,
                    "meta_planos_20": round(meta_planos_20, 1),
                    "pipeline": total_pipeline,
                    "ativos": ativos_pipeline,
                    "fechados_pipeline": fechados_pipeline,
                    "perdidos_pipeline": perdidos_pipeline,
                    "taxa_fechamento": round(taxa_fechamento, 1),
                    "comissao_apm_pm": round(comissao_apm_pm, 2),
                    "comissao_apm_rio": round(comissao_apm_rio, 2),
                    "comissao_apm_total": round(comissao_apm_total, 2),
                    "comissao_vendedor_pm": round(comissao_vendedor_pm, 2),
                    "comissao_vendedor_rio": round(comissao_vendedor_rio, 2),
                    "comissao_vendedor_total": round(comissao_vendedor_total, 2),
                    "duplicatas_ignoradas": duplicatas,
                },
                "meses": meses,
                "consultores": por_consultor,
                "solucoes": por_solucao,
                "produtos": por_produto,
                "modelos": por_modelo,
                "rio_tipos": por_tipo_rio,
                "modalidades": por_modalidade,
                "ufs": por_uf,
                "temperaturas": por_temp,
                "aging": aging,
            }
            json_dash = json.dumps(dashboard_data, ensure_ascii=False).replace("</", "<\\/")

            anos_ordenados = sorted(anos, reverse=True) or [ano_atual]
            op_anos = "".join(
                f'<option value="{html.escape(a)}" {"selected" if a == filtro_ano else ""}>{html.escape(a)}</option>'
                for a in anos_ordenados
            )

            op_mes = f'<option value="" {"selected" if not filtro_mes else ""}>Ano inteiro</option>'
            op_mes += f'<option value="S1" {"selected" if filtro_mes == "S1" else ""}>1º semestre</option>'
            op_mes += f'<option value="S2" {"selected" if filtro_mes == "S2" else ""}>2º semestre</option>'
            for m, nome_m in MESES_PT.items():
                op_mes += f'<option value="{m}" {"selected" if filtro_mes == str(m) else ""}>{nome_m.capitalize()}</option>'

            op_consultores = '<option value="">Todos os consultores</option>'
            if not is_gestao:
                op_consultores = f'<option value="{html.escape(usuario_logado)}" selected>{html.escape(usuario_logado)}</option>'
            else:
                for c in consultores:
                    op_consultores += (
                        f'<option value="{html.escape(c)}" '
                        f'{"selected" if norm(c) == norm(filtro_consultor) else ""}>{html.escape(c)}</option>'
                    )

            # Produto: catálogo real das abas PM e RIO.
            # Usamos prefixo técnico no value para diferenciar nomes iguais
            # que eventualmente existam nas duas abas.
            op_produtos = '<option value="">Todos os produtos</option>'

            if catalogo_pm:
                op_produtos += '<optgroup label="Plano de Manutenção — aba PM">'
                for p_nome in catalogo_pm:
                    valor = "PM|" + p_nome
                    selecionado = norm(valor) == norm(filtro_produto)
                    op_produtos += (
                        f'<option value="{html.escape(valor)}" '
                        f'{"selected" if selecionado else ""}>{html.escape(p_nome)}</option>'
                    )
                op_produtos += '</optgroup>'

            if catalogo_rio:
                op_produtos += '<optgroup label="Telemetria RIO — aba RIO">'
                for p_nome in catalogo_rio:
                    valor = "RIO|" + p_nome
                    selecionado = norm(valor) == norm(filtro_produto)
                    op_produtos += (
                        f'<option value="{html.escape(valor)}" '
                        f'{"selected" if selecionado else ""}>{html.escape(p_nome)}</option>'
                    )
                op_produtos += '</optgroup>'

            op_uf = ""
            for uf in ("", "PE", "AL"):
                rotulo = "Todos os estados" if not uf else uf
                op_uf += f'<option value="{uf}" {"selected" if (filtro_uf == uf or (not filtro_uf and not uf)) else ""}>{rotulo}</option>'
            if not is_gestao:
                op_uf = f'<option value="{mapa_regiao.get(norm(usuario_logado), "")}" selected>{mapa_regiao.get(norm(usuario_logado), "Não identificado")}</option>'

            titulo_visao = (
                f"Visão gerencial consolidada — {perfil_usuario}"
                if is_gestao
                else f"Visão individual — {usuario_logado}"
            )

            linhas_vendas = ""
            for v in vendas[:150]:
                anexo = v["anexo"]
                link_anexo = (
                    f'<a href="{html.escape(anexo)}" target="_blank" rel="noopener noreferrer" '
                    f'class="dash-link">Comprovante</a>'
                    if anexo and anexo.lower() != "nan" else "-"
                )
                linhas_vendas += f"""
                <tr>
                    <td>{html.escape(v["cliente"] or "-")}</td>
                    <td>{html.escape(v["contrato"] or "-")}</td>
                    <td>{html.escape(v["modelo"] or "-")}</td>
                    <td>{html.escape(v["plano"] or "-")}</td>
                    <td>{html.escape(v["rio"] or "-")}</td>
                    <td class="num">{v["qtd"]}</td>
                    <td>{html.escape(v["vendedor"] or "-")}</td>
                    <td>{html.escape(v["data_txt"] or "-")}</td>
                    <td>{link_anexo}</td>
                </tr>
                """
            if not linhas_vendas:
                linhas_vendas = '<tr><td colspan="9" class="empty">Nenhuma venda encontrada para os filtros atuais.</td></tr>'

            linhas_pipeline = ""
            for n in sorted(negocios_ativos, key=lambda x: x["data"] or datetime.min, reverse=True)[:100]:
                linhas_pipeline += f"""
                <tr>
                    <td><span class="status status-{norm(n["temperatura"]).replace(" ", "-").lower()}">{html.escape(n["temperatura"] or "-")}</span></td>
                    <td>{html.escape(n["cliente"] or "-")}</td>
                    <td>{html.escape(n["modelo"] or "-")}</td>
                    <td>{html.escape(n["produto"] or "-")}</td>
                    <td>{html.escape(n["vendedor"] or "-")}</td>
                    <td>{html.escape(n["data_txt"] or "-")}</td>
                </tr>
                """
            if not linhas_pipeline:
                linhas_pipeline = '<tr><td colspan="6" class="empty">Nenhum negócio ativo encontrado.</td></tr>'

            # Tabela de preços: separa visualmente PREV, MAX e PLUS.
            linhas_top3_planos = ""
            ultimo_plano = None
            for p_plano in top3_planos:
                plano_atual = str(p_plano.get("plano", "")).strip().upper()
                if ultimo_plano is not None and plano_atual != ultimo_plano:
                    linhas_top3_planos += "<tr class=\"plano-separador\"><td colspan=\"7\"></td></tr>"
                intervalo = p_plano.get("horas") if p_plano["unidade"] == "HORA" else p_plano.get("km")
                unidade_intervalo = "h" if p_plano["unidade"] == "HORA" else "km"
                intervalo_exibicao = (
                    f"{intervalo:,.0f} {unidade_intervalo}".replace(",", ".")
                    if intervalo is not None else "-"
                )
                linhas_top3_planos += (
                    f"<tr>"
                    f"<td><b>{html.escape(str(p_plano['plano']))}</b></td>"
                    f"<td>{html.escape(str(p_plano['modelo']))}</td>"
                    f"<td class='num'>R$ {p_plano['valor']:,.2f}</td>"
                    f"<td>{html.escape(str(p_plano['periodo'] or '-'))} meses</td>"
                    f"<td>{html.escape(intervalo_exibicao)}</td>"
                    f"<td><span class='grupo-manutencao grupo-{normalizar_chave_planilha(p_plano['grupo_manutencao']).replace(' ', '-').lower()}'>{html.escape(p_plano['grupo_manutencao'])}</span></td>"
                    f"<td>{html.escape(p_plano['intervalo_revisao'])}</td>"
                    f"</tr>"
                )
                ultimo_plano = plano_atual
            if not linhas_top3_planos:
                linhas_top3_planos = '<tr><td colspan="7" class="empty">Nenhum preço mensal disponível na aba PM_Precos.</td></tr>'

            linhas_campanha_vw = ""
            ultimo_plano_campanha = None
            for preco_campanha in precos_campanha_vw:
                plano_atual = preco_campanha["plano"]
                if ultimo_plano_campanha and plano_atual != ultimo_plano_campanha:
                    linhas_campanha_vw += (
                        '<tr class="plano-separador"><td colspan="7"></td></tr>'
                    )
                km_campanha = preco_campanha["km"]
                km_exibicao = (
                    f'{km_campanha:,.0f} km'.replace(",", ".")
                    if km_campanha is not None else "-"
                )
                periodo_campanha = preco_campanha["periodo"]
                contrato_exibicao = (
                    f'{periodo_campanha:,.0f} meses'.replace(",", ".")
                    if periodo_campanha is not None else "-"
                )
                classe_grupo = normalizar_chave_planilha(
                    preco_campanha["grupo_manutencao"]
                ).replace(" ", "-").lower()
                linhas_campanha_vw += (
                    "<tr>"
                    f'<td><b>{html.escape(preco_campanha["plano"])}</b></td>'
                    f'<td>{html.escape(preco_campanha["modelo"])}</td>'
                    f'<td class="num">R$ {preco_campanha["valor"]:,.2f}</td>'
                    f'<td>{html.escape(contrato_exibicao)}</td>'
                    f'<td>{html.escape(km_exibicao)}</td>'
                    f'<td><span class="grupo-manutencao grupo-{classe_grupo}">'
                    f'{html.escape(preco_campanha["grupo_manutencao"])}</span></td>'
                    f'<td>{html.escape(preco_campanha["intervalo_revisao"])}</td>'
                    "</tr>"
                )
                ultimo_plano_campanha = plano_atual

            quadro_campanha_vw = ""
            if linhas_campanha_vw:
                quadro_campanha_vw = f"""
                <div class="dash-table-card" style="margin-bottom:14px">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                        <h3 style="margin:0;">🎯 Outubro Volks | Total Prev ou Max com até 70% de desconto para os Gigantes VW</h3>
                        <span class="dash-note" style="margin:0;">Valores promocionais da campanha</span>
                    </div>
                    <div class="dash-table-scroll" style="margin-top:10px;">
                        <table class="dash-table dash-table-precos">
                            <thead>
                                <tr>
                                    <th>Plano</th>
                                    <th>Modelo</th>
                                    <th>Valor mensal</th>
                                    <th>Contrato</th>
                                    <th>KM / Horas</th>
                                    <th>Grupo de manutenção</th>
                                    <th>Intervalo de revisão</th>
                                </tr>
                            </thead>
                            <tbody>{linhas_campanha_vw}</tbody>
                        </table>
                    </div>
                </div>
                """

            vendedores_tabela = {}
            if not filtro_mes:
                vendedores_tabela.update({
                    norm(nome): nome
                    for nome in consultores
                    if passa_pessoa(nome) and passa_uf(nome)
                })
            for chave_consultor, dados_consultor in vendas_por_consultor_mes.items():
                vendedores_tabela.setdefault(chave_consultor, dados_consultor["nome"])

            linhas_vendas_mensais = ""
            for chave_consultor in sorted(
                vendedores_tabela,
                key=lambda chave: norm(vendedores_tabela[chave]),
            ):
                nome_consultor = vendedores_tabela[chave_consultor]
                dados_meses = vendas_por_consultor_mes.get(
                    chave_consultor, {"meses": {}},
                )["meses"]
                celulas_mes = ""
                for numero_mes in range(1, 13):
                    dados_mes = dados_meses.get(numero_mes, {"pm": 0, "rio": 0})
                    celulas_mes += (
                        f'<td class="num {"has-sales" if dados_mes["pm"] else "no-sales"}">{dados_mes["pm"]}</td>'
                        f'<td class="num {"has-sales" if dados_mes["rio"] else "no-sales"}">{dados_mes["rio"]}</td>'
                    )
                linhas_vendas_mensais += (
                    f'<tr><th scope="row">{html.escape(nome_consultor or "Sem consultor")}</th>'
                    f'{celulas_mes}</tr>'
                )

            if not linhas_vendas_mensais:
                linhas_vendas_mensais = (
                    '<tr><td class="empty" colspan="25">'
                    'Nenhum consultor com vendas encontrado para os filtros atuais.'
                    '</td></tr>'
                )

            cabecalho_meses_vendas = "".join(
                f'<th colspan="2">{html.escape(MESES_PT[numero_mes].capitalize())}</th>'
                for numero_mes in range(1, 13)
            )
            subcabecalho_meses_vendas = (
                '<th>PM</th><th>RIO</th>' * 12
            )

            html_dashboard = f"""
            <style>
                .dash-wrap{{max-width:1500px;margin:0 auto;padding:4px 0 40px}}
                .dash-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:16px}}
                .dash-head h2{{margin:0;color:#002244;font-size:23px;font-weight:800}}
                .dash-head p{{margin:5px 0 0;color:#64748b;font-size:13px}}
                .dash-badge{{background:#eef6ff;color:#155e9b;border:1px solid #bfdbfe;border-radius:999px;padding:7px 12px;font-size:11px;font-weight:800}}
                .dash-filters{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;margin-bottom:16px;box-shadow:0 2px 6px rgba(15,23,42,.04)}}
                .dash-filters-grid{{display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:10px}}
                .dash-filters label{{display:block;font-size:10px;font-weight:800;color:#64748b;text-transform:uppercase;margin-bottom:5px}}
                .dash-filters select{{font-size:13px;padding:9px 10px;background:#f8fafc}}
                .dash-actions{{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}}
                .dash-btn{{border:0;border-radius:7px;padding:9px 12px;font-weight:700;font-size:12px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:5px}}
                .dash-btn-primary{{background:#002244;color:#fff}}
                .dash-btn-light{{background:#f1f5f9;color:#334155;border:1px solid #cbd5e1}}
                .dash-kpis{{display:grid;grid-template-columns:repeat(6,minmax(145px,1fr));gap:10px;margin-bottom:16px}}
                .dash-kpi{{background:#fff;border:1px solid #e2e8f0;border-radius:11px;padding:14px;box-shadow:0 2px 6px rgba(15,23,42,.04);min-height:95px}}
                .dash-kpi small{{display:block;color:#64748b;font-size:10px;font-weight:800;text-transform:uppercase}}
                .dash-kpi strong{{display:block;color:#0f172a;font-size:25px;line-height:1.1;margin-top:7px}}
                .dash-kpi span{{font-size:11px;color:#94a3b8}}
                .dash-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:14px}}
                .dash-card{{background:#fff;border:1px solid #e2e8f0;border-radius:11px;padding:14px;box-shadow:0 2px 6px rgba(15,23,42,.04);min-width:0}}
                .dash-card h3{{font-size:13px;color:#002244;margin:0 0 10px;font-weight:800}}
                .dash-chart{{height:280px;position:relative}}
                .dash-table-card{{background:#fff;border:1px solid #e2e8f0;border-radius:11px;padding:14px;margin-bottom:14px}}
                .dash-table-scroll{{overflow:auto;max-height:460px}}
                .dash-table{{width:100%;border-collapse:collapse;font-size:12px;min-width:780px}}
                .dash-table th{{position:sticky;top:0;background:#002244;color:#fff;padding:9px;text-align:left;z-index:1}}
                .dash-table td{{padding:9px;border-bottom:1px solid #eef2f7;color:#334155}}
                .dash-table-precos th:nth-child(3),.dash-table-precos td:nth-child(3){{text-align:center}}
                .dash-table tr.plano-separador td{{padding:0;height:8px;background:#f1f5f9;border-bottom:1px solid #cbd5e1}}
                .dash-table .num{{text-align:center;font-weight:800}}
                .grupo-manutencao{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:800;white-space:nowrap}}
                .grupo-rodoviario{{background:#dbeafe;color:#1e40af}}
                .grupo-misto{{background:#fef3c7;color:#92400e}}
                .grupo-severo{{background:#fee2e2;color:#991b1b}}
                .grupo-especial{{background:#ede9fe;color:#5b21b6}}
                .dash-monthly-scroll{{overflow:auto;max-height:560px;border:1px solid #e2e8f0;border-radius:8px}}
                .dash-monthly-table{{width:max-content;min-width:100%;border-collapse:separate;border-spacing:0;font-size:11px}}
                .dash-monthly-table th,.dash-monthly-table td{{min-width:52px;padding:8px 7px;border-bottom:1px solid #e2e8f0;border-right:1px solid #eef2f7;text-align:center;white-space:nowrap}}
                .dash-monthly-table thead th{{position:sticky;top:0;z-index:2;background:#002244;color:#fff}}
                .dash-monthly-table thead tr:nth-child(2) th{{top:32px;background:#155e9b}}
                .dash-monthly-table thead tr:first-child th:first-child{{left:0;z-index:4;min-width:165px;text-align:left}}
                .dash-monthly-table tbody th{{position:sticky;left:0;z-index:1;min-width:165px;background:#f8fafc;color:#334155;text-align:left}}
                .dash-monthly-table tbody tr:nth-child(even) td{{background:#f8fafc}}
                .dash-monthly-table tbody .num{{font-variant-numeric:tabular-nums}}
                .dash-monthly-table tbody .has-sales{{background:#dcfce7!important;color:#166534;font-weight:900;box-shadow:inset 0 0 0 1px #86efac}}
                .dash-monthly-table tbody .no-sales{{color:#cbd5e1;font-weight:500}}
                .dash-link{{color:#0066cc;font-weight:700;text-decoration:none}}
                .empty{{text-align:center;color:#94a3b8;padding:22px!important}}
                .status{{display:inline-block;border-radius:999px;padding:4px 7px;font-size:10px;font-weight:800;background:#f1f5f9}}
                .status-super-quente{{background:#fee2e2;color:#991b1b}}
                .status-quente{{background:#ffedd5;color:#9a3412}}
                .status-morno{{background:#fef3c7;color:#92400e}}
                .status-frio{{background:#e2e8f0;color:#475569}}
                .dash-note{{font-size:11px;color:#64748b;margin-top:8px;line-height:1.45}}
                .dash-mini-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
                .dash-mini{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px}}
                .dash-mini b{{display:block;color:#002244;font-size:18px}}
                .dash-mini span{{font-size:10px;color:#64748b}}
                @media(max-width:1100px){{.dash-kpis{{grid-template-columns:repeat(3,1fr)}}.dash-filters-grid{{grid-template-columns:repeat(3,1fr)}}}}
                @media(max-width:760px){{.dash-grid{{grid-template-columns:1fr}}.dash-kpis{{grid-template-columns:repeat(2,1fr)}}.dash-filters-grid{{grid-template-columns:1fr 1fr}}}}
                @media(max-width:480px){{.dash-kpis{{grid-template-columns:1fr 1fr}}.dash-filters-grid{{grid-template-columns:1fr}}.dash-head h2{{font-size:19px}}}}
            </style>

            <div class="dash-wrap">
                <div class="dash-head">
                    <div>
                        <h2>📊 Dashboard Executivo</h2>
                        <p>{html.escape(titulo_visao)} · dados consolidados de Plano de Manutenção e RIO. O filtro Produto usa o catálogo das abas PM e RIO.</p>
                    </div>
                    <div class="dash-badge">{"GESTÃO GLOBAL" if is_gestao else "ACESSO INDIVIDUAL"}</div>
                </div>

                <form class="dash-filters" method="GET" action="/modulo/dashboard">
                    <div class="dash-filters-grid">
                        <div><label>Ano</label><select name="ano" onchange="this.form.submit()">{op_anos}</select></div>
                        <div><label>Período</label><select name="mes" onchange="this.form.submit()">{op_mes}</select></div>
                        <div><label>Consultor</label><select name="consultor" {"disabled" if not is_gestao else ""} onchange="this.form.submit()">{op_consultores}</select></div>
                        <div><label>Produto</label><select name="produto" onchange="this.form.submit()">{op_produtos}</select></div>
                        <div><label>Estado</label><select name="uf" {"disabled" if not is_gestao else ""} onchange="this.form.submit()">{op_uf}</select></div>
                    </div>
                    {"<input type='hidden' name='consultor' value='" + html.escape(usuario_logado) + "'>" if not is_gestao else ""}
                    {"<input type='hidden' name='uf' value='" + html.escape(mapa_regiao.get(norm(usuario_logado), "")) + "'>" if not is_gestao else ""}
                    <div class="dash-actions">
                        <a class="dash-btn dash-btn-light" href="/modulo/dashboard">↺ Limpar filtros</a>
                        <span class="dash-note">O filtro por consultor é aplicado no servidor. Consultores não recebem dados de outros usuários.</span>
                    </div>
                </form>

                <div class="dash-kpis" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
                    <div class="dash-kpi"><small>Base de Caminhões</small><strong>{total_vendas_caminhao}</strong><span>Negociações + unidades em Vendas PM</span></div>
                    <div class="dash-kpi"><small>Total de Planos Vendidos</small><strong>{total_planos_vendidos}</strong><span>PREV + MAX + PLUS</span></div>
                    <div class="dash-kpi"><small>Planos Prev</small><strong>{qtd_prev}</strong><span>modalidade Prev</span></div>
                    <div class="dash-kpi"><small>Planos Max</small><strong>{qtd_max}</strong><span>modalidade Max</span></div>
                    <div class="dash-kpi"><small>Planos Plus</small><strong>{qtd_plus}</strong><span>modalidade Plus</span></div>
                    <div class="dash-kpi"><small>Telemetria RIO</small><strong>{qtd_rio}</strong><span>vendas com RIO</span></div>
                    <div class="dash-kpi" style="border-left: 4px solid {'#38a169' if taxa_conversao_plano >= 20 else '#e53e3e'};">
                        <small>Conversão em Plano</small>
                        <strong>{taxa_conversao_plano:.1f}%</strong>
                        <span>meta mínima: 20%</span>
                    </div>
                </div>

                <div class="dash-table-card">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
                        <h3 style="margin:0;color:#002244;font-size:15px;">📅 Vendas mensais por consultor</h3>
                        <span class="dash-note" style="margin:0;">Quantidades de planos de manutenção (PM) e RIO</span>
                    </div>
                    <div class="dash-monthly-scroll">
                        <table class="dash-monthly-table">
                            <thead>
                                <tr><th rowspan="2">Consultor</th>{cabecalho_meses_vendas}</tr>
                                <tr>{subcabecalho_meses_vendas}</tr>
                            </thead>
                            <tbody>{linhas_vendas_mensais}</tbody>
                        </table>
                    </div>
                    <div class="dash-note">Os valores respeitam os filtros de ano, período, consultor, produto e estado. Em períodos parciais, são exibidos os consultores com vendas no recorte.</div>
                </div>

                {quadro_campanha_vw}

                <div class="dash-table-card card-planos-manutencao" style="margin-bottom:14px">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                        <h3 style="margin:0;">💰 Melhor preço para plano de manutenção</h3>
                        <span class="dash-note" style="margin:0;">3 modelos com menor preço mensal em PREV · MAX · PLUS</span>
                    </div>
                    <div class="dash-table-scroll" style="margin-top:10px;">
                        <table class="dash-table dash-table-precos">
                            <thead>
                                <tr>
                                    <th>Plano</th>
                                    <th>Modelo</th>
                                    <th>Valor mensal</th>
                                    <th>Contrato</th>
                                    <th>KM / Horas</th>
                                    <th>Grupo de manutenção</th>
                                    <th>Intervalo de revisão</th>
                                </tr>
                            </thead>
                            <tbody>
                                {linhas_top3_planos}
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="dash-card" style="margin-bottom:14px">
                    <h3>🔎 Distribuição rápida do pipeline</h3>
                    <div class="dash-mini-grid">
                        <div class="dash-mini"><b>{por_temp["Super Quente"]}</b><span>Super Quente</span></div>
                        <div class="dash-mini"><b>{por_temp["Quente"]}</b><span>Quente</span></div>
                        <div class="dash-mini"><b>{por_temp["Morno"]}</b><span>Morno</span></div>
                        <div class="dash-mini"><b>{por_temp["Frio"]}</b><span>Frio</span></div>
                    </div>
                    <div class="dash-note">A classificação acima é baseada na coluna TEMPERATURA existente nas abas de negócios.</div>
                </div>

                <div class="dash-grid">
                    <div class="dash-card"><h3>📈 Evolução mensal de unidades</h3><div class="dash-chart"><canvas id="dashMes"></canvas></div></div>
                    <div class="dash-card"><h3>👥 Vendas por consultor</h3><div class="dash-chart"><canvas id="dashConsultor"></canvas></div></div>
                    <div class="dash-card"><h3>📊 Vendas por plano de manutenção</h3><div class="dash-chart"><canvas id="dashSolucao"></canvas></div></div>
                    <div class="dash-card"><h3>📡 Vendas de RIO por tipo</h3><div class="dash-chart"><canvas id="dashRioTipos"></canvas></div></div>
                    <div class="dash-card"><h3>🔥 Temperatura do pipeline</h3><div class="dash-chart"><canvas id="dashTemp"></canvas></div></div>
                    <div class="dash-card"><h3>⏱️ Aging dos negócios ativos</h3><div class="dash-chart"><canvas id="dashAging"></canvas></div></div>
                </div>

                <div class="dash-table-card">
                    <h3>🧾 Últimas vendas do filtro</h3>
                    <div class="dash-table-scroll">
                        <table class="dash-table">
                            <thead><tr><th>Cliente</th><th>Contrato</th><th>Modelo</th><th>Plano de Manutenção</th><th>Produto RIO</th><th>Qtd.</th><th>Consultor</th><th>Data</th><th>Anexo</th></tr></thead>
                            <tbody>{linhas_vendas}</tbody>
                        </table>
                    </div>
                    <div class="dash-note">Exibindo até 150 registros nesta tela para preservar velocidade. Os KPIs usam todo o conjunto filtrado.</div>
                </div>

                <div class="dash-table-card">
                    <h3>🎯 Negócios ativos para acompanhamento</h3>
                    <div class="dash-table-scroll">
                        <table class="dash-table">
                            <thead><tr><th>Status</th><th>Cliente</th><th>Modelo</th><th>Produto</th><th>Consultor</th><th>Data</th></tr></thead>
                            <tbody>{linhas_pipeline}</tbody>
                        </table>
                    </div>
                    <div class="dash-note">Exibindo até 100 negócios ativos, ordenados pelos mais recentes.</div>
                </div>
            </div>

            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script>
            (function(){{
                const D = {json_dash};
                const byId = id => document.getElementById(id);
                const common = {{
                    responsive:true,
                    maintainAspectRatio:false,
                    plugins:{{legend:{{position:'bottom',labels:{{font:{{size:10}}}}}}}}
                }};

                const meses = Object.keys(D.meses).map(Number);
                new Chart(byId('dashMes'), {{
                    type:'line',
                    data:{{
                        labels:meses.map(m => D.meses[String(m)].nome.slice(0,3)),
                        datasets:[{{label:'Unidades',data:meses.map(m=>D.meses[String(m)].qtd),tension:.3,fill:false}}]
                    }},
                    options:common
                }});

                const consultores = Object.keys(D.consultores)
                    .filter(x => Number(D.consultores[x].qtd) > 0)
                    .sort((a,b)=>Number(D.consultores[b].qtd)-Number(D.consultores[a].qtd));
                new Chart(byId('dashConsultor'), {{
                    type:'bar',
                    data:{{
                        labels:consultores,
                        datasets:[{{label:'Unidades',data:consultores.map(x=>Number(D.consultores[x].qtd))}}]
                    }},
                    options:{{
                        ...common,
                        scales:{{y:{{beginAtZero:true,ticks:{{precision:0}}}}}},
                        plugins:{{legend:{{display:false}}}}
                    }}
                }});

                const modalidades = ['PREV','MAX','PLUS'];
                new Chart(byId('dashSolucao'), {{
                    type:'bar',
                    data:{{
                        labels:modalidades,
                        datasets:[{{label:'Unidades',data:modalidades.map(x=>Number((D.modalidades || {{}})[x] || 0))}}]
                    }},
                    options:{{
                        ...common,
                        scales:{{y:{{beginAtZero:true,ticks:{{precision:0}}}}}},
                        plugins:{{legend:{{display:false}}}}
                    }}
                }});

                const tiposRio = Object.keys(D.rio_tipos || {{}})
                    .filter(x => Number(D.rio_tipos[x]) > 0)
                    .sort((a,b)=>Number(D.rio_tipos[b])-Number(D.rio_tipos[a]))
                    .slice(0,10);
                new Chart(byId('dashRioTipos'), {{
                    type:'bar',
                    data:{{
                        labels:tiposRio,
                        datasets:[{{label:'Unidades RIO',data:tiposRio.map(x=>Number(D.rio_tipos[x]))}}]
                    }},
                    options:{{
                        ...common,
                        scales:{{y:{{beginAtZero:true,ticks:{{precision:0}}}}}},
                        plugins:{{legend:{{display:false}}}}
                    }}
                }});

                const temps = Object.keys(D.temperaturas);
                new Chart(byId('dashTemp'), {{
                    type:'doughnut',
                    data:{{labels:temps,datasets:[{{data:temps.map(x=>D.temperaturas[x])}}]}},
                    options:common
                }});

                const aging = Object.keys(D.aging);
                new Chart(byId('dashAging'), {{
                    type:'bar',
                    data:{{labels:aging,datasets:[{{label:'Negócios',data:aging.map(x=>D.aging[x])}}]}},
                    options:{{...common,plugins:{{legend:{{display:false}}}}}}
                }});
            }})();
            </script>
            """
            conteudo = html_dashboard

        except Exception as e:
            traceback.print_exc()
            conteudo = f'<div style="color:#c53030;background:#fff5f5;padding:20px;border-radius:8px;border:1px solid #feb2b2"><h3>Erro ao carregar o Dashboard</h3><p>{html.escape(str(e))}</p></div>'

    elif nome_modulo == "pedidos":
        erro_pedido = ""
        erro_lista_pedidos = ""
        pedido_edicao_id = str(
            request.form.get("id_pedido_edicao", "")
            if request.method == "POST"
            else request.args.get("editar", "")
        ).strip()
        mostrar_pedidos_feitos = (
            request.args.get("visao", "").strip().lower() == "feitos"
            and not pedido_edicao_id
        )
        pedidos_feitos = []
        valores_pedido = request.form if request.method == "POST" else {}
        vendedor_pedido = str(session.get("nome", "Usuário") or "Usuário")
        telefone_vendedor = str(session.get("telefone", "") or "")
        celular_vendedor = str(session.get("celular", "") or "")
        email_vendedor = str(session.get("email_usuario", "") or "")
        try:
            planilha_pedidos = conectar_google_sheets()
            registros_modelos_pedido = obter_registros_com_cache(
                planilha_pedidos,
                "Modelos",
                ttl=0,
                falhar_em_erro=True,
            )
        except Exception as e:
            traceback.print_exc()
            registros_modelos_pedido = []
            erro_pedido = (
                "Não foi possível carregar os modelos. "
                f"Detalhe: {html.escape(str(e))}"
            )

        registros_opcoes_pedido = []
        registros_pm_pedido = []
        registros_rio_pedido = []
        if not erro_pedido:
            try:
                registros_opcoes_pedido = obter_registros_com_cache(
                    planilha_pedidos,
                    "Form_Pedido",
                    ttl=0,
                    falhar_em_erro=True,
                )
                registros_pm_pedido = obter_registros_com_cache(
                    planilha_pedidos,
                    "PM",
                    ttl=0,
                    falhar_em_erro=True,
                )
                registros_rio_pedido = obter_registros_com_cache(
                    planilha_pedidos,
                    "RIO",
                    ttl=0,
                    falhar_em_erro=True,
                )
            except Exception as e:
                traceback.print_exc()
                erro_pedido = (
                    "Não foi possível carregar as opções das abas Form_Pedido, PM e RIO. "
                    f"Detalhe: {html.escape(str(e))}"
                )

        def opcoes_produtos_aba_pedido(registros, nome_aba):
            opcoes = []
            valores_normalizados = set()
            for registro in registros:
                produto = next(
                    (
                        str(valor or "").strip()
                        for chave, valor in reversed(list(registro.items()))
                        if (
                            normalizar_chave_planilha(chave) == "produto"
                            or re.fullmatch(
                                r"produto \d+",
                                normalizar_chave_planilha(chave),
                            )
                        )
                        and str(valor or "").strip()
                    ),
                    "",
                )
                produto_normalizado = normalizar_chave_planilha(produto)
                if (
                    produto
                    and produto_normalizado != "produto"
                    and produto_normalizado not in valores_normalizados
                ):
                    opcoes.append((produto, produto))
                    valores_normalizados.add(produto_normalizado)
            if not opcoes:
                raise ValueError(
                    f"A aba {nome_aba} não contém opções preenchidas na coluna PRODUTO."
                )
            return opcoes

        opcoes_plano_pedido = []
        opcoes_rio_pedido = []
        if not erro_pedido:
            try:
                opcoes_plano_pedido = opcoes_produtos_aba_pedido(
                    registros_pm_pedido,
                    "PM",
                )
                opcoes_rio_aba = opcoes_produtos_aba_pedido(
                    registros_rio_pedido,
                    "RIO",
                )
                opcoes_rio_pedido = [
                    opcao
                    for opcao in opcoes_rio_aba
                    if normalizar_chave_planilha(opcao[0]) != "nao"
                ]
            except ValueError as e:
                erro_pedido = html.escape(str(e))

        def opcoes_coluna_form_pedido(*nomes_coluna):
            colunas_normalizadas = {
                normalizar_chave_planilha(nome) for nome in nomes_coluna
            }
            opcoes = []
            valores_normalizados = set()
            for registro in registros_opcoes_pedido:
                for coluna, valor in registro.items():
                    coluna_normalizada = normalizar_chave_planilha(coluna)
                    if not (
                        coluna_normalizada in colunas_normalizadas
                        or any(
                            re.fullmatch(
                                rf"{re.escape(nome)} \d+",
                                coluna_normalizada,
                            )
                            for nome in colunas_normalizadas
                        )
                    ):
                        continue
                    valor = str(valor or "").strip()
                    valor_normalizado = normalizar_chave_planilha(valor)
                    if valor and valor_normalizado not in valores_normalizados:
                        opcoes.append((valor, valor))
                        valores_normalizados.add(valor_normalizado)
            return opcoes

        opcoes_ano_modelo_pedido = opcoes_coluna_form_pedido(
            "FAB/MODELO",
            "ANO/MODELO",
        )
        opcoes_cabine_pedido = opcoes_coluna_form_pedido("CABINE")
        opcoes_pagamento_pedido = opcoes_coluna_form_pedido("PAGAMENTO")
        opcoes_entrega_pedido = opcoes_coluna_form_pedido("ENTREGA")
        opcoes_prazo_entrega_pedido = opcoes_coluna_form_pedido(
            "PRAZO DE ENTREGA",
            "PRAZO_ENTREGA",
        )
        opcoes_dga_pedido = opcoes_coluna_form_pedido("DGA")
        opcoes_modalidade_faturamento_pedido = opcoes_coluna_form_pedido(
            "MODALIDADE FATURAMENTO",
            "MOD. FAT.",
        )
        opcoes_faturante_pedido = opcoes_coluna_form_pedido("FATURAMENTO")
        cnpj_por_faturante_pedido = {}
        for registro in registros_opcoes_pedido:
            faturante_registro = next(
                (
                    str(valor or "").strip()
                    for chave, valor in registro.items()
                    if normalizar_chave_planilha(chave) == "faturamento"
                ),
                "",
            )
            cnpj_registro = next(
                (
                    str(valor or "").strip()
                    for chave, valor in registro.items()
                    if normalizar_chave_planilha(chave) == "cnpj"
                ),
                "",
            )
            if faturante_registro and cnpj_registro:
                cnpj_por_faturante_pedido[faturante_registro] = cnpj_registro
        if not erro_pedido:
            opcoes_ausentes = [
                rotulo
                for rotulo, opcoes in (
                    ("FAB/MODELO", opcoes_ano_modelo_pedido),
                    ("CABINE", opcoes_cabine_pedido),
                    ("Pagamento", opcoes_pagamento_pedido),
                    ("Entrega", opcoes_entrega_pedido),
                    ("Prazo de entrega", opcoes_prazo_entrega_pedido),
                    ("DGA", opcoes_dga_pedido),
                    ("Modalidade de faturamento", opcoes_modalidade_faturamento_pedido),
                    ("Faturamento", opcoes_faturante_pedido),
                )
                if not opcoes
            ]
            if opcoes_ausentes:
                erro_pedido = (
                    "Cadastre opções para "
                    f"{', '.join(opcoes_ausentes)} na aba Form_Pedido."
                )

        modelos_pedido = {}
        for indice_modelo, registro_modelo in enumerate(registros_modelos_pedido):
            nome_modelo = str(registro_modelo.get("MODELO", "") or "").strip()
            if not nome_modelo:
                continue

            tipo_modelo = str(registro_modelo.get("TIPO", "") or "").strip()
            tipo_normalizado = (
                unicodedata.normalize("NFKD", tipo_modelo)
                .encode("ascii", "ignore")
                .decode("ascii")
                .casefold()
            )
            if "onibus" in tipo_normalizado or "bus" in tipo_normalizado:
                segmento_modelo = "Ônibus"
            elif "caminh" in tipo_normalizado or "truck" in tipo_normalizado:
                segmento_modelo = "Caminhão"
            else:
                segmento_modelo = ""

            dados_normalizados = [
                (normalizar_chave_planilha(chave), str(valor or "").strip())
                for chave, valor in registro_modelo.items()
            ]

            def obter_dado_modelo(*nomes):
                for nome in nomes:
                    nome_normalizado = normalizar_chave_planilha(nome)
                    encontrados = [
                        valor
                        for chave, valor in dados_normalizados
                        if chave == nome_normalizado
                        or re.fullmatch(
                            rf"{re.escape(nome_normalizado)} \d+",
                            chave,
                        )
                    ]
                    valor = next(
                        (item for item in reversed(encontrados) if item),
                        "",
                    )
                    if valor:
                        return valor
                return ""

            imagem_modelo = str(registro_modelo.get("IMG", "") or "").strip()
            id_imagem = extrair_id_arquivo_drive(imagem_modelo)
            if id_imagem:
                imagem_modelo = url_for(
                    "servir_comprovante_drive",
                    file_id=id_imagem,
                )
            elif imagem_modelo and not re.match(r"^https?://", imagem_modelo, re.I):
                imagem_modelo = ""

            link_ficha = str(registro_modelo.get("LINK", "") or "").strip()
            id_ficha = extrair_id_arquivo_drive(link_ficha)
            if id_ficha:
                link_ficha = f"https://drive.google.com/open?id={id_ficha}"
            elif link_ficha and not re.match(r"^https?://", link_ficha, re.I):
                link_ficha = ""

            modelos_pedido[str(indice_modelo)] = {
                "id": str(indice_modelo),
                "modelo": nome_modelo,
                "tipo": tipo_modelo,
                "segmento_modelo": segmento_modelo,
                "ano_modelo": obter_dado_modelo("FAB/MOD", "ANO/MODELO"),
                "tecnologia": obter_dado_modelo("TECNOLOGIA"),
                "tecnologia_motor": obter_dado_modelo("TECNO"),
                "segmento_ficha": obter_dado_modelo("SEGMENTO"),
                "pbt": obter_dado_modelo("PBT HOMOLOGADO (KG)", "PBT"),
                "entre_eixos": obter_dado_modelo(
                    "ENTRE EIXO",
                    "ENTRE EIXOS",
                    "ENTRE EIXOS (MM)",
                    "ENTRE-EIXOS",
                ),
                "cabine": obter_dado_modelo("CABINE"),
                "motor": obter_dado_modelo("MOTOR"),
                "potencia": obter_dado_modelo("POTENCIA", "POTÊNCIA"),
                "transmissao": obter_dado_modelo(
                    "TRANSMISSAO",
                    "TRANSMISSÃO",
                    "TRANSMISAO",
                ),
                "sistema_injecao": obter_dado_modelo(
                    "SISTEMA DE INJECAO",
                    "SISTEMA DE INJEÇÃO",
                ),
                "combustivel": obter_dado_modelo("COMBUSTIVEL", "COMBUSTÍVEL"),
                "categoria": str(registro_modelo.get("CATEGORIA", "") or "").strip(),
                "descricao": str(
                    registro_modelo.get("DESCRIÇÃO")
                    or registro_modelo.get("DESCRICAO")
                    or ""
                ).strip(),
                "eficiencia": str(
                    registro_modelo.get("EFICIÊNCIA")
                    or registro_modelo.get("EFICIENCIA")
                    or ""
                ).strip(),
                "conforto": str(registro_modelo.get("CONFORTO", "") or "").strip(),
                "seguranca": str(
                    registro_modelo.get("SEGURANÇA")
                    or registro_modelo.get("SEGURANCA")
                    or registro_modelo.get("SEGURANÇA ATIVA")
                    or registro_modelo.get("SEGURANCA ATIVA")
                    or ""
                ).strip(),
                "tecnologia": obter_dado_modelo("TECNOLOGIA"),
                "link": link_ficha,
                "imagem": imagem_modelo,
                "imagem_id": id_imagem,
            }

        campos_pedido = (
            "data_pedido", "cliente", "documento_cliente", "telefone_cliente",
            "email_cliente", "cidade", "uf", "modelo", "modelo_id", "quantidade",
            "valor_unitario", "plano_manutencao", "rio", "ano_modelo",
            "tecnologia", "tecnologia_motor", "segmento_ficha",
            "segmento", "cabine", "motor", "transmissao", "pbt", "entre_eixos",
            "potencia", "sistema_injecao", "combustivel",
            "informacoes_complementares", "garantia", "assistencia",
            "condicoes_pm", "modalidade_faturamento", "faturante", "dga",
            "cnpj_faturante", "pagamento", "cod_finame", "pac",
            "classificacao_fiscal", "local_entrega", "prazo_entrega",
            "detalhes", "validade",
            "imagem_modelo_id",
        )
        pedido_edicao = None
        if pedido_edicao_id and not erro_pedido:
            try:
                pedido_edicao = next(
                    (
                        pedido
                        for pedido in listar_pedidos_vendedor(
                            planilha_pedidos,
                            email_vendedor,
                        )
                        if str(pedido.get("ID_PEDIDO", "")).strip()
                        == pedido_edicao_id
                    ),
                    None,
                )
                if pedido_edicao is None:
                    erro_pedido = (
                        "A proposta não foi encontrada ou não pertence ao seu usuário."
                    )
                elif request.method != "POST":
                    indices_pedido = {
                        normalizar_chave_planilha(chave): valor
                        for chave, valor in pedido_edicao.items()
                    }
                    campos_planilha = {
                        "tecnologia_motor": "TECNO",
                        "segmento_ficha": "SEGMENTO_FICHA",
                    }
                    valores_pedido = {}
                    for campo in campos_pedido:
                        cabecalho = campos_planilha.get(campo, campo.upper())
                        valor = ""
                        for nome_cabecalho in (
                            cabecalho,
                            *ALIASES_CABECALHOS_PEDIDOS.get(cabecalho, ()),
                        ):
                            valor = indices_pedido.get(
                                normalizar_chave_planilha(nome_cabecalho),
                                "",
                            )
                            if str(valor or "").strip():
                                break
                        valores_pedido[campo] = str(valor or "").strip()
                    for campo_data in ("data_pedido", "validade"):
                        valor_data = valores_pedido[campo_data]
                        if valor_data:
                            try:
                                valores_pedido[campo_data] = (
                                    datetime.fromisoformat(valor_data[:10])
                                    .strftime("%Y-%m-%d")
                                )
                            except ValueError:
                                for formato_data in (
                                    "%d/%m/%Y",
                                    "%d/%m/%Y %H:%M:%S",
                                ):
                                    try:
                                        valores_pedido[campo_data] = datetime.strptime(
                                            valor_data,
                                            formato_data,
                                        ).strftime("%Y-%m-%d")
                                        break
                                    except ValueError:
                                        continue
            except Exception as e:
                traceback.print_exc()
                erro_pedido = (
                    "Não foi possível carregar a proposta para edição. "
                    f"Detalhe: {html.escape(str(e))}"
                )
        dados_pedido = {
            campo: str(valores_pedido.get(campo, "") or "").strip()
            for campo in campos_pedido
        }
        agora_pedido = datetime.now()
        proximo_mes_pedido = (
            datetime(agora_pedido.year + 1, 1, 1)
            if agora_pedido.month == 12
            else datetime(agora_pedido.year, agora_pedido.month + 1, 1)
        )
        if not pedido_edicao_id or not dados_pedido["validade"]:
            dados_pedido["validade"] = (
                proximo_mes_pedido - timedelta(days=1)
            ).strftime("%Y-%m-%d")
        if request.method != "POST" and not pedido_edicao_id:
            dados_pedido["garantia"] = dados_pedido["garantia"] or (
                "Os veículos são garantidos pelo fabricante contra eventuais defeitos "
                "materiais ou montagem por 12(doze) meses, exceto materiais de consumo "
                "corrente, desde que respeitado o calendário das revisões exigidas pelo "
                "fabricante."
            )
            dados_pedido["assistencia"] = dados_pedido["assistencia"] or (
                "No decorrer do período de garantia, os clientes de caminhões têm à "
                "disposição todo o suporte para socorro mecânico de assistência "
                "emergencial, durante 24h, 7 dias por semana, através do telefone: "
                "0800 019 3333."
            )
            dados_pedido["condicoes_pm"] = dados_pedido["condicoes_pm"] or (
                "Temos à disposição dos clientes o contrato de manutenção celebrado "
                "diretamente entre o cliente e a Fábrica no momento da compra, para a "
                "prestação de serviços de manutenção e reboque em todo território "
                "nacional."
            )
        dados_pedido["data_pedido"] = (
            dados_pedido["data_pedido"] or datetime.now().strftime("%Y-%m-%d")
        )
        dados_pedido["quantidade"] = dados_pedido["quantidade"] or "1"
        opcoes_uf_pedido = [
            ("PE", "PE"), ("AL", "AL"), ("PB", "PB"), ("RN", "RN"),
            ("CE", "CE"), ("BA", "BA"), ("SE", "SE"), ("PI", "PI"),
            ("MA", "MA"), ("TO", "TO"), ("GO", "GO"), ("DF", "DF"),
            ("MG", "MG"), ("SP", "SP"), ("RJ", "RJ"), ("Outro", "Outro"),
        ]
        opcoes_editaveis_pedido = (
            (opcoes_ano_modelo_pedido, "ano_modelo"),
            (opcoes_cabine_pedido, "cabine"),
            (opcoes_pagamento_pedido, "pagamento"),
            (opcoes_entrega_pedido, "local_entrega"),
            (opcoes_prazo_entrega_pedido, "prazo_entrega"),
            (opcoes_dga_pedido, "dga"),
            (opcoes_modalidade_faturamento_pedido, "modalidade_faturamento"),
            (opcoes_faturante_pedido, "faturante"),
            (opcoes_plano_pedido, "plano_manutencao"),
            (opcoes_rio_pedido, "rio"),
            (opcoes_uf_pedido, "uf"),
        )
        for opcoes, campo in opcoes_editaveis_pedido:
            valor = dados_pedido[campo]
            if valor and all(valor != opcao[0] for opcao in opcoes):
                opcoes.append((valor, valor))

        if (
            pedido_edicao
            and dados_pedido["modelo"]
            and not any(
                item["modelo"] == dados_pedido["modelo"]
                for item in modelos_pedido.values()
            )
        ):
            segmento_salvo = (
                dados_pedido["segmento"]
                if dados_pedido["segmento"] in {"Caminhão", "Ônibus"}
                else "Caminhão"
            )
            id_modelo_salvo = f"salvo-{pedido_edicao_id}"
            modelos_pedido[id_modelo_salvo] = {
                "id": id_modelo_salvo,
                "modelo": dados_pedido["modelo"],
                "tipo": str(pedido_edicao.get("TIPO", "") or ""),
                "segmento_modelo": segmento_salvo,
                "categoria": str(pedido_edicao.get("CATEGORIA", "") or ""),
                "ano_modelo": dados_pedido["ano_modelo"],
                "tecnologia": dados_pedido["tecnologia"],
                "tecnologia_motor": dados_pedido["tecnologia_motor"],
                "segmento_ficha": dados_pedido["segmento_ficha"],
                "pbt": dados_pedido["pbt"],
                "entre_eixos": dados_pedido["entre_eixos"],
                "cabine": dados_pedido["cabine"],
                "motor": dados_pedido["motor"],
                "potencia": dados_pedido["potencia"],
                "transmissao": dados_pedido["transmissao"],
                "sistema_injecao": dados_pedido["sistema_injecao"],
                "combustivel": dados_pedido["combustivel"],
                "descricao": "",
                "eficiencia": "",
                "conforto": "",
                "seguranca": "",
                "link": str(pedido_edicao.get("LINK_FICHA_TECNICA", "") or ""),
                "imagem": "",
                "imagem_id": dados_pedido["imagem_modelo_id"],
            }
        modelo_selecionado_pedido = dados_pedido["modelo_id"]
        modelo_inicial_pedido = next(
            (
                item
                for item in modelos_pedido.values()
                if item["modelo"] == dados_pedido["modelo"]
                and item["segmento_modelo"] in {"Caminhão", "Ônibus"}
            ),
            None,
        )
        if pedido_edicao and modelo_inicial_pedido:
            modelo_selecionado_pedido = modelo_inicial_pedido["id"]
        elif request.method != "POST" and modelo_inicial_pedido:
            modelo_selecionado_pedido = modelo_inicial_pedido["id"]
        if request.method != "POST" and dados_pedido["segmento"] not in {"Caminhão", "Ônibus"}:
            dados_pedido["segmento"] = (
                modelo_inicial_pedido["segmento_modelo"]
                if modelo_inicial_pedido
                else "Caminhão"
            )

        if request.method == "POST" and not erro_pedido:
            if dados_pedido["faturante"]:
                dados_pedido["cnpj_faturante"] = cnpj_por_faturante_pedido.get(
                    dados_pedido["faturante"],
                    "",
                )
            dados_pedido["email_cliente"] = dados_pedido["email_cliente"].lower()
            digitos_telefone_cliente = re.sub(
                r"\D",
                "",
                dados_pedido["telefone_cliente"],
            )
            if len(digitos_telefone_cliente) in {10, 11}:
                dados_pedido["telefone_cliente"] = formatar_telefone_br(
                    digitos_telefone_cliente
                )
            modelo_escolhido = modelos_pedido.get(modelo_selecionado_pedido)
            if modelo_escolhido:
                dados_pedido["modelo"] = modelo_escolhido["modelo"]
            quantidade_pedido = converter_numero(dados_pedido["quantidade"])
            valor_unitario_texto = re.sub(
                r"(?i)^\s*R\$\s*",
                "",
                dados_pedido["valor_unitario"],
            ).replace(" ", "")
            if "," in valor_unitario_texto:
                valor_unitario_texto = (
                    valor_unitario_texto.replace(".", "").replace(",", ".")
                )
            elif re.fullmatch(
                r"-?\d{1,3}(?:\.\d{3})+",
                valor_unitario_texto,
            ):
                valor_unitario_texto = valor_unitario_texto.replace(".", "")
            valor_unitario_pedido = converter_numero(valor_unitario_texto)
            if not dados_pedido["cliente"]:
                erro_pedido = "Informe o nome do cliente."
            elif not dados_pedido["documento_cliente"]:
                erro_pedido = "Informe o CPF/CNPJ do cliente."
            elif not validar_documento_cliente(dados_pedido["documento_cliente"]):
                erro_pedido = "Informe um CPF ou CNPJ válido."
            elif not dados_pedido["telefone_cliente"]:
                erro_pedido = "Informe o telefone do cliente."
            elif dados_pedido["email_cliente"] and not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+",
                dados_pedido["email_cliente"],
            ):
                erro_pedido = "Informe um e-mail válido para o cliente."
            elif len(digitos_telefone_cliente) not in {10, 11}:
                erro_pedido = (
                    "Informe o telefone com DDD e 8 ou 9 dígitos."
                )
            elif dados_pedido["segmento"] not in {"Caminhão", "Ônibus"}:
                erro_pedido = "Selecione se a proposta é para Caminhão ou Ônibus."
            elif not modelo_escolhido:
                erro_pedido = "Selecione um modelo válido da lista."
            elif modelo_escolhido["segmento_modelo"] != dados_pedido["segmento"]:
                erro_pedido = "O modelo selecionado não pertence ao segmento escolhido."
            elif not dados_pedido["ano_modelo"]:
                erro_pedido = "Selecione o ano/modelo do veículo."
            elif not dados_pedido["cabine"]:
                erro_pedido = "Selecione a cabine do veículo."
            elif any((
                dados_pedido["ano_modelo"]
                and dados_pedido["ano_modelo"] not in dict(opcoes_ano_modelo_pedido),
                dados_pedido["cabine"]
                and dados_pedido["cabine"] not in dict(opcoes_cabine_pedido),
                dados_pedido["pagamento"]
                and dados_pedido["pagamento"] not in dict(opcoes_pagamento_pedido),
                dados_pedido["local_entrega"]
                and dados_pedido["local_entrega"] not in dict(opcoes_entrega_pedido),
                dados_pedido["prazo_entrega"]
                and dados_pedido["prazo_entrega"]
                not in dict(opcoes_prazo_entrega_pedido),
                dados_pedido["dga"]
                and dados_pedido["dga"] not in dict(opcoes_dga_pedido),
                dados_pedido["modalidade_faturamento"]
                and dados_pedido["modalidade_faturamento"]
                not in dict(opcoes_modalidade_faturamento_pedido),
                dados_pedido["faturante"]
                and dados_pedido["faturante"] not in dict(opcoes_faturante_pedido),
                dados_pedido["plano_manutencao"]
                and dados_pedido["plano_manutencao"] not in dict(opcoes_plano_pedido),
                dados_pedido["rio"]
                and dados_pedido["rio"] not in dict(opcoes_rio_pedido),
            )):
                erro_pedido = "Selecione opções válidas nas listas do formulário."
            elif (
                quantidade_pedido is None
                or quantidade_pedido <= 0
                or not quantidade_pedido.is_integer()
            ):
                erro_pedido = "A quantidade deve ser um número inteiro maior que zero."
            elif valor_unitario_pedido is None or valor_unitario_pedido <= 0:
                erro_pedido = "Informe um valor unitário maior que zero."
            else:
                valor_total_pedido = quantidade_pedido * valor_unitario_pedido
                id_pedido = pedido_edicao_id or (
                    f"PED-{datetime.now():%Y%m%d}-{time.time_ns()}"
                )
                modelo_escolhido["segmento"] = dados_pedido["segmento"]
                valores_salvar_pedido = {
                    "ID_PEDIDO": id_pedido,
                    "DATA_PEDIDO": dados_pedido["data_pedido"],
                    "VENDEDOR": vendedor_pedido,
                    "TELEFONE_VENDEDOR": telefone_vendedor,
                    "CELULAR_VENDEDOR": celular_vendedor,
                    "EMAIL_VENDEDOR": email_vendedor,
                    "CLIENTE": dados_pedido["cliente"],
                    "DOCUMENTO_CLIENTE": dados_pedido["documento_cliente"],
                    "TELEFONE_CLIENTE": dados_pedido["telefone_cliente"],
                    "EMAIL_CLIENTE": dados_pedido["email_cliente"],
                    "CIDADE": dados_pedido["cidade"],
                    "UF": dados_pedido["uf"],
                    "MODELO": modelo_escolhido["modelo"],
                    "SEGMENTO": dados_pedido["segmento"],
                    "TIPO": modelo_escolhido["tipo"],
                    "CATEGORIA": modelo_escolhido["categoria"],
                    "QUANTIDADE": int(quantidade_pedido),
                    "VALOR_UNITARIO": valor_unitario_pedido,
                    "VALOR_TOTAL": valor_total_pedido,
                    "PLANO_MANUTENCAO": dados_pedido["plano_manutencao"],
                    "RIO": dados_pedido["rio"],
                    "ANO_MODELO": dados_pedido["ano_modelo"],
                    "TECNOLOGIA": dados_pedido["tecnologia"],
                    "TECNO": dados_pedido["tecnologia_motor"],
                    "SEGMENTO_FICHA": dados_pedido["segmento_ficha"],
                    "CABINE": dados_pedido["cabine"],
                    "MOTOR": dados_pedido["motor"],
                    "POTENCIA": dados_pedido["potencia"],
                    "TRANSMISSAO": dados_pedido["transmissao"],
                    "SISTEMA_INJECAO": dados_pedido["sistema_injecao"],
                    "PBT": dados_pedido["pbt"],
                    "ENTRE_EIXOS": dados_pedido["entre_eixos"],
                    "COMBUSTIVEL": dados_pedido["combustivel"],
                    "INFORMACOES_COMPLEMENTARES": dados_pedido["informacoes_complementares"],
                    "GARANTIA": dados_pedido["garantia"],
                    "ASSISTENCIA": dados_pedido["assistencia"],
                    "CONDICOES_PM": dados_pedido["condicoes_pm"],
                    "MODALIDADE_FATURAMENTO": dados_pedido["modalidade_faturamento"],
                    "FATURANTE": dados_pedido["faturante"],
                    "DGA": dados_pedido["dga"],
                    "CNPJ_FATURANTE": dados_pedido["cnpj_faturante"],
                    "PAGAMENTO": dados_pedido["pagamento"],
                    "COD_FINAME": dados_pedido["cod_finame"],
                    "PAC": dados_pedido["pac"],
                    "CLASSIFICACAO_FISCAL": dados_pedido["classificacao_fiscal"],
                    "LOCAL_ENTREGA": dados_pedido["local_entrega"],
                    "PRAZO_ENTREGA": dados_pedido["prazo_entrega"],
                    "DETALHES": dados_pedido["detalhes"],
                    "VALIDADE": dados_pedido["validade"],
                    "LINK_FICHA_TECNICA": modelo_escolhido["link"],
                    "IMAGEM_MODELO_ID": modelo_escolhido["imagem_id"],
                }
                try:
                    if pedido_edicao_id:
                        atualizar_pedido_na_planilha(
                            planilha_pedidos,
                            valores_salvar_pedido,
                            pedido_edicao_id,
                        )
                    else:
                        salvar_pedido_na_planilha(
                            planilha_pedidos,
                            valores_salvar_pedido,
                        )
                    return redirect(url_for(
                        "acessar_modulo",
                        nome_modulo="pedidos",
                        salvo=id_pedido,
                        editado="1" if pedido_edicao_id else None,
                    ))
                except Exception as e:
                    traceback.print_exc()
                    erro_pedido = (
                        f"Não foi possível gravar a proposta na aba {ABA_PEDIDOS_FEITOS}. "
                        f"Detalhe: {html.escape(str(e))}"
                    )

        def campo_pedido(
            nome,
            rotulo,
            tipo="text",
            opcoes=None,
            largura=False,
            readonly=False,
        ):
            valor = dados_pedido.get(nome, "")
            classe = "pedido-campo pedido-campo-largo" if largura else "pedido-campo"
            rotulo_html = html.escape(rotulo)
            nome_html = html.escape(nome, quote=True)
            obrigatorio = nome in {
                "cliente",
                "documento_cliente",
                "telefone_cliente",
                "quantidade",
                "valor_unitario",
                "ano_modelo",
                "cabine",
            }
            classe_rotulo = " class=\"obrigatorio\"" if obrigatorio else ""
            if opcoes is not None:
                opcoes_html = ['<option value="">Selecione...</option>']
                for opcao_valor, opcao_texto in opcoes:
                    selecionado = " selected" if valor == opcao_valor else ""
                    opcoes_html.append(
                        f'<option value="{html.escape(opcao_valor, quote=True)}"'
                        f'{selecionado}>{html.escape(opcao_texto)}</option>'
                    )
                controle = (
                    f'<select id="{nome_html}" name="{nome_html}"'
                    f' data-pedido-campo="{nome_html}"'
                    f'{" required" if obrigatorio else ""}>'
                    f'{"".join(opcoes_html)}</select>'
                )
            elif tipo == "textarea":
                controle = (
                    f'<textarea id="{nome_html}" name="{nome_html}" rows="3"'
                    f' data-pedido-campo="{nome_html}">'
                    f'{html.escape(valor)}</textarea>'
                )
            else:
                extra = ""
                if obrigatorio:
                    extra = " required"
                if nome == "quantidade":
                    extra += ' min="1" step="1"'
                if nome == "valor_unitario":
                    extra += ' inputmode="numeric" autocomplete="off" placeholder="R$ 0,00"'
                if nome == "documento_cliente":
                    extra += ' inputmode="numeric" autocomplete="off" maxlength="18" placeholder="CPF ou CNPJ"'
                if nome == "telefone_cliente":
                    extra += ' inputmode="numeric" autocomplete="tel" maxlength="15" placeholder="(00) 00000-0000"'
                if nome == "email_cliente":
                    extra += ' autocomplete="email" autocapitalize="none" maxlength="254"'
                if readonly:
                    extra += " readonly"
                valor_html = html.escape(valor, quote=True)
                controle = (
                    f'<input id="{nome_html}" name="{nome_html}" type="{tipo}"'
                    f' value="{valor_html}" data-pedido-campo="{nome_html}"{extra}>'
                )
            return (
                f'<div class="{classe}"><label for="{nome_html}"{classe_rotulo}>'
                f'{rotulo_html}</label>'
                f'{controle}</div>'
            )

        campos_cliente_html = "".join((
            campo_pedido("data_pedido", "Data da proposta", "date"),
            campo_pedido("cliente", "Cliente / Razão social"),
            campo_pedido("documento_cliente", "CPF / CNPJ"),
            campo_pedido("telefone_cliente", "Telefone do cliente", "tel"),
            campo_pedido("email_cliente", "E-mail do cliente", "email"),
            campo_pedido("cidade", "Cidade"),
            campo_pedido("uf", "Estado", opcoes=opcoes_uf_pedido),
        ))
        campos_caminhao_html = "".join((
            campo_pedido("ano_modelo", "Ano / modelo", opcoes=opcoes_ano_modelo_pedido),
            campo_pedido("cabine", "Cabine", opcoes=opcoes_cabine_pedido),
        ))
        campos_tecnicos_ocultos_html = "".join(
            f'<input type="hidden" id="{nome}" name="{nome}" '
            f'value="{html.escape(dados_pedido.get(nome, ""), quote=True)}" '
            f'data-pedido-campo="{nome}">'
            for nome in (
                "tecnologia",
                "segmento_ficha",
                "tecnologia_motor",
                "motor",
                "transmissao",
                "pbt",
                "entre_eixos",
                "potencia",
                "sistema_injecao",
                "combustivel",
            )
        )
        campos_modelo_pdf_html = "".join(
            f'<input type="hidden" id="{nome}" name="{nome}" '
            f'value="{html.escape(valor, quote=True)}">'
            for nome, valor in (
                ("modelo_tipo", ""),
                ("modelo_categoria", ""),
                ("link_ficha_tecnica", ""),
                ("imagem_modelo_id", ""),
            )
        )
        campos_condicoes_html = "".join((
            campo_pedido("plano_manutencao", "Plano de manutenção", opcoes=opcoes_plano_pedido),
            campo_pedido("rio", "Telemetria RIO", opcoes=opcoes_rio_pedido),
            campo_pedido("garantia", "Garantia", "textarea", largura=True),
            campo_pedido("assistencia", "Chame Volks", "textarea", largura=True),
            campo_pedido("condicoes_pm", "VolksTotal", "textarea", largura=True),
            campo_pedido("informacoes_complementares", "Informações", "textarea", largura=True),
        ))
        campos_faturamento_html = "".join((
            campo_pedido(
                "modalidade_faturamento",
                "Modalidade de faturamento",
                opcoes=opcoes_modalidade_faturamento_pedido,
            ),
            campo_pedido("faturante", "Faturante", opcoes=opcoes_faturante_pedido),
            campo_pedido("cnpj_faturante", "CNPJ do faturante", readonly=True),
            campo_pedido("pagamento", "Pagamento", opcoes=opcoes_pagamento_pedido),
            campo_pedido("dga", "DGA", opcoes=opcoes_dga_pedido),
            campo_pedido("cod_finame", "Código FINAME"),
            campo_pedido("pac", "PAC nº"),
            campo_pedido("classificacao_fiscal", "Classificação fiscal"),
            campo_pedido("local_entrega", "Entrega", opcoes=opcoes_entrega_pedido),
            campo_pedido(
                "prazo_entrega",
                "Prazo de entrega",
                opcoes=opcoes_prazo_entrega_pedido,
            ),
            campo_pedido("detalhes", "Detalhes / observações", "textarea", largura=True),
            campo_pedido(
                "validade",
                "Proposta válida até",
                "date",
                readonly=True,
            ),
        ))

        aviso_salvo = ""
        pedido_salvo = str(request.args.get("salvo", "")).strip()
        if pedido_salvo:
            texto_salvo = "atualizada" if request.args.get("editado") else "salva"
            aviso_salvo = (
                '<div class="pedido-alerta pedido-alerta-sucesso">'
                f'Proposta <b>{html.escape(pedido_salvo)}</b> {texto_salvo} na planilha '
                f'<b>{ABA_PEDIDOS_FEITOS}</b>. '
                f'<a href="{url_for("acessar_modulo", nome_modulo="pedidos", editar=pedido_salvo, imprimir="1")}">'
                'Imprimir / salvar PDF</a>.'
                '</div>'
            )
        pedidos_feitos_html = ""
        if mostrar_pedidos_feitos:
            try:
                pedidos_feitos = listar_pedidos_vendedor(
                    planilha_pedidos,
                    email_vendedor,
                )
            except Exception as e:
                traceback.print_exc()
                erro_lista_pedidos = (
                    "Não foi possível carregar seus pedidos. "
                    f"Detalhe: {html.escape(str(e))}"
                )

            linhas_pedidos_feitos = []
            for pedido_feito in pedidos_feitos:
                id_pedido_lista = str(pedido_feito.get("ID_PEDIDO", "")).strip()
                data_lista = str(pedido_feito.get("DATA_PEDIDO", "")).strip()
                cliente_lista = str(pedido_feito.get("CLIENTE", "")).strip()
                cnpj_lista = str(pedido_feito.get("DOCUMENTO_CLIENTE", "")).strip()
                modelo_lista = str(pedido_feito.get("MODELO", "")).strip()
                valor_lista = converter_numero(pedido_feito.get("VALOR_TOTAL"))
                valor_lista_txt = (
                    f"R$ {valor_lista:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    if valor_lista is not None else "—"
                )
                acao_pdf = (
                    f'<a class="pedido-btn pedido-btn-secundario pedido-acao-lista" '
                    f'href="{url_for("acessar_modulo", nome_modulo="pedidos", editar=id_pedido_lista, imprimir="1")}">'
                    'Imprimir / salvar PDF</a>'
                    if id_pedido_lista else '<span style="color:#94a3b8">ID não registrado</span>'
                )
                acao_editar = (
                    f'<a class="pedido-btn pedido-btn-secundario pedido-acao-lista" '
                    f'href="{url_for("acessar_modulo", nome_modulo="pedidos", editar=id_pedido_lista)}">'
                    'Editar / Abrir</a>'
                    if id_pedido_lista else ""
                )
                linhas_pedidos_feitos.append(
                    "<tr>"
                    f"<td>{html.escape(data_lista)}</td>"
                    f"<td><b>{html.escape(id_pedido_lista)}</b></td>"
                    f"<td>{html.escape(cliente_lista)}</td>"
                    f"<td>{html.escape(cnpj_lista or '—')}</td>"
                    f"<td>{html.escape(modelo_lista)}</td>"
                    f"<td class='pedido-valor'>{html.escape(valor_lista_txt)}</td>"
                    f"<td><div class='pedido-acoes-lista'>{acao_editar}{acao_pdf}</div></td>"
                    "</tr>"
                )
            tabela_pedidos_feitos = (
                "".join(linhas_pedidos_feitos)
                or '<tr><td colspan="7" class="pedido-sem-registro">Você ainda não salvou pedidos.</td></tr>'
            )
            pedidos_feitos_html = f"""
            <section class="pedido-card pedido-lista">
              <h3>Propostas feitas por {html.escape(vendedor_pedido)}</h3>
              {f'<div class="pedido-alerta pedido-alerta-erro">{erro_lista_pedidos}</div>' if erro_lista_pedidos else ''}
              <div class="pedido-tabela-scroll">
                <table>
                  <thead><tr><th>Data</th><th>ID da proposta</th><th>Cliente</th><th>CPF / CNPJ</th><th>Modelo</th><th>Valor total</th><th>Ações</th></tr></thead>
                  <tbody>{tabela_pedidos_feitos}</tbody>
                </table>
              </div>
            </section>
            """
        alerta_erro = (
            f'<div class="pedido-alerta pedido-alerta-erro">{erro_pedido}</div>'
            if erro_pedido else ""
        )
        modelos_json = json.dumps(
            list(modelos_pedido.values()),
            ensure_ascii=False,
        ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        cnpj_por_faturante_json = json.dumps(
            cnpj_por_faturante_pedido,
            ensure_ascii=False,
        ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        conteudo = render_template_string(
            """
            <style>
              .pedido-wrap{max-width:1200px;margin:0 auto;padding:4px 0 36px;color:#1e293b}
              .pedido-topo{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
              .pedido-topo h2{margin:0;color:#002244;font-size:23px}
              .pedido-topo p{margin:5px 0 0;color:#64748b;font-size:13px}
              .pedido-nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
              .pedido-card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:15px;box-shadow:0 2px 7px rgba(15,23,42,.04)}
              .pedido-card h3{margin:0 0 13px;color:#002244;font-size:15px;border-bottom:1px solid #e8edf4;padding-bottom:9px}
              .pedido-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
              .pedido-campo{min-width:0}
              .pedido-campo-largo{grid-column:1/-1}
              .pedido-campo label{display:block;font-size:11px;font-weight:800;color:#475569;margin-bottom:5px}
              .pedido-campo label.obrigatorio::after,.pedido-campo-rotulo.obrigatorio::after{content:" *";color:#dc2626;font-weight:900}
              .pedido-campo input,.pedido-campo select,.pedido-campo textarea{width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:7px;padding:10px 11px;background:#fff;color:#1e293b;font:inherit;font-size:13px}
              .pedido-campo input[readonly]{border-color:transparent;background:transparent;box-shadow:none;cursor:default}
              .pedido-campo textarea{resize:vertical}
              .pedido-segmento-campo{margin-bottom:13px}
              .pedido-segmento-rotulo{display:block;font-size:11px;font-weight:800;color:#475569;margin-bottom:6px}
              .pedido-segmento-opcoes{display:flex;gap:9px;max-width:430px}
              .pedido-segmento-opcao{flex:1;border:1px solid #cbd5e1;border-radius:8px;padding:11px 14px;background:#fff;color:#334155;font:inherit;font-size:13px;font-weight:800;cursor:pointer;transition:background .15s,border-color .15s,color .15s}
              .pedido-segmento-opcao:hover{border-color:#002244}
              .pedido-segmento-opcao.ativo{background:#002244;border-color:#002244;color:#fff}
              .pedido-veiculo-selecao{margin-bottom:13px}
              .pedido-modelo-foto{display:grid;grid-template-columns:minmax(280px,36%) minmax(0,1fr);align-items:center;gap:18px;min-height:230px;padding:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:9px}
              .pedido-modelo-foto img{display:none;width:100%;max-width:none;height:230px;object-fit:contain;background:#fff;border-radius:7px}
              .pedido-modelo-info{font-size:12px;color:#475569;line-height:1.55;background:#f8fafc}
              .pedido-modelo-titulo{margin:0 0 2px;color:#002244;font-size:21px;line-height:1.2}
              .pedido-modelo-identificacao{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0}
              .pedido-modelo-identificacao-item{min-width:0;padding:8px 10px;border:1px solid #e2e8f0;border-radius:7px;background:#fff}
              .pedido-modelo-identificacao-item b{display:block;margin-bottom:2px;color:#64748b;font-size:9px;font-weight:700;text-transform:uppercase}
              .pedido-modelo-identificacao-item span{display:block;color:#1e293b;font-size:12px;font-weight:600;overflow-wrap:anywhere}
              .pedido-dados-tecnicos{width:100%;border-collapse:collapse;table-layout:fixed;background:#fff;font-size:11px;margin-top:2px}
              .pedido-dados-tecnicos caption{padding:8px 0 4px;color:#002244;font-size:11px;font-weight:800;text-align:left}
              .pedido-dados-tecnicos th,.pedido-dados-tecnicos td{padding:5px 6px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top;overflow-wrap:anywhere}
              .pedido-dados-tecnicos th{color:#64748b;font-size:9px;font-weight:700;text-transform:uppercase;line-height:1.25}
              .pedido-dados-tecnicos td{color:#1e293b;font-weight:700;line-height:1.35}
              .pedido-acoes{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}
              .pedido-btn{border:0;border-radius:7px;padding:10px 14px;font-weight:800;font-size:12px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
              .pedido-btn-primario{background:#002244;color:white}
              .pedido-btn-secundario{background:#f1f5f9;color:#1e293b;border:1px solid #cbd5e1}
              .pedido-tabela-scroll{overflow:auto}
              .pedido-lista table{width:100%;border-collapse:collapse;font-size:12px;min-width:760px}
              .pedido-lista th{background:#002244;color:#fff;text-align:left;padding:10px}
              .pedido-lista td{padding:9px 10px;border-bottom:1px solid #e2e8f0;color:#334155}
              .pedido-acoes-lista{display:flex;align-items:center;gap:6px;flex-wrap:nowrap}
              .pedido-acao-lista{padding:6px 8px;font-size:10px;line-height:1.2;white-space:nowrap}
              .pedido-lista .pedido-valor{font-weight:800;white-space:nowrap}
              .pedido-sem-registro{text-align:center;padding:24px!important;color:#64748b!important}
              .pedido-alerta{padding:12px 14px;border-radius:8px;margin:0 0 14px;font-size:13px}
              .pedido-alerta-erro{background:#fff5f5;border:1px solid #feb2b2;color:#9b2c2c}
              .pedido-alerta-sucesso{background:#f0fff4;border:1px solid #9ae6b4;color:#276749}
              .pedido-doc{max-width:900px;margin:0 auto;background:#fff;border:1px solid #cbd5e1;padding:32px;color:#0f172a}
              .pedido-doc-marcas{display:flex;justify-content:center;align-items:center;gap:48px;margin-bottom:5px}
              .pedido-doc-marcas img{display:block;width:auto;height:auto;max-width:330px;max-height:126px;object-fit:contain}
              .pedido-doc-marcas img:last-child{max-width:210px;max-height:100px}
              .pedido-doc-cabecalho{display:flex;justify-content:space-between;gap:12px;align-items:center;border-bottom:2px solid #1e4778;padding-bottom:7px;margin-bottom:10px}
              .pedido-doc-subtitulo{font-size:11px;color:#64748b}
              .pedido-doc-cabecalho p{font-size:11px;color:#64748b;margin:5px 0 0}
              .pedido-doc-numero{text-align:right;font-size:9px;color:#475569;white-space:nowrap}
              .pedido-doc h3,.pedido-doc-meta,.pedido-doc-modelo,.pedido-doc-total,.pedido-doc-observacoes,.pedido-doc-final{width:100%;box-sizing:border-box}
              .pedido-doc h3{background:#1e4778;color:white;padding:6px 8px;font-size:10px;margin:10px 0 5px}
              .pedido-doc-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px 16px;font-size:11px}
              .pedido-doc-meta-vertical{grid-template-columns:minmax(0,1fr);gap:0}
              .pedido-doc-meta div{border-bottom:1px solid #e2e8f0;padding:4px 0;min-height:20px}
              .pedido-doc-meta b{color:#475569}
              .pedido-doc-complementares{width:100%;font-size:11px}
              .pedido-doc-complemento{display:grid;grid-template-columns:13% minmax(0,1fr);align-items:stretch;border-bottom:1px solid #dbe3ed}
              .pedido-doc-complemento b{display:flex;align-items:flex-start;padding:5px 7px;background:#1e4778;color:#fff}
              .pedido-doc-complemento span{min-width:0;padding:5px 7px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere}
              .pedido-doc-modelo{display:grid;grid-template-columns:minmax(180px,32%) minmax(0,1fr);gap:15px;align-items:center;border:1px solid #cbd5e1;padding:12px}
              .pedido-doc-modelo img{display:none;width:100%;height:145px;object-fit:contain;background:#fff;border:1px solid #edf2f7;border-radius:7px}
              .pedido-doc-modelo-nome{font-size:19px;font-weight:800;color:#1e4778}
              .pedido-dados-tecnicos-print{font-size:10px}
              .pedido-dados-tecnicos-print caption{font-size:10px;padding:5px 0 3px}
              .pedido-dados-tecnicos-print th{font-size:8px}
              .pedido-dados-tecnicos-print th,.pedido-dados-tecnicos-print td{padding:4px}
              .pedido-doc-observacoes{min-height:24px;white-space:pre-wrap;font-size:9px;line-height:1.4}
              .pedido-doc-total{display:flex;justify-content:flex-end;gap:18px;align-items:center;background:#1e4778;color:#fff;padding:8px 12px;margin-top:7px;font-size:11px;font-weight:800}
              .pedido-doc-final{page-break-inside:avoid;break-inside:avoid}
              .pedido-doc-validade{text-align:center;margin:5px 0 3px;padding:5px;background:#eef3f8;border:1px solid #cbd5e1;color:#1e4778;font-size:9px;font-weight:800}
              .pedido-doc-acordo{margin:4px 0 2px;font-size:7px}
              .pedido-doc-assinatura-cliente{height:42px;width:36%;margin:0 auto;border-bottom:1px solid #94a3b8}
              .pedido-doc-cliente{width:100%;box-sizing:border-box;text-align:center;font-size:9px;line-height:1.45;padding:2px 5px}
              .pedido-doc-contatos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:end;text-align:center;font-size:9px;line-height:1.45;border-bottom:2px solid #1e4778;padding-bottom:7px}
              .pedido-doc-contato{padding:2px 5px;text-align:center;overflow-wrap:anywhere}
              .pedido-doc-unidades{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;font-size:9px;line-height:1.4;text-align:center}
              .pedido-doc-unidade{padding:5px 8px}
              @media(max-width:720px){.pedido-grid{grid-template-columns:1fr 1fr}.pedido-modelo-foto,.pedido-doc-modelo{grid-template-columns:1fr;gap:12px;min-height:0;padding:12px}.pedido-modelo-foto img{height:210px}.pedido-doc-modelo img{height:190px}.pedido-doc{padding:18px}.pedido-doc-meta{grid-template-columns:1fr 1fr}}
              .pedido-doc-meta-vertical{grid-template-columns:minmax(0,1fr);gap:0}
              @media(max-width:480px){.pedido-modelo-foto img{height:190px}.pedido-doc-modelo img{height:170px}.pedido-modelo-identificacao{gap:6px}.pedido-modelo-identificacao-item{padding:7px}}
              @media(max-width:480px){.pedido-grid{grid-template-columns:1fr}.pedido-campo-largo{grid-column:auto}}
              @media print{
                @page{size:A4 portrait;margin:5mm}
                body *{visibility:hidden!important}
                #pedido-impressao,#pedido-impressao *{visibility:visible!important}
                #pedido-impressao{position:absolute;left:0;right:0;top:0;width:100%;max-width:900px;box-sizing:border-box;margin:0 auto;padding:0;border:0;box-shadow:none}
                .pedido-editor,.pedido-acoes,.pedido-alerta{display:none!important}
                #pedido-impressao{zoom:var(--pedido-print-zoom,1);transform-origin:top center}
                .pedido-doc h3,.pedido-doc-total,.pedido-doc-cabecalho,.pedido-doc-contato,.pedido-doc-validade{-webkit-print-color-adjust:exact;print-color-adjust:exact}
                a{color:#0f172a;text-decoration:none}
              }
              @media(max-width:720px){.pedido-lista{padding:12px}}
            </style>
            <div class="pedido-wrap">
              <div class="pedido-topo">
                <div><h2>Propostas a Clientes</h2>
                  <p>Preencha os dados, confira a proposta e imprima ou compartilhe pelo WhatsApp.</p></div>
              </div>
              <nav class="pedido-nav">
                <a class="pedido-btn {% if not mostrar_feitos %}pedido-btn-primario{% else %}pedido-btn-secundario{% endif %}" href="{{ url_for('acessar_modulo', nome_modulo='pedidos') }}">Nova proposta</a>
                <a class="pedido-btn {% if mostrar_feitos %}pedido-btn-primario{% else %}pedido-btn-secundario{% endif %}" href="{{ url_for('acessar_modulo', nome_modulo='pedidos', visao='feitos') }}">Propostas feitas</a>
              </nav>
              {{ aviso_salvo|safe }}{{ alerta_erro|safe }}
              {% if mostrar_feitos %}
                {{ pedidos_feitos_html|safe }}
              {% else %}
              {% if not modelos %}
                <div class="pedido-alerta pedido-alerta-erro">Não há modelos disponíveis na aba Modelos. Confira os dados antes de preencher um pedido.</div>
              {% endif %}
              <form method="POST" class="pedido-editor" id="formPedido">
                <input type="hidden" name="id_pedido_edicao" value="{{ pedido_edicao_id }}">
                {{ campos_tecnicos_ocultos|safe }}
                {{ campos_modelo_pdf|safe }}
                <section class="pedido-card">
                  <h3>1. Dados do cliente e da proposta</h3>
                  <div class="pedido-grid">{{ campos_cliente|safe }}</div>
                </section>
                <section class="pedido-card">
                  <h3>2. Segmento e configuração do veículo</h3>
                  <div class="pedido-segmento-campo">
                    <span class="pedido-segmento-rotulo">Segmento</span>
                    <input type="hidden" id="segmento" name="segmento" value="{{ segmento_selecionado }}" data-pedido-campo="segmento">
                    <div class="pedido-segmento-opcoes" role="group" aria-label="Selecione o segmento do veículo">
                      <button type="button" class="pedido-segmento-opcao" data-segmento="Caminhão" aria-pressed="false">🚚 Caminhão</button>
                      <button type="button" class="pedido-segmento-opcao" data-segmento="Ônibus" aria-pressed="false">🚌 Ônibus</button>
                    </div>
                  </div>
                  <div class="pedido-grid pedido-veiculo-selecao">
                    <div class="pedido-campo">
                      <label for="modelo" class="pedido-campo-rotulo obrigatorio">Modelo</label>
                      <input type="hidden" id="modelo_nome" name="modelo" value="{{ dados_pedido['modelo'] }}" data-pedido-campo="modelo">
                      <select id="modelo" name="modelo_id" required>
                        <option value="">Selecione primeiro o segmento...</option>
                      </select>
                      <a id="pedido-link-ficha" class="pedido-btn pedido-btn-secundario"
                         href="#" target="_blank" rel="noopener noreferrer"
                         style="display:none;margin-top:10px">Ficha Técnica</a>
                    </div>
                    {{ campos_caminhao|safe }}
                    </div>
                    <div class="pedido-modelo-foto">
                      <img id="pedido-imagem-modelo" alt="Imagem do veículo selecionado">
                      <div class="pedido-modelo-info" id="pedido-detalhes-modelo">
                      <div class="detalhe-label">Modelo selecionado</div>
                      <h4 id="pedido-detalhes-modelo-titulo" class="pedido-modelo-titulo">Selecione um modelo</h4>
                      <div class="pedido-modelo-identificacao">
                        <div class="pedido-modelo-identificacao-item"><b>Ano / modelo</b><span data-pedido-preview="ano_modelo">—</span></div>
                        <div class="pedido-modelo-identificacao-item"><b>Cabine</b><span data-pedido-preview="cabine">—</span></div>
                        <div class="pedido-modelo-identificacao-item"><b>Tipo</b><span id="pedido-tipo-modelo">—</span></div>
                        <div class="pedido-modelo-identificacao-item"><b>Categoria</b><span id="pedido-categoria-modelo">—</span></div>
                      </div>
                      <div id="pedido-dados-tecnicos-editor"></div>
                    </div>
                  </div>
                </section>
                <section class="pedido-card">
                  <h3>3. Planos e informações do veículo</h3>
                  <div class="pedido-grid">{{ campos_condicoes|safe }}</div>
                </section>
                <section class="pedido-card">
                  <h3>4. Valores e condições comerciais</h3>
                  <div class="pedido-grid">
                    {{ campo_quantidade|safe }}{{ campo_valor|safe }}
                    {{ campos_faturamento|safe }}
                  </div>
                  <p style="font-size:11px;color:#64748b;margin:10px 0 0">O valor total da proposta é calculado automaticamente pela quantidade e pelo valor unitário.</p>
                </section>
                <div class="pedido-acoes">
                  <button class="pedido-btn pedido-btn-primario" type="submit" {% if not modelos %}disabled{% endif %}>{% if pedido_edicao_id %}💾 Atualizar proposta{% else %}💾 Salvar proposta{% endif %}</button>
                  {% if pedido_edicao_id %}<a class="pedido-btn pedido-btn-secundario" href="{{ url_for('acessar_modulo', nome_modulo='pedidos', visao='feitos') }}">Cancelar edição</a>{% endif %}
                  <button class="pedido-btn pedido-btn-secundario" type="button" onclick="imprimirProposta()">📄 Imprimir / salvar PDF</button>
                </div>
              </form>

              <section class="pedido-doc" id="pedido-impressao">
                <div class="pedido-doc-marcas">
                  <img src="{{ url_for('static', filename='logo2.png') }}" alt="Novo Mundo">
                  <img src="{{ url_for('static', filename='VW_TRANS.png') }}" alt="Volkswagen Caminhões e Ônibus">
                </div>
                <header class="pedido-doc-cabecalho">
                  <div class="pedido-doc-subtitulo">Caminhões e Ônibus · Proposta sujeita à confirmação das condições comerciais</div>
                  <div class="pedido-doc-numero"><b>Data:</b> <span data-pedido-preview="data_pedido"></span></div>
                </header>
                <h3>CLIENTE</h3>
                <div class="pedido-doc-meta">
                  <div><b>Razão social / Nome:</b> <span data-pedido-preview="cliente"></span></div>
                  <div><b>CPF / CNPJ:</b> <span data-pedido-preview="documento_cliente"></span></div>
                  <div><b>Telefone:</b> <span data-pedido-preview="telefone_cliente"></span></div>
                  <div><b>E-mail:</b> <span data-pedido-preview="email_cliente"></span></div>
                  <div><b>Cidade:</b> <span data-pedido-preview="cidade"></span></div>
                  <div><b>Estado:</b> <span data-pedido-preview="uf"></span></div>
                </div>
                <h3>VEÍCULO OFERTADO</h3>
                <div class="pedido-doc-modelo">
                  <img id="pedido-imagem-impressao" alt="Imagem do veículo">
                  <div>
                    <div class="detalhe-label">Modelo selecionado</div>
                    <div class="pedido-doc-modelo-nome" data-pedido-preview="modelo">Selecione o modelo</div>
                    <div class="pedido-modelo-identificacao">
                      <div class="pedido-modelo-identificacao-item"><b>Ano / modelo</b><span data-pedido-preview="ano_modelo"></span></div>
                      <div class="pedido-modelo-identificacao-item"><b>Cabine</b><span data-pedido-preview="cabine"></span></div>
                      <div class="pedido-modelo-identificacao-item"><b>Tipo</b><span id="preview-tipo-modelo">—</span></div>
                      <div class="pedido-modelo-identificacao-item"><b>Categoria</b><span id="preview-categoria-modelo">—</span></div>
                    </div>
                    <div id="pedido-dados-tecnicos-impressao"></div>
                  </div>
                </div>
                <h3>PLANOS E INFORMAÇÕES COMPLEMENTARES</h3>
                <div class="pedido-doc-meta" id="pedido-doc-opcoes-plano" style="display:none">
                  <div data-pedido-opcional="plano_manutencao" style="display:none"><b>Plano de manutenção:</b> <span data-pedido-preview="plano_manutencao"></span></div>
                  <div data-pedido-opcional="rio" style="display:none"><b>Telemetria RIO:</b> <span data-pedido-preview="rio"></span></div>
                </div>
                <div class="pedido-doc-complementares">
                  <div class="pedido-doc-complemento"><b>Garantia</b><span data-pedido-preview="garantia"></span></div>
                  <div class="pedido-doc-complemento"><b>Chame Volks</b><span data-pedido-preview="assistencia"></span></div>
                  <div class="pedido-doc-complemento"><b>VolksTotal</b><span data-pedido-preview="condicoes_pm"></span></div>
                  <div class="pedido-doc-complemento"><b>Informações</b><span data-pedido-preview="informacoes_complementares"></span></div>
                </div>
                <h3>VALORES E CONDIÇÕES DE FATURAMENTO</h3>
                <div class="pedido-doc-meta">
                  <div><b>Quantidade:</b> <span data-pedido-preview="quantidade"></span></div>
                  <div><b>Valor unitário:</b> <span id="preview-valor-unitario"></span></div>
                  <div><b>Modalidade de faturamento:</b> <span data-pedido-preview="modalidade_faturamento"></span></div>
                  <div><b>Faturante:</b> <span data-pedido-preview="faturante"></span></div>
                  <div><b>CNPJ faturante:</b> <span data-pedido-preview="cnpj_faturante"></span></div>
                  <div><b>Pagamento:</b> <span data-pedido-preview="pagamento"></span></div>
                  <div><b>DGA:</b> <span data-pedido-preview="dga"></span></div>
                  <div><b>Código FINAME:</b> <span data-pedido-preview="cod_finame"></span></div>
                  <div><b>PAC nº:</b> <span data-pedido-preview="pac"></span></div>
                  <div><b>Classificação fiscal:</b> <span data-pedido-preview="classificacao_fiscal"></span></div>
                  <div><b>Entrega:</b> <span data-pedido-preview="local_entrega"></span></div>
                  <div><b>Prazo de entrega:</b> <span data-pedido-preview="prazo_entrega"></span></div>
                  <div><b>Validade da proposta:</b> <span data-pedido-preview="validade"></span></div>
                </div>
                <div class="pedido-doc-total"><span>VALOR TOTAL</span><span id="preview-valor-total">R$ 0,00</span></div>
                <div class="pedido-doc-observacoes" style="margin-top:7px"><b>Detalhes:</b><br><span data-pedido-preview="detalhes"></span></div>
                <footer class="pedido-doc-final">
                  <div class="pedido-doc-validade">Proposta válida até <span data-pedido-preview="validade">—</span></div>
                  <div class="pedido-doc-acordo">De acordo:</div>
                  <div class="pedido-doc-assinatura-cliente" aria-label="Espaço para assinatura do cliente"></div>
                  <div class="pedido-doc-cliente">
                    <b data-pedido-preview="cliente">Cliente</b><br>
                    <span data-pedido-preview="documento_cliente">CNPJ não informado</span><br>
                    Cliente
                  </div>
                  <div class="pedido-doc-contatos">
                    <div class="pedido-doc-contato">
                      <b>Ricardo Ricarte</b><br>
                      Superintendente<br>
                      (82) 99134-5112<br>
                      ricardo.ricarte@adtsa.com.br
                    </div>
                    <div class="pedido-doc-contato">
                      <b>{{ vendedor }}</b><br>
                      Consultor<br>
                      {% if telefone_vendedor %}{{ telefone_vendedor }}<br>{% endif %}
                      {% if celular_vendedor and celular_vendedor != telefone_vendedor %}{{ celular_vendedor }}<br>{% endif %}
                      {{ email_vendedor }}
                    </div>
                  </div>
                  <div class="pedido-doc-unidades">
                    <div class="pedido-doc-unidade">
                      <b>Unidade I</b><br>
                      Jaboatão – PE<br>
                      Br. 101 Sul, Km 82,9<br>
                      Prazeres - CEP 54.345-160<br>
                      (81) 2138-2300<br>
                      www.novomundocaminhoes.com.br
                    </div>
                    <div class="pedido-doc-unidade">
                      <b>Unidade II</b><br>
                      Maceió – AL<br>
                      Av. Lourival Melo Mota s/n<br>
                      Cidade Universitária - CEP 57.072-000<br>
                      (82) 3311-3700
                    </div>
                    <div class="pedido-doc-unidade">
                      <b>Unidade III</b><br>
                      Arapiraca – AL<br>
                      Rod AL 220, nº 2458 Km 68<br>
                      Senador Arnon Melo - CEP 57.315-745<br>
                      (82) 3482-5200<br>
                      *Imagens dos modelos meramente ilustrativas.
                    </div>
                  </div>
                </footer>
              </section>
            </div>
            <script>
              (function(){
                const modelos = {{ modelos_json|safe }};
                const cnpjPorFaturante = {{ cnpj_por_faturante_json|safe }};
                const modeloSelect = document.getElementById('modelo');
                const segmentoInput = document.getElementById('segmento');
                const botoesSegmento = document.querySelectorAll('[data-segmento]');
                const modeloInicial = {{ modelo_selecionado|tojson }};
                let primeiraAtualizacaoModelos = true;
                let modeloTecnicoAplicado = null;
                const moeda = new Intl.NumberFormat('pt-BR',{style:'currency',currency:'BRL'});
                const campos = document.querySelectorAll('[data-pedido-campo]');
                function campo(nome){return document.getElementById(nome);}
                function valorCampo(nome){
                  const el=campo(nome);
                  return el ? (el.value || '').trim() : '';
                }
                function formatarDocumentoCliente(){
                  const input=campo('documento_cliente');
                  if(!input) return;
                  const posicao=input.selectionStart ?? input.value.length;
                  const digitosAntesCursor=input.value.slice(0,posicao).replace(/\\D/g,'').length;
                  const digitos=input.value.replace(/\\D/g,'').slice(0,14);
                  let formatado;
                  if(digitos.length<=11){
                    formatado=digitos
                      .replace(/^(\\d{3})(\\d)/,'$1.$2')
                      .replace(/^(\\d{3})\\.(\\d{3})(\\d)/,'$1.$2.$3')
                      .replace(/(\\.\\d{3})(\\d{1,2})$/,'$1-$2');
                  }else{
                    formatado=digitos
                      .replace(/^(\\d{2})(\\d)/,'$1.$2')
                      .replace(/^(\\d{2})\\.(\\d{3})(\\d)/,'$1.$2.$3')
                      .replace(/(\\.\\d{3})(\\d)/,'$1/$2')
                      .replace(/(\\d{4})(\\d{1,2})$/,'$1-$2');
                  }
                  input.value=formatado;
                  let novaPosicao=0;
                  let digitosContados=0;
                  while(novaPosicao<formatado.length && digitosContados<digitosAntesCursor){
                    if(/\\d/.test(formatado[novaPosicao])) digitosContados++;
                    novaPosicao++;
                  }
                  input.setSelectionRange(novaPosicao,novaPosicao);
                }
                function formatarTelefoneCliente(){
                  const input=campo('telefone_cliente');
                  if(!input) return;
                  const posicao=input.selectionStart ?? input.value.length;
                  const digitosAntesCursor=input.value.slice(0,posicao).replace(/\\D/g,'').length;
                  const digitos=input.value.replace(/\\D/g,'').slice(0,11);
                  let formatado=digitos;
                  if(digitos.length>2){
                    const tamanhoNumero=digitos.length>10 ? 5 : 4;
                    const ddd=digitos.slice(0,2);
                    const numero=digitos.slice(2);
                    formatado='('+ddd+') '+numero;
                    if(numero.length>tamanhoNumero){
                      formatado='('+ddd+') '+numero.slice(0,tamanhoNumero)+'-'+numero.slice(tamanhoNumero);
                    }
                  }
                  input.value=formatado;
                  let novaPosicao=0;
                  let digitosContados=0;
                  while(novaPosicao<formatado.length && digitosContados<digitosAntesCursor){
                    if(/\\d/.test(formatado[novaPosicao])) digitosContados++;
                    novaPosicao++;
                  }
                  input.setSelectionRange(novaPosicao,novaPosicao);
                }
                function normalizarEmailCliente(){
                  const input=campo('email_cliente');
                  if(input) input.value=input.value.toLowerCase();
                }
                function textoSeguro(el,texto){if(el) el.textContent=texto || '—';}
                function dataBR(valor){
                  if(!valor) return '—';
                  const partes=valor.split('-');
                  return partes.length===3 ? partes[2]+'/'+partes[1]+'/'+partes[0] : valor;
                }
                function parseNumeroBR(valor){
                  let texto=String(valor || '').trim()
                    .replace(/R\\$\\s?/gi,'')
                    .replace(/\\s/g,'');
                  if(!texto) return null;
                  if(texto.includes(',')){
                    texto=texto.replace(/\\./g,'').replace(',','.');
                  }else if(/^-?\\d{1,3}(?:\\.\\d{3})+$/.test(texto)){
                    texto=texto.replace(/\\./g,'');
                  }
                  const n=Number(texto);
                  return Number.isFinite(n) ? n : null;
                }
                function numeroBR(valor){
                  return parseNumeroBR(valor) ?? 0;
                }
                function formatarValorUnitario(){
                  const input=campo('valor_unitario');
                  const numero=parseNumeroBR(input ? input.value : '');
                  if(input && numero!==null) input.value=moeda.format(numero);
                }
                function aplicarMascaraValorUnitario(){
                  const input=campo('valor_unitario');
                  if(!input) return;
                  const digitos=input.value.replace(/\\D/g,'');
                  if(!digitos){
                    input.value='';
                    return;
                  }
                  const numero=Number(digitos)/100;
                  if(!Number.isFinite(numero)) return;
                  input.value=moeda.format(numero);
                  input.setSelectionRange(input.value.length,input.value.length);
                }
                const camposTecnicosPedido=[
                  ['TECNOLOGIA DO MOTOR','tecnologia_motor'],
                  ['PBT','pbt'],
                  ['ENTRE-EIXOS','entre_eixos'],
                  ['MOTOR','motor'],
                  ['POTÊNCIA','potencia'],
                  ['TRANSMISSÃO','transmissao'],
                  ['SISTEMA DE INJEÇÃO','sistema_injecao'],
                  ['COMBUSTÍVEL','combustivel']
                ];
                function renderizarDadosTecnicos(container,modelo){
                  if(!container) return;
                  container.replaceChildren();
                  if(!modelo) return;
                  const dados=[];
                  camposTecnicosPedido.forEach(([rotulo,nome])=>{
                    const valor=valorCampo(nome);
                    if(!valor) return;
                    dados.push([rotulo,valor]);
                  });
                  if(!dados.length) return;
                  const tabela=document.createElement('table');
                  tabela.className='pedido-dados-tecnicos';
                  if(container.id==='pedido-dados-tecnicos-impressao'){
                    tabela.classList.add('pedido-dados-tecnicos-print');
                  }
                  const legenda=document.createElement('caption');
                  legenda.textContent='⚙️ Dados técnicos';
                  tabela.appendChild(legenda);
                  const colunas=document.createElement('colgroup');
                  ['18%','32%','18%','32%'].forEach(largura=>{
                    const coluna=document.createElement('col');
                    coluna.style.width=largura;
                    colunas.appendChild(coluna);
                  });
                  tabela.appendChild(colunas);
                  const corpo=document.createElement('tbody');
                  for(let indice=0;indice<dados.length;indice+=2){
                    const linha=document.createElement('tr');
                    dados.slice(indice,indice+2).forEach(([rotulo,valor])=>{
                      const titulo=document.createElement('th');
                      titulo.scope='row';
                      titulo.textContent=rotulo;
                      const conteudo=document.createElement('td');
                      conteudo.textContent=valor;
                      linha.append(titulo,conteudo);
                    });
                    if(dados.slice(indice,indice+2).length===1){
                      linha.append(document.createElement('th'),document.createElement('td'));
                    }
                    corpo.appendChild(linha);
                  }
                  tabela.appendChild(corpo);
                  container.appendChild(tabela);
                }
                function atualizarOpcoesModelos(){
                  const segmento=segmentoInput.value;
                  const modeloAnterior=modeloSelect.value || (
                    primeiraAtualizacaoModelos ? modeloInicial : ''
                  );
                  const modelosDoSegmento=modelos.filter(
                    item=>item.segmento_modelo===segmento
                  );
                  modeloSelect.replaceChildren(new Option(
                    !segmento
                      ? 'Selecione primeiro o segmento...'
                      : modelosDoSegmento.length
                        ? 'Selecione um modelo...'
                        : 'Nenhum modelo disponível para este segmento.',
                    ''
                  ));
                  const gruposPorCategoria=new Map();
                  modelosDoSegmento.forEach(item=>{
                    const categoria=item.categoria || 'Sem categoria';
                    if(!gruposPorCategoria.has(categoria)){
                      const grupo=document.createElement('optgroup');
                      grupo.label=categoria;
                      gruposPorCategoria.set(categoria,grupo);
                      modeloSelect.add(grupo);
                    }
                    gruposPorCategoria.get(categoria).appendChild(
                      new Option(item.modelo,item.id)
                    );
                  });
                  modeloSelect.disabled=!segmento;
                  if(modelos.some(item=>item.id===modeloAnterior && item.segmento_modelo===segmento)){
                    modeloSelect.value=modeloAnterior;
                  }else{
                    modeloSelect.value='';
                  }
                  primeiraAtualizacaoModelos=false;
                  botoesSegmento.forEach(botao=>{
                    const selecionado=botao.dataset.segmento===segmento;
                    botao.classList.toggle('ativo',selecionado);
                    botao.setAttribute('aria-pressed',String(selecionado));
                  });
                }
                function atualizarModelo(){
                  const m=modelos.find(item=>item.id===modeloSelect.value);
                  const img=document.getElementById('pedido-imagem-modelo');
                  const imgPrint=document.getElementById('pedido-imagem-impressao');
                  const detalhes=document.getElementById('pedido-detalhes-modelo');
                  const ficha=document.getElementById('pedido-link-ficha');
                  if(!m){
                    modeloTecnicoAplicado=null;
                    campo('modelo_nome').value='';
                    campo('segmento_ficha').value='';
                    campo('modelo_tipo').value='';
                    campo('modelo_categoria').value='';
                    campo('link_ficha_tecnica').value='';
                    campo('imagem_modelo_id').value='';
                    img.style.display='none'; imgPrint.style.display='none';
                    document.getElementById('pedido-detalhes-modelo-titulo').textContent='Selecione um modelo para exibir sua imagem e informações técnicas.';
                    document.getElementById('pedido-dados-tecnicos-editor').replaceChildren();
                    document.getElementById('pedido-dados-tecnicos-impressao').replaceChildren();
                    document.getElementById('pedido-tipo-modelo').textContent='—';
                    document.getElementById('pedido-categoria-modelo').textContent='—';
                    document.getElementById('preview-tipo-modelo').textContent='—';
                    document.getElementById('preview-categoria-modelo').textContent='—';
                    ficha.removeAttribute('href');
                    ficha.style.display='none';
                    sincronizarPreview();
                    return;
                  }
                  const categoria=m.categoria || '';
                  const camposTecnicos={
                    ano_modelo:'ano_modelo',
                    tecnologia:'tecnologia',
                    tecnologia_motor:'tecnologia_motor',
                    segmento_ficha:'segmento_ficha',
                    cabine:'cabine',
                    motor:'motor',
                    potencia:'potencia',
                    transmissao:'transmissao',
                    sistema_injecao:'sistema_injecao',
                    pbt:'pbt',
                    entre_eixos:'entre_eixos',
                    combustivel:'combustivel'
                  };
                  const trocarModelo=modeloTecnicoAplicado && modeloTecnicoAplicado!==m.id;
                  Object.entries(camposTecnicos).forEach(([nome,chave])=>{
                    const el=campo(nome);
                    if(el && (trocarModelo || !el.value)) el.value=m[chave] || '';
                  });
                  modeloTecnicoAplicado=m.id;
                  campo('modelo_nome').value=m.modelo;
                  campo('modelo_tipo').value=m.tipo || '';
                  campo('modelo_categoria').value=categoria;
                  campo('link_ficha_tecnica').value=m.link || '';
                  campo('imagem_modelo_id').value=m.imagem_id || '';
                  [img,imgPrint].forEach(el=>{
                    if(m.imagem){el.src=m.imagem;el.style.display='block';}
                    else{el.removeAttribute('src');el.style.display='none';}
                  });
                  document.getElementById('pedido-detalhes-modelo-titulo').textContent=m.modelo;
                  document.getElementById('pedido-tipo-modelo').textContent=m.tipo || '—';
                  document.getElementById('pedido-categoria-modelo').textContent=categoria || '—';
                  document.getElementById('preview-tipo-modelo').textContent=m.tipo || '—';
                  document.getElementById('preview-categoria-modelo').textContent=categoria || '—';
                  renderizarDadosTecnicos(
                    document.getElementById('pedido-dados-tecnicos-editor'),
                    m,
                  );
                  renderizarDadosTecnicos(
                    document.getElementById('pedido-dados-tecnicos-impressao'),
                    m,
                  );
                  if(m.link){
                    ficha.href=m.link;
                    ficha.style.display='inline-flex';
                  }else{
                    ficha.removeAttribute('href');
                    ficha.style.display='none';
                  }
                  sincronizarPreview();
                }
                function sincronizarPreview(){
                  campos.forEach(el=>{
                    const nome=el.dataset.pedidoCampo;
                    const destinos=document.querySelectorAll('[data-pedido-preview="'+nome+'"]');
                    if(!destinos.length) return;
                    const texto=el.tagName==='SELECT' ? (el.selectedOptions[0]?.textContent || '') : el.value;
                    destinos.forEach(destino=>{
                      destino.textContent=nome==='data_pedido'||nome==='validade' ? dataBR(texto) : (texto || '—');
                      if(nome==='plano_manutencao'||nome==='rio'){
                        const opcional=destino.closest('[data-pedido-opcional]');
                        if(opcional) opcional.style.display=el.value ? '' : 'none';
                      }
                    });
                  });
                  const grupoOpcoesPlano=document.getElementById('pedido-doc-opcoes-plano');
                  if(grupoOpcoesPlano){
                    grupoOpcoesPlano.style.display=
                      (valorCampo('plano_manutencao')||valorCampo('rio')) ? '' : 'none';
                  }
                  const modeloAtual=modelos.find(item=>item.id===modeloSelect.value);
                  renderizarDadosTecnicos(
                    document.getElementById('pedido-dados-tecnicos-impressao'),
                    modeloAtual,
                  );
                  renderizarDadosTecnicos(
                    document.getElementById('pedido-dados-tecnicos-editor'),
                    modeloAtual,
                  );
                  const quantidade=numeroBR(valorCampo('quantidade'));
                  const unitario=numeroBR(valorCampo('valor_unitario'));
                  document.getElementById('preview-valor-unitario').textContent=moeda.format(unitario);
                  document.getElementById('preview-valor-total').textContent=moeda.format(quantidade*unitario);
                }
                campos.forEach(el=>el.addEventListener('input',sincronizarPreview));
                campos.forEach(el=>el.addEventListener('change',sincronizarPreview));
                const faturanteInput=campo('faturante');
                const cnpjFaturanteInput=campo('cnpj_faturante');
                function atualizarCnpjFaturante(){
                  if(!faturanteInput || !cnpjFaturanteInput) return;
                  cnpjFaturanteInput.value=cnpjPorFaturante[faturanteInput.value] || '';
                  sincronizarPreview();
                }
                if(faturanteInput && cnpjFaturanteInput){
                  faturanteInput.addEventListener('change',atualizarCnpjFaturante);
                  atualizarCnpjFaturante();
                }
                const documentoClienteInput=campo('documento_cliente');
                if(documentoClienteInput){
                  documentoClienteInput.addEventListener('input',formatarDocumentoCliente);
                  if(documentoClienteInput.value) formatarDocumentoCliente();
                }
                const telefoneClienteInput=campo('telefone_cliente');
                if(telefoneClienteInput){
                  telefoneClienteInput.addEventListener('input',formatarTelefoneCliente);
                  if(telefoneClienteInput.value) formatarTelefoneCliente();
                }
                const emailClienteInput=campo('email_cliente');
                if(emailClienteInput){
                  emailClienteInput.addEventListener('input',normalizarEmailCliente);
                }
                const valorUnitarioInput=campo('valor_unitario');
                const formularioPedido=document.getElementById('formPedido');
                if(valorUnitarioInput){
                  valorUnitarioInput.addEventListener('input',aplicarMascaraValorUnitario);
                  valorUnitarioInput.addEventListener('blur',formatarValorUnitario);
                  if(valorUnitarioInput.value.trim()) formatarValorUnitario();
                }
                if(formularioPedido){
                  formularioPedido.addEventListener('submit',formatarValorUnitario);
                }
                modeloSelect.addEventListener('change',atualizarModelo);
                botoesSegmento.forEach(botao=>botao.addEventListener('click',()=>{
                  segmentoInput.value=botao.dataset.segmento;
                  ['ano_modelo','tecnologia','tecnologia_motor','segmento_ficha',
                    'cabine','motor','potencia','transmissao','sistema_injecao',
                    'pbt','entre_eixos','combustivel'].forEach(nome=>{
                    const el=campo(nome);
                    if(el) el.value='';
                  });
                  modeloTecnicoAplicado=null;
                  atualizarOpcoesModelos();
                  atualizarModelo();
                  segmentoInput.dispatchEvent(new Event('change',{bubbles:true}));
                }));
                window.imprimirProposta=function(){
                  const formulario=document.getElementById('formPedido');
                  if(formulario && !formulario.reportValidity()) return;
                  if(!documentoImpressao) return;
                  documentoImpressao.style.setProperty('--pedido-print-zoom','1');
                  requestAnimationFrame(()=>{
                    const pixelsPorMm=96/25.4;
                    const larguraDisponivel=194*pixelsPorMm;
                    const alturaDisponivel=281*pixelsPorMm;
                    const escala=Math.min(
                      1,
                      larguraDisponivel/documentoImpressao.offsetWidth,
                      alturaDisponivel/documentoImpressao.scrollHeight
                    )*0.99;
                    documentoImpressao.style.setProperty('--pedido-print-zoom',String(escala));
                    requestAnimationFrame(()=>window.print());
                  });
                };
                const documentoImpressao=document.getElementById('pedido-impressao');
                window.addEventListener('afterprint',()=>{
                  if(documentoImpressao){
                    documentoImpressao.style.removeProperty('--pedido-print-zoom');
                  }
                });
                atualizarOpcoesModelos();
                atualizarModelo();
                sincronizarPreview();
                if({{ imprimir_ao_abrir|tojson }}){
                  window.addEventListener('load',()=>window.setTimeout(()=>window.imprimirProposta(),350),{once:true});
                }
              })();
            </script>
              {% endif %}
            </div>
            """,
            aviso_salvo=aviso_salvo,
            alerta_erro=alerta_erro,
            mostrar_feitos=mostrar_pedidos_feitos,
            pedidos_feitos_html=pedidos_feitos_html,
            modelos=list(modelos_pedido.values()),
            modelo_selecionado=modelo_selecionado_pedido,
            dados_pedido=dados_pedido,
            segmento_selecionado=dados_pedido["segmento"],
            modelos_json=modelos_json,
            cnpj_por_faturante_json=cnpj_por_faturante_json,
            vendedor=vendedor_pedido,
            telefone_vendedor=telefone_vendedor,
            email_vendedor=email_vendedor,
            pedido_salvo=pedido_salvo,
            pedido_edicao_id=pedido_edicao_id,
            imprimir_ao_abrir=bool(
                pedido_edicao_id and request.args.get("imprimir") == "1"
            ),
            campos_cliente=campos_cliente_html,
            campos_caminhao=campos_caminhao_html,
            campos_tecnicos_ocultos=campos_tecnicos_ocultos_html,
            campos_modelo_pdf=campos_modelo_pdf_html,
            campos_condicoes=campos_condicoes_html,
            campo_quantidade=campo_pedido("quantidade", "Quantidade", "number"),
            campo_valor=campo_pedido("valor_unitario", "Valor unitário (R$)"),
            campos_faturamento=campos_faturamento_html,
        )

    elif nome_modulo == "traton":
        conteudo = f"""
        <div style="height: calc(100vh - 90px); width: 100%; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); background-color: #ffffff;">
            <iframe src="https://tratonfs.github.io/finance-simulator/" style="width: 100%; height: 100%; border: none;" allowfullscreen></iframe>
        </div>
        """

    elif nome_modulo == "visitas":
        try:
            planilha = conectar_google_sheets()
            try:
                aba_negocios = planilha.worksheet("Negocios_PM")
            except gspread.exceptions.WorksheetNotFound:
                aba_negocios = planilha.add_worksheet(title="Negocios_PM", rows=1000, cols=10)
                aba_negocios.append_row(["TEMPERATURA", "DATA", "VENDEDOR", "CLIENTE", "MODELO", "PLANO DE MANUTENÇÃO", "RIO", "CONTATO DO CLIENTE", "TELEFONE", "COMENTÁRIOS"])

            usuario_logado = str(session.get("nome", "")).strip().upper()
            linhas_brutas = aba_negocios.get_all_values()

            kpis = {"total": 0, "super quente": 0, "quente": 0, "morno": 0, "frio": 0}
            registros_visitas = []

            if len(linhas_brutas) > 1:
                cabecalhos = [c.upper().strip() for c in linhas_brutas[0]]
                for idx_linha, linha in enumerate(linhas_brutas[1:], start=2):
                    item_dict = {"_index_planilha": idx_linha}
                    for i, val in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i]:
                            item_dict[cabecalhos[i]] = val

                    vend_val = str(item_dict.get('VENDEDOR', '')).strip().upper()
                    temp_val = str(item_dict.get('TEMPERATURA', '')).strip()
                    temp_lower = temp_val.lower()

                    # Restrição: Apenas registros do usuário logado E remove fechados e perdidas
                    if (
                        registro_pertence_ao_usuario(item_dict, usuario_logado)
                        and temp_lower not in ["fechado", "perdida"]
                    ):
                        registros_visitas.append(item_dict)
                        kpis["total"] += 1
                        if temp_lower in kpis:
                            kpis[temp_lower] += 1

            tabela_visitas_linhas = ""
            for reg in registros_visitas:
                temp = reg.get('TEMPERATURA', '')
                dt = reg.get('DATA', '')
                vend = reg.get('VENDEDOR', '')
                cli = reg.get('CLIENTE', '')
                mod = reg.get('MODELO', '')
                plano = reg.get('PLANO DE MANUTENÇÃO', '')
                rio = reg.get('RIO', '')
                contato = reg.get('CONTATO DO CLIENTE', '')
                tel = reg.get('TELEFONE', '')
                com = reg.get('COMENTÁRIOS', '')

                tabela_visitas_linhas += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;"><b>{temp}</b></td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{dt}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{vend}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{cli}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{mod}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{plano}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{rio}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{contato}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{tel}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7; font-size: 12px;">{com}</td>
                </tr>
                """

            if not tabela_visitas_linhas:
                tabela_visitas_linhas = '<tr><td colspan="10" style="padding: 20px; text-align: center; color: #718096;">Nenhum negócio ativo encontrado para o seu usuário.</td></tr>'

            conteudo = f"""
            <div>
                <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 14px; font-size: 17px;">{modulo_titulo}</h2>
                <p style="color: #4a5568; font-size: 13px; margin-bottom: 15px;">Acompanhamento exclusivo dos seus negócios ativos (excluindo fechados e perdidos):</p>

                <!-- KPIs Estilo Painel -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 15px;">
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #3182ce;">
                        <div style="font-size:10px; color:#718096; font-weight:700;">TOTAL ATIVOS</div>
                        <div style="font-size:20px; font-weight:bold; color:#2d3748;">{kpis['total']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #e53e3e;">
                        <div style="font-size:10px; color:#e53e3e; font-weight:700;">SUPER QUENTE</div>
                        <div style="font-size:20px; font-weight:bold; color:#e53e3e;">{kpis['super quente']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #dd6b20;">
                        <div style="font-size:10px; color:#dd6b20; font-weight:700;">QUENTE</div>
                        <div style="font-size:20px; font-weight:bold; color:#dd6b20;">{kpis['quente']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #d69e2e;">
                        <div style="font-size:10px; color:#d69e2e; font-weight:700;">MORNO</div>
                        <div style="font-size:20px; font-weight:bold; color:#d69e2e;">{kpis['morno']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #4a5568;">
                        <div style="font-size:10px; color:#4a5568; font-weight:700;">FRIO</div>
                        <div style="font-size:20px; font-weight:bold; color:#4a5568;">{kpis['frio']}</div>
                    </div>
                </div>

                <!-- Tabela de Acompanhamento (Somente Leitura) -->
                <div class="produto-detalhe-card negocios-tabela-card">
                    <div class="negocios-tabela-wrap">
                        <table class="negocios-tabela">
                            <thead>
                                <tr style="background: #002244; color: #ffffff;">
                                    <th style="padding: 10px;">Temp.</th>
                                    <th style="padding: 10px;">Data</th>
                                    <th style="padding: 10px;">Vendedor</th>
                                    <th style="padding: 10px;">Cliente</th>
                                    <th style="padding: 10px;">Modelo</th>
                                    <th style="padding: 10px;">Plano</th>
                                    <th style="padding: 10px;">RIO</th>
                                    <th style="padding: 10px;">Contato</th>
                                    <th style="padding: 10px;">Telefone</th>
                                    <th style="padding: 10px;">Comentários</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tabela_visitas_linhas}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px;"><b>Erro ao carregar Visitas:</b> {e}</div>'
    elif nome_modulo in ["locacao_vendas", "consorcio_vendas"]:
        nome_aba_planilha = "Vendas_LOC" if nome_modulo == "locacao_vendas" else "Vendas_Consorcio"
        try:
            planilha = conectar_google_sheets()
            is_gestao_operacional = usuario_eh_gestao(session.get("perfil"))
            usuario_operacional = str(session.get("nome", "")).strip()
            try:
                aba_vendas = planilha.worksheet(nome_aba_planilha)
            except gspread.exceptions.WorksheetNotFound:
                aba_vendas = planilha.add_worksheet(title=nome_aba_planilha, rows=1000, cols=9)
                aba_vendas.append_row(["CLIENTE", "PRODUTO", "DATA DA VENDA", "MODELO", "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "ANEXO 3"])

            mapa_vendedor_estado = {}
            try:
                aba_usuarios_l = planilha.worksheet("Usuarios")
                regs_u = obter_registros_seguros(aba_usuarios_l)
                for u in regs_u:
                    n_u = str(u.get("NOME", "")).strip()
                    perfil_u = str(u.get("PERFIL", "")).strip().upper()
                    estado = "AL" if "AL" in perfil_u else "PE"
                    if n_u:
                        mapa_vendedor_estado[n_u.lower()] = estado
            except Exception:
                pass

            mapa_modelo_familia = {}
            try:
                aba_mod_pesquisa = planilha.worksheet("Modelos")
                regs_mod = obter_registros_seguros(aba_mod_pesquisa)
                for rm in regs_mod:
                    m_nome = str(rm.get("MODELO", "")).strip().lower()
                    m_cat = str(rm.get("CATEGORIA", "")).strip().lower()
                    m_tipo = str(rm.get("TIPO", "")).strip().lower()
                    if m_nome:
                        mapa_modelo_familia[m_nome] = f"{m_nome} {m_cat} {m_tipo}"
            except Exception:
                pass

            nome_aba_neg_sync = "Negocio_LOC" if nome_modulo == "locacao_vendas" else "Negocios_Consorcio"
            try:
                aba_neg_sync = planilha.worksheet(nome_aba_neg_sync)
                regs_neg = obter_registros_seguros(aba_neg_sync)
                regs_vendas_atuais = obter_registros_seguros(aba_vendas)
                clientes_ja_em_vendas = set(str(r.get("CLIENTE", "")).strip().lower() for r in regs_vendas_atuais)

                for rn in regs_neg:
                    if (
                        not is_gestao_operacional
                        and not registro_pertence_ao_usuario(rn, usuario_operacional)
                    ):
                        continue
                    temp_n = str(rn.get("TEMPERATURA", "")).strip().lower()
                    if temp_n == "fechado":
                        cli_n = str(rn.get("CLIENTE", "")).strip()
                        if cli_n and cli_n.lower() not in clientes_ja_em_vendas:
                            data_n = str(rn.get("DATA", "")).strip()
                            vend_n = str(rn.get("VENDEDOR", "")).strip()
                            mod_n = str(rn.get("MODELO", "")).strip()
                            pm_n = str(rn.get("PLANO DE MANUTENÇÃO", "")).strip()
                            rio_n = str(rn.get("RIO", "")).strip()
                            prod_n = f"{pm_n} / {rio_n}".strip(" /")
                            aba_vendas.append_row([cli_n, prod_n, data_n, mod_n, "1", vend_n, "", "", ""])
                            invalidar_cache_ab_as(nome_aba_planilha)
                            clientes_ja_em_vendas.add(cli_n.lower())
            except Exception:
                pass

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if not is_gestao_operacional:
                    indice_alvo = request.form.get(
                        "index_linha" if acao_form == "excluir" else "index_edicao",
                        "",
                    ).strip()
                    if acao_form == "excluir" and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_vendas, int(indice_alvo), usuario_operacional
                        )
                    ):
                        abort(403)
                    if acao_form == "cadastrar" and indice_alvo and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_vendas, int(indice_alvo), usuario_operacional
                        )
                    ):
                        abort(403)
                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_vendas.delete_rows(index_linha)
                        invalidar_cache_ab_as(nome_aba_planilha)
                        sucesso_msg = "Registro excluído com sucesso!"
                elif acao_form == "cadastrar":
                    index_edicao = request.form.get("index_edicao", "").strip()
                    cliente_v = request.form.get("cliente", "").strip()
                    produto_v = request.form.get("produto", "").strip()
                    data_v = request.form.get("data_venda", "").strip()
                    modelo_v = request.form.get("modelo", "").strip()
                    qtd_v = request.form.get("quantidade", "").strip()
                    vendedor_v = (
                        request.form.get("vendedor", "").strip()
                        if is_gestao_operacional else usuario_operacional
                    )
                    
                    anexos = ["", "", ""]
                    if index_edicao:
                        try:
                            linha_atual = aba_vendas.row_values(int(index_edicao))
                            if len(linha_atual) >= 7: anexos[0] = linha_atual[6]
                            if len(linha_atual) >= 8: anexos[1] = linha_atual[7]
                            if len(linha_atual) >= 9: anexos[2] = linha_atual[8]
                        except Exception:
                            pass

                    falha_upload = False
                    for idx_file in range(3):
                        file_key = f"anexo_{idx_file+1}"
                        if file_key in request.files:
                            file_obj = request.files[file_key]
                            if file_obj and file_obj.filename:
                                try:
                                    anexos[idx_file] = subir_comprovante_google_drive(
                                        file_obj,
                                        permitir_fallback_local=not bool(os.environ.get("RENDER")),
                                    )
                                except Exception as erro:
                                    print(f"Erro ao salvar comprovante da venda: {erro}")
                                    erro_msg = "Não foi possível salvar o comprovante no Drive. A venda não foi gravada; tente novamente."
                                    falha_upload = True
                                    break

                    if cliente_v and not falha_upload:
                        dados_venda_linha = [cliente_v, produto_v, data_v, modelo_v, qtd_v, vendedor_v, anexos[0], anexos[1], anexos[2]]
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_vendas.update(f"A{idx_int}:I{idx_int}", [dados_venda_linha])
                            invalidar_cache_ab_as(nome_aba_planilha)
                            sucesso_msg = "Registro atualizado com sucesso!"
                        else:
                            aba_vendas.append_row(dados_venda_linha)
                            invalidar_cache_ab_as(nome_aba_planilha)
                            sucesso_msg = "Registro salvo com sucesso!"
                    elif not cliente_v and not erro_msg:
                        erro_msg = "Informe o cliente."

            linhas_vendas_brutas = aba_vendas.get_all_values()
            mes_selecionado = request.args.get("mes", "todos").strip().lower()

            meses_nomes = {
                "anual": "Anual (Todos)",
                "semestre1": "1º Semestre (Jan a Jun)",
                "semestre2": "2º Semestre (Jul a Dez)",
                "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
                "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
                "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro"
            }
            options_meses = ''
            for k, v in meses_nomes.items():
                sel = ' selected' if mes_selecionado == k else ''
                options_meses += f'<option value="{k}"{sel}>{v}</option>'

            registros_vendas_filtrados = []
            if len(linhas_vendas_brutas) > 1:
                cab_v = [c.upper().strip() for c in linhas_vendas_brutas[0]]
                for idx_l, linha_v in enumerate(linhas_vendas_brutas[1:], start=2):
                    dict_v = {"_index_planilha": idx_l}
                    for i, val in enumerate(linha_v):
                        if i < len(cab_v) and cab_v[i]:
                            dict_v[cab_v[i]] = val

                    if (
                        not is_gestao_operacional
                        and not registro_pertence_ao_usuario(dict_v, usuario_operacional)
                    ):
                        continue

                    data_venda_val = dict_v.get('DATA DA VENDA', '').strip()
                    match_mes = re.search(r'^\d{1,2}/(\d{1,2})/(?:\d{2}|\d{4})', data_venda_val)
                    mes_item = match_mes.group(1).zfill(2) if match_mes else ""

                    incluir_registro = False
                    if mes_selecionado in ["todos", "anual", ""]:
                        incluir_registro = True
                    elif mes_selecionado == "semestre1" and mes_item in ["01","02","03","04","05","06"]:
                        incluir_registro = True
                    elif mes_selecionado == "semestre2" and mes_item in ["07","08","09","10","11","12"]:
                        incluir_registro = True
                    elif mes_selecionado == mes_item:
                        incluir_registro = True

                    if incluir_registro:
                        dt_obj = datetime.max
                        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                            try:
                                dt_obj = datetime.strptime(data_venda_val, fmt)
                                break
                            except ValueError:
                                pass
                        dict_v["_dt_obj"] = dt_obj
                        registros_vendas_filtrados.append(dict_v)

            registros_vendas_filtrados.sort(key=lambda x: x["_dt_obj"])

            tabela_vendas_linhas = ""
            contador_vendas = 0

            for reg in registros_vendas_filtrados:
                contador_vendas += 1
                idx_l = reg["_index_planilha"]
                cli = reg.get('CLIENTE', '')
                prod = reg.get('PRODUTO', '')
                if not str(prod).strip():
                    prod = reg.get('PLANO DE MANUTENÇÃO', '') or reg.get('P. MANUTENÇÃO', '') or reg.get('PLANO', '')
                dt_v = reg.get('DATA DA VENDA', '')
                mod = reg.get('MODELO', '')
                qtd_str = reg.get('QUANTIDADE', '1')
                vend = reg.get('VENDEDOR', 'Desconhecido')
                estado_v = mapa_vendedor_estado.get(vend.strip().lower(), "PE")

                anexos_html = ""
                for anexo_idx in range(1, 4):
                    link_anexo = reg.get(f'ANEXO {anexo_idx}', '')
                    if link_anexo:
                        url_imagem = url_comprovante_no_app(link_anexo)
                        if url_imagem:
                            url_segura = html.escape(url_imagem, quote=True)
                            anexos_html += f'''
                            <div onclick="abrirImagemModal('{url_segura}')" title="Clique para ampliar" style="display: inline-block; margin-right: 12px; cursor: pointer; background: #fff; padding: 4px; border: 1px solid #cbd5e0; border-radius: 4px;">
                                <img src="{url_segura}" alt="Anexo {anexo_idx}" class="img-comprovacao">
                            </div>
                            '''
                        else:
                            anexos_html += f'<span style="display:inline-block;margin-right:12px;color:#a33;">Comprovante {anexo_idx} indisponível; reenvie o arquivo.</span>'

                botoes_v = f"""
                <div style="display: flex; gap: 4px;">
                    <button type="button" class="btn-acao btn-editar no-print" onclick="carregarVendaParaEdicao({idx_l}, '{cli}', '{prod}', '{dt_v}', '{mod}', '{qtd_str}', '{vend}')">Alterar</button>
                    <button type="button" class="btn-acao btn-excluir no-print" onclick="excluirVenda({idx_l}, '{nome_modulo}')">Excluir</button>
                </div>
                """

                tabela_vendas_linhas += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: none;"><b>{cli}</b></td>
                    <td style="padding: 10px; border-bottom: none;">{prod}</td>
                    <td style="padding: 10px; border-bottom: none;">{dt_v}</td>
                    <td style="padding: 10px; border-bottom: none;">{mod}</td>
                    <td style="padding: 10px; border-bottom: none;">{qtd_str}</td>
                    <td style="padding: 10px; border-bottom: none;">{vend} ({estado_v})</td>
                    <td style="padding: 10px; border-bottom: none;" class="no-print">-</td>
                    <td style="padding: 10px; border-bottom: none;" class="no-print">{botoes_v}</td>
                </tr>
                <tr style="background-color: #fafbfc;">
                    <td colspan="9" style="padding: 8px 10px 12px 10px; border-bottom: 1px solid #edf2f7;">
                        <span style="font-size: 11px; font-weight: 700; color: #4a5568; text-transform: uppercase; display: block; margin-bottom: 4px;">Comprovações / Anexos:</span>
                        {anexos_html if anexos_html else '<span style="color: #a0aec0; font-size: 12px;">Nenhum anexo enviado.</span>'}
                    </td>
                </tr>
                """

            if contador_vendas == 0:
                tabela_vendas_linhas = '<tr><td colspan="8" style="padding: 20px; text-align: center; color: #718096;">Nenhum registro encontrado para este filtro.</td></tr>'

            conteudo = f"""
            <div>
                <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 14px; font-size: 17px;">{modulo_titulo}</h2>
                
                {f'<div class="sucesso">{sucesso_msg}</div>' if sucesso_msg else ''}
                {f'<div class="error">{erro_msg}</div>' if erro_msg else ''}

                <div class="produto-detalhe-card" style="margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <h3 id="tituloFormVendaCard" style="font-size: 15px; color: #002244; margin: 0;">Registrar Novo Contrato / Venda</h3>
                        <button type="button" id="btnCancelarEdicaoVenda" onclick="cancelarEdicaoVenda()" style="display: none; background: #cbd5e0; border: none; padding: 4px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; font-weight: 600;">Cancelar Edição</button>
                    </div>

                    <form method="POST" enctype="multipart/form-data">
                        <input type="hidden" name="acao_form" value="cadastrar">
                        <input type="hidden" id="editVendaIndexInput" name="index_edicao" value="">

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Cliente</label>
                                <input type="text" name="cliente" placeholder="Nome do Cliente / Empresa" required>
                            </div>
                            <div>
                                <label>Produto / Detalhes</label>
                                <input type="text" name="produto" placeholder="Descrição do contrato" required>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Data</label>
                                <input type="text" name="data_venda" value="{datetime.now().strftime('%d/%m/%Y')}" required>
                            </div>
                            <div>
                                <label>Modelo do Veículo</label>
                                <input type="text" name="modelo" placeholder="Ex: Delivery 11.180">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Quantidade</label>
                                <input type="text" name="quantidade" value="1" required>
                            </div>
                            <div>
                                <label>Vendedor</label>
                                <input type="text" name="vendedor" value="{nome_usuario_logado}" readonly style="background-color: #edf2f7;">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Anexo 1</label>
                                <input type="file" name="anexo_1" accept="image/*" capture="environment">
                            </div>
                            <div>
                                <label>Anexo 2 (Opcional)</label>
                                <input type="file" name="anexo_2" accept="image/*" capture="environment">
                            </div>
                            <div>
                                <label>Anexo 3 (Opcional)</label>
                                <input type="file" name="anexo_3" accept="image/*" capture="environment">
                            </div>
                        </div>

                        <div style="display: flex; gap: 10px; align-items: center; margin-top: 15px; flex-wrap: wrap;">
                            <button type="submit" id="btnSubmitVendaForm" class="btn-login" style="flex: 2; margin: 0;">Salvar Registro</button>
                            <select id="filtroMesSelect" onchange="window.location.href='/modulo/{nome_modulo}?mes=' + this.value" style="flex: 1; padding: 12px; font-size: 14px; border-radius: 6px; border: 1px solid #cbd5e0; background: #fff; font-weight: 600; height: 46px; margin: 0;">
                                {options_meses}
                            </select>
                            <button type="button" onclick="gerarPDFRelatorio()" class="btn-acao btn-pdf" style="flex: 1.5; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📄 Gerar PDF</button>
                        </div>
                    </form>
                </div>

                <div id="secaoRelatorioPDF" class="produto-detalhe-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #002244; padding-bottom: 10px; margin-bottom: 14px;">
                        <div>
                            <h3 style="font-size: 16px; color: #002244; margin: 0 0 4px 0;">{modulo_titulo}</h3>
                            <p style="font-size: 12px; color: #4a5568; margin: 0;">Emitido por: <b>{nome_usuario_logado}</b> em {datetime.now().strftime('%d/%m/%Y às %H:%M')}</p>
                        </div>
                        <div>
                            <img src="{url_for('static', filename='logo.png')}" alt="Novo Mundo" style="max-height: 40px; width: auto;">
                        </div>
                    </div>

                    <div style="overflow-x: auto;">
                        <table id="tabelaVendas" style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" data-sort-dir="asc">
                            <thead>
                                <tr style="background: #002244; color: #ffffff; border-bottom: 2px solid #001529;">
                                    <th style="padding: 10px;">Cliente</th>
                                    <th style="padding: 10px;">Produto</th>
                                    <th style="padding: 10px;">Data</th>
                                    <th style="padding: 10px;">Modelo</th>
                                    <th style="padding: 10px;">Qtd</th>
                                    <th style="padding: 10px;">Vendedor</th>
                                    <th style="padding: 10px;" class="no-print">Comprovação</th>
                                    <th style="padding: 10px;" class="no-print">Ações</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tabela_vendas_linhas}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """
        except HTTPException:
            raise
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar o módulo:</b> {e}</div>'

    elif nome_modulo in ["locacao_negocios", "consorcio_negocios"]:
        nome_aba_planilha = "Negocio_LOC" if nome_modulo == "locacao_negocios" else "Negocios_Consorcio"
        nome_aba_vendas_sync = "Vendas_LOC" if nome_modulo == "locacao_negocios" else "Vendas_Consorcio"

        try:
            planilha = conectar_google_sheets()
            is_gestao_operacional = usuario_eh_gestao(session.get("perfil"))
            usuario_operacional = str(session.get("nome", "")).strip()
            try:
                aba_negocios = planilha.worksheet(nome_aba_planilha)
            except gspread.exceptions.WorksheetNotFound:
                aba_negocios = planilha.add_worksheet(title=nome_aba_planilha, rows=1000, cols=9)
                aba_negocios.append_row(["TEMPERATURA", "DATA", "VENDEDOR", "CLIENTE", "MODELO", "PLANO DE MANUTENÇÃO", "RIO", "CONTATO DO CLIENTE", "COMENTÁRIOS"])

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if not is_gestao_operacional:
                    indice_alvo = request.form.get(
                        "index_linha" if acao_form == "excluir" else "index_edicao",
                        "",
                    ).strip()
                    if acao_form == "excluir" and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_negocios, int(indice_alvo), usuario_operacional
                        )
                    ):
                        abort(403)
                    if acao_form != "excluir" and indice_alvo and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_negocios, int(indice_alvo), usuario_operacional
                        )
                    ):
                        abort(403)

                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_negocios.delete_rows(index_linha)
                        invalidar_cache_ab_as(nome_aba_planilha)
                        sucesso_msg = "Registro excluído com sucesso!"
                else:
                    index_edicao = request.form.get("index_edicao", "").strip()
                    temperatura = request.form.get("temperatura", "").strip()
                    data_neg = request.form.get("data", "").strip()
                    vendedor_form = (
                        request.form.get("vendedor", "").strip()
                        if is_gestao_operacional else usuario_operacional
                    )
                    cliente = request.form.get("cliente", "").strip()
                    modelo = request.form.get("modelo", "").strip()
                    plano_manutencao = request.form.get("plano_manutencao", "").strip()
                    rio_val = request.form.get("rio", "").strip()
                    contato = request.form.get("contato", "").strip()
                    comentarios = request.form.get("comentarios", "").strip()

                    if cliente and vendedor_form:
                        dados_linha = [temperatura, data_neg, vendedor_form, cliente, modelo, plano_manutencao, rio_val, contato, comentarios]
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_negocios.update(f"A{idx_int}:I{idx_int}", [dados_linha])
                            invalidar_cache_ab_as(nome_aba_planilha)
                            sucesso_msg = "Negócio atualizado com sucesso!"
                        else:
                            aba_negocios.append_row(dados_linha)
                            invalidar_cache_ab_as(nome_aba_planilha)
                            sucesso_msg = "Negócio cadastrado com sucesso!"

                        if temperatura.strip().lower() == "fechado":
                            try:
                                try:
                                    aba_vendas_sync = planilha.worksheet(nome_aba_vendas_sync)
                                except gspread.exceptions.WorksheetNotFound:
                                    aba_vendas_sync = planilha.add_worksheet(title=nome_aba_vendas_sync, rows=1000, cols=9)
                                    aba_vendas_sync.append_row(["CLIENTE", "PRODUTO", "DATA DA VENDA", "MODELO", "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "ANEXO 3"])
                                
                                produto_combinado = f"{plano_manutencao} / {rio_val}".strip(" /")
                                aba_vendas_sync.append_row([cliente, produto_combinado, data_neg, modelo, "1", vendedor_form, "", "", ""])
                                sucesso_msg += " Negócio fechado sincronizado automaticamente!"
                            except Exception as sync_err:
                                print(f"Erro ao sincronizar: {sync_err}")
                    else:
                        erro_msg = "Preencha ao menos o Cliente e o Vendedor."

            aba_usuarios = planilha.worksheet("Usuarios")
            registros_usuarios = obter_registros_seguros(aba_usuarios)
            lista_consultores = []
            for u in registros_usuarios:
                perfil_u = str(u.get("PERFIL", "")).strip().upper()
                nome_u = str(u.get("NOME", "")).strip()
                if "CONSULTOR" in perfil_u and nome_u:
                    lista_consultores.append(nome_u)
            if not lista_consultores:
                lista_consultores = [session.get("nome", "Usuário")]
            if not is_gestao_operacional:
                lista_consultores = [usuario_operacional] if usuario_operacional else []

            aba_modelos = planilha.worksheet("Modelos")
            registros_modelos = obter_registros_seguros(aba_modelos)
            # 2. Busca dinâmica de Modelos
            lista_modelos = []
            try:
                aba_modelos = planilha.worksheet("Modelos")
                regs_m = obter_registros_seguros(aba_modelos)
                for rm in regs_m:
                    val_m = str(rm.get("MODELO", list(rm.values())[1] if len(rm) > 1 else (list(rm.values())[0] if rm else ""))).strip()
                    if val_m and val_m not in lista_modelos and val_m.upper() != "PRODUTO":
                        lista_modelos.append(val_m)
            except Exception:
                pass
            if not lista_modelos:
                lista_modelos = ["Delivery 11.180", "Constellation 24.280", "Meteor 28.460", "Meteor 29.530", "26.260 6x2", "30.320 8x2", "EXPRESS"]

            lista_planos_manutencao = ["PREV", "MAX", "PLUS"]
            lista_tipos_rio = ["Contrato Padrão", "Especial"]
            lista_temperaturas = ["Fechado", "Quente", "Super Quente", "Frio", "Morno"]

            linhas_brutas = aba_negocios.get_all_values()
            mes_selecionado = request.args.get("mes", "todos").strip().lower()

            options_consultores = "".join([f'<option value="{c}">{c}</option>' for c in lista_consultores])
            options_modelos = "".join([f'<option value="{m}">{m}</option>' for m in lista_modelos])
            options_planos_manutencao = "".join([f'<option value="{p}">{p}</option>' for p in lista_planos_manutencao])
            options_rio = "".join([f'<option value="{r}">{r}</option>' for r in lista_tipos_rio])
            options_temperaturas = "".join([f'<option value="{t}">{t}</option>' for t in lista_temperaturas])

            meses_nomes = {
                "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
                "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
                "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro"
            }
            options_meses = '<option value="todos"' + (' selected' if mes_selecionado == 'todos' else '') + '>Todos os Meses</option>'
            for k, v in meses_nomes.items():
                sel = ' selected' if mes_selecionado == k else ''
                options_meses += f'<option value="{k}"{sel}>{v}</option>'

            registros_filtrados_ordenados = []
            if len(linhas_brutas) > 1:
                cabecalhos = [c.upper().strip() for c in linhas_brutas[0]]
                for idx_linha, linha in enumerate(linhas_brutas[1:], start=2):
                    item_dict = {"_index_planilha": idx_linha}
                    for i, val in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i]:
                            item_dict[cabecalhos[i]] = val

                    if (
                        not is_gestao_operacional
                        and not registro_pertence_ao_usuario(item_dict, usuario_operacional)
                    ):
                        continue

                    temp_val = item_dict.get('TEMPERATURA','')
                    data_val = item_dict.get('DATA','').strip()

                    match_mes = re.search(r'^\d{1,2}/(\d{1,2})/(?:\d{2}|\d{4})', data_val)
                    mes_item = match_mes.group(1).zfill(2) if match_mes else ""

                    if temp_val.strip().lower() != "fechado":
                        if mes_selecionado == "todos" or mes_item == mes_selecionado:
                            dt_obj = datetime.max
                            for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                                try:
                                    dt_obj = datetime.strptime(data_val, fmt)
                                    break
                                except ValueError:
                                    pass
                            item_dict["_dt_obj"] = dt_obj
                            registros_filtrados_ordenados.append(item_dict)

            registros_filtrados_ordenados.sort(key=lambda x: x["_dt_obj"])

            tabela_linhas = ""
            contador_ativos = 0

            for reg in registros_filtrados_ordenados:
                contador_ativos += 1
                idx_linha = reg["_index_planilha"]
                temp_val = reg.get('TEMPERATURA','')
                data_val = reg.get('DATA','')
                vend_val = reg.get('VENDEDOR','')
                cli_val = reg.get('CLIENTE','')
                mod_val = reg.get('MODELO','')
                pm_val = reg.get('PLANO DE MANUTENÇÃO','')
                rio_val = reg.get('RIO','')
                cont_val = reg.get('CONTATO DO CLIENTE','')
                com_val = reg.get('COMENTÁRIOS','')

                botoes_acoes_html = f"""
                <div style="display: flex; gap: 4px;">
                    <button type="button" class="btn-acao btn-editar no-print" onclick="carregarParaEdicao({idx_linha}, '{temp_val}', '{data_val}', '{vend_val}', '{cli_val}', '{mod_val}', '{pm_val}', '{rio_val}', '{cont_val}', '', '{com_val}')">Alterar</button>
                    <button type="button" class="btn-acao btn-excluir no-print" onclick="excluirNegocio({idx_linha}, '{nome_modulo}')">Excluir</button>
                </div>
                """

                tabela_linhas += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;"><b>{temp_val}</b></td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{data_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{vend_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{cli_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{mod_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{pm_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{rio_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{cont_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7; font-size: 12px;">{com_val}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{botoes_acoes_html}</td>
                </tr>
                """

            if contador_ativos == 0:
                tabela_linhas = '<tr><td colspan="10" style="padding: 20px; text-align: center; color: #718096;">Nenhum registro encontrado para este filtro.</td></tr>'

            conteudo = f"""
            <div>
                <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 14px; font-size: 17px;">{modulo_titulo}</h2>
                
                {f'<div class="sucesso">{sucesso_msg}</div>' if sucesso_msg else ''}
                {f'<div class="error">{erro_msg}</div>' if erro_msg else ''}

                <div class="produto-detalhe-card" style="margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <h3 id="tituloFormCard" style="font-size: 15px; color: #002244; margin: 0;">Registrar Nova Negociação</h3>
                        <button type="button" id="btnCancelarEdicao" onclick="cancelarEdicao()" style="display: none; background: #cbd5e0; border: none; padding: 4px 10px; border-radius: 4px; font-size: 12px; cursor: pointer; font-weight: 600;">Cancelar Edição</button>
                    </div>

                    <form method="POST">
                        <input type="hidden" name="acao_form" value="cadastrar">
                        <input type="hidden" id="editIndexInput" name="index_edicao" value="">

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Temperatura</label>
                                <select name="temperatura" required>
                                    {options_temperaturas}
                                </select>
                            </div>
                            <div>
                                <label>Data</label>
                                <input type="text" name="data" value="{datetime.now().strftime('%d/%m/%Y')}" required>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Vendedor (Consultor)</label>
                                <select name="vendedor" required>
                                    {options_consultores}
                                </select>
                            </div>
                            <div>
                                <label>Cliente</label>
                                <input type="text" name="cliente" placeholder="Nome do Cliente / Empresa" required>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Modelo (Veículo)</label>
                                <select name="modelo" required>
                                    {options_modelos}
                                </select>
                            </div>
                            <div>
                                <label>Condição / Plano</label>
                                <select name="plano_manutencao">
                                    <option value="">Nenhum</option>
                                    {options_planos_manutencao}
                                </select>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Detalhes Adicionais</label>
                                <select name="rio">
                                    <option value="">Nenhum</option>
                                    {options_rio}
                                </select>
                            </div>
                            <div>
                                <label>Contato do Cliente</label>
                                <input type="text" name="contato" placeholder="Nome do contato">
                            </div>
                        </div>

                        <div class="input-group">
                            <label>Comentários / Acompanhamento</label>
                            <textarea name="comentarios" rows="3" placeholder="Descreva o andamento da negociação..."></textarea>
                        </div>

                        <div style="display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap;">
                            <button type="submit" id="btnSubmitForm" class="btn-login" style="flex: 2; margin: 0;">Salvar Negociação</button>
                            <select id="filtroMesSelect" onchange="window.location.href='/modulo/{nome_modulo}?mes=' + this.value" style="flex: 1; padding: 12px; font-size: 14px; border-radius: 6px; border: 1px solid #cbd5e0; background: #fff; font-weight: 600; height: 46px; margin: 0;">
                                {options_meses}
                            </select>
                            <button type="button" onclick="gerarPDFRelatorio()" class="btn-acao btn-pdf" style="flex: 1.5; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📄 Gerar PDF</button>
                        </div>
                    </form>
                </div>

                <div id="secaoRelatorioPDF" class="produto-detalhe-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #002244; padding-bottom: 10px; margin-bottom: 14px;">
                        <div>
                            <h3 style="font-size: 16px; color: #002244; margin: 0 0 4px 0;">{modulo_titulo}</h3>
                            <p style="font-size: 12px; color: #4a5568; margin: 0;">Emitido por: <b>{nome_usuario_logado}</b> em {datetime.now().strftime('%d/%m/%Y às %H:%M')}</p>
                        </div>
                        <div>
                            <img src="{url_for('static', filename='logo.png')}" alt="Novo Mundo" style="max-height: 40px; width: auto;">
                        </div>
                    </div>

                    <div style="overflow-x: auto;">
                        <table id="tabelaNegocios" style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" data-sort-dir="asc">
                            <thead>
                                <tr style="background: #002244; color: #ffffff; border-bottom: 2px solid #001529;">
                                    <th style="padding: 10px;">Temp.</th>
                                    <th style="padding: 10px;">Data</th>
                                    <th style="padding: 10px;">Vendedor</th>
                                    <th style="padding: 10px;">Cliente</th>
                                    <th style="padding: 10px;">Modelo</th>
                                    <th style="padding: 10px;">Plano</th>
                                    <th style="padding: 10px;">Info</th>
                                    <th style="padding: 10px;">Contato</th>
                                    <th style="padding: 10px;">Comentários</th>
                                    <th style="padding: 10px;" class="no-print">Ações</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tabela_linhas}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            """
        except HTTPException:
            raise
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar o módulo:</b> {e}</div>'

    elif nome_modulo == "vendas":
        try:
            # Sincronização automática dos relatórios da pasta Rel_Vendas do Drive
         
            planilha = conectar_google_sheets()
            is_gestao_vendas = usuario_eh_gestao(session.get("perfil"))
            usuario_vendas = str(session.get("nome", "")).strip()
            try:
                aba_vendas = planilha.worksheet("Vendas_PM")
            except gspread.exceptions.WorksheetNotFound:
                aba_vendas = planilha.add_worksheet(title="Vendas_PM", rows=1000, cols=11)
                aba_vendas.append_row([
                    "CLIENTE", "P. MANUTENÇÃO", "RIO", "DATA DA VENDA", "MODELO",
                    "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "Nº DO CONTRATO",
                    "CHASSIS"
                ])

            garantir_colunas_venda_pm(aba_vendas)
            if is_gestao_vendas:
                migrar_comprovantes_static(aba_vendas)

            mapa_vendedor_estado = {}
            try:
                aba_usuarios_l = planilha.worksheet("Usuarios")
                regs_u = obter_registros_seguros(aba_usuarios_l)
                for u in regs_u:
                    n_u = str(u.get("NOME", "")).strip()
                    perfil_u = str(u.get("PERFIL", "")).strip().upper()
                    
                    estado = "PE"
                    if "AL" in perfil_u:
                        estado = "AL"
                    elif "PE" in perfil_u:
                        estado = "PE"
                    
                    if n_u:
                        mapa_vendedor_estado[n_u.lower()] = estado
            except Exception as e_est:
                print(f"Aviso mapeamento de estado: {e_est}")

            mapa_modelo_familia = {}
            lista_modelos = []
            try:
                aba_mod_pesquisa = planilha.worksheet("Modelos")
                regs_mod = obter_registros_seguros(aba_mod_pesquisa)
                for rm in regs_mod:
                    valores_modelo = list(rm.values())
                    modelo_nome = str(
                        rm.get("MODELO")
                        or (valores_modelo[1] if len(valores_modelo) > 1 else "")
                    ).strip()
                    m_nome = modelo_nome.lower()
                    m_cat = str(rm.get("CATEGORIA", "")).strip().lower()
                    m_tipo = str(rm.get("TIPO", "")).strip().lower()
                    texto_completo_mod = f"{m_nome} {m_cat} {m_tipo}"
                    if m_nome:
                        mapa_modelo_familia[m_nome] = texto_completo_mod
                        if modelo_nome not in lista_modelos:
                            lista_modelos.append(modelo_nome)
            except Exception as e_fam:
                print(f"Aviso mapeamento de modelos: {e_fam}")

            lista_planos_manutencao = []
            for registro in obter_registros_com_cache(planilha, "PM"):
                valores_registro = list(registro.values())
                plano = str(
                    registro.get("PRODUTO")
                    or (valores_registro[1] if len(valores_registro) > 1 else "")
                ).strip()
                if plano and normalizar_chave_planilha(plano).upper() != "PRODUTO" and plano not in lista_planos_manutencao:
                    lista_planos_manutencao.append(plano)
            if not lista_planos_manutencao:
                lista_planos_manutencao = [
                    "VolksTotal PRE Prevenção e Economia", "VolksTotal MAX",
                    "VolksTotal PLUS", "PREV", "MAX", "PLUS",
                ]

            lista_tipos_rio = []
            for registro in obter_registros_com_cache(planilha, "RIO"):
                valores_registro = list(registro.values())
                tipo_rio = str(
                    registro.get("PRODUTO")
                    or (valores_registro[1] if len(valores_registro) > 1 else "")
                ).strip()
                if tipo_rio and normalizar_chave_planilha(tipo_rio).upper() != "PRODUTO" and tipo_rio not in lista_tipos_rio:
                    lista_tipos_rio.append(tipo_rio)
            if not lista_tipos_rio:
                lista_tipos_rio = [
                    "Diagnóstico Remoto", "Análise de Eficiência", "Performance",
                    "RIO GEO", "Relatório de Bloqueio",
                ]

            opcoes_planos_manutencao = "".join(
                f'<option value="{html.escape(plano, quote=True)}">{html.escape(plano)}</option>'
                for plano in lista_planos_manutencao
            )
            opcoes_tipos_rio = "".join(
                f'<option value="{html.escape(tipo, quote=True)}">{html.escape(tipo)}</option>'
                for tipo in lista_tipos_rio
            )

            try:
                aba_neg_sync = planilha.worksheet("Negocios_PM")
                regs_neg = obter_registros_seguros(aba_neg_sync)
                regs_vendas_atuais = obter_registros_seguros(aba_vendas)
                chaves_vendas = {chave_venda_pm(registro) for registro in regs_vendas_atuais}

                # A partir daqui, qualquer registro que ainda esteja em
                # Negocios_PM com status Fechado é concluído automaticamente.
                # Processamos de baixo para cima porque a exclusão de uma linha
                # altera os índices das linhas que estão acima dela.
                negocios_fechados_pendentes = [
                    (idx_neg, rn)
                    for idx_neg, rn in enumerate(regs_neg, start=2)
                    if str(rn.get("TEMPERATURA", "")).strip().lower() == "fechado"
                    and (
                        is_gestao_vendas
                        or registro_pertence_ao_usuario(rn, usuario_vendas)
                    )
                ]
                for idx_neg, rn in reversed(negocios_fechados_pendentes):
                    try:
                        mover_negocio_fechado_para_vendas(
                            aba_neg_sync, aba_vendas, idx_neg, rn, chaves_vendas
                        )
                    except Exception as exc_mover:
                        print(f"Aviso ao mover negócio fechado linha {idx_neg}: {exc_mover}")
            except Exception as e_sync_retroativa:
                print(f"Aviso sync retroativa: {e_sync_retroativa}")

            # Busca dinâmica de Vendedores (Apenas quem tem CONSULTOR no perfil)
            aba_usuarios = planilha.worksheet("Usuarios")
            registros_usuarios = obter_registros_seguros(aba_usuarios)
            lista_consultores = []
            for u in registros_usuarios:
                perfil_u = str(u.get("PERFIL", "")).strip().upper()
                nome_u = str(u.get("NOME", "")).strip()
                if "CONSULTOR" in perfil_u and nome_u:
                    if nome_u not in lista_consultores:
                        lista_consultores.append(nome_u)
            if not lista_consultores:
                lista_consultores = [session.get("nome", "Usuário")]
            if not is_gestao_vendas:
                lista_consultores = [usuario_vendas] if usuario_vendas else []

            nome_vendedor_logado = str(nome_usuario_logado or "").strip().lower()
            opcoes_modelos = "".join(
                f'<option value="{html.escape(modelo, quote=True)}">{html.escape(modelo)}</option>'
                for modelo in lista_modelos
            )
            opcoes_consultores = "".join(
                f'<option value="{html.escape(consultor, quote=True)}" '
                f'{"selected" if consultor.strip().lower() == nome_vendedor_logado else ""}>'
                f'{html.escape(consultor)}</option>'
                for consultor in lista_consultores
            )

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if not is_gestao_vendas:
                    indice_alvo = request.form.get(
                        "index_linha" if acao_form == "excluir" else "index_edicao",
                        "",
                    ).strip()
                    if acao_form == "excluir" and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_vendas, int(indice_alvo), usuario_vendas
                        )
                    ):
                        abort(403)
                    if acao_form == "cadastrar" and indice_alvo and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_vendas, int(indice_alvo), usuario_vendas
                        )
                    ):
                        abort(403)
                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_vendas.delete_rows(index_linha)
                        invalidar_cache_ab_as("Vendas_PM")
                        sucesso_msg = "Registro de venda excluído com sucesso!"
                elif acao_form == "cadastrar":
                    index_edicao = request.form.get("index_edicao", "").strip()
                    cliente_v = request.form.get("cliente", "").strip()
                    produto_legado = request.form.get("produto", "").strip()
                    plano_v = request.form.get("plano_manutencao", "").strip()
                    rio_v = request.form.get("rio", "").strip()
                    plano_v, rio_v = separar_produto_venda({
                        "P. MANUTENÇÃO": plano_v,
                        "RIO": rio_v,
                        "PRODUTO": produto_legado,
                    })
                    produto_v = " / ".join(valor for valor in (plano_v, rio_v) if valor) or produto_legado
                    contrato_v = request.form.get("numero_contrato", "").strip()
                    data_v = request.form.get("data_venda", "").strip()
                    modelo_v = request.form.get("modelo", "").strip()
                    chassis_v = request.form.get("chassis", "").strip()
                    qtd_v = request.form.get("quantidade", "").strip()
                    vendedor_v = (
                        request.form.get("vendedor", "").strip()
                        if is_gestao_vendas else usuario_vendas
                    )
                    placa_venda_existente = ""
                    
                    anexo_1_url = ""
                    if index_edicao:
                        try:
                            linha_atual = aba_vendas.row_values(int(index_edicao))
                            cabecalhos_venda = aba_vendas.row_values(1)
                            idx_anexo_1 = next(
                                (i for i, nome in enumerate(cabecalhos_venda)
                                 if normalizar_texto_comissao(nome) == "anexo 1"),
                                None,
                            )
                            if idx_anexo_1 is not None and len(linha_atual) > idx_anexo_1:
                                anexo_1_url = linha_atual[idx_anexo_1]
                            idx_placa = next(
                                (i for i, nome in enumerate(cabecalhos_venda)
                                 if normalizar_chave_planilha(nome) == "placa"),
                                None,
                            )
                            if idx_placa is not None and len(linha_atual) > idx_placa:
                                placa_venda_existente = linha_atual[idx_placa]
                        except Exception:
                            pass

                    if "anexo_1" in request.files:
                        file_obj = request.files["anexo_1"]
                        if file_obj and file_obj.filename:
                            nome_arquivo = nomear_comprovante_venda(
                                cliente_v,
                                produto_v,
                                chassis_v,
                                file_obj.filename,
                            )
                            anexo_1_url = subir_comprovante_google_drive(
                                file_obj,
                                permitir_fallback_local=False,
                                nome_arquivo=nome_arquivo,
                            )

                    if cliente_v and (plano_v or rio_v or produto_legado):
                        cabecalhos_venda = aba_vendas.row_values(1)
                        dados_venda_linha = montar_linha_venda_pm({
                            "CLIENTE": cliente_v,
                            "PRODUTO": produto_v,
                            "P. MANUTENÇÃO": plano_v,
                            "RIO": rio_v,
                            "CONTRATO": contrato_v,
                            "DATA": data_v,
                            "MODELO": modelo_v,
                            "PLACA": placa_venda_existente,
                            "CHASSIS": chassis_v,
                            "QUANTIDADE": qtd_v,
                            "VENDEDOR": vendedor_v,
                            "ANEXO 1": anexo_1_url,
                        }, cabecalhos_venda)
                        ultima_coluna = chr(ord("A") + len(cabecalhos_venda) - 1)
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_vendas.update(f"A{idx_int}:{ultima_coluna}{idx_int}", [dados_venda_linha])
                            invalidar_cache_ab_as("Vendas_PM")
                            sucesso_msg = "Venda atualizada com sucesso!"
                        else:
                            aba_vendas.append_row(dados_venda_linha)
                            invalidar_cache_ab_as("Vendas_PM")
                            sucesso_msg = "Venda registrada com sucesso!"
                    else:
                        erro_msg = "Informe o cliente e ao menos um produto (Plano de Manutenção ou RIO)."

            linhas_vendas_brutas = aba_vendas.get_all_values()
            
            # Captura de Filtros padronizados
            busca_cliente = request.args.get("busca", "").strip().lower()
            vend_selecionado = request.args.get("vend", "todos").strip().lower()
            if not is_gestao_vendas:
                vend_selecionado = usuario_vendas.lower()
            ano_selecionado = request.args.get("ano", str(datetime.now().year)).strip()
            periodo_selecionado = request.args.get("periodo", "anointeiro").strip().lower()

            options_filtro_vend = '<option value="todos"' + (' selected' if vend_selecionado == 'todos' else '') + '>Todos Vendedores</option>'
            for c in lista_consultores:
                sel_v = ' selected' if vend_selecionado == c.lower() else ''
                options_filtro_vend += f'<option value="{c}"{sel_v}>{c}</option>'

            anos_disponiveis = {str(datetime.now().year)}
            if len(linhas_vendas_brutas) > 1:
                cab_scan_v = [c.upper().strip() for c in linhas_vendas_brutas[0]]
                idx_dt_scan_v = cab_scan_v.index("DATA DA VENDA") if "DATA DA VENDA" in cab_scan_v else 2
                for l in linhas_vendas_brutas[1:]:
                    if len(l) > idx_dt_scan_v:
                        dt_scan = parse_data_comissao(l[idx_dt_scan_v])
                        if dt_scan:
                            anos_disponiveis.add(str(dt_scan.year))

            options_anos = ""
            for a_op in sorted(list(anos_disponiveis), reverse=True):
                sel_a = ' selected' if ano_selecionado == a_op else ''
                options_anos += f'<option value="{a_op}"{sel_a}>{a_op}</option>'

            meses_dict = {
                "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
                "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
                "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro"
            }

            options_periodo = f'<option value="anointeiro" {"selected" if periodo_selecionado == "anointeiro" else ""}>Ano Inteiro</option>'
            options_periodo += f'<option value="semestre1" {"selected" if periodo_selecionado == "semestre1" else ""}>1º Semestre</option>'
            options_periodo += f'<option value="semestre2" {"selected" if periodo_selecionado == "semestre2" else ""}>2º Semestre</option>'
            for m_num, m_nome in meses_dict.items():
                options_periodo += f'<option value="{m_num}" {"selected" if periodo_selecionado == m_num else ""}>{m_nome}</option>'

            titulo_relatorio_txt = f"RELATÓRIO DE VENDAS E COMISSÕES"

            registros_vendas_filtrados = []
            if len(linhas_vendas_brutas) > 1:
                cab_v = [c.upper().strip() for c in linhas_vendas_brutas[0]]
                for idx_l, linha_v in enumerate(linhas_vendas_brutas[1:], start=2):
                    dict_v = {"_index_planilha": idx_l}
                    for i, val in enumerate(linha_v):
                        if i < len(cab_v) and cab_v[i]:
                            dict_v[cab_v[i]] = val

                    if (
                        not is_gestao_vendas
                        and not registro_pertence_ao_usuario(dict_v, usuario_vendas)
                    ):
                        continue

                    cli_val = str(dict_v.get('CLIENTE', '')).strip().lower()
                    vend_val = str(dict_v.get('VENDEDOR', '')).strip().lower()
                    data_venda_val = str(dict_v.get('DATA DA VENDA', '')).strip()

                    dt_parse = parse_data_comissao(data_venda_val)
                    dt_obj = dt_parse if dt_parse else datetime.max
                    ano_item = str(dt_parse.year) if dt_parse else ""
                    mes_item = f"{dt_parse.month:02d}" if dt_parse else ""

                    if ano_selecionado and ano_item != ano_selecionado:
                        continue

                    if periodo_selecionado == "semestre1" and mes_item not in ["01","02","03","04","05","06"]:
                        continue
                    elif periodo_selecionado == "semestre2" and mes_item not in ["07","08","09","10","11","12"]:
                        continue
                    elif len(periodo_selecionado) == 2 and periodo_selecionado.isdigit() and mes_item != periodo_selecionado:
                        continue

                    if busca_cliente and busca_cliente not in cli_val:
                        continue
                    if vend_selecionado != "todos" and vend_val != vend_selecionado:
                        continue

                    dict_v["_dt_obj"] = dt_obj
                    registros_vendas_filtrados.append(dict_v)

            registros_vendas_filtrados.sort(key=lambda x: x["_dt_obj"])

            tabela_vendas_linhas = ""
            contador_vendas = 0
            
            comissoes_por_estado = {
                "PE": {"vendedores": {}, "total_pm": 0, "total_rio": 0, "total_geral": 0},
                "AL": {"vendedores": {}, "total_pm": 0, "total_rio": 0, "total_geral": 0}
            }

            total_qtd_pm_geral = 0
            total_qtd_rio_geral = 0
            
            dados_dashboard = {
                "resumo": {"qtd": 0, "apm": 0, "vendedores": 0},
                "meses": {}, 
                "vendedores": {},
                "produtos": {"PM": 0, "RIO": 0},
                "modelos": {}
            }

            for reg in registros_vendas_filtrados:
                contador_vendas += 1
                idx_l = reg["_index_planilha"]
                cli = reg.get('CLIENTE', '')
                contrato_reg = obter_numero_contrato(reg)
                plano_reg, rio_reg = separar_produto_venda(reg)
                prod = " / ".join(valor for valor in (plano_reg, rio_reg) if valor) or str(reg.get('PRODUTO', '')).strip()
                dt_v = reg.get('DATA DA VENDA', '')
                mod = reg.get('MODELO', '')
                qtd_str = reg.get('QUANTIDADE', '1')
                vend = reg.get('VENDEDOR', 'Desconhecido')
                chassis_reg = reg.get('CHASSIS', '') or reg.get('CHASSI', '')
                mes_str_grafico = reg["_dt_obj"].strftime("%m/%Y") if reg["_dt_obj"] != datetime.max else "Sem Data"
                
                calculo = calcular_comissoes_venda(
                    produto=prod,
                    modelo=mod,
                    quantidade=qtd_str,
                    registro=reg,
                )
                qtd_num = calculo["qtd"]
                is_pm = calculo["is_pm"]
                is_rio = calculo["is_rio"]
                comissao_pm_item = calculo["vendedor_pm"]
                comissao_rio_item = calculo["vendedor_rio"]
                comissao_apm_item = calculo["apm_pm"] + calculo["apm_rio"]

                estado_v = mapa_vendedor_estado.get(vend.strip().lower(), "PE")
                if estado_v not in comissoes_por_estado:
                    estado_v = "PE"

                if vend not in comissoes_por_estado[estado_v]["vendedores"]:
                    comissoes_por_estado[estado_v]["vendedores"][vend] = {"pm_qtd": 0, "pm_total": 0, "rio_qtd": 0, "rio_total": 0}

                if is_pm:
                    comissoes_por_estado[estado_v]["vendedores"][vend]["pm_qtd"] += qtd_num
                    comissoes_por_estado[estado_v]["vendedores"][vend]["pm_total"] += comissao_pm_item
                    comissoes_por_estado[estado_v]["total_pm"] += comissao_pm_item
                    total_qtd_pm_geral += qtd_num
                    dados_dashboard["produtos"]["PM"] += qtd_num
                if is_rio:
                    comissoes_por_estado[estado_v]["vendedores"][vend]["rio_qtd"] += qtd_num
                    comissoes_por_estado[estado_v]["vendedores"][vend]["rio_total"] += comissao_rio_item
                    comissoes_por_estado[estado_v]["total_rio"] += comissao_rio_item
                    total_qtd_rio_geral += qtd_num
                    dados_dashboard["produtos"]["RIO"] += qtd_num

                comissoes_por_estado[estado_v]["total_geral"] += (comissao_pm_item + comissao_rio_item)

                total_comissao_vend = comissao_pm_item + comissao_rio_item
                dados_dashboard["resumo"]["qtd"] += qtd_num
                dados_dashboard["resumo"]["apm"] += comissao_apm_item
                dados_dashboard["resumo"]["vendedores"] += total_comissao_vend

                modelo_nome = mod.strip().upper() if mod.strip() else "NÃO INFORMADO"
                if modelo_nome not in dados_dashboard["modelos"]:
                    dados_dashboard["modelos"][modelo_nome] = 0
                dados_dashboard["modelos"][modelo_nome] += qtd_num

                if mes_str_grafico not in dados_dashboard["meses"]:
                    dados_dashboard["meses"][mes_str_grafico] = {"qtd_vendas": 0, "pago_apm": 0, "pago_vendedores": 0}
                dados_dashboard["meses"][mes_str_grafico]["qtd_vendas"] += qtd_num
                dados_dashboard["meses"][mes_str_grafico]["pago_apm"] += comissao_apm_item
                dados_dashboard["meses"][mes_str_grafico]["pago_vendedores"] += total_comissao_vend

                if vend not in dados_dashboard["vendedores"]:
                    dados_dashboard["vendedores"][vend] = {"qtd": 0, "comissao": 0}
                dados_dashboard["vendedores"][vend]["qtd"] += qtd_num
                dados_dashboard["vendedores"][vend]["comissao"] += total_comissao_vend

                link_anexo_1 = reg.get('ANEXO 1', '')
                url_anexo_atual = url_comprovante_no_app(link_anexo_1) if link_anexo_1 else ""
                anexos_html = ""
                if link_anexo_1:
                    if url_anexo_atual:
                        url_segura = html.escape(url_anexo_atual, quote=True)
                        anexos_html = f'''
                        <div onclick="abrirImagemModal('{url_segura}')" title="Clique para ampliar" style="display: inline-block; cursor: pointer; background: #fff; padding: 4px; border: 1px solid #cbd5e0; border-radius: 4px;">
                            <img src="{url_segura}" alt="Anexo 1" class="img-comprovacao">
                        </div>
                        '''
                    else:
                        anexos_html = '<span style="color:#a33;">Comprovante indisponível; reenvie o arquivo.</span>'

                argumentos_edicao = ", ".join(
                    json.dumps(str(valor or ""), ensure_ascii=False)
                    for valor in (
                        cli, contrato_reg, plano_reg, rio_reg, dt_v, mod, qtd_str,
                        vend, chassis_reg, link_anexo_1, url_anexo_atual,
                    )
                )
                argumentos_edicao = html.escape(argumentos_edicao, quote=True)
                botoes_v = f"""
                <div style="display: flex; gap: 4px;">
                    <button type="button" class="btn-acao btn-editar no-print" onclick="carregarVendaParaEdicao({idx_l}, {argumentos_edicao})">Alterar</button>
                    <button type="button" class="btn-acao btn-excluir no-print" onclick="excluirVenda({idx_l}, 'vendas')">Excluir</button>
                </div>
                """

                tabela_vendas_linhas += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: none;"><b>{html.escape(str(cli))}</b></td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(contrato_reg))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(plano_reg))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(rio_reg))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(dt_v))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(mod))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(qtd_str))}</td>
                    <td style="padding: 10px; border-bottom: none;">{html.escape(str(vend))} ({html.escape(str(estado_v))})</td>
                    <td style="padding: 10px; border-bottom: none;" class="no-print">-</td>
                    <td style="padding: 10px; border-bottom: none;" class="no-print">{botoes_v}</td>
                </tr>
                <tr style="background-color: #fafbfc;">
                    <td colspan="10" style="padding: 8px 10px 12px 10px; border-bottom: 1px solid #edf2f7;">
                        <span style="font-size: 11px; font-weight: 700; color: #4a5568; text-transform: uppercase; display: block; margin-bottom: 4px;">Comprovação (Anexo 1):</span>
                        {anexos_html if anexos_html else '<span style="color: #a0aec0; font-size: 12px;">Nenhum anexo enviado.</span>'}
                    </td>
                </tr>
                """

            if contador_vendas == 0:
                tabela_vendas_linhas = '<tr><td colspan="10" style="padding: 20px; text-align: center; color: #718096;">Nenhuma venda encontrada para este filtro.</td></tr>'

            def formata_br(valor):
                return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

            bloco_comissoes_html = ""
            for sigla_est, dados_est in comissoes_por_estado.items():
                if dados_est["vendedores"]:
                    nome_est_completo = "Pernambuco (PE)" if sigla_est == "PE" else "Alagoas (AL)"
                    detalhes_vendedores_html = ""
                    for v_nome, d_v in dados_est["vendedores"].items():
                        tot_v = d_v["pm_total"] + d_v["rio_total"]
                        detalhes_vendedores_html += f"""
                        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px;">
                            <div style="font-weight: 700; color: #002244; font-size: 13px; margin-bottom: 4px;">Consultor(a): {v_nome}</div>
                            <div style="font-size: 12px; color: #4a5568; display: flex; justify-content: space-between;">
                                <span>Planos de Manutenção ({d_v['pm_qtd']} un. × R$ {formata_br(COMISSAO_VENDEDOR_PM)}):</span>
                                <b>R$ {formata_br(d_v['pm_total'])}</b>
                            </div>
                            <div style="font-size: 12px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 4px;">
                                <span>Telemetria RIO ({d_v['rio_qtd']} un. × R$ {formata_br(COMISSAO_VENDEDOR_RIO)}):</span>
                                <b>R$ {formata_br(d_v['rio_total'])}</b>
                            </div>
                            <div style="font-size: 13px; color: #2f855a; font-weight: 700; border-top: 1px dashed #cbd5e0; padding-top: 4px; display: flex; justify-content: space-between;">
                                <span>Total Vendedor:</span>
                                <span>R$ {formata_br(tot_v)}</span>
                            </div>
                        </div>
                        """
                    
                    bloco_comissoes_html += f"""
                    <div style="margin-bottom: 16px; background: #f8fafc; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px;">
                        <h4 style="font-size: 14px; color: #002244; border-bottom: 2px solid #002244; padding-bottom: 4px; margin-bottom: 10px; text-transform: uppercase;">📍 Loja / Região: {nome_est_completo}</h4>
                        {detalhes_vendedores_html}
                        <div style="text-align: right; font-size: 14px; font-weight: 700; color: #002244; margin-top: 8px; border-top: 1px solid #cbd5e0; padding-top: 6px;">
                            Total Região {sigla_est}: R$ {formata_br(dados_est['total_geral'])}
                        </div>
                    </div>
                    """

            if not bloco_comissoes_html:
                bloco_comissoes_html = '<p style="color: #718096; font-size: 13px; text-align: center;">Nenhuma comissão registrada para o período.</p>'

            comissao_minha_pm = total_qtd_pm_geral * COMISSAO_APM_PM
            comissao_minha_rio = total_qtd_rio_geral * COMISSAO_APM_RIO
            comissao_minha_total = comissao_minha_pm + comissao_minha_rio

            bloco_minha_comissao = f"""
            <div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 16px; margin-top: 20px; border-left: 5px solid #2f855a;">
                <h3 style="font-size: 15px; color: #002244; margin-bottom: 12px; border-bottom: 2px solid #cbd5e0; padding-bottom: 6px;">🎯 Resumo da Sua Comissão (APM - Apoio ao Plano de Manutenção)</h3>
                <div style="font-weight: 700; color: #002244; font-size: 13px; margin-bottom: 8px;">Logado como: {nome_usuario_logado} (Vendedor APM)</div>
                <div style="font-size: 13px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 4px;">
                    <span>Planos de Manutenção ({total_qtd_pm_geral} un. × R$ {formata_br(COMISSAO_APM_PM)}):</span>
                    <b>R$ {formata_br(comissao_minha_pm)}</b>
                </div>
                <div style="font-size: 13px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span>Telemetria RIO ({total_qtd_rio_geral} un. × R$ {formata_br(COMISSAO_APM_RIO)}):</span>
                    <b>R$ {formata_br(comissao_minha_rio)}</b>
                </div>
                <div style="font-size: 15px; color: #2f855a; font-weight: 700; border-top: 1px dashed #cbd5e0; padding-top: 8px; display: flex; justify-content: space-between;">
                    <span>Sua Comissão Total (Todas as Vendas):</span>
                    <span>R$ {formata_br(comissao_minha_total)}</span>
                </div>
            </div>
            """
            
            json_dashboard_data = json.dumps(dados_dashboard)

            conteudo = f"""
            <div>
                <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 14px; font-size: 17px;">Controle de Vendas Fechadas e Comissões</h2>
                
                {f'<div class="sucesso">{sucesso_msg}</div>' if sucesso_msg else ''}
                {f'<div class="error">{erro_msg}</div>' if erro_msg else ''}

                <!-- Botões de Ação Externa (Gerar PDF e Ver Gráficos) -->
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button type="button" onclick="gerarPDFRelatorio()" class="btn-acao btn-pdf" style="flex: 1; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📄 Gerar PDF (Comissões)</button>
                    <button type="button" onclick="alternarVisaoDashboard()" id="btnAlternarVisao" class="btn-acao btn-graficos" style="flex: 1; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📊 Ver Gráficos</button>
                </div>

                <!-- Formulário Retrátil / Sanfona (Igual a Negócios em Andamento) -->
                <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 6px; margin-bottom: 15px; overflow: hidden;">
                    <button type="button" onclick="toggleFormularioVenda()" style="width: 100%; background: #f7fafc; border: none; padding: 12px 16px; text-align: left; font-weight: 700; color: #002244; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                        <span id="iconeSanfonaVenda">▶</span> <span id="tituloFormVendaCard">➕ Registrar Nova Venda / Comprovação</span>
                    </button>
                    
                    <div id="containerFormularioVenda" style="display: none; padding: 16px; border-top: 1px solid #e2e8f0; background: #fff;">
                        <form method="POST" enctype="multipart/form-data">
                            <input type="hidden" name="acao_form" value="cadastrar">
                            <input type="hidden" id="editVendaIndexInput" name="index_edicao" value="">

                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                                <div>
                                    <label>Cliente</label>
                                    <input type="text" name="cliente" placeholder="Nome do Cliente / Empresa" required>
                                </div>
                                <div>
                                    <label>Nº do Contrato</label>
                                    <input type="text" name="numero_contrato" placeholder="Número do contrato">
                                </div>
                                <div>
                                    <label>Plano de Manutenção</label>
                                    <select name="plano_manutencao">
                                        <option value="">Nenhum</option>
                                        {opcoes_planos_manutencao}
                                    </select>
                                </div>
                                <div>
                                    <label>RIO</label>
                                    <select name="rio">
                                        <option value="">Nenhum</option>
                                        {opcoes_tipos_rio}
                                    </select>
                                </div>
                            </div>

                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                                <div>
                                    <label>Data da Venda</label>
                                    <input type="text" name="data_venda" value="{datetime.now().strftime('%d/%m/%Y')}" required>
                                </div>
                                <div>
                                    <label>Modelo do Veículo</label>
                                    <select name="modelo">
                                        <option value="">Selecione um modelo...</option>
                                        {opcoes_modelos}
                                    </select>
                                </div>
                            </div>

                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                                <div>
                                    <label>Quantidade</label>
                                    <input type="text" name="quantidade" placeholder="Ex: 2 veículos" required>
                                </div>
                                <div>
                                    <label>Vendedor</label>
                                    <select name="vendedor" required>
                                        <option value="">Selecione um consultor...</option>
                                        {opcoes_consultores}
                                    </select>
                                </div>
                            </div>

                            <div style="margin-bottom: 10px;">
                                <label>Chassi</label>
                                <input type="text" name="chassis" placeholder="Chassi do veículo">
                            </div>

                            <div style="margin-bottom: 10px;">
                                <label>Anexo 1 (Comprovação / Imagem)</label>
                                <input type="file" name="anexo_1" accept="image/*" capture="environment">
                                <div id="comprovanteAtual" role="status" style="display:none; margin-top:8px; padding:9px 10px; background:#eff6ff; border:1px solid #bfdbfe; border-radius:6px; color:#1e3a5f; font-size:12px;">
                                    <span id="comprovanteAtualNome"></span>
                                    <a id="comprovanteAtualLink" href="#" target="_blank" rel="noopener noreferrer" style="display:none; margin-left:8px; color:#0056b3; font-weight:700;">Abrir comprovante</a>
                                    <div id="comprovanteAtualInstrucao" style="margin-top:3px;"></div>
                                </div>
                            </div>

                            <div style="display: flex; gap: 10px; margin-top: 15px;">
                                <button type="submit" id="btnSubmitVendaForm" class="btn-login" style="width: auto; padding: 10px 24px;">Salvar Venda</button>
                                <button type="button" id="btnCancelarEdicaoVenda" onclick="cancelarEdicaoVenda()" style="display:none; background:#cbd5e0; border:none; padding:10px 16px; border-radius:6px; cursor:pointer; font-weight:600;">Cancelar Edição</button>
                            </div>
                        </form>
                    </div>
                </div>

                <!-- Barra de Filtros Padronizada -->
                <div class="produto-detalhe-card" style="padding: 12px; margin-bottom: 15px;">
                    <div style="font-weight:700; color:#002244; margin-bottom:8px; font-size:13px;">Lista de Vendas</div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        <input type="text" id="filtroBusca" value="{busca_cliente}" placeholder="🔍 Buscar cliente..." style="flex: 2; min-width: 200px; padding: 10px;" onkeypress="if(event.key === 'Enter') aplicarFiltrosVendas()">
                        
                        <select id="filtroVend" style="flex: 1; min-width: 140px; padding: 10px;" onchange="aplicarFiltrosVendas()">
                            {options_filtro_vend}
                        </select>

                        <select id="filtroAno" style="flex: 0.8; min-width: 90px; padding: 10px;" onchange="aplicarFiltrosVendas()">
                            {options_anos}
                        </select>

                        <select id="filtroPeriodo" style="flex: 1; min-width: 130px; padding: 10px;" onchange="aplicarFiltrosVendas()">
                            {options_periodo}
                        </select>
                    </div>
                </div>

                <div id="secaoRelatorioPDF" class="produto-detalhe-card relatorio-vendas">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #002244; padding-bottom: 10px; margin-bottom: 14px;">
                        <div>
                            <h3 style="font-size: 16px; color: #002244; margin: 0 0 4px 0;">{titulo_relatorio_txt}</h3>
                            <p style="font-size: 12px; color: #4a5568; margin: 0;">Emitido por: <b>{nome_usuario_logado}</b> em {datetime.now().strftime('%d/%m/%Y às %H:%M')}</p>
                        </div>
                        <div>
                            <img src="{url_for('static', filename='logo.png')}" alt="Novo Mundo" style="max-height: 40px; width: auto;">
                        </div>
                    </div>

                    <div style="overflow-x: auto; margin-bottom: 20px;">
                        <table id="tabelaVendas" style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" data-sort-dir="asc">
                            <thead>
                                <tr style="background: #002244; color: #ffffff; border-bottom: 2px solid #001529;">
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 0, 'text')">Cliente</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 1, 'text')">Nº Contrato</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 2, 'text')">Plano</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 3, 'text')">RIO</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 4, 'data')">Data</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 5, 'text')">Modelo</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 6, 'num')">Qtd.</th>
                                    <th style="padding: 6px; white-space: nowrap; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 7, 'text')">Vendedor/Região</th>
                                    <th style="padding: 6px; white-space: nowrap;" class="no-print">Anexo</th>
                                    <th style="padding: 6px; white-space: nowrap;" class="no-print">Ações</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tabela_vendas_linhas}
                            </tbody>
                        </table>
                    </div>

                    <div style="background: #f1f5f9; border: 1px solid #cbd5e0; border-radius: 8px; padding: 16px; margin-top: 20px;">
                        <h3 style="font-size: 15px; color: #002244; margin-bottom: 12px; border-bottom: 2px solid #002244; padding-bottom: 6px;">Resumo Detalhado de Comissões por Região (PE / AL)</h3>
                        {bloco_comissoes_html}
                    </div>

                    {bloco_minha_comissao}
                </div>

                <div id="secaoDashboard" class="produto-detalhe-card" style="display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #002244; padding-bottom: 10px; margin-bottom: 20px;">
                        <h3 style="font-size: 16px; color: #002244; margin: 0;">📊 Dashboard Inteligente</h3>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 25px;">
                        <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border-left: 5px solid #d69e2e; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                            <div style="font-size: 11px; color: #718096; font-weight: 800; text-transform: uppercase;">Total de Veículos/Planos (Qtd)</div>
                            <div id="kpi-qtd" style="font-size: 26px; font-weight: bold; color: #1a202c; margin-top: 5px;">0</div>
                        </div>
                        <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border-left: 5px solid #2b6cb0; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                            <div style="font-size: 11px; color: #718096; font-weight: 800; text-transform: uppercase;">Total Sua Comissão (APM)</div>
                            <div id="kpi-apm" style="font-size: 26px; font-weight: bold; color: #2b6cb0; margin-top: 5px;">R$ 0,00</div>
                        </div>
                        <div style="background: #f7fafc; padding: 20px; border-radius: 8px; border-left: 5px solid #2f855a; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                            <div style="font-size: 11px; color: #718096; font-weight: 800; text-transform: uppercase;">Total Pago aos Vendedores</div>
                            <div id="kpi-vend" style="font-size: 26px; font-weight: bold; color: #2f855a; margin-top: 5px;">R$ 0,00</div>
                        </div>
                    </div>

                    <div class="dashboard-grid">
                        <div class="chart-container">
                            <canvas id="chartEvolucaoMes"></canvas>
                        </div>
                        <div class="chart-container">
                            <canvas id="chartRankingVendedores"></canvas>
                        </div>
                        <div class="chart-container">
                            <canvas id="chartProdutos"></canvas>
                        </div>
                        <div class="chart-container">
                            <canvas id="chartModelos"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <script>
                function toggleFormularioVenda() {{
                    var container = document.getElementById('containerFormularioVenda');
                    var icone = document.getElementById('iconeSanfonaVenda');
                    if (container.style.display === 'none') {{
                        container.style.display = 'block';
                        icone.innerHTML = '▼';
                    }} else {{
                        container.style.display = 'none';
                        icone.innerHTML = '▶';
                    }}
                }}

                function carregarVendaParaEdicao(idx, cliente, contrato, plano, rio, dataVenda, modelo, quantidade, vendedor, chassis, anexoAtual, urlAnexoAtual) {{
                    var container = document.getElementById('containerFormularioVenda');
                    container.style.display = 'block';
                    document.getElementById('iconeSanfonaVenda').innerHTML = '▼';

                    document.getElementById('editVendaIndexInput').value = idx;
                    document.getElementById('tituloFormVendaCard').innerText = "✏️ Alterar Venda / Comprovação (Linha " + idx + ")";
                    document.getElementById('btnSubmitVendaForm').innerText = "Atualizar Venda";
                    document.getElementById('btnCancelarEdicaoVenda').style.display = "inline-block";

                    document.querySelector('[name="cliente"]').value = cliente;
                    document.querySelector('[name="numero_contrato"]').value = contrato;
                    selecionarOpcaoVenda('plano_manutencao', plano);
                    selecionarOpcaoVenda('rio', rio);
                    document.querySelector('[name="data_venda"]').value = dataVenda;
                    selecionarOpcaoVenda('modelo', modelo);
                    document.querySelector('[name="quantidade"]').value = quantidade;
                    selecionarOpcaoVenda('vendedor', vendedor);
                    document.querySelector('[name="chassis"]').value = chassis;
                    mostrarComprovanteAtual(anexoAtual, urlAnexoAtual);

                    window.scrollTo({{ top: 0, behavior: 'smooth' }});
                }}

                function mostrarComprovanteAtual(caminho, url) {{
                    var painel = document.getElementById('comprovanteAtual');
                    var nome = document.getElementById('comprovanteAtualNome');
                    var link = document.getElementById('comprovanteAtualLink');
                    var instrucao = document.getElementById('comprovanteAtualInstrucao');
                    var caminhoAtual = String(caminho || '').trim();

                    painel.style.display = caminhoAtual ? 'block' : 'none';
                    if (!caminhoAtual) return;

                    var nomeArquivo = caminhoAtual.split('/').pop().split(/[?#]/)[0];
                    nome.textContent = 'Comprovante atual: ' + (nomeArquivo || 'arquivo já cadastrado');
                    if (url) {{
                        link.href = url;
                        link.style.display = 'inline';
                        instrucao.textContent = 'Escolha outro arquivo somente se quiser substituir este comprovante.';
                    }} else {{
                        link.removeAttribute('href');
                        link.style.display = 'none';
                        instrucao.textContent = 'O caminho está cadastrado, mas o arquivo não está disponível no app.';
                    }}
                }}

                function selecionarOpcaoVenda(nomeCampo, valor) {{
                    var select = document.querySelector('[name="' + nomeCampo + '"]');
                    var valorSelecionado = String(valor || '').trim();
                    if (!select) return;

                    if (valorSelecionado && !Array.from(select.options).some(function(opcao) {{
                        return opcao.value === valorSelecionado;
                    }})) {{
                        select.add(new Option(valorSelecionado, valorSelecionado));
                    }}
                    select.value = valorSelecionado;
                }}

                function cancelarEdicaoVenda() {{
                    document.getElementById('editVendaIndexInput').value = "";
                    document.getElementById('tituloFormVendaCard').innerText = "➕ Registrar Nova Venda / Comprovação";
                    document.getElementById('btnSubmitVendaForm').innerText = "Salvar Venda";
                    document.getElementById('btnCancelarEdicaoVenda').style.display = "none";
                    document.getElementById('containerFormularioVenda').style.display = 'none';
                    document.getElementById('iconeSanfonaVenda').innerHTML = '▶';

                    document.querySelector('[name="cliente"]').value = "";
                    document.querySelector('[name="numero_contrato"]').value = "";
                    document.querySelector('[name="plano_manutencao"]').value = "";
                    document.querySelector('[name="rio"]').value = "";
                    document.querySelector('[name="modelo"]').value = "";
                    document.querySelector('[name="quantidade"]').value = "";
                    document.querySelector('[name="chassis"]').value = "";
                    mostrarComprovanteAtual('', '');
                }}

                function aplicarFiltrosVendas() {{
                    var busca = document.getElementById('filtroBusca').value;
                    var vend = document.getElementById('filtroVend').value;
                    var ano = document.getElementById('filtroAno').value;
                    var periodo = document.getElementById('filtroPeriodo').value;
                    window.location.href = '/modulo/vendas?busca=' + encodeURIComponent(busca) + '&vend=' + encodeURIComponent(vend) + '&ano=' + ano + '&periodo=' + periodo;
                }}

                var graficosIniciados = false;
                var dadosPainel = {json_dashboard_data};

                function renderizarGraficos() {{
                    if (graficosIniciados) return;
                    graficosIniciados = true;

                    if (typeof ChartDataLabels !== 'undefined') {{
                        Chart.register(ChartDataLabels);
                        Chart.defaults.set('plugins.datalabels', {{
                            color: '#ffffff',
                            font: {{ weight: 'bold', size: 12 }},
                            textShadowBlur: 4,
                            textShadowColor: 'rgba(0,0,0,0.6)'
                        }});
                    }}

                    document.getElementById('kpi-qtd').innerText = dadosPainel.resumo.qtd + " un";
                    document.getElementById('kpi-apm').innerText = "R$ " + dadosPainel.resumo.apm.toLocaleString('pt-BR', {{minimumFractionDigits: 2}});
                    document.getElementById('kpi-vend').innerText = "R$ " + dadosPainel.resumo.vendedores.toLocaleString('pt-BR', {{minimumFractionDigits: 2}});

                    var mesesLabels = Object.keys(dadosPainel.meses).sort();
                    var dataQtd = mesesLabels.map(m => dadosPainel.meses[m].qtd_vendas);
                    var dataApm = mesesLabels.map(m => dadosPainel.meses[m].pago_apm);
                    var dataVend = mesesLabels.map(m => dadosPainel.meses[m].pago_vendedores);

                    new Chart(document.getElementById('chartEvolucaoMes'), {{
                        type: 'bar',
                        data: {{
                            labels: mesesLabels,
                            datasets: [
                                {{ 
                                    label: 'Comissão APM (R$)', data: dataApm, backgroundColor: '#2b6cb0', yAxisID: 'yValor',
                                    datalabels: {{ anchor: 'center', align: 'center', formatter: (val) => val > 0 ? 'R$ '+val.toLocaleString('pt-BR') : '' }}
                                }},
                                {{ 
                                    label: 'Comissão Vendedores (R$)', data: dataVend, backgroundColor: '#2f855a', yAxisID: 'yValor',
                                    datalabels: {{ anchor: 'center', align: 'center', formatter: (val) => val > 0 ? 'R$ '+val.toLocaleString('pt-BR') : '' }}
                                }},
                                {{ 
                                    label: 'Qtd de Vendas', data: dataQtd, type: 'line', borderColor: '#d69e2e', borderWidth: 3, pointRadius: 5, yAxisID: 'yQtd',
                                    datalabels: {{ anchor: 'top', align: 'top', color: '#d69e2e', font: {{ size: 13, weight: 'bold' }}, formatter: (val) => val > 0 ? val + ' un' : '', textShadowBlur: 0 }}
                                }}
                            ]
                        }},
                        options: {{ 
                            responsive: true, maintainAspectRatio: false, 
                            plugins: {{ title: {{ display: true, text: 'Evolução Mensal (Valores e Quantidades)', font: {{ size: 14 }} }} }},
                            scales: {{ 
                                yValor: {{ type: 'linear', position: 'left' }}, 
                                yQtd: {{ type: 'linear', position: 'right', grid: {{ drawOnChartArea: false }} }} 
                            }} 
                        }}
                    }});

                    var vendedoresLabels = Object.keys(dadosPainel.vendedores || {{}})
                        .filter(v => v && String(v).trim() !== '');
                    vendedoresLabels.sort((a, b) => {{
                        var qtdDiff = (dadosPainel.vendedores[b].qtd || 0) - (dadosPainel.vendedores[a].qtd || 0);
                        if (qtdDiff !== 0) return qtdDiff;
                        return (dadosPainel.vendedores[b].comissao || 0) - (dadosPainel.vendedores[a].comissao || 0);
                    }});
                    var vendComissao = vendedoresLabels.map(v => Number(dadosPainel.vendedores[v].comissao || 0));
                    var vendQtd = vendedoresLabels.map(v => Number(dadosPainel.vendedores[v].qtd || 0));

                    var canvasConsultor = document.getElementById('chartRankingVendedores');
                    if (!vendedoresLabels.length) {{
                        var ctxConsultor = canvasConsultor.getContext('2d');
                        ctxConsultor.font = '600 14px Segoe UI, sans-serif';
                        ctxConsultor.fillStyle = '#718096';
                        ctxConsultor.textAlign = 'center';
                        ctxConsultor.fillText('Nenhuma venda encontrada para os filtros atuais', canvasConsultor.width / 2, 150);
                    }}

                    new Chart(document.getElementById('chartRankingVendedores'), {{
                        type: 'bar',
                        data: {{ 
                            labels: vendedoresLabels, 
                            datasets: [{{ 
                                label: 'Total Pago ao Vendedor (R$)', 
                                data: vendComissao, 
                                backgroundColor: '#002244',
                                datalabels: {{
                                    anchor: 'start', 
                                    align: 'end',
                                    color: '#ffffff',
                                    formatter: (val, ctx) => 'Qtd: ' + vendQtd[ctx.dataIndex] + ' | R$ ' + val.toLocaleString('pt-BR')
                                }}
                            }}] 
                        }},
                        options: {{ 
                            indexAxis: 'y',
                            responsive: true, maintainAspectRatio: false,
                            plugins: {{ title: {{ display: true, text: 'Vendas por Consultor', font: {{ size: 14 }} }} }}
                        }}
                    }});

                    new Chart(document.getElementById('chartProdutos'), {{
                        type: 'doughnut',
                        data: {{
                            labels: ['Planos de Manutenção (PM)', 'Telemetria RIO'],
                            datasets: [{{
                                data: [dadosPainel.produtos.PM, dadosPainel.produtos.RIO],
                                backgroundColor: ['#e53e3e', '#3182ce'],
                                datalabels: {{
                                    color: '#fff', font: {{ size: 15, weight: 'bold' }},
                                    formatter: (val) => val > 0 ? val + ' un' : ''
                                }}
                            }}]
                        }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            plugins: {{ title: {{ display: true, text: 'Mix de Produtos (PM vs RIO)', font: {{ size: 14 }} }} }}
                        }}
                    }});

                    var modLabels = Object.keys(dadosPainel.modelos).sort((a, b) => dadosPainel.modelos[b] - dadosPainel.modelos[a]);
                    var modData = modLabels.map(m => dadosPainel.modelos[m]);

                    new Chart(document.getElementById('chartModelos'), {{
                        type: 'bar',
                        data: {{
                            labels: modLabels,
                            datasets: [{{
                                label: 'Qtd de Veículos',
                                data: modData,
                                backgroundColor: '#4a5568',
                                datalabels: {{
                                    anchor: 'start', 
                                    align: 'end',
                                    color: '#ffffff',
                                    formatter: (val) => val > 0 ? val + ' un' : ''
                                }}
                            }}]
                        }},
                        options: {{
                            indexAxis: 'y',
                            responsive: true, maintainAspectRatio: false,
                            plugins: {{ title: {{ display: true, text: 'Modelos Mais Vendidos', font: {{ size: 14 }} }} }}
                        }}
                    }});
                }}
            </script>
            """
        except HTTPException:
            raise
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar Vendas:</b> {e}</div>'

    elif nome_modulo == "negocios":
        try:
            # A sincronização só é disparada pelo link do menu. Redirecionar
            # remove o sinalizador para não repetir a importação ao atualizar.
            if request.method == "GET" and request.args.get("sincronizar") == "1":
                msg_sync = importar_relatorios_drive_vendas()
                print(msg_sync)
                session["msg_sync_negocios"] = msg_sync
                return redirect(url_for("acessar_modulo", nome_modulo="negocios"))

            perfil_negocios = str(session.get("perfil", "")).strip().upper()
            is_gestao_negocios = usuario_eh_gestao(perfil_negocios)
            usuario_negocios = str(session.get("nome", "")).strip()

            planilha = conectar_google_sheets()

            try:
                aba_negocios = planilha.worksheet("Negocios_PM")
            except gspread.exceptions.WorksheetNotFound:
                aba_negocios = planilha.add_worksheet(title="Negocios_PM", rows=1000, cols=11)
                aba_negocios.append_row(["TEMPERATURA", "DATA", "VENDEDOR", "CLIENTE", "MODELO", "CHASSIS", "PLANO DE MANUTENÇÃO", "RIO", "CONTATO DO CLIENTE", "TELEFONE", "COMENTÁRIOS"])
            
            msg_sync = session.pop("msg_sync_negocios", None)
            aviso_sync = None
            sucesso_msg = (
                html.escape(msg_sync)
                if msg_sync and (
                    msg_sync.startswith("Sincronização concluída!")
                    or msg_sync == "Nenhum arquivo novo para importar."
                )
                else None
            )
            if sucesso_msg and " Conferência manual: " in msg_sync:
                resumo_sync, aviso_sync = msg_sync.split(" Conferência manual: ", 1)
                sucesso_msg = html.escape(resumo_sync)
                aviso_sync = html.escape(aviso_sync)
            erro_msg = (
                html.escape(msg_sync)
                if msg_sync and sucesso_msg is None
                else None
            )

            # 1. Busca dinâmica de Vendedores (Apenas quem tem CONSULTOR no perfil)
            aba_usuarios = planilha.worksheet("Usuarios")
            registros_usuarios = obter_registros_seguros(aba_usuarios)
            lista_consultores = []
            for u in registros_usuarios:
                perfil_u = str(u.get("PERFIL", "")).strip().upper()
                nome_u = str(u.get("NOME", "")).strip()
                if "CONSULTOR" in perfil_u and nome_u:
                    if nome_u not in lista_consultores:
                        lista_consultores.append(nome_u)
            
            if not lista_consultores:
                lista_consultores = [session.get("nome", "Usuário")]
            if not is_gestao_negocios:
                lista_consultores = [usuario_negocios] if usuario_negocios else []

            # 2. Busca dinâmica de Modelos
            lista_modelos = []
            try:
                aba_modelos = planilha.worksheet("Modelos")
                regs_m = obter_registros_seguros(aba_modelos)
                for rm in regs_m:
                    val_m = str(rm.get("MODELO", list(rm.values())[1] if len(rm) > 1 else (list(rm.values())[0] if rm else ""))).strip()
                    if val_m and val_m not in lista_modelos and val_m.upper() != "PRODUTO":
                        lista_modelos.append(val_m)
            except Exception:
                pass
            
            if not lista_modelos:
                lista_modelos = ["Delivery 11.180", "Constellation 24.280", "Meteor 28.460", "Meteor 29.530", "26.260 6x2", "30.320 8x2", "EXPRESS"]

            # 3. Busca dinâmica de Planos de Manutenção (Lendo estritamente a Coluna B / PRODUTO da aba PM)
            lista_planos_manutencao = []
            try:
                aba_pm = planilha.worksheet("PM")
                regs_pm = obter_registros_seguros(aba_pm)
                for rp in regs_pm:
                    val_p = str(rp.get("PRODUTO", list(rp.values())[1] if len(rp) > 1 else (list(rp.values())[0] if rp else ""))).strip()
                    if val_p and val_p not in lista_planos_manutencao and val_p.upper() != "PRODUTO":
                        lista_planos_manutencao.append(val_p)
            except Exception:
                pass
            if not lista_planos_manutencao:
                lista_planos_manutencao = ["VolksTotal PRE Prevenção e Economia", "VolksTotal MAX", "VolksTotal PLUS", "PREV", "MAX", "PLUS"]

            # 4. Busca dinâmica de RIO (Lendo estritamente a Coluna B / PRODUTO da aba RIO)
            lista_tipos_rio = []
            try:
                aba_rio_origem = planilha.worksheet("RIO")
                regs_rio = obter_registros_seguros(aba_rio_origem)
                for rr in regs_rio:
                    val_r = str(rr.get("PRODUTO", list(rr.values())[1] if len(rr) > 1 else (list(rr.values())[0] if rr else ""))).strip()
                    if val_r and val_r not in lista_tipos_rio and val_r.upper() != "PRODUTO":
                        lista_tipos_rio.append(val_r)
            except Exception:
                pass
            if not lista_tipos_rio:
                lista_tipos_rio = ["Diagnóstico Remoto", "Análise de Eficiência", "Performance", "RIO GEO", "Relatório de Bloqueio"]

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if not is_gestao_negocios:
                    indice_alvo = request.form.get(
                        "index_linha" if acao_form == "excluir" else "index_edicao",
                        "",
                    ).strip()
                    if acao_form == "excluir" and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_negocios, int(indice_alvo), usuario_negocios
                        )
                    ):
                        abort(403)
                    if acao_form != "excluir" and indice_alvo and (
                        not indice_alvo.isdigit()
                        or not registro_planilha_pertence_ao_usuario(
                            aba_negocios, int(indice_alvo), usuario_negocios
                        )
                    ):
                        abort(403)
                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_negocios.delete_rows(index_linha)
                        invalidar_cache_ab_as("Negocios_PM")
                        sucesso_msg = "Registro excluído com sucesso!"
                else:
                    index_edicao = request.form.get("index_edicao", "").strip()
                    temperatura = request.form.get("temperatura", "").strip()
                    data_neg = request.form.get("data", "").strip()
                    vendedor_form = (
                        request.form.get("vendedor", "").strip()
                        if is_gestao_negocios else usuario_negocios
                    )
                    cliente = request.form.get("cliente", "").strip()
                    modelo = request.form.get("modelo", "").strip()
                    chassis = request.form.get("chassis", "").strip()  # <--- CAPTURA O CHASSIS
                    plano_manutencao = request.form.get("plano_manutencao", "").strip()
                    rio_val = request.form.get("rio", "").strip()
                    contato = request.form.get("contato", "").strip()
                    telefone = request.form.get("telefone", "").strip()
                    comentarios = request.form.get("comentarios", "").strip()

                    if cliente and vendedor_form:
                        dados_linha = [temperatura, data_neg, vendedor_form, cliente, modelo, chassis, plano_manutencao, rio_val, contato, telefone, comentarios]
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_negocios.update(f"A{idx_int}:K{idx_int}", [dados_linha])  # <--- ATUALIZADO PARA K
                            invalidar_cache_ab_as("Negocios_PM")
                            sucesso_msg = "Negócio atualizado com sucesso!"
                        else:
                            aba_negocios.append_row(dados_linha)
                            invalidar_cache_ab_as("Negocios_PM")
                            sucesso_msg = "Negócio cadastrado com sucesso!"

                        if temperatura.lower() == "fechado":
                            try:
                                try:
                                    aba_vendas_fechadas = planilha.worksheet("Vendas_PM")
                                except gspread.exceptions.WorksheetNotFound:
                                    aba_vendas_fechadas = planilha.add_worksheet(
                                        title="Vendas_PM", rows=1000, cols=9
                                    )
                                    aba_vendas_fechadas.append_row([
                                        "CLIENTE", "P. MANUTENÇÃO", "RIO", "DATA DA VENDA",
                                        "MODELO", "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2"
                                    ])

                                negocio_fechado = {
                                    "CLIENTE": cliente,
                                    "PRODUTO": f"{plano_manutencao} / {rio_val}".strip(" /"),
                                    "DATA": data_neg,
                                    "MODELO": modelo,
                                    "CHASSIS": chassis,
                                    "QUANTIDADE": "1",
                                    "PLANO DE MANUTENÇÃO": plano_manutencao,
                                    "RIO": rio_val,
                                    "VENDEDOR": vendedor_form,
                                }

                                # Importante: não existe mais etapa/botão "Vender".
                                # Ao escolher Fechado e salvar, a venda vai diretamente
                                # para Vendas_PM, que é a base usada pelas comissões.
                                registros_vendas_fechadas = obter_registros_seguros(aba_vendas_fechadas)
                                chaves_vendas_fechadas = {
                                    chave_venda_pm(r) for r in registros_vendas_fechadas
                                }

                                index_origem = int(index_edicao) if index_edicao else len(aba_negocios.get_all_values())
                                nova_venda = sincronizar_negocio_fechado(
                                    aba_vendas_fechadas,
                                    negocio_fechado,
                                    chaves_vendas_fechadas
                                )

                                # Só remove da Negocios_PM depois que a venda foi
                                # criada ou confirmada como já existente.
                                if index_origem > 1:
                                    aba_negocios.delete_rows(index_origem)

                                if nova_venda:
                                    sucesso_msg = "Negócio fechado e transferido automaticamente para Vendas Fechadas. As comissões serão calculadas pelas regras vigentes."
                                else:
                                    sucesso_msg = "Negócio já constava em Vendas Fechadas e foi retirado de Negócios em Andamento."
                            except Exception as erro_sync:
                                print(f"Erro ao transferir negócio fechado para Vendas_PM: {erro_sync}")
                                erro_msg = (
                                    "Não foi possível concluir a transferência para Vendas Fechadas. "
                                    "O registro permanece em Negócios em Andamento para não ser perdido."
                                )
                    else:
                        erro_msg = "Preencha ao menos o Cliente e o Vendedor."

            lista_temperaturas = ["Super Quente", "Quente", "Morno", "Frio", "Perdida", "Fechado"]
            linhas_brutas = aba_negocios.get_all_values()
            
            # Captura de Filtros via URL
            busca_cliente = request.args.get("busca", "").strip().lower()
            vend_selecionado = request.args.get("vend", "todos").strip().lower()
            if not is_gestao_negocios:
                vend_selecionado = usuario_negocios.lower()
            ano_selecionado = request.args.get("ano", str(datetime.now().year)).strip()
            periodo_selecionado = request.args.get("periodo", "anointeiro").strip().lower()
            temp_selecionada = request.args.get("temp", "todas").strip().lower()

            options_filtro_vend = '<option value="todos"' + (' selected' if vend_selecionado == 'todos' else '') + '>Todos Vendedores</option>'
            for c in lista_consultores:
                sel_v = ' selected' if vend_selecionado == c.lower() else ''
                options_filtro_vend += f'<option value="{c}"{sel_v}>{c}</option>'

            anos_disponiveis = {str(datetime.now().year)}
            if len(linhas_brutas) > 1:
                cab_scan = [c.upper().strip() for c in linhas_brutas[0]]
                idx_dt_scan = cab_scan.index("DATA") if "DATA" in cab_scan else 1
                for l in linhas_brutas[1:]:
                    if len(l) > idx_dt_scan:
                        m_a = re.search(r'/\d{2}/(\d{4}|\d{2})', l[idx_dt_scan])
                        if m_a:
                            a_val = m_a.group(1)
                            if len(a_val) == 2: a_val = "20" + a_val
                            anos_disponiveis.add(a_val)

            options_anos = ""
            for a_op in sorted(list(anos_disponiveis), reverse=True):
                sel_a = ' selected' if ano_selecionado == a_op else ''
                options_anos += f'<option value="{a_op}"{sel_a}>{a_op}</option>'

            meses_dict = {
                "01": "Janeiro", "02": "Fevereiro", "03": "Março", "04": "Abril",
                "05": "Maio", "06": "Junho", "07": "Julho", "08": "Agosto",
                "09": "Setembro", "10": "Outubro", "11": "Novembro", "12": "Dezembro"
            }

            options_periodo = f'<option value="anointeiro" {"selected" if periodo_selecionado == "anointeiro" else ""}>Ano Inteiro</option>'
            options_periodo += f'<option value="semestre1" {"selected" if periodo_selecionado == "semestre1" else ""}>1º Semestre</option>'
            options_periodo += f'<option value="semestre2" {"selected" if periodo_selecionado == "semestre2" else ""}>2º Semestre</option>'
            for m_num, m_nome in meses_dict.items():
                options_periodo += f'<option value="{m_num}" {"selected" if periodo_selecionado == m_num else ""}>{m_nome}</option>'

            kpis = {"total": 0, "fechado": 0, "super quente": 0, "quente": 0, "morno": 0, "perdida": 0, "frio": 0}
            registros_filtrados = []

            if len(linhas_brutas) > 1:
                cabecalhos = [c.upper().strip() for c in linhas_brutas[0]]
                for idx_linha, linha in enumerate(linhas_brutas[1:], start=2):
                    item_dict = {"_index_planilha": idx_linha}
                    for i, val in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i]:
                            item_dict[cabecalhos[i]] = val

                    temp_val = str(item_dict.get('TEMPERATURA', '')).strip()
                    temp_lower = temp_val.lower()
                    data_val = str(item_dict.get('DATA', '')).strip()
                    vend_val = str(item_dict.get('VENDEDOR', '')).strip().lower()
                    cli_val = str(item_dict.get('CLIENTE', '')).strip().lower()

                    dt_obj = datetime.max
                    ano_item = ""
                    mes_item = ""
                    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                        try:
                            dt_obj = datetime.strptime(data_val, fmt)
                            mes_item = f"{dt_obj.month:02d}"
                            ano_item = str(dt_obj.year)
                            break
                        except ValueError:
                            pass

                    if not ano_item:
                        m_ano = re.search(r'/(\d{4}|\d{2})$', data_val)
                        if m_ano:
                            a = m_ano.group(1)
                            ano_item = "20" + a if len(a) == 2 else a
                        m_mes = re.search(r'^\d{1,2}/(\d{1,2})/', data_val)
                        mes_item = m_mes.group(1).zfill(2) if m_mes else ""

                    if (
                        not is_gestao_negocios
                        and not registro_pertence_ao_usuario(item_dict, usuario_negocios)
                    ):
                        continue

                    if ano_selecionado and ano_item != ano_selecionado:
                        continue

                    if periodo_selecionado == "semestre1" and mes_item not in ["01","02","03","04","05","06"]:
                        continue
                    elif periodo_selecionado == "semestre2" and mes_item not in ["07","08","09","10","11","12"]:
                        continue
                    elif len(periodo_selecionado) == 2 and periodo_selecionado.isdigit() and mes_item != periodo_selecionado:
                        continue

                    if temp_lower == "fechado":
                        continue
                    if temp_lower == "perdida" and not is_gestao_negocios:
                        continue

                    kpis["total"] += 1
                    if temp_lower in kpis:
                        kpis[temp_lower] += 1

                    if busca_cliente and busca_cliente not in cli_val:
                        continue
                    if (
                        vend_selecionado != "todos"
                        and normalizar_identidade_comercial(vend_val)
                        != normalizar_identidade_comercial(vend_selecionado)
                    ):
                        continue
                    if temp_selecionada != "todas" and temp_lower != temp_selecionada:
                        continue

                    item_dict["_dt_obj"] = dt_obj
                    registros_filtrados.append(item_dict)

            registros_filtrados.sort(key=lambda x: x["_dt_obj"], reverse=True)

            tabela_linhas = ""
            for reg in registros_filtrados:
                idx_l = reg["_index_planilha"]
                temp = reg.get('TEMPERATURA', '')
                dt = reg.get('DATA', '')
                vend = reg.get('VENDEDOR', '')
                cli = reg.get('CLIENTE', '')
                mod = reg.get('MODELO', '')
                chassis = reg.get('CHASSIS', '')  # <--- LÊ O CHASSIS
                plano = reg.get('PLANO DE MANUTENÇÃO', '')
                rio = reg.get('RIO', '')
                contato = reg.get('CONTATO DO CLIENTE', '')
                tel = reg.get('TELEFONE', '')
                com = reg.get('COMENTÁRIOS', '')

                botoes_acoes = f"""
                <div style="display: flex; gap: 4px; align-items: center; justify-content: flex-end;">
                    <button type="button" class="btn-acao btn-editar" onclick="carregarNegocioParaEdicao({idx_l}, '{temp}', '{dt}', '{vend}', '{cli}', '{mod}', '{chassis}', '{plano}', '{rio}', '{contato}', '{tel}', '{com}')">Alterar</button>
                    <button type="button" class="btn-acao btn-excluir" onclick="excluirNegocioAndamento({idx_l})">Excluir</button>
                </div>
                """

                tabela_linhas += f"""
                <tr>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;"><b>{temp}</b></td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{dt}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{vend}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{cli}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{mod}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{chassis}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{plano}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{rio}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{contato}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7;">{tel}</td>
                    <td style="padding: 10px; border-bottom: 1px solid #edf2f7; font-size: 12px;">{com}</td>
                    <td class="coluna-acoes">{botoes_acoes}</td>
                </tr>
                """

            if not tabela_linhas:
                tabela_linhas = '<tr><td colspan="12" style="padding: 20px; text-align: center; color: #718096;">Nenhum registro encontrado.</td></tr>'

            conteudo = f"""
            <div>
                <!-- KPIs Estilo Painel -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 15px;">
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #3182ce; cursor:pointer;" onclick="filtrarTempNegocios('todas')">
                        <div style="font-size:10px; color:#718096; font-weight:700;">TOTAL</div>
                        <div style="font-size:20px; font-weight:bold; color:#2d3748;">{kpis['total']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #e53e3e; cursor:pointer;" onclick="filtrarTempNegocios('sup. quente')">
                        <div style="font-size:10px; color:#e53e3e; font-weight:700;">SUPER QUENTE</div>
                        <div style="font-size:20px; font-weight:bold; color:#e53e3e;">{kpis['super quente']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #dd6b20; cursor:pointer;" onclick="filtrarTempNegocios('quente')">
                        <div style="font-size:10px; color:#dd6b20; font-weight:700;">QUENTE</div>
                        <div style="font-size:20px; font-weight:bold; color:#dd6b20;">{kpis['quente']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #d69e2e; cursor:pointer;" onclick="filtrarTempNegocios('morno')">
                        <div style="font-size:10px; color:#d69e2e; font-weight:700;">MORNO</div>
                        <div style="font-size:20px; font-weight:bold; color:#d69e2e;">{kpis['morno']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #742a2a; cursor:pointer;" onclick="filtrarTempNegocios('perdida')">
                        <div style="font-size:10px; color:#742a2a; font-weight:700;">PERDIDA</div>
                        <div style="font-size:20px; font-weight:bold; color:#742a2a;">{kpis['perdida']}</div>
                    </div>
                    <div style="background:#fff; border:1px solid #cbd5e0; border-radius:6px; padding:10px; border-left:4px solid #4a5568; cursor:pointer;" onclick="filtrarTempNegocios('frio')">
                        <div style="font-size:10px; color:#4a5568; font-weight:700;">FRIO</div>
                        <div style="font-size:20px; font-weight:bold; color:#4a5568;">{kpis['frio']}</div>
                    </div>
                </div>

                {f'<div class="sucesso">{sucesso_msg}</div>' if sucesso_msg else ''}
                {f'<div class="error">{erro_msg}</div>' if erro_msg else ''}
                {f'<div style="background:#fffaf0; color:#744210; border:1px solid #f6ad55; padding:12px; border-radius:6px; margin-bottom:15px; font-size:13px;"><b>Importado com dados para conferência manual:</b><br>{aviso_sync}</div>' if aviso_sync else ''}

                <!-- Botão Oculto / Sanfona para Registrar Nova Negociação -->
                <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 6px; margin-bottom: 15px; overflow: hidden;">
                    <button type="button" onclick="toggleFormularioNegocio()" style="width: 100%; background: #f7fafc; border: none; padding: 12px 16px; text-align: left; font-weight: 700; color: #002244; cursor: pointer; display: flex; align-items: center; gap: 8px;">
                        <span id="iconeSanfona">▶</span> <span id="tituloBotaoSanfona">➕ Registrar Nova Negociação</span>
                    </button>
                    
                    <div id="containerFormulario" style="display: none; padding: 16px; border-top: 1px solid #e2e8f0; background: #fff;">
                        <form method="POST">
                            <input type="hidden" name="acao_form" value="cadastrar">
                            <input type="hidden" id="editIndexInput" name="index_edicao" value="">

                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 10px;">
                                <div><label>Temperatura</label><select name="temperatura" required>{"".join([f'<option value="{t}">{t}</option>' for t in lista_temperaturas])}</select></div>
                                <div><label>Data</label><input type="text" name="data" value="{datetime.now().strftime('%d/%m/%Y')}" required></div>
                                <div><label>Vendedor</label><select name="vendedor" required>{"".join([f'<option value="{c}">{c}</option>' for c in lista_consultores])}</select></div>
                                <div><label>Cliente</label><input type="text" name="cliente" placeholder="Nome do Cliente" required></div>
                                <div><label>Modelo</label><select name="modelo"><option value="">Selecione...</option>{"".join([f'<option value="{m}">{m}</option>' for m in lista_modelos])}</select></div>
                                <div><label>Chassis</label><input type="text" name="chassis" placeholder="Chassis do veículo"></div>
                                <div><label>Plano</label><select name="plano_manutencao"><option value="">Nenhum</option>{"".join([f'<option value="{p}">{p}</option>' for p in lista_planos_manutencao])}</select></div>
                            </div>

                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 10px;">
                                <div><label>RIO</label><select name="rio"><option value="">Nenhum</option>{"".join([f'<option value="{r}">{r}</option>' for r in lista_tipos_rio])}</select></div>
                                <div><label>Contato</label><input type="text" name="contato" placeholder="Nome do contato"></div>
                                <div><label>Telefone</label><input type="text" name="telefone" placeholder="(00) 00000-0000"></div>
                            </div>

                            <div class="input-group">
                                <label>Comentários</label>
                                <textarea name="comentarios" rows="2" placeholder="Detalhes da negociação..."></textarea>
                            </div>

                            <div style="display: flex; gap: 10px; margin-top: 10px;">
                                <button type="submit" id="btnSubmitForm" class="btn-login" style="width: auto; padding: 10px 24px;">Salvar Nova Negociação</button>
                                <button type="button" id="btnCancelarEdicao" onclick="cancelarNegocioEdicao()" style="display:none; background:#cbd5e0; border:none; padding:10px 16px; border-radius:6px; cursor:pointer; font-weight:600;">Cancelar Edição</button>
                            </div>
                        </form>
                    </div>
                </div>

                <!-- Barra de Filtros -->
                <div class="produto-detalhe-card" style="padding: 12px; margin-bottom: 15px;">
                    <div style="font-weight:700; color:#002244; margin-bottom:8px; font-size:13px;">Lista de Negócios</div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        <input type="text" id="filtroBusca" value="{busca_cliente}" placeholder="🔍 Buscar cliente..." style="flex: 2; min-width: 200px; padding: 10px;" onkeypress="if(event.key === 'Enter') aplicarFiltrosNegocios()">
                        
                        <select id="filtroVend" style="flex: 1; min-width: 140px; padding: 10px;" onchange="aplicarFiltrosNegocios()">
                            {options_filtro_vend}
                        </select>

                        <select id="filtroAno" style="flex: 0.8; min-width: 90px; padding: 10px;" onchange="aplicarFiltrosNegocios()">
                            {options_anos}
                        </select>

                        <select id="filtroPeriodo" style="flex: 1; min-width: 130px; padding: 10px;" onchange="aplicarFiltrosNegocios()">
                            {options_periodo}
                        </select>
                    </div>
                </div>

                <!-- Tabela de Negócios -->
                <div class="produto-detalhe-card negocios-tabela-card">
                    <div class="negocios-scroll-top" id="barraScrollNegocios" aria-label="Rolagem horizontal da tabela">
                        <div class="negocios-scroll-top-inner" id="barraScrollNegociosInner"></div>
                    </div>
                    <div class="negocios-tabela-wrap" id="negociosTabelaWrapPrincipal">
                        <table class="negocios-tabela" id="tabelaNegociosPrincipal">
                            <thead>
                                <tr style="background: #002244; color: #ffffff;">
                                    <th style="padding: 10px;">Temp.</th>
                                    <th style="padding: 10px;">Data</th>
                                    <th style="padding: 10px;">Vendedor</th>
                                    <th style="padding: 10px;">Cliente</th>
                                    <th style="padding: 10px;">Modelo</th>
                                    <th style="padding: 10px;">Chassis</th>
                                    <th style="padding: 10px;">Plano</th>
                                    <th style="padding: 10px;">RIO</th>
                                    <th style="padding: 10px;">Contato</th>
                                    <th style="padding: 10px;">Telefone</th>
                                    <th style="padding: 10px;">Comentários</th>
                                    <th class="coluna-acoes-header">Ações</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tabela_linhas}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            """
        except HTTPException:
            raise
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px;"><b>Erro ao carregar Negócios:</b> {e}</div>'
    


    elif nome_modulo == "rio":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            produtos_rio = obter_registros_com_cache(planilha, "RIO")

            pilulas_rio = []
            for item in produtos_rio:
                p_nome = str(item.get("PRODUTO", "")).strip()
                if p_nome:
                    active_cls = "active" if p_nome == produto_selecionado else ""
                    pilulas_rio.append(f'<a href="/modulo/rio?produto={urllib.parse.quote(p_nome)}" class="submodulo-pill {active_cls}">{p_nome}</a>')
            
            nav_superior_html = f"""
            <div class="submodulo-nav-container">
                <div class="submodulo-nav-label">Navegação Rápida — Telemetria RIO</div>
                <div class="submodulo-nav-scroll">{"".join(pilulas_rio)}</div>
            </div>
            """

            if produto_selecionado:
                item_escolhido = next((item for item in produtos_rio if str(item.get("PRODUTO", "")).strip() == produto_selecionado), None)

                if item_escolhido:
                    prod = item_escolhido.get("PRODUTO", "")
                    foco = item_escolhido.get("FOCO", "")
                    descricao = item_escolhido.get("DESCRIÇÃO", "")
                    valor = formatar_moeda(item_escolhido.get("VALOR R$", ""), manter_todos_decimais=False)
                    video = (
                        item_escolhido.get("VIDEO$", "")
                        or item_escolhido.get("VIDEO", "")
                        or item_escolhido.get("LINK_WHATSAPP", "")
                        or item_escolhido.get("LINK_WHATSAP", "")
                    )

                    def destacar_termos(texto):
                        if not texto: return ""
                        texto_formatado = re.sub(r"(Foco:)", r"<b>\1</b>", str(texto), flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Descrição:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Funcionalidades:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Diferencial Estratégico:|Diferencial Estrategico:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        return texto_formatado

                    foco_formatado = destacar_termos(foco)
                    descricao_formatada = destacar_termos(descricao)

                    contato_texto = f"{nome_usuario_logado}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    agora = datetime.now()
                    mes_vigente = MESES_PT.get(agora.month, "corrente")
                    ano_vigente = agora.year
                    validade_texto = f"{mes_vigente}/{ano_vigente}"

                    texto_whatsapp = f"📦 *Produto:* 🔧 {prod}\n\n🎯 *Foco:* {foco}\n\n📝 *Descrição:* {descricao}\n\n💰 *Valor:* {valor}\n\n⚠️ *Nota:* Proposta válida para {validade_texto}.\n\n🎬 *Assista ao vídeo explicativo aqui:* {video}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(str(texto_whatsapp))
                    url_video_embed = converter_para_embed(video)

                    btn_ver_video = f'<button type="button" class="btn-acao btn-video" onclick="abrirVideoModal(\'{url_video_embed}\')">▶ Assistir Vídeo</button>' if video else ""
                    btn_enviar_wpp = f'<a href="{link_wpp_compartilhar}" target="_blank" rel="noopener noreferrer" class="btn-acao btn-whatsapp">📤 Enviar WhatsApp</a>'

                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Produto</h2>
                        
                        <div class="produto-detalhe-card">
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Produto</div>
                                <div class="detalhe-valor detalhe-produto-nome">{prod}</div>
                            </div>
                            
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Foco</div>
                                <div class="detalhe-valor" style="color: #0066cc; font-weight: 600;">{foco_formatado}</div>
                            </div>
                            
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Descrição</div>
                                <div class="detalhe-valor" style="white-space: pre-line;">{descricao_formatada}</div>
                            </div>
                            
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Valor</div>
                                <div class="detalhe-valor detalhe-preco">{valor}</div>
                            </div>
                            
                            <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                <div class="detalhe-label" style="margin-bottom: 6px;">Ações Rápidas</div>
                                <div class="acoes-produto">
                                    {btn_ver_video}
                                    {btn_enviar_wpp}
                                </div>
                            </div>
                        </div>
                    </div>
                    """
                else:
                    conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Produto não encontrado.</p></div>'
            else:
                botoes_produtos = "".join([f'<a href="/modulo/rio?produto={urllib.parse.quote(str(item.get("PRODUTO", "")))}" class="submenu-btn">{item.get("PRODUTO", "")}</a>' for item in produtos_rio if item.get("PRODUTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Telemetria RIO — Selecione um Produto</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo o produto para ver os detalhes, foco, descrição, valor e acionar as ferramentas:</p>
                    <div class="submenus-grid">{botoes_produtos}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba RIO:</b> {e}</div>'

    elif nome_modulo == "pm":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            produtos_pm = obter_registros_com_cache(planilha, "PM")

            pilulas_pm = []
            for item in produtos_pm:
                p_nome = str(item.get("PRODUTO", "")).strip()
                if p_nome:
                    active_cls = "active" if p_nome == produto_selecionado else ""
                    pilulas_pm.append(f'<a href="/modulo/pm?produto={urllib.parse.quote(p_nome)}" class="submodulo-pill {active_cls}">{p_nome}</a>')
            
            nav_superior_html = f"""
            <div class="submodulo-nav-container">
                <div class="submodulo-nav-label">Navegação Rápida — Planos de Manutenção</div>
                <div class="submodulo-nav-scroll">{"".join(pilulas_pm)}</div>
            </div>
            """

            if produto_selecionado:
                item_escolhido = next((item for item in produtos_pm if str(item.get("PRODUTO", "")).strip() == produto_selecionado), None)

                if item_escolhido:
                    prod = item_escolhido.get("PRODUTO", "")
                    foco = item_escolhido.get("FOCO", "")
                    descricao = item_escolhido.get("DESCRIÇÃO", "")
                    coberturas = item_escolhido.get("COBERTURAS", "")
                    video = (
                        item_escolhido.get("VIDEO", "")
                        or item_escolhido.get("VIDEO$", "")
                        or item_escolhido.get("LINK_WHATSAPP", "")
                        or item_escolhido.get("LINK_WHATSAP", "")
                    )

                    def destacar_termos(texto):
                        if not texto: return ""
                        texto_formatado = re.sub(r"(Foco:)", r"<b>\1</b>", str(texto), flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Descrição:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Funcionalidades:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Diferencial Estratégico:|Diferencial Estrategico:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Coberturas:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        return texto_formatado

                    foco_formatado = destacar_termos(foco)
                    descricao_formatada = destacar_termos(descricao)
                    coberturas_formatadas = destacar_termos(coberturas)

                    contato_texto = f"{nome_usuario_logado}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    agora = datetime.now()
                    mes_vigente = MESES_PT.get(agora.month, "corrente")
                    ano_vigente = agora.year
                    validade_texto = f"{mes_vigente}/{ano_vigente}"

                    texto_whatsapp = f"📦 *Plano de Manutenção:* 🔧 {prod}\n\n🎯 *Foco:* {foco}\n\n📝 *Descrição:* {descricao}\n\n🛡️ *Coberturas:* {coberturas}\n\n⚠️ *Nota:* Proposta válida para {validade_texto}.\n\n🎬 *Assista ao vídeo explicativo aqui:* {video}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(str(texto_whatsapp))
                    url_video_embed = converter_para_embed(video)

                    btn_ver_video = f'<button type="button" class="btn-acao btn-video" onclick="abrirVideoModal(\'{url_video_embed}\')">▶ Assistir Vídeo</button>' if video else ""
                    btn_enviar_wpp = f'<a href="{link_wpp_compartilhar}" target="_blank" rel="noopener noreferrer" class="btn-acao btn-whatsapp">📤 Enviar WhatsApp</a>'

                    bloco_cobertura_html = ""
                    if coberturas:
                        bloco_cobertura_html = f"""
                        <div class="detalhe-linha">
                            <div class="detalhe-label">Coberturas do Plano</div>
                            <button type="button" class="btn-toggle-cobertura" onclick="toggleCobertura()">
                                <span>Exibir / Ocultar Coberturas</span>
                                <span id="cobertura-seta">▼</span>
                            </button>
                            <div id="cobertura-conteudo" class="conteudo-cobertura">{coberturas_formatadas}</div>
                        </div>
                        """

                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Plano</h2>
                        
                        <div class="produto-detalhe-card">
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Produto / Plano</div>
                                <div class="detalhe-valor detalhe-produto-nome">{prod}</div>
                            </div>
                            
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Foco</div>
                                <div class="detalhe-valor" style="color: #0066cc; font-weight: 600;">{foco_formatado}</div>
                            </div>
                            
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Descrição</div>
                                <div class="detalhe-valor" style="white-space: pre-line;">{descricao_formatada}</div>
                            </div>
                            
                            {bloco_cobertura_html}
                            
                            <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                <div class="detalhe-label" style="margin-bottom: 6px;">Ações Rápidas</div>
                                <div class="acoes-produto">
                                    {btn_ver_video}
                                    {btn_enviar_wpp}
                                </div>
                            </div>
                        </div>
                    </div>
                    """
                else:
                    conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Plano não encontrado.</p></div>'
            else:
                botoes_produtos = "".join([f'<a href="/modulo/pm?produto={urllib.parse.quote(str(item.get("PRODUTO", "")))}" class="submenu-btn">{item.get("PRODUTO", "")}</a>' for item in produtos_pm if item.get("PRODUTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Plano de Manutenção — Selecione um Plano</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo o plano de manutenção para ver os detalhes, foco, descrição, coberturas e ferramentas:</p>
                    <div class="submenus-grid">{botoes_produtos}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba PM:</b> {e}</div>'

    elif nome_modulo == "valores":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            dados_precos = obter_registros_com_cache(planilha, "PM_Precos")
            dados_modelos = obter_registros_com_cache(planilha, "Modelos")

            pilulas_valores = []
            for item in dados_precos:
                v_nome = str(item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO") or "").strip()
                if v_nome:
                    active_cls = "active" if v_nome == produto_selecionado else ""
                    pilulas_valores.append(f'<a href="/modulo/valores?produto={urllib.parse.quote(v_nome)}" class="submodulo-pill {active_cls}">{v_nome}</a>')

            nav_superior_html = f"""
            <div class="submodulo-nav-container">
                <div class="submodulo-nav-label">Navegação Rápida — Modelos</div>
                <div class="submodulo-nav-scroll">{"".join(pilulas_valores)}</div>
            </div>
            """

            if produto_selecionado:
                item_escolhido = next((item for item in dados_precos if str(item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO") or "").strip() == produto_selecionado), None)

                if item_escolhido:
                    titulo_principal = (
                        item_escolhido.get("MODELO")
                        or item_escolhido.get("PRODUTO")
                        or item_escolhido.get("ITEM")
                        or item_escolhido.get("PLANO")
                        or "Detalhes do Item"
                    )
                    periodo_val = item_escolhido.get("PERIODO", "")
                    
                    bloco_ficha_tecnica_html = ""

                    km_geral_val = item_escolhido.get("KM", "")
                    familia_modelo = identificar_familia_modelo(titulo_principal, dados_modelos)
                    grupo_manutencao_km = identificar_grupo_manutencao(
                        familia_modelo,
                        converter_intervalo_manutencao(km_geral_val),
                    )
                    intervalo_revisao_km = obter_intervalo_revisao(
                        titulo_principal,
                        familia_modelo,
                        grupo_manutencao_km,
                    )
                    planos_km_info = [
                        {"nome": "Plano PREV", "classe": "prev", "km_col": "PREV_VALOR KM" if "PREV_VALOR KM" in item_escolhido else "KM", "mensal_col": "VALOR MENSAL" if "VALOR MENSAL" in item_escolhido else "", "total_col": "TOTAL CONTRATO" if "TOTAL CONTRATO" in item_escolhido else ""},
                        {"nome": "Plano MAX", "classe": "max", "km_col": "MAX_VALOR KM" if "MAX_VALOR KM" in item_escolhido else "KM_1", "mensal_col": "VALOR MENSAL_1" if "VALOR MENSAL_1" in item_escolhido else "", "total_col": "TOTAL CONTRATO_1" if "TOTAL CONTRATO_1" in item_escolhido else ""},
                        {"nome": "Plano PLUS", "classe": "plus", "km_col": "PLUS_VALOR KM" if "PLUS_VALOR KM" in item_escolhido else "KM_2", "mensal_col": "VALOR MENSAL_2" if "VALOR MENSAL_2" in item_escolhido else "", "total_col": "TOTAL CONTRATO_2" if "TOTAL CONTRATO_2" in item_escolhido else ""}
                    ]

                    cards_km_html = ""
                    for p in planos_km_info:
                        km_val = formatar_moeda(item_escolhido.get(p["km_col"], ""), manter_todos_decimais=True)
                        mensal_val = formatar_moeda(item_escolhido.get(p["mensal_col"], ""), manter_todos_decimais=False) if p["mensal_col"] else ""
                        total_val = formatar_moeda(item_escolhido.get(p["total_col"], ""), manter_todos_decimais=False) if p["total_col"] else ""

                        if km_val != "-" or mensal_val != "-" or total_val != "-":
                            cards_km_html += f"""
                            <div class="card-plano {p['classe']}">
                                <div class="plano-titulo">{p['nome']} (KM)</div>
                                <div class="plano-linha-com-grupo">
                                    <div class="plano-col">
                                        <div class="detalhe-label">Valor KM</div>
                                        <div class="detalhe-valor" style="font-weight: 600;">{km_val}</div>
                                    </div>
                                    <div class="plano-col">
                                        <div class="detalhe-label">Valor Mensal</div>
                                        <div class="detalhe-valor" style="font-weight: 600; color: #2f855a;">{mensal_val}</div>
                                    </div>
                                    <div class="plano-col">
                                        <div class="detalhe-label">Total Contrato</div>
                                        <div class="detalhe-valor" style="font-weight: 600; color: #2b6cb0;">{total_val}</div>
                                    </div>
                                    <div class="plano-col">
                                        <div class="detalhe-label">Grupo de Manutenção</div>
                                        <div class="detalhe-valor" style="font-weight: 600;">{grupo_manutencao_km}</div>
                                    </div>
                                    <div class="plano-col">
                                        <div class="detalhe-label">Intervalo da Revisão</div>
                                        <div class="detalhe-valor" style="font-weight: 600;">{intervalo_revisao_km}</div>
                                    </div>
                                </div>
                            </div>
                            """

                    chaves_item = list(item_escolhido.keys())
                    hora_geral_val = ""
                    for indice_hora in range(3):
                        coluna_hora_resumo = encontrar_coluna_por_indice(
                            chaves_item,
                            "HORA",
                            indice_hora,
                        )
                        valor_hora_resumo = item_escolhido.get(coluna_hora_resumo, "") if coluna_hora_resumo else ""
                        if str(valor_hora_resumo).strip():
                            hora_geral_val = valor_hora_resumo
                            break
                    horas_contrato = converter_intervalo_manutencao(hora_geral_val)
                    horas_contrato_exibicao = (
                        f"{horas_contrato:,.0f} h".replace(",", ".")
                        if horas_contrato is not None
                        else "-"
                    )
                    intervalo_revisao_horas = obter_intervalo_revisao(
                        titulo_principal,
                        familia_modelo,
                        "Especial",
                        intervalo_horas=converter_intervalo_manutencao(hora_geral_val),
                    )
                    planos_hora_info = [
                        {"nome": "Plano PREV", "classe": "prev", "hora_indice": 0, "mensal_indice": 3},
                        {"nome": "Plano MAX", "classe": "max", "hora_indice": 1, "mensal_indice": 4},
                        {"nome": "Plano PLUS", "classe": "plus", "hora_indice": 2, "mensal_indice": 5},
                    ]

                    cards_horas_html = ""
                    for p in planos_hora_info:
                        nome_coluna_hora = f"{p['nome'].replace('Plano ', '').upper()}_VALOR HORA"
                        coluna_hora = encontrar_coluna_por_indice(
                            chaves_item,
                            nome_coluna_hora,
                            0,
                        ) or encontrar_coluna_por_indice(
                            chaves_item,
                            "HORA",
                            p["hora_indice"],
                        )
                        coluna_mensal = encontrar_coluna_por_indice(
                            chaves_item,
                            "VALOR MENSAL",
                            p["mensal_indice"],
                        )
                        coluna_total = encontrar_coluna_por_indice(
                            chaves_item,
                            "TOTAL CONTRATO",
                            p["mensal_indice"],
                        )
                        hora_val_crua = item_escolhido.get(coluna_hora, "") if coluna_hora else ""
                        mensal_val_crua = item_escolhido.get(coluna_mensal, "") if coluna_mensal else ""
                        total_val_crua = item_escolhido.get(coluna_total, "") if coluna_total else ""

                        hora_val = formatar_moeda(hora_val_crua, manter_todos_decimais=True)
                        mensal_val = formatar_moeda(mensal_val_crua, manter_todos_decimais=False)
                        total_val = formatar_moeda(total_val_crua, manter_todos_decimais=False)

                        if hora_val != "-" or mensal_val != "-" or total_val != "-":
                            cards_horas_html += f"""
                                <div class="card-plano {p['classe']}">
                                    <div class="plano-titulo">{p['nome']} (HORAS)</div>
                                    <div class="plano-linha-com-grupo">
                                        <div class="plano-col">
                                            <div class="detalhe-label">Valor Hora</div>
                                            <div class="detalhe-valor" style="font-weight: 600;">{hora_val}</div>
                                        </div>
                                        <div class="plano-col">
                                            <div class="detalhe-label">Valor Mensal</div>
                                            <div class="detalhe-valor" style="font-weight: 600; color: #2f855a;">{mensal_val}</div>
                                        </div>
                                        <div class="plano-col">
                                            <div class="detalhe-label">Total Contrato</div>
                                            <div class="detalhe-valor" style="font-weight: 600; color: #2b6cb0;">{total_val}</div>
                                        </div>
                                        <div class="plano-col">
                                            <div class="detalhe-label">Grupo de Manutenção</div>
                                            <div class="detalhe-valor" style="font-weight: 600;">Especial</div>
                                        </div>
                                        <div class="plano-col">
                                            <div class="detalhe-label">Intervalo da Revisão</div>
                                            <div class="detalhe-valor" style="font-weight: 600;">{intervalo_revisao_horas}</div>
                                        </div>
                                    </div>
                                </div>
                                """

                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes de Valores</h2>
                        
                        <div class="produto-detalhe-card">
                            <div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; margin-bottom: 14px;">
                                <div style="margin-bottom: 8px;">
                                    <div class="detalhe-label" style="color: #002244; margin-bottom: 2px;">Modelo / Item</div>
                                    <div class="detalhe-valor detalhe-produto-nome" style="font-size: 19px; color: #1a202c;">{titulo_principal}</div>
                                </div>
                                
                                <div style="display: flex; gap: 10px; margin-top: 10px; border-top: 1px solid #d8e2ec; padding-top: 8px;">
                                    <div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;">
                                        <div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Quilometragem (KM)</div>
                                        <div style="font-size: 15px; font-weight: 700; color: #1a202c;">{km_geral_val if km_geral_val else '-'}</div>
                                    </div>
                                    <div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;">
                                        <div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Período do Contrato</div>
                                        <div style="font-size: 15px; font-weight: 700; color: #1a202c;">{periodo_val} Meses</div>
                                    </div>
                                </div>
                            </div>
                            
                            {bloco_ficha_tecnica_html}
                            
                            { '<div style="font-size: 13px; font-weight: 700; color: #4a5568; margin-bottom: 6px; text-transform: uppercase;">Valores por Quilometragem (KM)</div>' if cards_km_html else '' }
                            <div class="grid-planos">{cards_km_html}</div>

                            { '<div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; margin-top: 18px; margin-bottom: 14px;"><div style="display: flex; gap: 10px;"><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Horas (H)</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + horas_contrato_exibicao + '</div></div><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Período do Contrato</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + str(periodo_val) + ' Meses</div></div></div></div>' if hora_geral_val else '' }

                            { '<div style="font-size: 13px; font-weight: 700; color: #4a5568; margin-top: 10px; margin-bottom: 6px; text-transform: uppercase;">Valores por Horas (H)</div>' if cards_horas_html else '' }
                            <div class="grid-planos">{cards_horas_html}</div>
                        </div>
                    </div>
                    """
                else:
                    conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Item não encontrado.</p></div>'
            else:
                botoes_itens = "".join([f'<a href="/modulo/valores?produto={urllib.parse.quote(str(item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO")))}" class="submenu-btn">{item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO")}</a>' for item in dados_precos if (item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO"))])
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Tabela de Valores — Selecione um Modelo</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo o modelo ou item para consultar os preços e informações detalhadas:</p>
                    <div class="submenus-grid">{botoes_itens}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba PM_Precos:</b> {e}</div>'

    elif nome_modulo == "informes":
        informe_selecionado = request.args.get("item")

        try:
            planilha = conectar_google_sheets()
            dados_informes = obter_registros_com_cache(planilha, "Informes")

            pilulas_informes = []
            for item in dados_informes:
                inf_nome = str(item.get("ASSUNTO", "")).strip()
                if inf_nome:
                    active_cls = "active" if inf_nome == informe_selecionado else ""
                    pilulas_informes.append(f'<a href="/modulo/informes?item={urllib.parse.quote(inf_nome)}" class="submodulo-pill {active_cls}">{inf_nome}</a>')

            nav_superior_html = f"""
            <div class="submodulo-nav-container">
                <div class="submodulo-nav-label">Navegação Rápida — Comunicados</div>
                <div class="submodulo-nav-scroll">{"".join(pilulas_informes)}</div>
            </div>
            """

            if informe_selecionado:
                item_escolhido = next((item for item in dados_informes if str(item.get("ASSUNTO", "")).strip() == informe_selecionado), None)

                if item_escolhido:
                    assunto_val = item_escolhido.get("ASSUNTO", "")
                    informacao_val = item_escolhido.get("INFORMAÇÃO", "") or item_escolhido.get("INFORMACAO", "")
                    circular_val = item_escolhido.get("CIRCULAR", "").strip()

                    link_pdf = circular_val
                    if circular_val:
                        if circular_val.lower() in mapa_drive:
                            link_pdf = mapa_drive[circular_val.lower()]
                        elif not circular_val.startswith("http"):
                            link_pdf = f"https://drive.google.com/drive/search?q={urllib.parse.quote(circular_val)}"

                    bloco_circular_html = ""
                    if circular_val:
                        bloco_circular_html = f"""
                        <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 12px; margin-top: 14px;">
                            <div class="detalhe-label" style="color: #002244; margin-bottom: 6px;">Circular Oficial</div>
                            <div class="acoes-ficha-tecnica">
                                <a href="{link_pdf}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-abrir-pdf">📄 ABRIR CIRCULAR (PDF)</a>
                            </div>
                        </div>
                        """

                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Comunicado</h2>
                        
                        <div class="produto-detalhe-card">
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Assunto</div>
                                <div class="detalhe-valor detalhe-produto-nome" style="color: #002244;">{assunto_val}</div>
                            </div>
                            
                            <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                <div class="detalhe-label">Informação Explicativa</div>
                                <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.6; margin-top: 6px;">{informacao_val}</div>
                            </div>
                            
                            {bloco_circular_html}
                        </div>
                    </div>
                    """
                else:
                    conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Informe não encontrado.</p></div>'
            else:
                botoes_informes = "".join([f'<a href="/modulo/informes?item={urllib.parse.quote(str(item.get("ASSUNTO", "")))}" class="submenu-btn">{item.get("ASSUNTO", "")}</a>' for item in dados_informes if item.get("ASSUNTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Informes e Circulares — Avisos e Comunicados</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Selecione abaixo um comunicado para visualizar a explicação detalhada e acessar a circular oficial:</p>
                    <div class="submenus-grid">{botoes_informes}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba Informes:</b> {e}</div>'

    elif nome_modulo == "argumentos":
        argumento_selecionado = request.args.get("item")

        try:
            planilha = conectar_google_sheets()
            dados_argumentos = obter_registros_com_cache(planilha, "Argumentos")

            pilulas_argumentos = []
            for item in dados_argumentos:
                arg_nome = str(item.get("QUESTIONAMENTO", "")).strip()
                if arg_nome:
                    active_cls = "active" if arg_nome == argumento_selecionado else ""
                    pilulas_argumentos.append(f'<a href="/modulo/argumentos?item={urllib.parse.quote(arg_nome)}" class="submodulo-pill {active_cls}">{arg_nome}</a>')

            nav_superior_html = f"""
            <div class="submodulo-nav-container">
                <div class="submodulo-nav-label">Navegação Rápida — Objeções / Dúvidas</div>
                <div class="submodulo-nav-scroll">{"".join(pilulas_argumentos)}</div>
            </div>
            """

            if argumento_selecionado:
                item_escolhido = next((item for item in dados_argumentos if str(item.get("QUESTIONAMENTO", "")).strip() == argumento_selecionado), None)

                if item_escolhido:
                    pergunta_val = item_escolhido.get("QUESTIONAMENTO", "")
                    resposta_val = item_escolhido.get("RESPOSTA", "")

                    contato_texto = f"{nome_usuario_logado}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    texto_whatsapp = f"💡 *Questionamento:* {pergunta_val}\n\n💬 *Resposta / Argumento:* {resposta_val}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(str(texto_whatsapp))

                    btn_enviar_wpp = f"""
                    <div style="margin-top: 14px;">
                        <a href="{link_wpp_compartilhar}" target="_blank" rel="noopener noreferrer" class="btn-acao btn-whatsapp" style="width: 100%; display: inline-flex; justify-content: center; align-items: center; padding: 12px; text-decoration: none; border-radius: 6px; font-weight: 600; color: #ffffff; background-color: #2f855a;">📤 Enviar Resposta via WhatsApp</a>
                    </div>
                    """

                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Argumentos de Venda</h2>
                        
                        <div class="produto-detalhe-card">
                            <div class="detalhe-linha">
                                <div class="detalhe-label" style="color: #0066cc;">Questionamento</div>
                                <div class="detalhe-valor detalhe-produto-nome" style="font-size: 16px; color: #1a202c;">{pergunta_val}</div>
                            </div>
                            
                            <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                <div class="detalhe-label" style="color: #2f855a;">Resposta Sugerida</div>
                                <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.6; margin-top: 6px; font-size: 14px;">{resposta_val}</div>
                            </div>
                            
                            {btn_enviar_wpp}
                        </div>
                    </div>
                    """
                else:
                    conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Argumento não encontrado.</p></div>'
            else:
                botoes_argumentos = "".join([f'<a href="/modulo/argumentos?item={urllib.parse.quote(str(item.get("QUESTIONAMENTO", "")))}" class="submenu-btn">{item.get("QUESTIONAMENTO", "")}</a>' for item in dados_argumentos if item.get("QUESTIONAMENTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Argumentos de Venda — Objeções e Respostas</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Selecione abaixo a dúvida ou objeção do cliente para visualizar a melhor linha de argumentação:</p>
                    <div class="submenus-grid">{botoes_argumentos}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba Argumentos:</b> {e}</div>'

    elif nome_modulo == "fichatecnica":
        tipo_selecionado = request.args.get("tipo")
        categoria_selecionada = request.args.get("categoria")
        modelo_selecionado = request.args.get("modelo")

        try:
            planilha = conectar_google_sheets()
            dados_modelos = obter_registros_com_cache(
                planilha,
                "Modelos",
                ttl=0,
                falhar_em_erro=True,
            )
            aliases_modelo = {
                "MODELO": ("MODELO", "NOME DO MODELO"),
                "TIPO": ("TIPO", "TIPO DE VEICULO", "TIPO DO VEICULO"),
                "CATEGORIA": ("CATEGORIA", "LINHA"),
                "IMG": ("IMG", "IMAGEM", "FOTO"),
                "LINK": ("LINK", "FICHA TECNICA", "LINK FICHA TECNICA"),
            }
            registros_normalizados = []
            for registro in dados_modelos:
                valores_normalizados = {
                    normalizar_chave_planilha(chave): valor
                    for chave, valor in registro.items()
                }
                registro_normalizado = dict(registro)
                for campo, aliases in aliases_modelo.items():
                    if str(registro_normalizado.get(campo, "") or "").strip():
                        continue
                    valor = next(
                        (
                            valores_normalizados.get(
                                normalizar_chave_planilha(alias),
                                "",
                            )
                            for alias in aliases
                            if str(
                                valores_normalizados.get(
                                    normalizar_chave_planilha(alias),
                                    "",
                                )
                                or ""
                            ).strip()
                        ),
                        "",
                    )
                    if valor:
                        registro_normalizado[campo] = valor
                registros_normalizados.append(registro_normalizado)
            dados_modelos = registros_normalizados

            tipos_disponiveis = sorted(list(set(str(item.get("TIPO", "")).strip() for item in dados_modelos if str(item.get("TIPO", "")).strip())))

            if not tipo_selecionado:
                botoes_tipos = "".join([f'<a href="/modulo/fichatecnica?tipo={urllib.parse.quote(t)}" class="submenu-btn">{t}</a>' for t in tipos_disponiveis])
                if not botoes_tipos:
                    botoes_tipos = (
                        '<p style="color:#64748b">Não há tipos de veículos disponíveis '
                        'na aba Modelos. Verifique se os registros têm modelo e tipo '
                        'preenchidos.</p>'
                    )
                conteudo = f"""
                <div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Ficha Técnica — Selecione a Categoria</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo entre Caminhões ou Ônibus para visualizar as categorias:</p>
                    <div class="submenus-grid">{botoes_tipos}</div>
                </div>
                """
            elif not categoria_selecionada:
                modelos_do_tipo = [item for item in dados_modelos if str(item.get("TIPO", "")).strip().lower() == tipo_selecionado.lower()]
                categorias_disponiveis = sorted(list(set(str(item.get("CATEGORIA", "")).strip() for item in modelos_do_tipo if str(item.get("CATEGORIA", "")).strip())))

                botoes_categorias = "".join([f'<a href="/modulo/fichatecnica?tipo={urllib.parse.quote(tipo_selecionado)}&categoria={urllib.parse.quote(c)}" class="submenu-btn">{c}</a>' for c in categorias_disponiveis])
                conteudo = f"""
                <div>
                    <div style="margin-bottom: 10px;">
                        <a href="/modulo/fichatecnica" style="font-size: 13px; font-weight: 600; color: #0066cc; text-decoration: none;">← Voltar para Tipos</a>
                    </div>
                    <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">{tipo_selecionado} — Selecione a Linha / Categoria</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo a categoria para ver os modelos correspondentes:</p>
                    <div class="submenus-grid">{botoes_categorias}</div>
                </div>
                """
            else:
                modelos_filtrados = [
                    item for item in dados_modelos 
                    if str(item.get("TIPO", "")).strip().lower() == tipo_selecionado.lower() 
                    and str(item.get("CATEGORIA", "")).strip().lower() == categoria_selecionada.lower()
                ]

                pilulas_modelos = []
                for item in modelos_filtrados:
                    m_nome = str(item.get("MODELO", "")).strip()
                    if m_nome:
                        active_cls = "active" if m_nome == modelo_selecionado else ""
                        pilulas_modelos.append(f'<a href="/modulo/fichatecnica?tipo={urllib.parse.quote(tipo_selecionado)}&categoria={urllib.parse.quote(categoria_selecionada)}&modelo={urllib.parse.quote(m_nome)}" class="submodulo-pill {active_cls}">{m_nome}</a>')
                
                nav_superior_html = f"""
                <div style="margin-bottom: 10px; display: flex; gap: 15px;">
                    <a href="/modulo/fichatecnica" style="font-size: 13px; font-weight: 600; color: #0066cc; text-decoration: none;">← Tipos</a>
                    <a href="/modulo/fichatecnica?tipo={urllib.parse.quote(tipo_selecionado)}" style="font-size: 13px; font-weight: 600; color: #0066cc; text-decoration: none;">← Categorias de {tipo_selecionado}</a>
                </div>
                <div class="submodulo-nav-container">
                    <div class="submodulo-nav-label">Navegação — Modelos ({categoria_selecionada})</div>
                    <div class="submodulo-nav-scroll">{"".join(pilulas_modelos)}</div>
                </div>
                """

                if modelo_selecionado:
                    item_escolhido = next((item for item in modelos_filtrados if str(item.get("MODELO", "")).strip() == modelo_selecionado), None)

                    if item_escolhido:
                        m_tipo = item_escolhido.get("TIPO", "")
                        m_categoria = item_escolhido.get("CATEGORIA", "")
                        m_modelo = item_escolhido.get("MODELO", "")
                        m_descricao = item_escolhido.get("DESCRIÇÃO", "") or item_escolhido.get("DESCRICAO", "")
                        m_eficiencia = item_escolhido.get("EFICIÊNCIA", "") or item_escolhido.get("EFICIENCIA", "")
                        m_conforto = item_escolhido.get("CONFORTO", "")
                        
                        m_seguranca_ativa = item_escolhido.get("SEGURANÇA ATIVA", "") or item_escolhido.get("SEGURANCA ATIVA", "")
                        m_tecnologia = item_escolhido.get("TECNOLOGIA", "")
                        dados_modelo_normalizados = [
                            (normalizar_chave_planilha(chave), valor)
                            for chave, valor in item_escolhido.items()
                        ]

                        def obter_dado_tecnico(*nomes):
                            for nome in nomes:
                                nome_normalizado = normalizar_chave_planilha(nome)
                                valores_correspondentes = [
                                    valor
                                    for chave, valor in dados_modelo_normalizados
                                    if chave == nome_normalizado
                                    or re.fullmatch(
                                        rf"{re.escape(nome_normalizado)} \d+",
                                        chave,
                                    )
                                ]
                                valor = next(
                                    (
                                        valor
                                        for valor in reversed(valores_correspondentes)
                                        if str(valor or "").strip()
                                    ),
                                    "",
                                )
                                if valor:
                                    return str(valor).strip()
                            return ""

                        linhas_dados_tecnicos = [
                            ("Tecnologia do motor", obter_dado_tecnico("TECNO")),
                            ("PBT", obter_dado_tecnico("PBT", "PBT HOMOLOGADO (KG)")),
                            (
                                "Entre-eixos",
                                obter_dado_tecnico(
                                    "ENTRE EIXO",
                                    "ENTRE EIXOS",
                                    "ENTRE EIXOS (MM)",
                                    "ENTRE-EIXOS",
                                ),
                            ),
                            ("Motor", obter_dado_tecnico("MOTOR")),
                            ("Potência", obter_dado_tecnico("POTENCIA")),
                            (
                                "Transmissão",
                                obter_dado_tecnico(
                                    "TRANSMISSAO",
                                    "TRANSMISSÃO",
                                    "TRANSMISAO",
                                ),
                            ),
                            (
                                "Sistema de injeção",
                                obter_dado_tecnico("SISTEMA DE INJECAO", "SISTEMA DE INJEÇÃO"),
                            ),
                            (
                                "Combustível",
                                obter_dado_tecnico("COMBUSTIVEL", "COMBUSTÍVEL"),
                            ),
                        ]
                        linhas_dados_tecnicos = [
                            (rotulo, valor)
                            for rotulo, valor in linhas_dados_tecnicos
                            if valor
                        ]
                        bloco_dados_tecnicos_html = ""
                        texto_dados_tecnicos_wpp = ""
                        if linhas_dados_tecnicos:
                            linhas_tabela_tecnica = []
                            for indice in range(0, len(linhas_dados_tecnicos), 2):
                                celulas = []
                                for rotulo, valor in linhas_dados_tecnicos[indice:indice + 2]:
                                    celulas.append(
                                        f"<th>{html.escape(rotulo)}</th>"
                                        f"<td>{html.escape(valor)}</td>"
                                    )
                                if len(celulas) == 2:
                                    celulas.extend(("<th></th><td></td>",))
                                linhas_tabela_tecnica.append(
                                    f"<tr>{''.join(celulas)}</tr>"
                                )
                            bloco_dados_tecnicos_html = f"""
                            <table class="ficha-dados-tecnicos">
                                <caption>⚙️ Dados técnicos</caption>
                                <colgroup>
                                    <col style="width:18%">
                                    <col style="width:32%">
                                    <col style="width:18%">
                                    <col style="width:32%">
                                </colgroup>
                                <tbody>{"".join(linhas_tabela_tecnica)}</tbody>
                            </table>
                            """
                            texto_dados_tecnicos_wpp = (
                                "⚙️ *DADOS TÉCNICOS:*\n"
                                + "\n".join(
                                    f"*{rotulo}:* {valor}"
                                    for rotulo, valor in linhas_dados_tecnicos
                                )
                                + "\n\n"
                            )

                        m_imagem = str(item_escolhido.get("IMG", "") or "").strip()
                        id_imagem = extrair_id_arquivo_drive(m_imagem)
                        if id_imagem:
                            imagem_modelo_url = url_for(
                                "servir_comprovante_drive",
                                file_id=id_imagem,
                            )
                        elif re.match(r"^https?://", m_imagem, re.I):
                            imagem_modelo_url = m_imagem
                        else:
                            imagem_modelo_url = ""
                        
                        m_link = str(item_escolhido.get("LINK", "")).strip()
                        if not m_link:
                            for k, v in item_escolhido.items():
                                if "link" in k.lower() and str(v).strip():
                                    m_link = str(v).strip()
                                    break

                        link_pdf = m_link
                        if m_link:
                            if m_link.lower() in mapa_drive:
                                link_pdf = mapa_drive[m_link.lower()]
                            elif not m_link.startswith("http"):
                                link_pdf = f"https://drive.google.com/drive/search?q={urllib.parse.quote(m_link)}"

                        bloco_pdf_html = ""
                        if m_link:
                            texto_wpp_ft = (
                                f"📋 *Ficha Técnica - {m_modelo}*\n\n"
                                f"*Tipo:* {m_tipo} | *Categoria:* {m_categoria}\n\n"
                                f"📝 *DESCRIÇÃO:*\n{m_descricao}\n\n"
                                f"⚡ *EFICIÊNCIA:*\n{m_eficiencia}\n\n"
                                f"🛋️ *CONFORTO:*\n{m_conforto}\n\n"
                                f"🛡️ *SEGURANÇA ATIVA:*\n{m_seguranca_ativa}\n\n"
                                f"💻 *TECNOLOGIA:*\n{m_tecnologia}\n\n"
                                f"{texto_dados_tecnicos_wpp}"
                                f"📄 *Ficha Técnica (PDF):* {link_pdf}"
                            )
                            link_wpp_ft = f"https://api.whatsapp.com/send?text={urllib.parse.quote(str(texto_wpp_ft))}"

                            bloco_pdf_html = f"""
                            <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 12px; margin-top: 14px;">
                                <div class="detalhe-label" style="color: #002244; margin-bottom: 6px;">Documento / Ficha Técnica (PDF)</div>
                                <div class="acoes-ficha-tecnica">
                                    <a href="{html.escape(link_pdf, quote=True)}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-abrir-pdf">📂 ABRIR PDF</a>
                                    <a href="{html.escape(link_wpp_ft, quote=True)}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-wpp-pdf">📤 ENVIAR VIA WHATSAPP</a>
                                </div>
                            </div>
                            """

                        imagem_modelo_html = (
                            f'<img src="{html.escape(imagem_modelo_url, quote=True)}" '
                            f'alt="Caminhão {html.escape(str(m_modelo), quote=True)}" '
                            'loading="lazy" referrerpolicy="no-referrer" '
                            'style="width:100%;height:100%;max-height:300px;object-fit:contain;'
                            'border-radius:8px;background:#fff;" '
                            'onerror="this.parentElement.innerHTML=\'<div class=&quot;ficha-imagem-vazia&quot;>'
                            'Foto do modelo não disponível</div>\'">'
                            if imagem_modelo_url else
                            '<div class="ficha-imagem-vazia">Foto do modelo não disponível</div>'
                        )

                        conteudo = f"""
                        <style>
                            .ficha-modelo-destaque {{
                                display:grid;
                                grid-template-columns:minmax(240px, 38%) minmax(0, 1fr);
                                gap:20px;
                                align-items:stretch;
                                margin-bottom:18px;
                                padding:16px;
                                border:1px solid #e2e8f0;
                                border-radius:10px;
                                background:linear-gradient(135deg,#f8fafc,#fff);
                            }}
                            .ficha-imagem-modelo {{
                                display:flex;
                                align-items:center;
                                justify-content:center;
                                min-height:220px;
                                padding:10px;
                                border:1px solid #edf2f7;
                                border-radius:8px;
                                background:#fff;
                            }}
                            .ficha-imagem-vazia {{
                                color:#94a3b8;
                                font-size:13px;
                                text-align:center;
                            }}
                            .ficha-identificacao-modelo {{
                                display:flex;
                                flex-direction:column;
                                justify-content:center;
                                gap:12px;
                            }}
                            .ficha-identificacao-modelo h3 {{
                                margin:0;
                                color:#002244;
                                font-size:25px;
                                line-height:1.2;
                            }}
                            .ficha-identificacao-badges {{
                                display:grid;
                                grid-template-columns:repeat(2,minmax(0,1fr));
                                gap:9px;
                                align-items:stretch;
                            }}
                            .ficha-identificacao-badge {{
                                min-width:0;
                                min-height:58px;
                                padding:10px 12px;
                                border:1px solid #e2e8f0;
                                border-radius:7px;
                                background:#fff;
                                display:flex;
                                flex-direction:column;
                                justify-content:center;
                                box-sizing:border-box;
                            }}
                            .ficha-dados-tecnicos {{
                                width:100%;
                                border-collapse:collapse;
                                table-layout:fixed;
                                background:#fff;
                                font-size:11px;
                                margin-top:2px;
                            }}
                            .ficha-dados-tecnicos caption {{
                                padding:8px 0 4px;
                                color:#002244;
                                font-size:11px;
                                font-weight:800;
                                text-align:left;
                            }}
                            .ficha-dados-tecnicos th,
                            .ficha-dados-tecnicos td {{
                                padding:5px 6px;
                                border-bottom:1px solid #e2e8f0;
                                text-align:left;
                                vertical-align:top;
                                overflow-wrap:anywhere;
                            }}
                            .ficha-dados-tecnicos th {{
                                color:#64748b;
                                font-size:9px;
                                font-weight:700;
                                text-transform:uppercase;
                                line-height:1.25;
                            }}
                            .ficha-dados-tecnicos td {{
                                color:#1e293b;
                                font-weight:700;
                                line-height:1.35;
                            }}
                            @media(max-width:760px) {{
                                .ficha-modelo-destaque {{grid-template-columns:1fr;gap:14px}}
                                .ficha-imagem-modelo {{min-height:180px}}
                                .ficha-identificacao-modelo h3 {{font-size:21px}}
                            }}
                            @media(max-width:480px) {{
                                .ficha-identificacao-badges {{grid-template-columns:1fr 1fr;gap:7px}}
                                .ficha-identificacao-badge {{padding:8px;min-height:52px}}
                                .ficha-dados-tecnicos {{font-size:10px}}
                                .ficha-dados-tecnicos th,
                                .ficha-dados-tecnicos td {{padding:5px 4px}}
                            }}
                        </style>
                        <div>
                            {nav_superior_html}
                            <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Ficha Técnica do Modelo</h2>
                            
                            <section class="ficha-modelo-destaque">
                                <div class="ficha-imagem-modelo">
                                    {imagem_modelo_html}
                                </div>
                                <div class="ficha-identificacao-modelo">
                                    <div>
                                        <div class="detalhe-label">Modelo selecionado</div>
                                        <h3>{html.escape(str(m_modelo))}</h3>
                                    </div>
                                    <div class="ficha-identificacao-badges">
                                        <div class="ficha-identificacao-badge">
                                            <div class="detalhe-label">Tipo</div>
                                            <div class="detalhe-valor" style="font-weight:600;">{html.escape(str(m_tipo)) or "—"}</div>
                                        </div>
                                        <div class="ficha-identificacao-badge">
                                            <div class="detalhe-label">Categoria</div>
                                            <div class="detalhe-valor" style="font-weight:600;">{html.escape(str(m_categoria)) or "—"}</div>
                                        </div>
                                    </div>
                                    {bloco_dados_tecnicos_html}
                                </div>
                            </section>

                            <div class="produto-detalhe-card">
                                <div class="detalhe-linha">
                                    <div class="detalhe-label">📝 Descrição</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{html.escape(str(m_descricao))}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">⚡ Eficiência</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{html.escape(str(m_eficiencia))}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">🛋️ Conforto</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{html.escape(str(m_conforto))}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">🛡️ Segurança Ativa</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{html.escape(str(m_seguranca_ativa))}</div>
                                </div>

                                <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                    <div class="detalhe-label">💻 Tecnologia</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{html.escape(str(m_tecnologia))}</div>
                                </div>

                                {bloco_pdf_html}
                            </div>
                        </div>
                        """
                    else:
                        conteudo = f'<div>{nav_superior_html}<p style="color: #c53030;">Modelo não encontrado.</p></div>'
                else:
                    botoes_modelos = "".join([f'<a href="/modulo/fichatecnica?tipo={urllib.parse.quote(tipo_selecionado)}&categoria={urllib.parse.quote(categoria_selecionada)}&modelo={urllib.parse.quote(str(item.get("MODELO", "")))}" class="submenu-btn">{item.get("MODELO", "")}</a>' for item in modelos_filtrados if item.get("MODELO")])
                    conteudo = f"""
                    <div>
                        {nav_superior_html}
                        <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Modelos da Categoria: {categoria_selecionada}</h2>
                        <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Escolha abaixo o modelo desejado para consultar suas especificações completas:</p>
                        <div class="submenus-grid">{botoes_modelos}</div>
                    </div>
                    """
        except Exception as e:
            conteudo = (
                '<div style="color:#c53030;background:#fff5f5;padding:15px;'
                'border-radius:8px;border:1px solid #feb2b2">'
                '<b>Erro ao carregar os dados da aba Modelos:</b> '
                f'{html.escape(str(e))}</div>'
            )

    else:
        conteudo = f"""
        <div>
            <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">{modulo_titulo}</h2>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px;">
                <p style="color: #4a5568; font-size: 14px; line-height: 1.6;">Conteúdo em desenvolvimento para este módulo.</p>
            </div>
        </div>
        """

    if request.method == "POST":
        abas_cache_por_modulo = {
            "rio": ("RIO",),
            "pm": ("PM",),
            "valores": ("PM_Precos",),
            "informes": ("Informes",),
            "argumentos": ("Argumentos",),
            "fichatecnica": ("Modelos",),
            "vendas": ("Vendas_PM", "Negocios_PM"),
            "negocios": ("Negocios_PM", "Vendas_PM"),
        }
        invalidar_cache_ab_as(*abas_cache_por_modulo.get(nome_modulo, ()))

    return render_template_string(
        TEMPLATE_HTML, 
        conteudo_modulo=conteudo, 
        modulo_ativo=nome_modulo,
        modulo_titulo=modulo_titulo
    )

@app.route("/api/limpar-cache", methods=["POST"])
def limpar_cache():
    global CACHE_IA, CACHE_PLANILHAS, CACHE_LOGIN_DADOS, CACHE_DRIVE, CACHE_PLANILHA_CLIENTE
    CACHE_IA["contexto_sistema"] = ""
    CACHE_IA["timestamp"] = 0
    CACHE_IA["registros"] = None
    CACHE_IA["planilha_id"] = ""

    CACHE_PLANILHAS = {"dados": {}, "timestamps": {}}
    CACHE_LOGIN_DADOS = {"dados": {}, "timestamp": 0, "usuario": ""}
    CACHE_DRIVE = {"conteudo": {}, "mapa": {}, "timestamp": 0}
    CACHE_PLANILHA_CLIENTE = {"cliente": None, "timestamp": 0}
    if "CACHE_CAMPANHAS" in globals():
        CACHE_CAMPANHAS.clear()

    return jsonify({"mensagem": "Base de dados, cache de planilhas e IA atualizados com sucesso!"})

@app.route("/api/chat-ia", methods=["POST"])
def chat_ia():
    global CACHE_IA
    
    if not session.get("logado"):
        return jsonify({"resposta": "Sessão expirada. Faça login novamente."}), 401
    
    dados = request.get_json()
    pergunta_usuario = dados.get("mensagem", "").strip()
    
    if not pergunta_usuario:
        return jsonify({"resposta": "Por favor, digite uma pergunta."})

    palavras = pergunta_usuario.split()
    texto_lower = pergunta_usuario.lower()
    cumprimentos = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem", "eae", "hey", "salve"]
    
    if len(palavras) < 3 and texto_lower in cumprimentos:
        return jsonify({
            "resposta": "Olá! Como posso ajudar você com os negócios, planos de manutenção ou telemetria hoje?"
        })

    try:
        agora = time.time()
        
        if CACHE_IA["registros"] is None or (agora - CACHE_IA["timestamp"] > TEMPO_CACHE_SEGUNDOS):
            print("🔄 IA: Atualizando cache otimizado...")
            planilha = conectar_google_sheets()
            registros_ia, planilha_id = carregar_indice_planilha_ia(planilha)
            CACHE_IA["registros"] = registros_ia
            CACHE_IA["planilha_id"] = planilha_id
            CACHE_IA["timestamp"] = agora

        dados_planilha, links_permitidos = selecionar_contexto_ia(
            CACHE_IA["registros"], pergunta_usuario
        )
        instrucao_sistema = (
            "Você é o Assistente Oficial da Novo Mundo Caminhões e Ônibus. "
            "Responda em português, de forma direta, usando somente os registros relevantes fornecidos. "
            "Se não houver evidência suficiente nesses registros, diga que não encontrou a informação. "
            "Nunca invente URLs, nunca compartilhe links de acesso à planilha ou ao Google Sheets e só informe links que apareçam literalmente nos registros fornecidos. "
            "Não revele dados pessoais, credenciais ou informações de abas administrativas."
        )
        CACHE_IA["contexto_sistema"] = f"{instrucao_sistema}\n\nRegistros relevantes:\n{dados_planilha}"

        cliente_ia = criar_cliente_gemini()
        
        prompt_completo = f"{CACHE_IA['contexto_sistema']}\n\nPergunta do Usuário: {pergunta_usuario}\nResposta:"
        
        resposta_ia = None
        modelos_para_tentar = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]
        modelo_configurado = os.environ.get("GEMINI_MODEL", "").strip()
        if modelo_configurado in modelos_para_tentar:
            modelos_para_tentar.remove(modelo_configurado)
            modelos_para_tentar.insert(0, modelo_configurado)
        elif modelo_configurado:
            print("GEMINI_MODEL fora dos modelos permitidos; usando Gemini 3.5 Flash Lite e Gemini 3.6 Flash.")
        
        ultimo_erro = None
        for nome_modelo in modelos_para_tentar:
            try:
                resposta_ia = cliente_ia.models.generate_content(
                    model=nome_modelo,
                    contents=prompt_completo
                )
                if resposta_ia and resposta_ia.text:
                    break
            except Exception as err:
                ultimo_erro = err
                continue
                
        if not resposta_ia or not resposta_ia.text:
            raise Exception(f"Todos os modelos falharam. Último erro: {ultimo_erro}")
        
        resposta_filtrada = filtrar_links_resposta_ia(
            resposta_ia.text,
            links_permitidos,
            CACHE_IA["planilha_id"],
        )
        return jsonify({"resposta": resposta_filtrada})

    except Exception as e:
        erro_texto = str(e)
        erro_lower = erro_texto.lower()
        print(f"Erro na IA: {erro_texto}")
        traceback.print_exc()

        if "gemini_api_key não configurada" in erro_lower:
            mensagem_erro = "A chave do Gemini não está configurada. Cadastre GEMINI_API_KEY no ambiente do servidor."
        elif "429" in erro_texto or "quota" in erro_lower or "resource_exhausted" in erro_lower:
            mensagem_erro = "O limite de uso do Gemini foi atingido. Aguarde e tente novamente."
        elif "401" in erro_texto or "unauthenticated" in erro_lower or "authentication" in erro_lower:
            mensagem_erro = "A chave do Gemini foi recusada. Confira GEMINI_API_KEY no ambiente do servidor."
        elif "403" in erro_texto or "permission denied" in erro_lower or "forbidden" in erro_lower:
            mensagem_erro = "O Gemini recusou o acesso. Confira se a chave está ativa e tem permissão para usar a API."
        elif "404" in erro_texto or "not found" in erro_lower or "model" in erro_lower:
            mensagem_erro = "O modelo Gemini configurado não está disponível. Confira GEMINI_MODEL no ambiente do servidor."
        else:
            mensagem_erro = "O Gemini não respondeu. Confira os logs do servidor para ver o erro técnico."

        return jsonify({"resposta": mensagem_erro})


@app.route("/api/atualizacoes", methods=["GET"])
def api_atualizacoes():
    """Retorna informes publicados e alterações observadas nas abas dos módulos.

    Importante: nunca retorna link de acesso à planilha/Drive. O sino leva
    diretamente ao menu interno do sistema onde o conteúdo pode ser consultado.
    Alterações de abas são comparadas com o último estado observado pelo processo.
    """
    if not session.get("logado"):
        return jsonify({"atualizacoes": [], "nao_lidas": 0}), 401

    try:
        limite = max(1, min(int(request.args.get("limite", 12)), 30))
    except (ValueError, TypeError):
        limite = 12

    agora = time.time()
    if (
        CACHE_ATUALIZACOES_ABAS["timestamp"]
        and agora - CACHE_ATUALIZACOES_ABAS["timestamp"] < 300
    ):
        final_cache = CACHE_ATUALIZACOES_ABAS["atualizacoes"][:limite]
        return jsonify({"atualizacoes": final_cache, "nao_lidas": len(final_cache)})

    atualizacoes = []
    nomes_modulos_por_aba = {
        "RIO": [("Telemetria RIO", "rio"), ("Dashboard Executivo", "dashboard")],
        "PM": [("Plano de Manutenção", "pm"), ("Dashboard Executivo", "dashboard")],
        "PM_Precos": [("Tabela de Valores", "valores"), ("Dashboard Executivo", "dashboard")],
        "Informes": [("Informes e Circulares", "informes"), ("Dashboard Executivo", "dashboard")],
        "Modelos": [("Ficha Técnica", "fichatecnica"), ("Dashboard Executivo", "dashboard")],
        "Argumentos": [("Argumentos de Venda", "argumentos")],
        "Negocios_PM": [("Visitas e Acompanhamento", "visitas"), ("Dashboard Executivo", "dashboard")],
        "Vendas_PM": [("Dashboard Executivo", "dashboard")],
        "Campanhas_VW": [("Campanha VW PREV", "camp_vw_prev")],
        "Regras_Camp_VW": [("Campanha VW PREV", "camp_vw_prev")],
    }

    try:
        planilha = conectar_google_sheets()
        dados_abas = obter_linhas_abas_em_lote(planilha, list(nomes_modulos_por_aba))
        for nome_aba, destinos in nomes_modulos_por_aba.items():
            registros_aba = registros_de_linhas_planilha(dados_abas[nome_aba])

            hash_aba = hashlib.sha256(
                json.dumps(registros_aba, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            hash_anterior = CACHE_ATUALIZACOES_ABAS["hashes"].get(nome_aba)
            CACHE_ATUALIZACOES_ABAS["hashes"][nome_aba] = hash_aba

            # A primeira leitura estabelece o estado inicial e não transforma
            # todo o conteúdo já existente em uma falsa atualização.
            if hash_anterior is not None and hash_anterior != hash_aba:
                momento = datetime.now().strftime("%d/%m/%Y %H:%M")
                id_evento = hashlib.sha256(
                    f"{nome_aba}|{hash_anterior}|{hash_aba}|{momento}".encode("utf-8")
                ).hexdigest()[:20]
                for titulo, modulo in destinos:
                    CACHE_ATUALIZACOES_ABAS["eventos"].append({
                        "id": f"aba:{id_evento}:{modulo}",
                        "tipo": "Atualização",
                        "titulo": f"{titulo} atualizado",
                        "descricao": f"Foram detectadas alterações nos dados relacionados a {titulo}.",
                        "data": momento,
                        "link": f"/modulo/{modulo}",
                    })
                CACHE_ATUALIZACOES_ABAS["eventos"] = CACHE_ATUALIZACOES_ABAS["eventos"][-100:]

            if nome_aba != "Informes":
                continue

            for item in registros_aba:
                assunto = str(item.get("ASSUNTO", "")).strip()
                info = str(item.get("INFORMAÇÃO", "") or item.get("INFORMACAO", "")).strip()
                circular = str(item.get("CIRCULAR", "")).strip()
                data_atualizacao = str(
                    item.get("DATA", "")
                    or item.get("DATA ATUALIZAÇÃO", "")
                    or item.get("DATA ATUALIZACAO", "")
                    or item.get("MÊS", "")
                    or item.get("MES", "")
                ).strip()

                if not assunto and not circular and not info:
                    continue

                if assunto:
                    link_interno = "/modulo/informes?item=" + urllib.parse.quote(assunto)
                else:
                    link_interno = "/modulo/informes"

                base_id = "|".join((assunto, circular, info, data_atualizacao))
                hash_conteudo = hashlib.sha1(base_id.encode("utf-8", "ignore")).hexdigest()[:16]
                chave_informe = hashlib.sha1("|".join((assunto, circular)).encode("utf-8", "ignore")).hexdigest()[:16]

                descricao_partes = []
                if circular:
                    descricao_partes.append(f"Circular: {circular}")
                if info:
                    descricao_partes.append(info[:180])
                descricao = " — ".join(descricao_partes) or "Atualização disponível no menu Informes e Circulares."

                atualizacoes.append({
                    "id": f"informe:{chave_informe}:{hash_conteudo}",
                    "tipo": "Atualização",
                    "titulo": assunto or "Novo informe disponível",
                    "descricao": descricao,
                    "data": data_atualizacao,
                    "link": link_interno,
                })

    except Exception as e:
        print(f"API atualizações: erro ao verificar as abas: {e}")
        if CACHE_ATUALIZACOES_ABAS["timestamp"]:
            final_cache = CACHE_ATUALIZACOES_ABAS["atualizacoes"][:limite]
            return jsonify({"atualizacoes": final_cache, "nao_lidas": len(final_cache)})
        return jsonify({
            "atualizacoes": [],
            "nao_lidas": 0,
            "erro": "Google Sheets indisponível temporariamente para verificar atualizações.",
        }), 503

    # Mais recentes primeiro quando houver uma data reconhecível; mantém a
    # ordem da planilha como critério de desempate.
    def chave_atualizacao(item):
        texto = str(item.get("data", "")).strip()
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(texto, fmt)
            except ValueError:
                pass
        return datetime.min

    atualizacoes.extend(CACHE_ATUALIZACOES_ABAS["eventos"])
    atualizacoes.sort(key=chave_atualizacao, reverse=True)
    CACHE_ATUALIZACOES_ABAS["atualizacoes"] = atualizacoes[:30]
    CACHE_ATUALIZACOES_ABAS["timestamp"] = time.time()
    final = CACHE_ATUALIZACOES_ABAS["atualizacoes"][:limite]
    return jsonify({"atualizacoes": final, "nao_lidas": len(final)})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
