from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
import mercadopago
import anthropic
import json
import os
import uuid
import traceback

app = Flask(__name__, static_folder='static')
CORS(app)

MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

print(f"DEBUG INICIAL: MP_ACCESS_TOKEN presente: {bool(MP_ACCESS_TOKEN)}", flush=True)
print(f"DEBUG INICIAL: ANTHROPIC_API_KEY presente: {bool(ANTHROPIC_API_KEY)}", flush=True)

sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

pagamentos_aprovados = set()
analises_cache = {}

# ============================================================
# CONTROLE DE LIMITE GRATUITO POR IP
# ============================================================
LIMITE_DIARIO = 2
usos_por_ip = {}


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'desconhecido'


def verifica_limite_grauito(ip):
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    registro = usos_por_ip.get(ip)
    if not registro or registro.get('data') != hoje:
        return True
    return registro.get('count', 0) < LIMITE_DIARIO


def registra_uso_gratuito(ip):
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    registro = usos_por_ip.get(ip)
    if not registro or registro.get('data') != hoje:
        usos_por_ip[ip] = {'data': hoje, 'count': 1}
    else:
        usos_por_ip[ip]['count'] = registro.get('count', 0) + 1


def get_base_url():
    proto = request.headers.get('X-Forwarded-Proto', 'https')
    host = request.headers.get('X-Forwarded-Host') or request.host
    if proto != 'https':
        proto = 'https'
    return f"{proto}://{host}"


# ============================================================
# PROMPTS
# ============================================================
SYSTEM_DIAGNOSTICO = """Voce e uma consultora estrategica senior de Instagram, com perfil de analista de marca. Tom: premium, profissional, consultivo, inteligente. NUNCA agressivo, sarcastico, humilhante ou destrutivo.

OBJETIVO: gerar percepcao estrategica HONESTA que faca a pessoa pensar "faz sentido, e isso mesmo que meu perfil transmite". Despertar curiosidade pela analise completa, SEM entregar solucao detalhada agora.

REGRAS DE LINGUAGEM:
- Reconheca pontos positivos reais antes de apontar pontos de melhoria
- Aponte problemas como "oportunidades estrategicas" ou "pontos que limitam o crescimento"
- Use linguagem consultiva: "observei que", "o perfil ainda nao comunica", "ha espaco para fortalecer"
- Tom analitico, nao emocional
- Frases construtivas, nunca absolutas

PROIBIDO usar palavras ou expressoes como:
- "caotico", "caos", "bagunca"
- "destruindo", "matando", "afundando"
- "ninguem entende", "ninguem ve", "ninguem segue"
- "inconsistencia total", "totalmente desorganizado"
- "fracasso", "perdido", "errado"
- Linguagem de coach motivacional
- Frases absolutas tipo "voce nunca vai crescer"

ESTRUTURA OBRIGATORIA:
1. percepcao_inicial: como um visitante estrategico le o perfil nos primeiros 3 segundos (1-2 frases, tom neutro e profissional)
2. pontos_fortes: 2 pontos positivos REAIS que voce observou no perfil (curtos, especificos)
3. problemas: 3 pontos de melhoria estrategicos, descritos como oportunidades (curtos, especificos, NAO da solucao)
4. impacto: o que esses pontos estao impedindo o perfil de alcancar (1 paragrafo, tom analitico, sem drama)
5. frase_gancho: uma frase curta que cria curiosidade pela analise completa, SEM ameacar.

NAO entregue solucoes nesta versao. Apenas diagnostico estrategico.

Responda APENAS com JSON valido sem markdown sem backticks:
{"percepcao_inicial": "...", "pontos_fortes": ["...", "..."], "problemas": ["...", "...", "..."], "impacto": "...", "frase_gancho": "..."}"""


