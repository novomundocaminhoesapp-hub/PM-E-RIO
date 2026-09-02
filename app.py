import os
import time
from datetime import datetime
import re
import urllib.parse
import traceback
import json
import unicodedata
from google import genai

from flask import Flask, redirect, render_template_string, request, session, url_for, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

app = Flask(__name__)
app.secret_key = "chave_secreta_pm_rio"

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
    "vendas": "Vendas Fechadas",
    "locacao_vendas": "Locação - Vendas",
    "locacao_negocios": "Locação - Negócios",
    "consorcio_vendas": "Consórcio - Vendas",
    "consorcio_negocios": "Consórcio - Negócios"
}

escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CACHE_IA = {
    "contexto_sistema": "",
    "timestamp": 0
}
TEMPO_CACHE_SEGUNDOS = 300


def criar_cliente_gemini():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    return genai.Client(api_key=api_key)


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

def conectar_google_sheets():
    if 'GOOGLE_CREDENTIALS' in os.environ:
        credenciais_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
        credenciais = Credentials.from_service_account_info(credenciais_dict, scopes=escopos)
    else:
        credenciais = Credentials.from_service_account_file("credenciais.json", scopes=escopos)
    
    cliente = gspread.authorize(credenciais)
    return cliente.open("PM e RIO Novo")

def obter_registros_seguros(aba):
    linhas = aba.get_all_values()
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

