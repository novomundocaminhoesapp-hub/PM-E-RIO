import json
import os
import time
from datetime import datetime
import re
import urllib.parse
import traceback
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
    "argumentos": "Argumentos de Venda"
}

# Escopos usados apenas pelo Google Sheets e Drive.
escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# CACHE GLOBAL DA IA PARA EVITAR TIMEOUT (5 MINUTOS)
CACHE_IA = {
    "contexto_sistema": "",
    "timestamp": 0
}
TEMPO_CACHE_SEGUNDOS = 300


def criar_cliente_gemini():
    """Cria o cliente do Gemini usando a GEMINI_API_KEY do ambiente."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "A variável de ambiente GEMINI_API_KEY não foi configurada."
        )
    return genai.Client(api_key=api_key)
    if not api_key:
        raise RuntimeError(
            "A variável de ambiente GEMINI_API_KEY não foi configurada. "
            "Crie sua chave gratuita no Google AI Studio e defina GEMINI_API_KEY."
        )
    print("🤖 Gemini: usando GEMINI_API_KEY (Google AI Studio - Gratuito)")
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

def obter_conteudo_pastas_drive():
    try:
        if 'GOOGLE_CREDENTIALS' in os.environ:
            credenciais_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
            credenciais = Credentials.from_service_account_info(credenciais_dict, scopes=escopos)
        else:
            credenciais = Credentials.from_service_account_file("credenciais.json", scopes=escopos)
        
        service = build('drive', 'v3', credentials=credenciais)
        
        results = service.files().list(
            pageSize=300,
            fields="files(id, name, mimeType, webViewLink, parents)"
        ).execute()
        files = results.get('files', [])
        
        lista_arquivos = []
        mapa_links = {}
        for f in files:
            nome = f.get('name')
            link = f.get('webViewLink', '')
            mime = f.get('mimeType', '')
            lista_arquivos.append(f"- Arquivo: {nome} | Tipo: {mime} | Link: {link}")
            if nome:
                mapa_links[nome.strip().lower()] = link
                
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


TEMPLATE_HTML = """
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
        input { width: 100%; padding: 12px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 16px; background-color: #f7fafc; color: #2d3748; }
        input:focus { border-color: #0066cc; background-color: #fff; outline: none; }
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
        .btn-acao { flex: 1; padding: 12px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; text-align: center; text-decoration: none; display: inline-flex; justify-content: center; align-items: center; cursor: pointer; border: none; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }
        .btn-video { background-color: #002244; color: #ffffff; }
        .btn-video:hover { background-color: #001529; }
        .btn-whatsapp { background-color: #2f855a; color: #ffffff; }
        .btn-whatsapp:hover { background-color: #276749; }

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

        /* Estilos do Widget da IA */
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
            background: #002244; color: white; padding: 12px 16px; font-weight: 600;
            display: flex; justify-content: space-between; align-items: center;
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
        .ai-msg { max-width: 80%; padding: 10px 12px; border-radius: 8px; font-size: 13px; line-height: 1.4; }
        .ai-msg.bot { background: #e2e8f0; color: #2d3748; align-self: flex-start; }
        .ai-msg.user { background: #002244; color: #ffffff; align-self: flex-end; }
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

        function forcarAtualizacao() {
            var url = window.location.pathname + window.location.search;
            var separador = url.indexOf('?') !== -1 ? '&' : '?';
            window.location.href = url + separador + '_t=' + new Date().getTime();
        }

        function toggleAIChat() {
            var modal = document.getElementById('ai-chat-modal');
            modal.style.display = modal.style.display === 'flex' ? 'none' : 'flex';
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

        function enviarMensagemIA() {
            var input = document.getElementById('ai-user-input');
            var mensagem = input.value.trim();
            if (!mensagem) return;
            var chatBody = document.getElementById('ai-chat-messages');
            chatBody.innerHTML += `<div class="ai-msg user">${mensagem}</div>`;
            input.value = '';
            chatBody.scrollTop = chatBody.scrollHeight;

            fetch('/api/chat-ia', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mensagem: mensagem })
            })
            .then(response => response.json().then(data => {
                var respostaBot = data.resposta || "Desculpe, ocorreu um erro.";
                chatBody.innerHTML += `<div class="ai-msg bot">${respostaBot}</div>`;
                chatBody.scrollTop = chatBody.scrollHeight;
            }))
            .catch(error => {
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
        <!-- BARRA SUPERIOR -->
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
                <li class="drawer-item {% if modulo_ativo == 'rio' %}active{% endif %}">
                    <a href="/modulo/rio" onclick="closeDrawer()"><span class="drawer-icon">📡</span> Telemetria RIO</a>
                </li>
                <li class="drawer-item {% if modulo_ativo == 'pm' %}active{% endif %}">
                    <a href="/modulo/pm" onclick="closeDrawer()"><span class="drawer-icon">🛠</span> Plano de Manutenção</a>
                </li>
                <li class="drawer-item {% if modulo_ativo == 'valores' %}active{% endif %}">
                    <a href="/modulo/valores" onclick="closeDrawer()"><span class="drawer-icon">💲</span> Tabela de Valores</a>
                </li>
                <li class="drawer-item {% if modulo_ativo == 'informes' %}active{% endif %}">
                    <a href="/modulo/informes" onclick="closeDrawer()"><span class="drawer-icon">📢</span> Informes e Circulares</a>
                </li>
                <li class="drawer-item {% if modulo_ativo == 'fichatecnica' %}active{% endif %}">
                    <a href="/modulo/fichatecnica" onclick="closeDrawer()"><span class="drawer-icon">📋</span> Ficha Técnica</a>
                </li>
                <li class="drawer-item {% if modulo_ativo == 'argumentos' %}active{% endif %}">
                    <a href="/modulo/argumentos" onclick="closeDrawer()"><span class="drawer-icon">💡</span> Argumentos de Venda</a>
                </li>
                <li class="drawer-item" style="margin-top: 20px; border-top: 1px solid #edf2f7;">
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

        <!-- WIDGET FLUTUANTE DA IA -->
        <div id="ai-float-btn" onclick="toggleAIChat()" title="Assistente IA - Novo Mundo">
            <img src="{{ url_for('static', filename='logo.png') }}" alt="IA">
            <span class="ai-pulse-ring"></span>
        </div>

        <div id="ai-chat-modal" class="ai-chat-container">
            <div class="ai-chat-header">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-size: 18px;">🤖</span>
                    <span>Assistente Inteligente RIO</span>
                </div>
                <button type="button" onclick="toggleAIChat()" style="background:none; border:none; color:white; font-size:20px; cursor:pointer;">&times;</button>
            </div>
            
            <div id="ai-chat-messages" class="ai-chat-body">
                <div class="ai-msg bot">
                    Sou o Assistente Virtual da Novo Mundo Caminhões, como posso ajudar?
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
                usuarios = aba_usuarios.get_all_records()

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

@app.route("/modulo/<nome_modulo>")
def acessar_modulo(nome_modulo):
    if not session.get("logado"):
        return redirect(url_for("login"))

    conteudo = ""
    modulo_titulo = NOMES_MODULOS.get(nome_modulo, "Início")

    _, mapa_drive = obter_conteudo_pastas_drive()

    if nome_modulo == "rio":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            aba_rio = planilha.worksheet("RIO")
            produtos_rio = aba_rio.get_all_records()

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

                    nome_vendedor = session.get("nome", "Usuário")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

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
            produtos_pm = aba_pm.get_all_records()

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

                    nome_vendedor = session.get("nome", "Usuário")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

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
            linhas = aba_precos.get_all_values()

            if len(linhas) > 1:
                cabecalhos = linhas[0]
                dados_precos = []
                for linha in linhas[1:]:
                    item_dict = {}
                    for i, valor_celula in enumerate(linha):
                        if i < len(cabecalhos) and cabecalhos[i].strip():
                            nome_coluna = cabecalhos[i].strip()
                            if nome_coluna in item_dict:
                                idx = 1
                                while f"{nome_coluna}_{idx}" in item_dict:
                                    idx += 1
                                nome_coluna = f"{nome_coluna}_{idx}"
                            item_dict[nome_coluna] = valor_celula
                    dados_precos.append(item_dict)
            else:
                dados_precos = []

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
            dados_informes = aba_informes.get_all_records()

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
            dados_argumentos = aba_argumentos.get_all_records()

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

                    nome_vendedor = session.get("nome", "Usuário")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

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
            dados_modelos = aba_modelos.get_all_records()

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

@app.route("/api/chat-ia", methods=["POST"])
def chat_ia():
    global CACHE_IA
    
    if not session.get("logado"):
        return jsonify({"resposta": "Sessão expirada. Faça login novamente."}), 401
    
    dados = request.get_json()
    pergunta_usuario = dados.get("mensagem", "").strip()
    
    if not pergunta_usuario:
        return jsonify({"resposta": "Por favor, digite uma pergunta."})

    # TRATAMENTO RÁPIDO PARA CUMPRIMENTOS OU FRASES MUITO CURTAS (< 3 PALAVRAS)
    palavras = pergunta_usuario.split()
    texto_lower = pergunta_usuario.lower()
    cumprimentos = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem", "eae", "hey", "salve"]
    
    if len(palavras) < 3 or texto_lower in cumprimentos:
        return jsonify({
            "resposta": "Olá! Você precisa formular sua pergunta com base no conteúdo do App."
        })

    try:
        agora = time.time()
        
        # VERIFICAÇÃO DO CACHE (Atualiza se estiver vazio ou se passou de 5 minutos)
        if not CACHE_IA["contexto_sistema"] or (agora - CACHE_IA["timestamp"] > TEMPO_CACHE_SEGUNDOS):
            print("🔄 IA: Atualizando cache de dados (Lendo Planilha e Google Drive)...")
            planilha = conectar_google_sheets()
            contexto_abas = []
            
            try:
                todas_as_abas = planilha.worksheets()
                for aba in todas_as_abas:
                    nome_aba = aba.title
                    try:
                        registros = aba.get_all_records()
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
                "Você é o Assistente Virtual inteligente, articulado e prestativo da Novo Mundo Caminhões. "
                "DIRETRIZES DE COMPORTAMENTO:\n"
                "1. Responda às dúvidas da equipe STRICTAMENTE E EXCLUSIVAMENTE com base na base de dados unificada abaixo "
                "(que contém TODAS AS ABAS da planilha principal 'PM e RIO Novo' e TODOS OS ARQUIVOS/PASTAS do Google Drive: Modelos, Vídeos, Circulares, Base de Conhecimento e Documentos).\n"
                "2. Se a pergunta do usuário não constar nos dados fornecidos ou estiver fora do escopo, responda obrigatoriamente: "
                "'Você precisa formular sua pergunta com base no conteúdo do App.'\n"
                "3. NUNCA faça apenas um 'copia e cola' bruto ou resposta fria em tabela. Seja inteligente: analise as informações, interprete os dados técnicos, explique o contexto com clareza, formate a resposta em linguagem natural profissional e didática (destacando pontos importantes como PBT, especificações, motorização, valores ou regras de contratos).\n"
                "4. É terminantemente proibido utilizar conhecimentos gerais externos ou inventar informações.\n"
                "5. Sempre que relevante, mencione o nome do documento ou link correspondente disponível nos registros.\n\n"
                f"=== CONTEÚDO COMPLETO DE TODAS AS ABAS DA PLANILHA ===\n{dados_planilha}\n\n"
                f"=== ARQUIVOS NAS PASTAS DO GOOGLE DRIVE (Modelos, Vídeos, Circulares, Docs) ===\n{dados_drive}"
            )
            
            CACHE_IA["contexto_sistema"] = instrucao_sistema
            CACHE_IA["timestamp"] = agora
        else:
            print("⚡ IA: Usando dados em cache (Evitando chamadas desnecessárias).")

        client = criar_cliente_gemini()
        
        prompt_final = f"""
        {CACHE_IA["contexto_sistema"]}

        PERGUNTA DO USUÁRIO:
        {pergunta_usuario}
        """

        # SISTEMA DE CONTINGÊNCIA (FALLBACK) COM MODELOS DA LINHA 3.x
        modelos_para_tentar = [
            "gemini-3.6-flash", 
            "gemini-3.5-flash-lite", 
            "gemini-3.1-pro"
        ]
        response = None
        ultimo_erro = None

        for modelo_atual in modelos_para_tentar:
            try:
                print(f"🤖 Tentando IA com o modelo: {modelo_atual}")
                response = client.models.generate_content(
                    model=modelo_atual,
                    contents=prompt_final
                )
                if response and response.text:
                    break
            except Exception as err:
                ultimo_erro = err
                print(f"⚠️ Falha temporária com o modelo {modelo_atual}: {err}")
                continue

        if not response or not response.text:
            raise RuntimeError(f"Todos os modelos estão temporariamente ocupados. Último erro: {ultimo_erro}")

        return jsonify({
            "resposta": response.text
        })

    except Exception as e:
        erro_detalhado = traceback.format_exc()
        print("--- ERRO COMPLETO DO GEMINI / SERVIDOR ---")
        print(erro_detalhado)
        print("------------------------------------------")
        
        return jsonify({
            "resposta": f"🚨 ERRO INTERNO DO SERVIDOR (Alta demanda ou falha de leitura): {str(e)}"
        })

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