SYSTEM_COMPLETO = """Voce e uma consultora estrategica senior de Instagram. Agora entregue a ANALISE COMPLETA com solucoes praticas, estrategia clara e ideias de conteudo aplicaveis. Tom: profissional, consultivo, premium, construtivo. Sem linguagem de coach.

Responda APENAS com JSON valido sem markdown sem backticks: {"bio_reescrita": "nova bio sugerida", "solucoes": ["solucao 1", "solucao 2", "solucao 3", "solucao 4", "solucao 5"], "pilares_conteudo": ["pilar 1", "pilar 2", "pilar 3"], "ideias_conteudo": [{"titulo": "titulo", "formato": "Reel", "descricao": "descricao", "hook": "hook"}], "plano_acao": ["acao 1", "acao 2", "acao 3"]}"""


# ============================================================
# ROTAS DE API
# ============================================================
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/diagnostico', methods=['POST'])
def gerar_diagnostico():
    try:
        ip = get_client_ip()
        print(f"DEBUG: requisicao do IP {ip}", flush=True)
        if not verifica_limite_grauito(ip):
            print(f"DEBUG: IP {ip} atingiu limite diario", flush=True)
            return jsonify({
                'success': False,
                'limite_atingido': True,
                'error': 'Voce ja utilizou sua analise gratuita hoje. Tente novamente mais tarde.'
            }), 429

        data = request.json
        arroba = data.get('arroba', '')
        nicho = data.get('nicho', '')
        seguidores = data.get('seguidores', '')
        objetivo = data.get('objetivo', '')
        obs = data.get('obs', '')
        imagens = data.get('imagens', [])

        print(f"DEBUG: arroba={arroba}, nicho={nicho}", flush=True)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        content = []

        for img_data in imagens[:5]:
            if ',' in img_data:
                header, b64 = img_data.split(',', 1)
                media_type = header.split(':')[1].split(';')[0]
            else:
                b64 = img_data
                media_type = 'image/jpeg'
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64}
            })

        content.append({
            "type": "text",
            "text": "Perfil: " + arroba + "\nNicho: " + nicho + "\nSeguidores: " + seguidores + "\nObjetivo: " + objetivo + "\nGere o diagnostico estrategico seguindo todas as regras de tom."
        })

        print("DEBUG: chamando Anthropic API...", flush=True)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=SYSTEM_DIAGNOSTICO,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        analise = json.loads(raw[start:end])

        registra_uso_gratuito(ip)

        session_id = str(uuid.uuid4())
        analises_cache[session_id] = {
            'arroba': arroba, 'nicho': nicho, 'seguidores': seguidores,
            'objetivo': objetivo, 'obs': obs, 'imagens': imagens, 'diagnostico': analise
        }

        return jsonify({'success': True, 'analise': analise, 'session_id': session_id})

    except Exception as e:
        print(f"ERRO DETALHADO: {str(e)}", flush=True)
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/criar-pagamento', methods=['POST'])
def criar_pagamento():
    try:
        if not MP_ACCESS_TOKEN or not sdk:
            return jsonify({'success': False, 'error': 'MP_ACCESS_TOKEN nao configurado'}), 500

        data = request.json or {}
        session_id = data.get('session_id', str(uuid.uuid4()))
        base_url = get_base_url()

        is_test = MP_ACCESS_TOKEN.startswith("TEST-")

        preference_data = {
            "items": [{
                "title": "Analise Estrategica Completa Instagram",
                "quantity": 1,
                "unit_price": 19.90,
                "currency_id": "BRL"
            }],
            "back_urls": {
                "success": base_url + "/sucesso?session=" + session_id,
                "failure": base_url + "/erro?session=" + session_id,
                "pending": base_url + "/pendente?session=" + session_id
            },
            "auto_return": "approved",
            "external_reference": session_id,
            "notification_url": base_url + "/api/webhook",
            "binary_mode": False
        }

        result = sdk.preference().create(preference_data)
        status = result.get("status", 0)
        response_body = result.get("response", {}) or {}

        if status >= 400 or "id" not in response_body:
            return jsonify({
                'success': False, 'error': 'Mercado Pago rejeitou a preferencia',
                'mp_response': response_body
            }), 502

        checkout_url = response_body.get("sandbox_init_point") if is_test else response_body.get("init_point")
        if not checkout_url:
            checkout_url = response_body.get("init_point")

        return jsonify({
            'success': True, 'checkout_url': checkout_url,
            'preference_id': response_body.get("id"), 'session_id': session_id
        })

    except Exception as e:
        print(f"ERRO criar_pagamento: {str(e)}", flush=True)
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        print(f"DEBUG WEBHOOK: {data}", flush=True)
        if data and data.get('type') == 'payment':
            payment_id = data['data']['id']
            payment_info = sdk.payment().get(payment_id)
            if payment_info['response']['status'] == 'approved':
                pagamentos_aprovados.add(payment_info['response']['external_reference'])
    except Exception as e:
        print(f"ERRO webhook: {e}", flush=True)
    return jsonify({'status': 'ok'})