def obter_conteudo_pastas_drive():
    try:
        if 'GOOGLE_CREDENTIALS' in os.environ:
            credenciais_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
            credenciais = Credentials.from_service_account_info(credenciais_dict, scopes=escopos)
        else:
            credenciais = Credentials.from_service_account_file("credenciais.json", scopes=escopos)
        
        service = build('drive', 'v3', credentials=credenciais)
        
        lista_arquivos = []
        mapa_links = {}
        page_token = None
        
        while True:
            results = service.files().list(
                pageSize=1000,
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token
            ).execute()
            
            files = results.get('files', [])
            
            for f in files:
                nome = f.get('name')
                link = f.get('webViewLink', '')
                mime = f.get('mimeType', '')
                lista_arquivos.append(f"- Arquivo: {nome} | Tipo: {mime}")
                if nome:
                    mapa_links[nome.strip().lower()] = link
                    
            page_token = results.get('nextPageToken')
            if not page_token:
                break
                
        return "\n".join(lista_arquivos), mapa_links
    except Exception as e:
        return f"Não foi possível listar os arquivos do Drive: {e}", {}

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
        .logo { max-width: 220px; height: auto; }
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

        @media print {
            body * { visibility: hidden; }
            #secaoRelatorioPDF, #secaoRelatorioPDF *, #secaoDashboard, #secaoDashboard * { visibility: visible; }
            #secaoRelatorioPDF, #secaoDashboard { position: absolute; left: 0; top: 0; width: 100%; margin: 0; padding: 15px; background: #fff; }
            .no-print { display: none !important; }
            .chart-container { page-break-inside: avoid; margin-bottom: 20px; height: 250px !important; }
            
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
            window.print();
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

        function carregarVendaParaEdicao(indexLinha, cliente, produto, dataVenda, modelo, quantidade, vendedor) {
            document.getElementById('editVendaIndexInput').value = indexLinha;
            document.getElementById('tituloFormVendaCard').innerText = "✏️ Alterar Venda / Comprovação (Linha " + indexLinha + ")";
            document.getElementById('btnSubmitVendaForm').innerText = "Atualizar Venda";
            document.getElementById('btnCancelarEdicaoVenda').style.display = "inline-block";

            document.querySelector('[name="cliente"]').value = cliente;
            document.querySelector('[name="produto"]').value = produto;
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
            document.querySelector('[name="produto"]').value = "";
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
                    <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo Novo Mundo" class="logo">
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
                    <h2 style="font-size: 18px; color: #002244; margin-bottom: 15px;">Acesso Restrito</h2>
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
            <div class="topbar-right">
                <button onclick="forcarAtualizacao()" title="Atualizar">↻</button>
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
                {% if session.get('perm_argumentos') %}
                <li class="drawer-item {% if modulo_ativo == 'argumentos' %}active{% endif %}" style="border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-bottom: 4px;">
                    <a href="/modulo/argumentos" onclick="closeDrawer()"><span class="drawer-icon">💡</span> Argumentos de Venda</a>
                </li>
                {% endif %}
                {% if session.get('perm_negocios') %}
                <li class="drawer-item {% if modulo_ativo == 'negocios' %}active{% endif %}">
                    <a href="/modulo/negocios" onclick="closeDrawer()"><span class="drawer-icon">🤝</span> Negócios em Andamento</a>
                </li>
                {% endif %}
                {% if session.get('perm_vendas') %}
                <li class="drawer-item {% if modulo_ativo == 'vendas' %}active{% endif %}">
                    <a href="/modulo/vendas" onclick="closeDrawer()"><span class="drawer-icon">💰</span> Vendas Fechadas</a>
                </li>
                {% endif %}
                
                {% if session.get('perm_locacao_vendas') %}
                <li class="drawer-item {% if modulo_ativo == 'locacao_vendas' %}active{% endif %}">
                    <a href="/modulo/locacao_vendas" onclick="closeDrawer()"><span class="drawer-icon">🔑</span> Locação - Vendas</a>
                </li>
                {% endif %}
                {% if session.get('perm_locacao_negocios') %}
                <li class="drawer-item {% if modulo_ativo == 'locacao_negocios' %}active{% endif %}">
                    <a href="/modulo/locacao_negocios" onclick="closeDrawer()"><span class="drawer-icon">📝</span> Locação - Negócios</a>
                </li>
                {% endif %}

                {% if session.get('perm_consorcio_vendas') %}
                <li class="drawer-item {% if modulo_ativo == 'consorcio_vendas' %}active{% endif %}">
                    <a href="/modulo/consorcio_vendas" onclick="closeDrawer()"><span class="drawer-icon">📋</span> Consórcio - Vendas</a>
                </li>
                {% endif %}
                {% if session.get('perm_consorcio_negocios') %}
                <li class="drawer-item {% if modulo_ativo == 'consorcio_negocios' %}active{% endif %}">
                    <a href="/modulo/consorcio_negocios" onclick="closeDrawer()"><span class="drawer-icon">🤝</span> Consórcio - Negócios</a>
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
                    
                    # NOVA FUNÇÃO DE LEITURA INTELIGENTE (Ignora acentos e espaços da planilha)
                    def normalize_key(k):
                        return unicodedata.normalize('NFKD', str(k)).encode('ASCII', 'ignore').decode('utf-8').strip().upper()

                    user_norm_keys = {normalize_key(k): str(v).strip().upper() for k, v in usuario_encontrado.items()}

                    def tem_permissao(chaves_alvo):
                        for chave in chaves_alvo:
                            # Verifica a chave exata e possíveis variações com sufixo numérico inseridas pelo gspread
                            chaves_possiveis = [chave] + [f"{chave}_{i}" for i in range(1, 10)]
                            for c in chaves_possiveis:
                                val = user_norm_keys.get(c, "")
                                if val in ["X", "SIM", "S", "V", "TRUE", "1", "OK"]:
                                    return True
                        return False

                    session["perm_rio"] = tem_permissao(["TELEMETRIA RIO", "RIO"])
                    session["perm_pm"] = tem_permissao(["PLANO DE MANUTENCAO", "PM"])
                    session["perm_valores"] = tem_permissao(["TABELA DE VALORES", "TABELAS DE VALORES", "VALORES"])
                    session["perm_informes"] = tem_permissao(["INFORME E CIRCULARES", "INFORMES"])
                    session["perm_fichatecnica"] = tem_permissao(["FICHA TECNICA", "FICHATECNICA"])
                    session["perm_argumentos"] = tem_permissao(["ARGUMENTOS DE VENDA", "ARGUMENTOS"])
                    session["perm_negocios"] = tem_permissao(["NEGOCIOS EM ANDAMENTO", "NEGOCIOS EM ANDAMENTO_1", "NEGOCIOS"])
                    
                    # Tratamento isolado para Vendas Gerais flexibilizado
                    val_vendas = False
                    for k_norm, v_val in user_norm_keys.items():
                        if "VENDAS" in k_norm and "LOC" not in k_norm and "CON" not in k_norm:
                            if v_val in ["X", "SIM", "S", "V", "TRUE", "1", "OK"]:
                                val_vendas = True
                                break
                    session["perm_vendas"] = val_vendas

                    # Correção específica para Locação e Consórcio
                    session["perm_locacao_vendas"] = tem_permissao(["LOCACAO", "LOCACAO VENDAS"])
                    session["perm_locacao_negocios"] = tem_permissao(["EM ANDAMENTO LOCACAO", "NEGOCIOS EM ANDAMENTO LOCACAO"])
                    
                    session["perm_consorcio_vendas"] = tem_permissao(["CONSORIOCO", "CONSORCIO"])
                    session["perm_consorcio_negocios"] = tem_permissao(["NEGOCIOS EM ANDAMENTO CONSORCIO", "NEGOCIOS CONSORCIO", "NEGOCIOS EM ANDAMENTO CONSORCIO_1"])

                    session.pop("historico_ia", None)
                    
                    registrar_log_acesso(usuario_encontrado.get("NOME"), "Login efetuado via Flask")

                    return redirect(url_for("acessar_modulo", nome_modulo="rio"))
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
                erro = f"Erro de conexão ou processamento: {e}"

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
        "vendas": session.get("perm_vendas", False),
        "locacao_vendas": session.get("perm_locacao_vendas", False),
        "locacao_negocios": session.get("perm_locacao_negocios", False),
        "consorcio_vendas": session.get("perm_consorcio_vendas", False),
        "consorcio_negocios": session.get("perm_consorcio_negocios", False)
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
    _, mapa_drive = obter_conteudo_pastas_drive()
    nome_usuario_logado = session.get('nome', 'Usuário')

    if nome_modulo in ["locacao_vendas", "consorcio_vendas"]:
        nome_aba_planilha = "Vendas_LOC" if nome_modulo == "locacao_vendas" else "Vendas_Consorcio"
        try:
            planilha = conectar_google_sheets()
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
                            clientes_ja_em_vendas.add(cli_n.lower())
            except Exception:
                pass

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_vendas.delete_rows(index_linha)
                        sucesso_msg = "Registro excluído com sucesso!"
                elif acao_form == "cadastrar":
                    index_edicao = request.form.get("index_edicao", "").strip()
                    cliente_v = request.form.get("cliente", "").strip()
                    produto_v = request.form.get("produto", "").strip()
                    data_v = request.form.get("data_venda", "").strip()
                    modelo_v = request.form.get("modelo", "").strip()
                    qtd_v = request.form.get("quantidade", "").strip()
                    vendedor_v = request.form.get("vendedor", "").strip()
                    
                    anexos = ["", "", ""]
                    if index_edicao:
                        try:
                            linha_atual = aba_vendas.row_values(int(index_edicao))
                            if len(linha_atual) >= 7: anexos[0] = linha_atual[6]
                            if len(linha_atual) >= 8: anexos[1] = linha_atual[7]
                            if len(linha_atual) >= 9: anexos[2] = linha_atual[8]
                        except Exception:
                            pass

                    for idx_file in range(3):
                        file_key = f"anexo_{idx_file+1}"
                        if file_key in request.files:
                            file_obj = request.files[file_key]
                            if file_obj and file_obj.filename:
                                filename_seguro = f"{int(time.time())}_{file_obj.filename}"
                                upload_folder = os.path.join("static", "uploads")
                                os.makedirs(upload_folder, exist_ok=True)
                                caminho_completo = os.path.join(upload_folder, filename_seguro)
                                file_obj.save(caminho_completo)
                                anexos[idx_file] = f"/static/uploads/{filename_seguro}"

                    if cliente_v:
                        dados_venda_linha = [cliente_v, produto_v, data_v, modelo_v, qtd_v, vendedor_v, anexos[0], anexos[1], anexos[2]]
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_vendas.update(f"A{idx_int}:I{idx_int}", [dados_venda_linha])
                            sucesso_msg = "Registro atualizado com sucesso!"
                        else:
                            aba_vendas.append_row(dados_venda_linha)
                            sucesso_msg = "Registro salvo com sucesso!"
                    else:
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
                dt_v = reg.get('DATA DA VENDA', '')
                mod = reg.get('MODELO', '')
                qtd_str = reg.get('QUANTIDADE', '1')
                vend = reg.get('VENDEDOR', 'Desconhecido')
                estado_v = mapa_vendedor_estado.get(vend.strip().lower(), "PE")

                anexos_html = ""
                for anexo_idx in range(1, 4):
                    link_anexo = reg.get(f'ANEXO {anexo_idx}', '')
                    if link_anexo:
                        anexos_html += f'''
                        <div onclick="abrirImagemModal('{link_anexo}')" title="Clique para ampliar" style="display: inline-block; margin-right: 12px; cursor: pointer; background: #fff; padding: 4px; border: 1px solid #cbd5e0; border-radius: 4px;">
                            <img src="{link_anexo}" alt="Anexo {anexo_idx}" class="img-comprovacao">
                        </div>
                        '''

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
                    <td colspan="8" style="padding: 8px 10px 12px 10px; border-bottom: 1px solid #edf2f7;">
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
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar o módulo:</b> {e}</div>'

    elif nome_modulo in ["locacao_negocios", "consorcio_negocios"]:
        nome_aba_planilha = "Negocio_LOC" if nome_modulo == "locacao_negocios" else "Negocios_Consorcio"
        nome_aba_vendas_sync = "Vendas_LOC" if nome_modulo == "locacao_negocios" else "Vendas_Consorcio"

        try:
            planilha = conectar_google_sheets()
            try:
                aba_negocios = planilha.worksheet(nome_aba_planilha)
            except gspread.exceptions.WorksheetNotFound:
                aba_negocios = planilha.add_worksheet(title=nome_aba_planilha, rows=1000, cols=9)
                aba_negocios.append_row(["TEMPERATURA", "DATA", "VENDEDOR", "CLIENTE", "MODELO", "PLANO DE MANUTENÇÃO", "RIO", "CONTATO DO CLIENTE", "COMENTÁRIOS"])

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()

                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_negocios.delete_rows(index_linha)
                        sucesso_msg = "Registro excluído com sucesso!"
                else:
                    index_edicao = request.form.get("index_edicao", "").strip()
                    temperatura = request.form.get("temperatura", "").strip()
                    data_neg = request.form.get("data", "").strip()
                    vendedor_form = request.form.get("vendedor", "").strip()
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
                            sucesso_msg = "Negócio atualizado com sucesso!"
                        else:
                            aba_negocios.append_row(dados_linha)
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

            aba_modelos = planilha.worksheet("Modelos")
            registros_modelos = obter_registros_seguros(aba_modelos)
            lista_modelos = []
            for m in registros_modelos:
                m_nome = str(m.get("MODELO", "")).strip()
                if m_nome and m_nome not in lista_modelos:
                    lista_modelos.append(m_nome)
            if not lista_modelos:
                lista_modelos = ["Delivery 11.180", "Constellation 24.280"]

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
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar o módulo:</b> {e}</div>'

    elif nome_modulo == "vendas":
        try:
            planilha = conectar_google_sheets()
            try:
                aba_vendas = planilha.worksheet("Vendas_PM")
            except gspread.exceptions.WorksheetNotFound:
                aba_vendas = planilha.add_worksheet(title="Vendas_PM", rows=1000, cols=9)
                aba_vendas.append_row(["CLIENTE", "PRODUTO", "DATA DA VENDA", "MODELO", "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "ANEXO 3"])

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
            try:
                aba_mod_pesquisa = planilha.worksheet("Modelos")
                regs_mod = obter_registros_seguros(aba_mod_pesquisa)
                for rm in regs_mod:
                    m_nome = str(rm.get("MODELO", "")).strip().lower()
                    m_cat = str(rm.get("CATEGORIA", "")).strip().lower()
                    m_tipo = str(rm.get("TIPO", "")).strip().lower()
                    texto_completo_mod = f"{m_nome} {m_cat} {m_tipo}"
                    if m_nome:
                        mapa_modelo_familia[m_nome] = texto_completo_mod
            except Exception as e_fam:
                print(f"Aviso mapeamento de modelos: {e_fam}")

            try:
                aba_neg_sync = planilha.worksheet("Negocios_PM")
                regs_neg = obter_registros_seguros(aba_neg_sync)
                regs_vendas_atuais = obter_registros_seguros(aba_vendas)
                
                clientes_ja_em_vendas = set(str(r.get("CLIENTE", "")).strip().lower() for r in regs_vendas_atuais)

                for rn in regs_neg:
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
                            clientes_ja_em_vendas.add(cli_n.lower())
            except Exception as e_sync_retroativa:
                print(f"Aviso sync retroativa: {e_sync_retroativa}")

            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()
                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_vendas.delete_rows(index_linha)
                        sucesso_msg = "Registro de venda excluído com sucesso!"
                elif acao_form == "cadastrar":
                    index_edicao = request.form.get("index_edicao", "").strip()
                    cliente_v = request.form.get("cliente", "").strip()
                    produto_v = request.form.get("produto", "").strip()
                    data_v = request.form.get("data_venda", "").strip()
                    modelo_v = request.form.get("modelo", "").strip()
                    qtd_v = request.form.get("quantidade", "").strip()
                    vendedor_v = request.form.get("vendedor", "").strip()
                    
                    anexos = ["", "", ""]
                    if index_edicao:
                        try:
                            linha_atual = aba_vendas.row_values(int(index_edicao))
                            if len(linha_atual) >= 7: anexos[0] = linha_atual[6]
                            if len(linha_atual) >= 8: anexos[1] = linha_atual[7]
                            if len(linha_atual) >= 9: anexos[2] = linha_atual[8]
                        except Exception:
                            pass

                    for idx_file in range(3):
                        file_key = f"anexo_{idx_file+1}"
                        if file_key in request.files:
                            file_obj = request.files[file_key]
                            if file_obj and file_obj.filename:
                                filename_seguro = f"{int(time.time())}_{file_obj.filename}"
                                upload_folder = os.path.join("static", "uploads")
                                os.makedirs(upload_folder, exist_ok=True)
                                caminho_completo = os.path.join(upload_folder, filename_seguro)
                                file_obj.save(caminho_completo)
                                anexos[idx_file] = f"/static/uploads/{filename_seguro}"

                    if cliente_v:
                        dados_venda_linha = [cliente_v, produto_v, data_v, modelo_v, qtd_v, vendedor_v, anexos[0], anexos[1], anexos[2]]
                        if index_edicao:
                            idx_int = int(index_edicao)
                            aba_vendas.update(f"A{idx_int}:I{idx_int}", [dados_venda_linha])
                            sucesso_msg = "Venda atualizada com sucesso!"
                        else:
                            aba_vendas.append_row(dados_venda_linha)
                            sucesso_msg = "Venda registrada com sucesso!"
                    else:
                        erro_msg = "Informe o cliente para registrar a venda."

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

            titulo_relatorio_txt = f"RELATÓRIO DE VENDAS E COMISSÕES"
            if mes_selecionado in meses_nomes and mes_selecionado != "todos" and mes_selecionado != "anual":
                titulo_relatorio_txt += f" ({meses_nomes[mes_selecionado]})"

            registros_vendas_filtrados = []
            if len(linhas_vendas_brutas) > 1:
                cab_v = [c.upper().strip() for c in linhas_vendas_brutas[0]]
                for idx_l, linha_v in enumerate(linhas_vendas_brutas[1:], start=2):
                    dict_v = {"_index_planilha": idx_l}
                    for i, val in enumerate(linha_v):
                        if i < len(cab_v) and cab_v[i]:
                            dict_v[cab_v[i]] = val

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
                        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m/%G", "%d/%m/%g"):
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
                prod = reg.get('PRODUTO', '')
                dt_v = reg.get('DATA DA VENDA', '')
                mod = reg.get('MODELO', '')
                qtd_str = reg.get('QUANTIDADE', '1')
                vend = reg.get('VENDEDOR', 'Desconhecido')
                mes_str_grafico = reg["_dt_obj"].strftime("%m/%Y") if reg["_dt_obj"] != datetime.max else "Sem Data"
                
                try:
                    qtd_num = int(re.sub(r'\D', '', str(qtd_str)))
                    if qtd_num <= 0: qtd_num = 1
                except ValueError:
                    qtd_num = 1

                prod_upper = prod.upper()
                comissao_pm_item = 0
                comissao_rio_item = 0
                comissao_apm_item = 0

                is_pm = any(p_termo in prod_upper for p_termo in ["PREV", "MAX", "PLUS", "PLANO"])
                is_rio = any(r_termo in prod_upper for r_termo in ["RIO", "DIAGNÓSTICO", "DIAGNOSTICO"])

                mod_lower = mod.strip().lower()
                info_modelo_texto = mapa_modelo_familia.get(mod_lower, "")
                if not info_modelo_texto:
                    for k_mod, v_mod in mapa_modelo_familia.items():
                        if k_mod in mod_lower or mod_lower in k_mod:
                            info_modelo_texto = v_mod
                            break
                
                texto_analise_modelo = f"{mod_lower} {info_modelo_texto}"

                if "delivery" in texto_analise_modelo:
                    valor_unitario_pm_vendedor = 200
                elif "constellation" in texto_analise_modelo:
                    valor_unitario_pm_vendedor = 300
                elif "meteor" in texto_analise_modelo or "cavalo" in texto_analise_modelo or "420" in texto_analise_modelo or "530" in texto_analise_modelo or "460" in texto_analise_modelo:
                    valor_unitario_pm_vendedor = 500
                else:
                    valor_unitario_pm_vendedor = 300

                estado_v = mapa_vendedor_estado.get(vend.strip().lower(), "PE")
                if estado_v not in comissoes_por_estado:
                    estado_v = "PE"

                if vend not in comissoes_por_estado[estado_v]["vendedores"]:
                    comissoes_por_estado[estado_v]["vendedores"][vend] = {"pm_qtd": 0, "pm_total": 0, "rio_qtd": 0, "rio_total": 0}

                if is_pm:
                    comissao_pm_item = valor_unitario_pm_vendedor * qtd_num
                    comissao_apm_item += 250.0 * qtd_num
                    comissoes_por_estado[estado_v]["vendedores"][vend]["pm_qtd"] += qtd_num
                    comissoes_por_estado[estado_v]["vendedores"][vend]["pm_total"] += comissao_pm_item
                    comissoes_por_estado[estado_v]["total_pm"] += comissao_pm_item
                    total_qtd_pm_geral += qtd_num
                    dados_dashboard["produtos"]["PM"] += qtd_num
                if is_rio:
                    comissao_rio_item = 200 * qtd_num
                    comissao_apm_item += 150.0 * qtd_num
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

                anexos_html = ""
                for anexo_idx in range(1, 4):
                    link_anexo = reg.get(f'ANEXO {anexo_idx}', '')
                    if link_anexo:
                        anexos_html += f'''
                        <div onclick="abrirImagemModal('{link_anexo}')" title="Clique para ampliar" style="display: inline-block; margin-right: 12px; cursor: pointer; background: #fff; padding: 4px; border: 1px solid #cbd5e0; border-radius: 4px;">
                            <img src="{link_anexo}" alt="Anexo {anexo_idx}" class="img-comprovacao">
                        </div>
                        '''

                botoes_v = f"""
                <div style="display: flex; gap: 4px;">
                    <button type="button" class="btn-acao btn-editar no-print" onclick="carregarVendaParaEdicao({idx_l}, '{cli}', '{prod}', '{dt_v}', '{mod}', '{qtd_str}', '{vend}')">Alterar</button>
                    <button type="button" class="btn-acao btn-excluir no-print" onclick="excluirVenda({idx_l}, 'vendas')">Excluir</button>
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
                    <td colspan="8" style="padding: 8px 10px 12px 10px; border-bottom: 1px solid #edf2f7;">
                        <span style="font-size: 11px; font-weight: 700; color: #4a5568; text-transform: uppercase; display: block; margin-bottom: 4px;">Comprovações / Anexos:</span>
                        {anexos_html if anexos_html else '<span style="color: #a0aec0; font-size: 12px;">Nenhum anexo enviado.</span>'}
                    </td>
                </tr>
                """

            if contador_vendas == 0:
                tabela_vendas_linhas = '<tr><td colspan="8" style="padding: 20px; text-align: center; color: #718096;">Nenhuma venda encontrada para este filtro.</td></tr>'

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
                                <span>Planos de Manutenção ({d_v['pm_qtd']} un.):</span>
                                <b>R$ {formata_br(d_v['pm_total'])}</b>
                            </div>
                            <div style="font-size: 12px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 4px;">
                                <span>Telemetria RIO ({d_v['rio_qtd']} un. × R$ 200,00):</span>
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

            comissao_minha_pm = total_qtd_pm_geral * 250.0
            comissao_minha_rio = total_qtd_rio_geral * 150.0
            comissao_minha_total = comissao_minha_pm + comissao_minha_rio

            bloco_minha_comissao = f"""
            <div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 16px; margin-top: 20px; border-left: 5px solid #2f855a;">
                <h3 style="font-size: 15px; color: #002244; margin-bottom: 12px; border-bottom: 2px solid #cbd5e0; padding-bottom: 6px;">🎯 Resumo da Sua Comissão (APM - Apoio ao Plano de Manutenção)</h3>
                <div style="font-weight: 700; color: #002244; font-size: 13px; margin-bottom: 8px;">Logado como: {nome_usuario_logado} (Vendedor APM)</div>
                <div style="font-size: 13px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 4px;">
                    <span>Planos de Manutenção ({total_qtd_pm_geral} un. × R$ 250,00):</span>
                    <b>R$ {formata_br(comissao_minha_pm)}</b>
                </div>
                <div style="font-size: 13px; color: #4a5568; display: flex; justify-content: space-between; margin-bottom: 8px;">
                    <span>Telemetria RIO ({total_qtd_rio_geral} un. × R$ 150,00):</span>
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

                <div class="produto-detalhe-card" style="margin-bottom: 20px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <h3 id="tituloFormVendaCard" style="font-size: 15px; color: #002244; margin: 0;">Registrar Nova Venda / Comprovação</h3>
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
                                <label>Produto (Plano / RIO)</label>
                                <input type="text" name="produto" placeholder="Ex: PREV / RIO" required>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Data da Venda</label>
                                <input type="text" name="data_venda" value="{datetime.now().strftime('%d/%m/%Y')}" required>
                            </div>
                            <div>
                                <label>Modelo do Veículo</label>
                                <input type="text" name="modelo" placeholder="Ex: Delivery 11.180 / Meteor">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Quantidade</label>
                                <input type="text" name="quantidade" placeholder="Ex: 2 veículos" required>
                            </div>
                            <div>
                                <label>Vendedor</label>
                                <input type="text" name="vendedor" value="{nome_usuario_logado}" readonly style="background-color: #edf2f7;">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Anexo 1 (Câmera / Galeria)</label>
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
                            <button type="submit" id="btnSubmitVendaForm" class="btn-login" style="flex: 2; margin: 0;">Salvar Venda</button>
                            <select id="filtroMesSelect" onchange="window.location.href='/modulo/vendas?mes=' + this.value" style="flex: 1; padding: 12px; font-size: 14px; border-radius: 6px; border: 1px solid #cbd5e0; background: #fff; font-weight: 600; height: 46px; margin: 0;">
                                {options_meses}
                            </select>
                            <button type="button" onclick="gerarPDFRelatorio()" class="btn-acao btn-pdf" style="flex: 1.5; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📄 Gerar PDF (Comissões)</button>
                            <button type="button" onclick="alternarVisaoDashboard()" id="btnAlternarVisao" class="btn-acao btn-graficos" style="flex: 1.5; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📊 Ver Gráficos</button>
                        </div>
                    </form>
                </div>

                <div id="secaoRelatorioPDF" class="produto-detalhe-card">
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
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 0, 'text')">Cliente ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 1, 'text')">Produto ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 2, 'data')">Data da Venda ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 3, 'text')">Modelo ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 4, 'num')">Quantidade ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaVendas', 5, 'text')">Vendedor / Região ↕</th>
                                    <th style="padding: 10px;" class="no-print">Comprovação</th>
                                    <th style="padding: 10px;" class="no-print">Ações</th>
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
                        <h3 style="font-size: 16px; color: #002244; margin: 0;">📊 Dashboard Inteligente ({meses_nomes.get(mes_selecionado, mes_selecionado)})</h3>
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

                    var vendedoresLabels = Object.keys(dadosPainel.vendedores);
                    vendedoresLabels.sort((a, b) => dadosPainel.vendedores[b].comissao - dadosPainel.vendedores[a].comissao);
                    var vendComissao = vendedoresLabels.map(v => dadosPainel.vendedores[v].comissao);
                    var vendQtd = vendedoresLabels.map(v => dadosPainel.vendedores[v].qtd);

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
                            plugins: {{ title: {{ display: true, text: 'Ranking de Vendedores', font: {{ size: 14 }} }} }}
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
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar Vendas:</b> {e}</div>'

    elif nome_modulo == "negocios":
        try:
            planilha = conectar_google_sheets()
            
            try:
                aba_negocios = planilha.worksheet("Negocios_PM")
            except gspread.exceptions.WorksheetNotFound:
                aba_negocios = planilha.add_worksheet(title="Negocios_PM", rows=1000, cols=9)
                aba_negocios.append_row(["TEMPERATURA", "DATA", "VENDEDOR", "CLIENTE", "MODELO", "PLANO DE MANUTENÇÃO", "RIO", "CONTATO DO CLIENTE", "COMENTÁRIOS"])
            
            sucesso_msg = None
            erro_msg = None

            if request.method == "POST" and "acao_form" in request.form:
                acao_form = request.form.get("acao_form", "").strip()

                if acao_form == "excluir":
                    index_linha = int(request.form.get("index_linha", 0))
                    if index_linha > 1:
                        aba_negocios.delete_rows(index_linha)
                        sucesso_msg = "Registro excluído com sucesso!"
                else:
                    index_edicao = request.form.get("index_edicao", "").strip()
                    temperatura = request.form.get("temperatura", "").strip()
                    data_neg = request.form.get("data", "").strip()
                    vendedor_form = request.form.get("vendedor", "").strip()
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
                            sucesso_msg = "Negócio atualizado com sucesso!"
                        else:
                            aba_negocios.append_row(dados_linha)
                            sucesso_msg = "Negócio cadastrado com sucesso!"

                        if temperatura.strip().lower() == "fechado":
                            try:
                                try:
                                    aba_vendas_sync = planilha.worksheet("Vendas_PM")
                                except gspread.exceptions.WorksheetNotFound:
                                    aba_vendas_sync = planilha.add_worksheet(title="Vendas_PM", rows=1000, cols=9)
                                    aba_vendas_sync.append_row(["CLIENTE", "PRODUTO", "DATA DA VENDA", "MODELO", "QUANTIDADE", "VENDEDOR", "ANEXO 1", "ANEXO 2", "ANEXO 3"])
                                
                                produto_combinado = f"{plano_manutencao} / {rio_val}".strip(" /")
                                aba_vendas_sync.append_row([cliente, produto_combinado, data_neg, modelo, "1", vendedor_form, "", "", ""])
                                sucesso_msg += " Negócio fechado sincronizado automaticamente para Vendas!"
                            except Exception as sync_err:
                                print(f"Erro ao sincronizar venda: {sync_err}")
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

            aba_modelos = planilha.worksheet("Modelos")
            registros_modelos = obter_registros_seguros(aba_modelos)
            lista_modelos = []
            for m in registros_modelos:
                m_nome = str(m.get("MODELO", "")).strip()
                if m_nome and m_nome not in lista_modelos:
                    lista_modelos.append(m_nome)
            if not lista_modelos:
                lista_modelos = ["Delivery 11.180", "Constellation 24.280"]

            lista_planos_manutencao = ["PREV", "MAX", "PLUS"]

            try:
                aba_rio_dados = planilha.worksheet("RIO")
                registros_rio = obter_registros_seguros(aba_rio_dados)
                lista_tipos_rio = []
                for item in registros_rio:
                    p_nome = str(item.get("PRODUTO", "")).strip()
                    if p_nome and p_nome not in lista_tipos_rio:
                        lista_tipos_rio.append(p_nome)
                if not lista_tipos_rio:
                    lista_tipos_rio = ["RIO", "Diagnóstico Remoto", "RIO Geo"]
            except Exception:
                lista_tipos_rio = ["RIO", "Diagnóstico Remoto", "RIO Geo"]

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

            titulo_relatorio_txt = f"RELATÓRIO DE NEGÓCIOS EM ANDAMENTO"
            if mes_selecionado != "todos" and mes_selecionado in meses_nomes:
                titulo_relatorio_txt += f" ({meses_nomes[mes_selecionado]})"

            registros_filtrados_ordenados = []

            if len(linhas_brutas) > 1:
                cabecalhos = [c.upper().strip() for c in linhas_brutas[0]]
                for idx_linha, linha in enumerate(linhas_brutas[1:], start=2):
                    item_dict = {"_index_planilha": idx_linha}
                    for i, val in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i]:
                            item_dict[cabecalhos[i]] = val

                    temp_val = item_dict.get('TEMPERATURA','')
                    data_val = item_dict.get('DATA','').strip()

                    match_mes = re.search(r'^\d{1,2}/(\d{1,2})/(?:\d{2}|\d{4})', data_val)
                    mes_item = match_mes.group(1).zfill(2) if match_mes else ""

                    if temp_val.strip().lower() != "fechado":
                        if mes_selecionado == "todos" or mes_item == mes_selecionado:
                            dt_obj = datetime.max
                            for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m/%G", "%d/%m/%g"):
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
                    <button type="button" class="btn-acao btn-excluir no-print" onclick="excluirNegocio({idx_linha}, 'negocios')">Excluir</button>
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
                tabela_linhas = '<tr><td colspan="10" style="padding: 20px; text-align: center; color: #718096;">Nenhum negócio encontrado para este filtro.</td></tr>'

            conteudo = f"""
            <div>
                <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 14px; font-size: 17px;">Negócios em Andamento (Gerência / Consultores)</h2>
                
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
                                <label>Plano de Manutenção</label>
                                <select name="plano_manutencao">
                                    <option value="">Nenhum</option>
                                    {options_planos_manutencao}
                                </select>
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 10px;">
                            <div>
                                <label>Telemetria RIO</label>
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
                            <select id="filtroMesSelect" onchange="window.location.href='/modulo/negocios?mes=' + this.value" style="flex: 1; padding: 12px; font-size: 14px; border-radius: 6px; border: 1px solid #cbd5e0; background: #fff; font-weight: 600; height: 46px; margin: 0;">
                                {options_meses}
                            </select>
                            <button type="button" onclick="gerarPDFRelatorio()" class="btn-acao btn-pdf" style="flex: 1.5; height: 46px; font-size: 13px; font-weight: 600; margin: 0;">📄 Gerar PDF</button>
                        </div>
                    </form>
                </div>

                <div id="secaoRelatorioPDF" class="produto-detalhe-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #002244; padding-bottom: 10px; margin-bottom: 14px;">
                        <div>
                            <h3 style="font-size: 16px; color: #002244; margin: 0 0 4px 0;">{titulo_relatorio_txt}</h3>
                            <p style="font-size: 12px; color: #4a5568; margin: 0;">Emitido por: <b>{nome_usuario_logado}</b> em {datetime.now().strftime('%d/%m/%Y às %H:%M')}</p>
                        </div>
                        <div>
                            <img src="{url_for('static', filename='logo.png')}" alt="Novo Mundo" style="max-height: 40px; width: auto;">
                        </div>
                    </div>

                    <p class="no-print" style="font-size: 11px; color: #718096; margin-bottom: 12px;">*Nota: Exclui automaticamente os negócios com temperatura "Fechado", respeita o mês filtrado e ordena por data.</p>

                    <div style="overflow-x: auto;">
                        <table id="tabelaNegocios" style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;" data-sort-dir="asc">
                            <thead>
                                <tr style="background: #002244; color: #ffffff; border-bottom: 2px solid #001529;">
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 0, 'text')">Temp. ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 1, 'data')">Data ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 2, 'text')">Vendedor ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 3, 'text')">Cliente ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 4, 'text')">Modelo ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 5, 'text')">Plano M. ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 6, 'text')">RIO ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 7, 'text')">Contato ↕</th>
                                    <th style="padding: 10px; cursor: pointer;" onclick="ordenarTabela('tabelaNegocios', 8, 'text')">Comentários ↕</th>
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
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar Negócios:</b> {e}</div>'

    elif nome_modulo == "rio":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            aba_rio = planilha.worksheet("RIO")
            produtos_rio = obter_registros_seguros(aba_rio)

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
            aba_pm = planilha.worksheet("PM")
            produtos_pm = obter_registros_seguros(aba_pm)

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
            aba_precos = planilha.worksheet("PM_Precos")
            dados_precos = obter_registros_seguros(aba_precos)

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
                                <div class="plano-linha-tripla">
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
                                </div>
                            </div>
                            """

                    hora_geral_val = item_escolhido.get("HORA", "")
                    planos_hora_info = [
                        {"nome": "Plano PREV", "classe": "prev", "hora_col": "PREV_VALOR HORA" if "PREV_VALOR HORA" in item_escolhido else "HORA"},
                        {"nome": "Plano MAX", "classe": "max", "hora_col": "MAX_VALOR HORA" if "MAX_VALOR HORA" in item_escolhido else "HORA_1"},
                        {"nome": "Plano PLUS", "classe": "plus", "hora_col": "PLUS_VALOR HORA" if "PLUS_VALOR HORA" in item_escolhido else "HORA_2"}
                    ]

                    cards_horas_html = ""
                    if hora_geral_val:
                        for p in planos_hora_info:
                            hora_val_crua = item_escolhido.get(p["hora_col"], "")
                            mensal_val_crua = ""
                            total_val_crua = ""

                            if p["nome"] == "Plano PREV":
                                chaves_mensal = [k for k in item_escolhido.keys() if k.startswith("VALOR MENSAL")]
                                if len(chaves_mensal) > 3: mensal_val_crua = item_escolhido.get(chaves_mensal[3], "")
                                chaves_total = [k for k in item_escolhido.keys() if k.startswith("TOTAL CONTRATO")]
                                if len(chaves_total) > 3: total_val_crua = item_escolhido.get(chaves_total[3], "")
                            elif p["nome"] == "Plano MAX":
                                chaves_mensal = [k for k in item_escolhido.keys() if k.startswith("VALOR MENSAL")]
                                if len(chaves_mensal) > 4: mensal_val_crua = item_escolhido.get(chaves_mensal[4], "")
                                chaves_total = [k for k in item_escolhido.keys() if k.startswith("TOTAL CONTRATO")]
                                if len(chaves_total) > 4: total_val_crua = item_escolhido.get(chaves_total[4], "")
                            elif p["nome"] == "Plano PLUS":
                                chaves_mensal = [k for k in item_escolhido.keys() if k.startswith("VALOR MENSAL")]
                                if len(chaves_mensal) > 5: mensal_val_crua = item_escolhido.get(chaves_mensal[5], "")
                                chaves_total = [k for k in item_escolhido.keys() if k.startswith("TOTAL CONTRATO")]
                                if len(chaves_total) > 5: total_val_crua = item_escolhido.get(chaves_total[5], "")

                            hora_val = formatar_moeda(hora_val_crua, manter_todos_decimais=True)
                            mensal_val = formatar_moeda(mensal_val_crua, manter_todos_decimais=False)
                            total_val = formatar_moeda(total_val_crua, manter_todos_decimais=False)

                            if hora_val != "-" or mensal_val != "-" or total_val != "-":
                                cards_horas_html += f"""
                                <div class="card-plano {p['classe']}">
                                    <div class="plano-titulo">{p['nome']} (HORAS)</div>
                                    <div class="plano-linha-tripla">
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

                            { '<div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; margin-top: 18px; margin-bottom: 14px;"><div style="display: flex; gap: 10px;"><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Horas (H)</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + str(hora_geral_val) + '</div></div><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Período do Contrato</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + str(periodo_val) + ' Meses</div></div></div></div>' if hora_geral_val else '' }

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
            aba_informes = planilha.worksheet("Informes")
            dados_informes = obter_registros_seguros(aba_informes)

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
            aba_argumentos = planilha.worksheet("Argumentos")
            dados_argumentos = obter_registros_seguros(aba_argumentos)

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
            aba_modelos = planilha.worksheet("Modelos")
            
            dados_modelos = obter_registros_seguros(aba_modelos)

            tipos_disponiveis = sorted(list(set(str(item.get("TIPO", "")).strip() for item in dados_modelos if str(item.get("TIPO", "")).strip())))

            if not tipo_selecionado:
                botoes_tipos = "".join([f'<a href="/modulo/fichatecnica?tipo={urllib.parse.quote(t)}" class="submenu-btn">{t}</a>' for t in tipos_disponiveis])
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
                                f"📄 *Ficha Técnica (PDF):* {link_pdf}"
                            )
                            link_wpp_ft = f"https://api.whatsapp.com/send?text={urllib.parse.quote(str(texto_wpp_ft))}"

                            bloco_pdf_html = f"""
                            <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 12px; margin-top: 14px;">
                                <div class="detalhe-label" style="color: #002244; margin-bottom: 6px;">Documento / Ficha Técnica (PDF)</div>
                                <div class="acoes-ficha-tecnica">
                                    <a href="{link_pdf}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-abrir-pdf">📂 ABRIR PDF</a>
                                    <a href="{link_wpp_ft}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-wpp-pdf">📤 ENVIAR VIA WHATSAPP</a>
                                </div>
                            </div>
                            """

                        conteudo = f"""
                        <div>
                            {nav_superior_html}
                            <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Ficha Técnica do Modelo</h2>
                            
                            <div class="produto-detalhe-card">
                                <div class="detalhe-linha">
                                    <div class="detalhe-label">Modelo</div>
                                    <div class="detalhe-valor detalhe-produto-nome" style="font-size: 19px; color: #002244;">{m_modelo}</div>
                                </div>

                                <div style="display: flex; gap: 10px; margin-bottom: 12px;">
                                    <div style="flex: 1; background: #f7fafc; padding: 8px 10px; border-radius: 6px; border: 1px solid #edf2f7;">
                                        <div class="detalhe-label">Tipo</div>
                                        <div class="detalhe-valor" style="font-weight: 600;">{m_tipo}</div>
                                    </div>
                                    <div style="flex: 1; background: #f7fafc; padding: 8px 10px; border-radius: 6px; border: 1px solid #edf2f7;">
                                        <div class="detalhe-label">Categoria</div>
                                        <div class="detalhe-valor" style="font-weight: 600;">{m_categoria}</div>
                                    </div>
                                </div>
                                
                                <div class="detalhe-linha">
                                    <div class="detalhe-label">📝 Descrição</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{m_descricao}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">⚡ Eficiência</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{m_eficiencia}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">🛋️ Conforto</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{m_conforto}</div>
                                </div>

                                <div class="detalhe-linha">
                                    <div class="detalhe-label">🛡️ Segurança Ativa</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{m_seguranca_ativa}</div>
                                </div>

                                <div class="detalhe-linha" style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">
                                    <div class="detalhe-label">💻 Tecnologia</div>
                                    <div class="detalhe-valor" style="white-space: pre-line; line-height: 1.5;">{m_tecnologia}</div>
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
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba Modelos:</b> {e}</div>'

    else:
        conteudo = f"""
        <div>
            <h2 style="color: #002244; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">{modulo_titulo}</h2>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px;">
                <p style="color: #4a5568; font-size: 14px; line-height: 1.6;">Conteúdo em desenvolvimento para este módulo.</p>
            </div>
        </div>
        """

    return render_template_string(
        TEMPLATE_HTML, 
        conteudo_modulo=conteudo, 
        modulo_ativo=nome_modulo,
        modulo_titulo=modulo_titulo
    )

@app.route("/api/limpar-cache", methods=["POST"])
def limpar_cache():
    global CACHE_IA
    CACHE_IA["contexto_sistema"] = ""
    CACHE_IA["timestamp"] = 0
    return jsonify({"mensagem": "Base de dados e cache da IA atualizados com sucesso!"})

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
        
        if not CACHE_IA["contexto_sistema"] or (agora - CACHE_IA["timestamp"] > TEMPO_CACHE_SEGUNDOS):
            print("🔄 IA: Atualizando cache de dados (Planilha e Listagem do Drive)...")
            planilha = conectar_google_sheets()
            contexto_abas = []
            
            try:
                todas_as_abas = planilha.worksheets()
                for aba in todas_as_abas:
                    nome_aba = aba.title
                    try:
                        registros = obter_registros_seguros(aba)
                        linhas_texto = [f"- " + " | ".join([f"{k}: {v}" for k, v in reg.items() if str(v).strip()]) for reg in registros]
                        contexto_abas.append(f"### ABA DA PLANILHA: {nome_aba}\n" + "\n".join(linhas_texto))
                    except Exception:
                        try:
                            valores = aba.get_all_values()
                            linhas_texto = [f"- " + " | ".join([str(c) for c in linha if str(c).strip()]) for linha in valores]
                            contexto_abas.append(f"### ABA DA PLANILHA (Valores): {nome_aba}\n" + "\n".join(linhas_texto))
                        except Exception:
                            pass
            except Exception as e:
                print(f"Erro ao varrer abas da planilha: {e}")
            
            dados_planilha = "\n\n".join(contexto_abas)
            dados_drive, _ = obter_conteudo_pastas_drive()

            instrucao_sistema = (
                "Você é o Assistente Novo Mundo Caminhões e Ônibus inteligente, articulado e prestativo. "
                "Responda sempre de forma clara, amigável e fundamentada EXCLUSIVAMENTE nos dados da planilha e do Drive fornecidos. "
                "Se não souber a resposta, seja honesto e diga que não encontrou essa informação."
            )
            
            CACHE_IA["contexto_sistema"] = f"Instruções:\n{instrucao_sistema}\n\nDados da Planilha:\n{dados_planilha}\n\nArquivos no Drive:\n{dados_drive}"
            CACHE_IA["timestamp"] = agora

        # Chamada real da API do Gemini para processar o chat
        cliente_ia = criar_cliente_gemini()
        
        prompt_completo = f"{CACHE_IA['contexto_sistema']}\n\nPergunta do Usuário: {pergunta_usuario}\nResposta:"
        
        resposta_ia = cliente_ia.models.generate_content(
            model="gemini-3.5-Flash-Lite",
            contents=prompt_completo
        )
        
        return jsonify({"resposta": resposta_ia.text})

    except Exception as e:
        print(f"Erro na IA: {e}")
        traceback.print_exc()
        return jsonify({"resposta": "Desculpe, ocorreu um erro interno de conexão. Tente novamente mais tarde."})

@app.route("/logout", methods=["GET", "POST"])
def logout():
    # Limpa todos os dados da sessão do usuário atual
    session.clear()
    # Redireciona de volta para a tela de login
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
