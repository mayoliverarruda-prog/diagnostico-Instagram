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
# Estrutura: { "ip": { "data": "2026-05-15", "count": 1 } }
usos_por_ip = {}


def get_client_ip():
    """Pega IP real do usuario (Railway usa proxy, IP vem em X-Forwarded-For)."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'desconhecido'


def verifica_limite_grauito(ip):
    """Retorna True se o IP ainda pode fazer analise gratuita."""
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    registro = usos_por_ip.get(ip)
    if not registro or registro.get('data') != hoje:
        return True
    return registro.get('count', 0) < LIMITE_DIARIO


def registra_uso_gratuito(ip):
    """Incrementa contador do IP no dia atual."""
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    registro = usos_por_ip.get(ip)
    if not registro or registro.get('data') != hoje:
        usos_por_ip[ip] = {'data': hoje, 'count': 1}
    else:
        usos_por_ip[ip]['count'] = registro.get('count', 0) + 1


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
5. frase_gancho: uma frase curta que cria curiosidade pela analise completa, SEM ameacar. Exemplo de bom tom: "O perfil tem fundacao, mas ainda nao comunica posicionamento de forma clara. A analise completa mostra exatamente o que ajustar."

NAO entregue solucoes nesta versao. Apenas diagnostico estrategico.

Responda APENAS com JSON valido sem markdown sem backticks:
{"percepcao_inicial": "...", "pontos_fortes": ["...", "..."], "problemas": ["...", "...", "..."], "impacto": "...", "frase_gancho": "..."}"""


SYSTEM_COMPLETO = """Voce e uma consultora estrategica senior de Instagram. Agora entregue a ANALISE COMPLETA com solucoes praticas, estrategia clara e ideias de conteudo aplicaveis. Tom: profissional, consultivo, premium, construtivo. Sem linguagem de coach.

Responda APENAS com JSON valido sem markdown sem backticks: {"bio_reescrita": "nova bio sugerida", "solucoes": ["solucao 1", "solucao 2", "solucao 3", "solucao 4", "solucao 5"], "pilares_conteudo": ["pilar 1", "pilar 2", "pilar 3"], "ideias_conteudo": [{"titulo": "titulo", "formato": "Reel", "descricao": "descricao", "hook": "hook"}], "plano_acao": ["acao 1", "acao 2", "acao 3"]}"""


# ============================================================
# UTILS
# ============================================================
def get_base_url():
    """URL base sempre com https (Railway usa proxy http internamente)."""
    proto = request.headers.get('X-Forwarded-Proto', 'https')
    host = request.headers.get('X-Forwarded-Host') or request.host
    if proto != 'https':
        proto = 'https'
    return f"{proto}://{host}"


# ============================================================
# ROTAS
# ============================================================
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/diagnostico', methods=['POST'])
def gerar_diagnostico():
    try:
        # --- LIMITACAO POR IP ---
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

        print(f"DEBUG: resposta recebida: {response.content[0].text[:200]}", flush=True)

        raw = response.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        analise = json.loads(raw[start:end])

        print("DEBUG: JSON parsed com sucesso", flush=True)

        # Registra uso APENAS apos sucesso (se der erro, nao gasta a cota)
        registra_uso_gratuito(ip)
        print(f"DEBUG: IP {ip} usou cota, agora em {usos_por_ip[ip]}", flush=True)

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
            return jsonify({
                'success': False,
                'error': 'MP_ACCESS_TOKEN nao configurado no servidor'
            }), 500

        data = request.json or {}
        session_id = data.get('session_id', str(uuid.uuid4()))
        base_url = get_base_url()

        is_test = MP_ACCESS_TOKEN.startswith("TEST-")
        print(f"DEBUG MP: token tipo = {'TEST' if is_test else 'APP_USR'}", flush=True)
        print(f"DEBUG MP: base_url = {base_url}", flush=True)

        preference_data = {
            "items": [{
                "title": "Analise Estrategica Completa Instagram",
                "quantity": 1,
                "unit_price": 19.90,
                "currency_id": "BRL"
            }],
            "back_urls": {
                "success": base_url + "/sucesso?session=" + session_id,
                "failure": base_url + "/erro",
                "pending": base_url + "/pendente"
            },
            "auto_return": "approved",
            "external_reference": session_id,
            "notification_url": base_url + "/api/webhook",
            "binary_mode": True
        }

        print(f"DEBUG MP: criando preference...", flush=True)
        result = sdk.preference().create(preference_data)
        status = result.get("status", 0)
        response_body = result.get("response", {}) or {}

        print(f"DEBUG MP: status={status}", flush=True)
        print(f"DEBUG MP: response={response_body}", flush=True)

        if status >= 400 or "id" not in response_body:
            return jsonify({
                'success': False,
                'error': 'Mercado Pago rejeitou a preferencia',
                'mp_status': status,
                'mp_message': response_body.get('message', 'sem mensagem'),
                'mp_response': response_body
            }), 502

        if is_test:
            checkout_url = response_body.get("sandbox_init_point") or response_body.get("init_point")
        else:
            checkout_url = response_body.get("init_point")

        if not checkout_url:
            return jsonify({
                'success': False,
                'error': 'MP nao retornou URL de checkout',
                'mp_response': response_body
            }), 502

        print(f"DEBUG MP: checkout_url = {checkout_url}", flush=True)

        return jsonify({
            'success': True,
            'checkout_url': checkout_url,
            'preference_id': response_body.get("id"),
            'session_id': session_id
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
                print(f"DEBUG: pagamento aprovado {payment_info['response']['external_reference']}", flush=True)
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
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64
                }
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
            'success': True,
            'diagnostico': diag,
            'analise_completa': resultado,
            'arroba': sessao['arroba']
        })

    except Exception as e:
        print(f"ERRO analise_completa: {str(e)}", flush=True)
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