@app.route('/api/verificar-pagamento', methods=['POST'])
def verificar_pagamento():
    data = request.json
    session_id = data.get('session_id')
    payment_id = data.get('payment_id')

    if payment_id and sdk:
        try:
            payment_info = sdk.payment().get(payment_id)
            if payment_info['response']['status'] == 'approved':
                ref = payment_info['response'].get('external_reference', session_id)
                pagamentos_aprovados.add(ref)
                return jsonify({'aprovado': True, 'session_id': ref})
        except Exception:
            pass

    return jsonify({'aprovado': session_id in pagamentos_aprovados})


@app.route('/api/analise-completa', methods=['POST'])
def analise_completa():
    try:
        data = request.json
        session_id = data.get('session_id')

        sessao = analises_cache.get(session_id)
        if not sessao:
            return jsonify({'success': False, 'error': 'Sessao nao encontrada'}), 404

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        content = []

        for img_data in sessao['imagens'][:5]:
            if ',' in img_data:
                header, b64 = img_data.split(',', 1)
                media_type = header.split(':')[1].split(';')[0]
            else:
                b64 = img_data
                media_type = 'image/jpeg'
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64}
            })

        diag = sessao['diagnostico']
        content.append({
            "type": "text",
            "text": "Perfil: " + sessao['arroba'] + "\nNicho: " + sessao['nicho'] + "\nProblemas: " + str(diag.get('problemas', [])) + "\nGere a analise completa com solucoes."
        })

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=SYSTEM_COMPLETO,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        resultado = json.loads(raw[start:end])

        return jsonify({
            'success': True, 'diagnostico': diag,
            'analise_completa': resultado, 'arroba': sessao['arroba']
        })

    except Exception as e:
        print(f"ERRO analise_completa: {str(e)}", flush=True)
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# PAGINAS DE RETORNO APOS PAGAMENTO
# ============================================================

