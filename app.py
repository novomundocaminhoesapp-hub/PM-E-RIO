import json
import os
from datetime import datetime
import re
import urllib.parse

from flask import Flask, redirect, render_template_string, request, session, url_for
from google.oauth2.service_account import Credentials
import gspread

app = Flask(__name__)
app.secret_key = "chave_secreta_pm_rio"

MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"
}

escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def conectar_google_sheets():
    if 'GOOGLE_CREDENTIALS' in os.environ:
        credenciais_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
        credenciais = Credentials.from_service_account_info(credenciais_dict, scopes=escopos)
    else:
        credenciais = Credentials.from_service_account_file("credenciais.json", scopes=escopos)
    
    cliente = gspread.authorize(credenciais)
    return cliente.open("PM e RIO Novo")

def converter_para_embed(url):
    """Converte links normais do YouTube ou Drive para o formato embed de reprodução interna."""
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
    <meta name="theme-color" content="#1a252f">

    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
      /* --- FORÇAR MENU NO RODAPÉ EM DISPOSITIVOS MÓVEIS --- */
        @media (max-width: 768px) {
            .user-info, .profile-section, .sidebar-header, .btn-logout {
                display: none !important;
            }

            .app-container, body, html {
                flex-direction: column !important;
                overflow-x: hidden !important;
            }

            div[class*="sidebar"], nav, .menu-container, .nav-tabs {
                position: fixed !important;
                bottom: 0 !important;
                top: auto !important;
                left: 0 !important;
                right: 0 !important;
                width: 100% !important;
                height: 60px !important;
                min-height: 60px !important;
                z-index: 99999 !important;
                background-color: #1a252f !important;
                display: flex !important;
                flex-direction: row !important;
                justify-content: space-around !important;
                align-items: center !important;
                padding: 0 !important;
                margin: 0 !important;
                box-shadow: 0 -2px 10px rgba(0, 0, 0, 0.3) !important;
                border: none !important;
            }

            div[class*="sidebar"] a, div[class*="sidebar"] button, nav a, .nav-link, .btn-menu {
                font-size: 11px !important;
                padding: 8px 2px !important;
                margin: 0 !important;
                text-align: center !important;
                color: #ffffff !important;
                flex: 1 !important;
                display: flex !important;
                flex-direction: column !important;
                align-items: center !important;
                justify-content: center !important;
                white-space: nowrap !important;
                border-radius: 0 !important;
            }

            .main-content, .content, body {
                padding-bottom: 80px !important;
                margin-bottom: 0 !important;
                width: 100% !important;
            }
        }
        }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; 
            background: #f0f2f5;
            display: flex; 
            justify-content: center; 
            align-items: center; 
            min-height: 100vh;
            padding: 0;
        }
        
        .card-login { 
            background: #ffffff; 
            padding: 30px 20px; 
            border-radius: 12px; 
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08); 
            width: 100%; 
            max-width: 380px; 
            text-align: center; 
            border-top: 4px solid #1e3c72;
            margin: 15px;
        }

        .app-container {
            display: flex;
            background: #ffffff;
            width: 100%;
            height: 100vh;
            max-width: 100%;
            border-radius: 0;
            box-shadow: none;
            overflow: hidden;
            border: none;
        }

        .sidebar {
            width: 100%;
            background: #1a252f;
            color: #ffffff;
            display: flex;
            flex-direction: row;
            align-items: center;
            justify-content: space-between;
            padding: 10px 15px;
            flex-shrink: 0;
            position: fixed;
            top: 0;
            left: 0;
            z-index: 1000;
            height: 60px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.15);
        }
        .sidebar-header { display: flex; align-items: center; gap: 10px; }
        .logo-sidebar { max-width: 145px; height: auto; display: block; }
        .user-mini-info { display: none; }

        .menu-nav {
            display: flex;
            flex-direction: row;
            gap: 6px;
            overflow-x: auto;
            white-space: nowrap;
            -webkit-overflow-scrolling: touch;
            scrollbar-width: none;
        }
        .menu-nav::-webkit-scrollbar { display: none; }

        .menu-item {
            display: inline-flex;
            align-items: center;
            padding: 8px 12px;
            background: rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            text-decoration: none;
            color: #cbd5e0;
            font-weight: 500;
            font-size: 13px;
        }
        .menu-item.active { background: #0066cc; color: white; font-weight: 600; }
        
        .content-area {
            flex-grow: 1;
            padding: 20px 15px 15px 15px;
            background: #ffffff;
            overflow-y: auto;
            margin-top: 60px;
            width: 100%;
            display: flex;
            flex-direction: column;
        }

        .submenus-grid { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        .submenu-btn {
            background: #ffffff;
            border: 1px solid #cbd5e0;
            border-left: 4px solid #0066cc;
            padding: 14px 16px;
            border-radius: 8px;
            text-decoration: none;
            color: #1a202c;
            font-weight: 600;
            font-size: 15px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: all 0.2s;
        }
        .submenu-btn:hover { background: #f7fafc; border-color: #0066cc; }
        .submenu-btn::after { content: '›'; font-size: 18px; color: #a0aec0; }

        .produto-detalhe-card {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 16px;
            margin-top: 5px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.02);
        }

        .detalhe-linha { margin-bottom: 12px; border-bottom: 1px solid #edf2f7; padding-bottom: 10px; }
        .detalhe-label { font-size: 11px; font-weight: 700; color: #4a5568; text-transform: uppercase; margin-bottom: 3px; }
        .detalhe-valor { font-size: 14px; color: #1a202c; }
        .detalhe-produto-nome { font-size: 16px; font-weight: 700; color: #1e3c72; }
        .detalhe-preco { font-size: 16px; font-weight: 700; color: #2f855a; }

        .acoes-produto { display: flex; gap: 8px; margin-top: 12px; }
        .btn-acao {
            flex: 1; padding: 12px 10px; border-radius: 6px; font-size: 13px; font-weight: 600;
            text-align: center; text-decoration: none; display: inline-flex; justify-content: center;
            align-items: center; cursor: pointer; border: none; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .btn-video { background-color: #2b6cb0; color: #ffffff; }
        .btn-video:hover { background-color: #2c5282; }
        .btn-whatsapp { background-color: #2f855a; color: #ffffff; }
        .btn-whatsapp:hover { background-color: #276749; }

        .btn-toggle-cobertura {
            background-color: #edf2f7; color: #2d3748; border: 1px solid #cbd5e0;
            padding: 10px 14px; border-radius: 6px; font-size: 13px; font-weight: 600;
            cursor: pointer; width: 100%; text-align: left; display: flex;
            justify-content: space-between; align-items: center; margin-top: 4px;
        }
        .btn-toggle-cobertura:hover { background-color: #e2e8f0; }
        .conteudo-cobertura {
            display: none; background: #ffffff; border: 1px solid #e2e8f0;
            border-radius: 6px; padding: 12px; margin-top: 6px; font-size: 13px;
            color: #2d3748; white-space: pre-line;
        }

        .grid-planos { display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }
        .card-plano {
            background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px;
            padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.02); border-left: 4px solid #0066cc;
        }
        .card-plano.max { border-left-color: #d69e2e; }
        .card-plano.plus { border-left-color: #2f855a; }

        .plano-titulo {
            font-size: 14px; font-weight: 700; color: #1a202c; margin-bottom: 10px;
            text-transform: uppercase; border-bottom: 1px solid #edf2f7; padding-bottom: 6px;
        }
        .plano-linha-tripla { display: flex; gap: 8px; margin-bottom: 8px; }
        .plano-col { flex: 1; background: #f7fafc; padding: 8px 10px; border-radius: 6px; border: 1px solid #edf2f7; }

        .btn-voltar {
            display: inline-block; background: #e2e8f0; color: #4a5568; padding: 6px 12px;
            border-radius: 6px; font-size: 12px; font-weight: 600; text-decoration: none; margin-bottom: 12px;
        }
        .btn-voltar:hover { background: #cbd5e0; }

        .acoes-ficha-tecnica { display: flex; gap: 8px; margin-top: 8px; }
        .btn-acao-ficha {
            flex: 1; padding: 10px; border-radius: 6px; font-size: 13px; font-weight: 600;
            text-align: center; text-decoration: none; display: inline-block; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .btn-abrir-pdf { background-color: #2b6cb0; color: #ffffff; }
        .btn-abrir-pdf:hover { background-color: #2c5282; }
        .btn-wpp-pdf { background-color: #2f855a; color: #ffffff; }
        .btn-wpp-pdf:hover { background-color: #276749; }

        .modal-video-overlay {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.85); z-index: 2000; justify-content: center;
            align-items: center; padding: 15px;
        }
        .modal-video-content {
            background: #000000; width: 100%; max-width: 720px; border-radius: 12px;
            overflow: hidden; position: relative; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            display: flex; flex-direction: column;
        }
        .modal-video-header {
            display: flex; justify-content: space-between; align-items: center;
            background: #1a252f; color: #ffffff; padding: 12px 16px; font-size: 14px; font-weight: 600;
        }
        .btn-fechar-modal { background: transparent; border: none; color: #ffffff; font-size: 24px; cursor: pointer; line-height: 1; padding: 0 4px; }
        .iframe-container { position: relative; width: 100%; padding-bottom: 56.25%; height: 0; background: #000; }
        .iframe-container iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: 0; }

        .logo-container { margin-bottom: 20px; display: flex; justify-content: center; }
        .logo { max-width: 220px; height: auto; }
        h2 { color: #1a202c; font-size: 17px; margin-bottom: 12px; font-weight: 600; }
        .input-group { text-align: left; margin-bottom: 15px; }
        label { display: block; font-weight: 600; font-size: 11px; color: #4a5568; margin-bottom: 5px; text-transform: uppercase; }
        input { width: 100%; padding: 12px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 16px; background-color: #f7fafc; color: #2d3748; }
        input:focus { border-color: #0066cc; background-color: #fff; outline: none; }
        button { background-color: #0066cc; color: white; border: none; padding: 12px; width: 100%; border-radius: 6px; cursor: pointer; font-size: 15px; font-weight: 600; }
        .error { background-color: #fff5f5; color: #c53030; padding: 12px; border-radius: 6px; font-size: 13px; margin-bottom: 15px; border: 1px solid #feb2b2; line-height: 1.4; font-weight: 500; }
        
        .desktop-logout { display: none; }
        .mobile-logout-container { display: block; margin-top: 30px; padding-top: 15px; border-top: 1px solid #edf2f7; }

        @media (min-width: 768px) {
            body { padding: 15px; align-items: center; }
            .card-login { max-width: 400px; padding: 40px 30px; }
            .app-container { max-width: 1200px; height: 90vh; border-radius: 12px; border: 1px solid #e1e4e8; }
            .sidebar { position: relative; width: 260px; height: 100%; flex-direction: column; align-items: stretch; justify-content: space-between; padding: 24px 16px; top: auto; left: auto; }
            .sidebar-header { flex-direction: column; align-items: center; text-align: center; border-bottom: 1px solid rgba(255, 255, 255, 0.1); padding-bottom: 16px; margin-bottom: 20px; }
            .logo-sidebar { max-width: 170px; margin: 0 auto; }
            .user-mini-info { display: block; font-size: 12px; color: #a0aec0; margin-top: 10px; }
            .menu-nav { flex-direction: column; gap: 6px; overflow-x: visible; }
            .menu-item { display: flex; padding: 12px 14px; font-size: 14px; }
            .content-area { margin-top: 0; padding: 40px; }
            .mobile-logout-container { display: none; }
            .desktop-logout { display: block; background-color: #2d3748; color: #cbd5e0; width: 100%; padding: 10px; font-size: 13px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); cursor: pointer; }
            .desktop-logout:hover { background-color: #e53e3e; color: #fff; }
        }
    </style>
    <script>
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
    </script>
</head>
<body>

    {% if not session.get('logado') %}
        <div class="card-login">
            <div class="logo-container">
                <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo Novo Mundo" class="logo">
            </div>
            
            <h2>Acesso Restrito</h2>
            {% if erro %}
                <div class="error">{{ erro }}</div>
            {% endif %}
            <form method="POST">
                <div class="input-group">
                    <label>E-mail Corporativo</label>
                    <input type="email" name="email" placeholder="seu.email@novomundo.com" required autocapitalize="none">
                </div>
                <div class="input-group">
                    <label>Senha</label>
                    <input type="password" name="senha" placeholder="••••••••" required>
                </div>
                <button type="submit">Entrar no Sistema</button>
            </form>
        </div>
    {% else %}
        <div class="app-container">
            <div class="sidebar">
                <div>
                    <div class="sidebar-header">
                        <img src="{{ url_for('static', filename='logo.png') }}" alt="Logo Novo Mundo" class="logo-sidebar">
                        <div class="user-mini-info">
                            <b>{{ session.get('nome') }}</b><br>
                            Perfil: {{ session.get('perfil') }}
                        </div>
                    </div>

                    <div class="menu-nav">
                        <a href="/modulo/rio" class="menu-item {% if modulo_ativo == 'rio' %}active{% endif %}">RIO</a>
                        <a href="/modulo/pm" class="menu-item {% if modulo_ativo == 'pm' %}active{% endif %}">PM</a>
                        <a href="/modulo/valores" class="menu-item {% if modulo_ativo == 'valores' %}active{% endif %}">VALORES</a>
                        <a href="/modulo/informes" class="menu-item {% if modulo_ativo == 'informes' %}active{% endif %}">INFORMES</a>
                        <a href="/modulo/argumentos" class="menu-item {% if modulo_ativo == 'argumentos' %}active{% endif %}">ARGUMENTOS</a>
                    </div>
                </div>

                <div>
                    <form action="/logout" method="POST" style="margin: 0;">
                        <button type="submit" class="desktop-logout">Encerrar Sessão</button>
                    </form>
                </div>
            </div>

            <div class="content-area">
                {% if conteudo_modulo %}
                    {{ conteudo_modulo | safe }}
                {% else %}
                    <div style="display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100%; text-align: center; color: #718096; padding: 20px;">
                        <h2 style="color: #1a202c; margin-bottom: 8px;">Painel de Inteligência Comercial</h2>
                        <p style="font-size: 14px; max-width: 400px; line-height: 1.5;">Toque nas abas do menu superior para alternar rapidamente entre os módulos.</p>
                    </div>
                {% endif %}

                <div class="mobile-logout-container">
                    <form action="/logout" method="POST" style="margin: 0;">
                        <button type="submit" style="background-color: #e53e3e; color: white; padding: 12px; font-size: 14px;">Sair do Sistema</button>
                    </form>
                </div>
            </div>
        </div>

        <!-- MODAL DE VÍDEO -->
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
    {% endif %}

</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def login():
    erro = None

    if request.method == "POST":
        input_email = request.form.get("email", "").strip().lower()
        input_senha = request.form.get("senha")

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

                session["logado"] = True
                session["nome"] = usuario_encontrado.get("NOME")
                session["perfil"] = usuario_encontrado.get("PERFIL")
                return redirect(url_for("acessar_modulo", nome_modulo="rio"))
            else:
                tentativas = session.get("tentativas_erro", 0) + 1
                session["tentativas_erro"] = tentativas

                if tentativas >= 3:
                    erro = "Você excedeu 3 tentativas incorretas. Procure a Torre de Controle da Novo Mundo para adquirir uma nova senha."
                else:
                    restantes = 3 - tentativas
                    erro = f"E-mail ou Senha incorretos. Você tem mais {restantes} tentativa(s) antes do bloqueio temporário."
        except Exception as e:
            erro = f"Erro de conexão ou processamento: {e}"

    return render_template_string(
        TEMPLATE_HTML,
        erro=erro
    )

@app.route("/modulo/<nome_modulo>")
def acessar_modulo(nome_modulo):
    if not session.get("logado"):
        return redirect(url_for("login"))

    conteudo = ""

    if nome_modulo == "rio":
        produto_selecionado = request.args.get("produto")

        try:
            planilha = conectar_google_sheets()
            aba_rio = planilha.worksheet("RIO")
            produtos_rio = aba_rio.get_all_records()

            if produto_selecionado:
                item_escolhido = next((item for item in produtos_rio if str(item.get("PRODUTO", "")) == produto_selecionado), None)

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
                        texto_formatado = re.sub(r"(Foco:)", r"<b>\1</b>", texto, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Descrição:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Funcionalidades:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Diferencial Estratégico:|Diferencial Estrategico:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        return texto_formatado

                    foco_formatado = destacar_termos(foco)
                    descricao_formatada = destacar_termos(descricao)

                    nome_vendedor = session.get("nome", "André Santana")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    agora = datetime.now()
                    mes_vigente = MESES_PT.get(agora.month, "corrente")
                    ano_vigente = agora.year
                    validade_texto = f"{mes_vigente}/{ano_vigente}"

                    texto_whatsapp = f"📦 *Produto:* 🔧 {prod}\n\n🎯 *Foco:* {foco}\n\n📝 *Descrição:* {descricao}\n\n💰 *Valor:* {valor}\n\n⚠️ *Nota:* Proposta válida para {validade_texto}.\n\n🎬 *Assista ao vídeo explicativo aqui:* {video}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(texto_whatsapp)
                    url_video_embed = converter_para_embed(video)

                    btn_ver_video = f'<button type="button" class="btn-acao btn-video" onclick="abrirVideoModal(\'{url_video_embed}\')">▶ Assistir Vídeo</button>' if video else ""
                    btn_enviar_wpp = f'<a href="{link_wpp_compartilhar}" target="_blank" rel="noopener noreferrer" class="btn-acao btn-whatsapp">📤 Enviar WhatsApp</a>'

                    conteudo = f"""
                    <div>
                        <a href="/modulo/rio" class="btn-voltar">‹ Voltar para os Produtos RIO</a>
                        <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Produto</h2>
                        
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
                    conteudo = '<div><a href="/modulo/rio" class="btn-voltar">‹ Voltar</a><p style="color: #c53030;">Produto não encontrado.</p></div>'
            else:
                botoes_produtos = "".join([f'<a href="/modulo/rio?produto={item.get("PRODUTO", "")}" class="submenu-btn">{item.get("PRODUTO", "")}</a>' for item in produtos_rio if item.get("PRODUTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Módulo RIO — Selecione um Produto</h2>
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

            if produto_selecionado:
                item_escolhido = next((item for item in produtos_pm if str(item.get("PRODUTO", "")) == produto_selecionado), None)

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
                        texto_formatado = re.sub(r"(Foco:)", r"<b>\1</b>", texto, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Descrição:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Funcionalidades:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Diferencial Estratégico:|Diferencial Estrategico:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        texto_formatado = re.sub(r"(Coberturas:)", r"<b>\1</b>", texto_formatado, flags=re.IGNORECASE)
                        return texto_formatado

                    foco_formatado = destacar_termos(foco)
                    descricao_formatada = destacar_termos(descricao)
                    coberturas_formatadas = destacar_termos(coberturas)

                    nome_vendedor = session.get("nome", "André Santana")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    agora = datetime.now()
                    mes_vigente = MESES_PT.get(agora.month, "corrente")
                    ano_vigente = agora.year
                    validade_texto = f"{mes_vigente}/{ano_vigente}"

                    texto_whatsapp = f"📦 *Plano de Manutenção:* 🔧 {prod}\n\n🎯 *Foco:* {foco}\n\n📝 *Descrição:* {descricao}\n\n🛡️ *Coberturas:* {coberturas}\n\n⚠️ *Nota:* Proposta válida para {validade_texto}.\n\n🎬 *Assista ao vídeo explicativo aqui:* {video}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(texto_whatsapp)
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
                        <a href="/modulo/pm" class="btn-voltar">‹ Voltar para os Planos de Manutenção</a>
                        <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Plano</h2>
                        
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
                    conteudo = '<div><a href="/modulo/pm" class="btn-voltar">‹ Voltar</a><p style="color: #c53030;">Plano não encontrado.</p></div>'
            else:
                botoes_produtos = "".join([f'<a href="/modulo/pm?produto={item.get("PRODUTO", "")}" class="submenu-btn">{item.get("PRODUTO", "")}</a>' for item in produtos_pm if item.get("PRODUTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Módulo P. Manutenção — Selecione um Plano</h2>
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

            if produto_selecionado:
                item_escolhido = next((item for item in dados_precos if (item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO") or "") == produto_selecionado), None)

                if item_escolhido:
                    titulo_principal = (
                        item_escolhido.get("MODELO")
                        or item_escolhido.get("PRODUTO")
                        or item_escolhido.get("ITEM")
                        or item_escolhido.get("PLANO")
                        or "Detalhes do Item"
                    )
                    periodo_val = item_escolhido.get("PERIODO", "")
                    valor_ficha_bruto = (
                        item_escolhido.get("FICHA", "")
                        or item_escolhido.get("FICHA TÉCNICA", "")
                        or item_escolhido.get("FICHA TECNICA", "")
                        or item_escolhido.get("FICHA_TECNICA", "")
                        or item_escolhido.get("PDF", "")
                    )

                    link_ficha_tecnica = valor_ficha_bruto
                    if valor_ficha_bruto and not valor_ficha_bruto.startswith("http"):
                        link_ficha_tecnica = f"https://drive.google.com/drive/search?q={urllib.parse.quote(valor_ficha_bruto)}"

                    bloco_ficha_tecnica_html = ""
                    if valor_ficha_bruto:
                        texto_wpp_ficha = f"📄 *Ficha Técnica - {titulo_principal}*\n\nConsulte o documento completo no link abaixo:\n{link_ficha_tecnica}"
                        link_wpp_ficha = f"https://api.whatsapp.com/send?text={urllib.parse.quote(texto_wpp_ficha)}"

                        bloco_ficha_tecnica_html = f"""
                        <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 12px; margin-bottom: 14px;">
                            <div class="detalhe-label" style="color: #1e3c72; margin-bottom: 6px;">Ficha Técnica (PDF)</div>
                            <div class="acoes-ficha-tecnica">
                                <a href="{link_ficha_tecnica}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-abrir-pdf">📂 ABRIR PDF</a>
                                <a href="{link_wpp_ficha}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-wpp-pdf">📤 ENVIAR VIA WHATSAPP</a>
                            </div>
                        </div>
                        """

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
                        <a href="/modulo/valores" class="btn-voltar">‹ Voltar para a lista de Valores</a>
                        <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes de Valores</h2>
                        
                        <div class="produto-detalhe-card">
                            <div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; margin-bottom: 14px;">
                                <div style="margin-bottom: 8px;">
                                    <div class="detalhe-label" style="color: #1e3c72; margin-bottom: 2px;">Modelo / Item</div>
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

                            { '<div style="background: #eef2f7; border: 1px solid #cbd5e0; border-radius: 8px; padding: 14px; margin-top: 18px; margin-bottom: 14px;"><div style="display: flex; gap: 10px;"><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Horas (H)</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + hora_geral_val + '</div></div><div style="flex: 1; background: #ffffff; padding: 8px 10px; border-radius: 6px; border: 1px solid #cbd5e0;"><div class="detalhe-label" style="color: #2b6cb0; margin-bottom: 2px;">Período do Contrato</div><div style="font-size: 15px; font-weight: 700; color: #1a202c;">' + periodo_val + ' Meses</div></div></div></div>' if hora_geral_val else '' }

                            { '<div style="font-size: 13px; font-weight: 700; color: #4a5568; margin-top: 10px; margin-bottom: 6px; text-transform: uppercase;">Valores por Horas (H)</div>' if cards_horas_html else '' }
                            <div class="grid-planos">{cards_horas_html}</div>
                        </div>
                    </div>
                    """
                else:
                    conteudo = '<div><a href="/modulo/valores" class="btn-voltar">‹ Voltar</a><p style="color: #c53030;">Item não encontrado.</p></div>'
            else:
                botoes_itens = "".join([f'<a href="/modulo/valores?produto={item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO")}" class="submenu-btn">{item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO")}</a>' for item in dados_precos if (item.get("MODELO") or item.get("PRODUTO") or item.get("ITEM") or item.get("PLANO"))])
                conteudo = f"""
                <div>
                    <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Módulo VALORES — Selecione um Modelo</h2>
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

            if informe_selecionado:
                item_escolhido = next((item for item in dados_informes if str(item.get("ASSUNTO", "")) == informe_selecionado), None)

                if item_escolhido:
                    assunto_val = item_escolhido.get("ASSUNTO", "")
                    informacao_val = item_escolhido.get("INFORMAÇÃO", "") or item_escolhido.get("INFORMACAO", "")
                    circular_val = item_escolhido.get("CIRCULAR", "")

                    bloco_circular_html = ""
                    if circular_val:
                        bloco_circular_html = f"""
                        <div style="background: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; padding: 12px; margin-top: 14px;">
                            <div class="detalhe-label" style="color: #1e3c72; margin-bottom: 6px;">Circular Oficial</div>
                            <div class="acoes-ficha-tecnica">
                                <a href="{circular_val}" target="_blank" rel="noopener noreferrer" class="btn-acao-ficha btn-abrir-pdf">📄 ABRIR CIRCULAR (PDF)</a>
                            </div>
                        </div>
                        """

                    conteudo = f"""
                    <div>
                        <a href="/modulo/informes" class="btn-voltar">‹ Voltar para os Informes</a>
                        <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Detalhes do Comunicado</h2>
                        
                        <div class="produto-detalhe-card">
                            <div class="detalhe-linha">
                                <div class="detalhe-label">Assunto</div>
                                <div class="detalhe-valor detalhe-produto-nome" style="color: #1e3c72;">{assunto_val}</div>
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
                    conteudo = '<div><a href="/modulo/informes" class="btn-voltar">‹ Voltar</a><p style="color: #c53030;">Informe não encontrado.</p></div>'
            else:
                botoes_informes = "".join([f'<a href="/modulo/informes?item={item.get("ASSUNTO", "")}" class="submenu-btn">{item.get("ASSUNTO", "")}</a>' for item in dados_informes if item.get("ASSUNTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Módulo INFORMES — Avisos e Comunicados</h2>
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

            if argumento_selecionado:
                item_escolhido = next((item for item in dados_argumentos if str(item.get("QUESTIONAMENTO", "")) == argumento_selecionado), None)

                if item_escolhido:
                    pergunta_val = item_escolhido.get("QUESTIONAMENTO", "")
                    resposta_val = item_escolhido.get("RESPOSTA", "")

                    nome_vendedor = session.get("nome", "André Santana")
                    contato_texto = f"{nome_vendedor}, Torre de Controle da Novo Mundo Caminhões - 📞 (81) 99686-0674"

                    texto_whatsapp = f"💡 *Questionamento:* {pergunta_val}\n\n💬 *Resposta / Argumento:* {resposta_val}\n\n👤 *Contato:* {contato_texto}"
                    link_wpp_compartilhar = "https://api.whatsapp.com/send?text=" + urllib.parse.quote(texto_whatsapp)

                    btn_enviar_wpp = f"""
                    <div style="margin-top: 14px;">
                        <a href="{link_wpp_compartilhar}" target="_blank" rel="noopener noreferrer" class="btn-acao btn-whatsapp" style="width: 100%; display: inline-flex; justify-content: center; align-items: center; padding: 12px; text-decoration: none; border-radius: 6px; font-weight: 600; color: #ffffff; background-color: #2f855a;">📤 Enviar Resposta via WhatsApp</a>
                    </div>
                    """

                    conteudo = f"""
                    <div>
                        <a href="/modulo/argumentos" class="btn-voltar">‹ Voltar para os Argumentos</a>
                        <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 8px; margin-bottom: 12px; font-size: 17px;">Argumentário Comercial</h2>
                        
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
                    conteudo = '<div><a href="/modulo/argumentos" class="btn-voltar">‹ Voltar</a><p style="color: #c53030;">Argumento não encontrado.</p></div>'
            else:
                botoes_argumentos = "".join([f'<a href="/modulo/argumentos?item={item.get("QUESTIONAMENTO", "")}" class="submenu-btn">{item.get("QUESTIONAMENTO", "")}</a>' for item in dados_argumentos if item.get("QUESTIONAMENTO")])
                conteudo = f"""
                <div>
                    <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">Módulo ARGUMENTOS — Objeções e Respostas</h2>
                    <p style="color: #4a5568; font-size: 13px; margin-bottom: 14px;">Selecione abaixo a dúvida ou objeção do cliente para visualizar a melhor linha de argumentação:</p>
                    <div class="submenus-grid">{botoes_argumentos}</div>
                </div>
                """
        except Exception as e:
            conteudo = f'<div style="color: #c53030; background: #fff5f5; padding: 15px; border-radius: 8px; border: 1px solid #feb2b2;"><b>Erro ao carregar os dados da aba Argumentos:</b> {e}</div>'

    else:
        titulo_atual = f"Módulo {nome_modulo.upper()}"
        conteudo = f"""
        <div>
            <h2 style="color: #1a202c; border-bottom: 2px solid #edf2f7; padding-bottom: 10px; margin-bottom: 14px; font-size: 17px;">{titulo_atual}</h2>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px;">
                <p style="color: #4a5568; font-size: 14px; line-height: 1.6;">Conteúdo em desenvolvimento para este módulo.</p>
            </div>
        </div>
        """

    return render_template_string(
        TEMPLATE_HTML, conteudo_modulo=conteudo, modulo_ativo=nome_modulo
    )

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