PAGINA_BASE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITULO__</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #0a0a14 0%, #1a0a2e 100%);
    color: #fff; min-height: 100vh; padding: 40px 20px;
  }
  .container { max-width: 900px; margin: 0 auto; }
  .header { text-align: center; margin-bottom: 40px; }
  .icone { font-size: 64px; margin-bottom: 16px; }
  h1 { font-size: 32px; font-weight: 700; margin-bottom: 12px; letter-spacing: -0.5px; }
  .subtitulo { color: #b8b8c8; font-size: 16px; line-height: 1.5; max-width: 600px; margin: 0 auto; }
  .card {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(192,38,211,0.25);
    border-radius: 16px; padding: 28px; margin-bottom: 20px; backdrop-filter: blur(10px);
  }
  .card h2 {
    font-size: 14px; text-transform: uppercase; letter-spacing: 2px;
    color: #ec4899; margin-bottom: 16px; font-weight: 600;
  }
  .card p, .card li { color: #e8e8f0; line-height: 1.7; font-size: 15px; }
  .card ul { list-style: none; padding-left: 0; }
  .card li { padding: 10px 0; padding-left: 24px; position: relative; border-bottom: 1px solid rgba(255,255,255,0.05); }
  .card li:last-child { border-bottom: none; }
  .card li:before { content: '→'; position: absolute; left: 0; color: #c026d3; font-weight: 700; }
  .bio-box {
    background: linear-gradient(135deg, rgba(192,38,211,0.15), rgba(236,72,153,0.10));
    border: 1px solid rgba(192,38,211,0.4); border-radius: 12px; padding: 20px;
    font-style: italic; color: #fff; line-height: 1.6;
  }
  .ideia { background: rgba(0,0,0,0.3); border-radius: 10px; padding: 16px; margin-bottom: 12px; }
  .ideia .titulo { font-weight: 600; color: #ec4899; margin-bottom: 6px; }
  .ideia .formato { display: inline-block; background: rgba(192,38,211,0.2); color: #f0abfc; font-size: 11px; padding: 2px 8px; border-radius: 4px; margin-bottom: 8px; }
  .ideia .desc, .ideia .hook { color: #b8b8c8; font-size: 14px; margin-top: 4px; }
  .ideia .hook { color: #fff; font-style: italic; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.08); }
  .botao {
    display: inline-block; background: linear-gradient(135deg, #c026d3, #ec4899);
    color: #fff; padding: 14px 32px; border-radius: 10px; text-decoration: none;
    font-weight: 600; font-size: 15px; border: none; cursor: pointer;
    transition: transform 0.15s ease;
  }
  .botao:hover { transform: translateY(-2px); }
  .botao-secundario { background: transparent; border: 1px solid rgba(255,255,255,0.2); color: #b8b8c8; }
  .loading { text-align: center; padding: 60px 20px; }
  .spinner {
    border: 3px solid rgba(192,38,211,0.2); border-top-color: #c026d3;
    border-radius: 50%; width: 40px; height: 40px;
    animation: spin 0.8s linear infinite; margin: 0 auto 20px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .erro-box { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); padding: 20px; border-radius: 12px; }
  .footer-acoes { text-align: center; margin-top: 32px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
</style>
</head>
<body>
<div class="container">
__CONTEUDO__
</div>
</body>
</html>"""


def render_pagina(titulo, conteudo):
    return PAGINA_BASE.replace("__TITULO__", titulo).replace("__CONTEUDO__", conteudo)


@app.route('/sucesso')
def pagina_sucesso():
    session_id = request.args.get('session', '')
    conteudo = """
<div class="header">
  <div class="icone">✓</div>
  <h1>Pagamento aprovado</h1>
  <p class="subtitulo">Sua analise estrategica completa esta sendo carregada.</p>
</div>
<div id="conteudo">
  <div class="loading">
    <div class="spinner"></div>
    <p class="subtitulo">Carregando sua analise...</p>
  </div>
</div>
<div class="footer-acoes">
  <a href="/" class="botao botao-secundario">Voltar ao inicio</a>
</div>
<script>
const sessionId = '__SESSION__';
async function carregar() {
  if (!sessionId) {
    document.getElementById('conteudo').innerHTML =
      '<div class="card erro-box"><p>Sessao nao identificada. Volte ao site e refaca a analise.</p></div>';
    return;
  }
  try {
    const resp = await fetch('/api/analise-completa', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId})
    });
    const dados = await resp.json();
    if (!dados.success) throw new Error(dados.error || 'Erro ao carregar analise');
    renderizar(dados);
  } catch (e) {
    document.getElementById('conteudo').innerHTML =
      '<div class="card erro-box"><p>Nao foi possivel carregar sua analise: ' + e.message + '</p><p style="margin-top:12px; font-size:13px;">Seu pagamento foi aprovado. Entre em contato para receber sua analise manualmente.</p></div>';
  }
}
function renderizar(dados) {
  const a = dados.analise_completa || {};
  const ideias = (a.ideias_conteudo || []).map(i =>
    '<div class="ideia"><div class="formato">' + (i.formato || 'Reel') + '</div>' +
    '<div class="titulo">' + (i.titulo || '') + '</div>' +
    '<div class="desc">' + (i.descricao || '') + '</div>' +
    '<div class="hook">Hook: ' + (i.hook || '') + '</div></div>'
  ).join('');
  document.getElementById('conteudo').innerHTML =
    '<div class="card"><h2>Nova Bio Sugerida</h2><div class="bio-box">' + (a.bio_reescrita || '') + '</div></div>' +
    '<div class="card"><h2>Solucoes Estrategicas</h2><ul>' + (a.solucoes || []).map(s => '<li>' + s + '</li>').join('') + '</ul></div>' +
    '<div class="card"><h2>Pilares de Conteudo</h2><ul>' + (a.pilares_conteudo || []).map(p => '<li>' + p + '</li>').join('') + '</ul></div>' +
    '<div class="card"><h2>Ideias de Conteudo</h2>' + ideias + '</div>' +
    '<div class="card"><h2>Plano de Acao</h2><ul>' + (a.plano_acao || []).map(p => '<li>' + p + '</li>').join('') + '</ul></div>';
}
carregar();
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Pagamento Aprovado", conteudo)


@app.route('/pendente')
def pagina_pendente():
    session_id = request.args.get('session', '')
    conteudo = """
<div class="header">
  <div class="icone">⏳</div>
  <h1>Aguardando confirmacao</h1>
  <p class="subtitulo">Seu pagamento esta sendo processado. Esta pagina vai atualizar automaticamente quando for aprovado.</p>
</div>
<div class="card">
  <h2>Status</h2>
  <p id="status">Verificando pagamento...</p>
  <p style="margin-top:16px; font-size:13px; color:#888;">
    Pagamentos via PIX costumam ser aprovados em segundos. Boleto pode levar ate 2 dias uteis.
  </p>
</div>
<div class="footer-acoes">
  <a href="/" class="botao botao-secundario">Voltar ao inicio</a>
</div>
<script>
const sessionId = '__SESSION__';
let tentativas = 0;
async function verificar() {
  tentativas++;
  try {
    const resp = await fetch('/api/verificar-pagamento', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId})
    });
    const dados = await resp.json();
    if (dados.aprovado) {
      document.getElementById('status').textContent = 'Pagamento aprovado! Redirecionando...';
      setTimeout(() => window.location.href = '/sucesso?session=' + sessionId, 1500);
    } else {
      document.getElementById('status').textContent = 'Aguardando... (verificacao ' + tentativas + ')';
      if (tentativas < 60) setTimeout(verificar, 5000);
      else document.getElementById('status').textContent = 'Aguardando ha muito tempo. Voce pode fechar esta pagina e voltar depois.';
    }
  } catch (e) {
    document.getElementById('status').textContent = 'Erro ao verificar. Tentando novamente...';
    if (tentativas < 60) setTimeout(verificar, 5000);
  }
}
if (sessionId) verificar();
else document.getElementById('status').textContent = 'Sessao nao identificada.';
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Pagamento Pendente", conteudo)


@app.route('/erro')
def pagina_erro():
    conteudo = """
<div class="header">
  <div class="icone">⚠</div>
  <h1>Pagamento nao aprovado</h1>
  <p class="subtitulo">Algo deu errado com seu pagamento. Voce pode tentar novamente com outro meio.</p>
</div>
<div class="card erro-box">
  <p>Possiveis motivos: cartao recusado, saldo insuficiente, dados incorretos, ou tempo expirado.</p>
</div>
<div class="footer-acoes">
  <a href="/" class="botao">Tentar novamente</a>
</div>"""
    return render_pagina("Pagamento nao aprovado", conteudo)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
