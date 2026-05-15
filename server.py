from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import mercadopago
import anthropic
import json
import os
import uuid

app = Flask(__name__, static_folder='static')
CORS(app)

MP_ACCESS_TOKEN = "APP_USR-5014720075912185-051421-1751eba1b3378583136757a507872f74-3403743588"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

pagamentos_aprovados = set()
analises_cache = {}

SYSTEM_DIAGNOSTICO = """Você é uma estrategista de conteúdo especialista em Instagram.
Analise o perfil e gere um diagnóstico HONESTO focado apenas nos PROBLEMAS sem soluções.
Seja direta, específica e cirúrgica. Não use linguagem de coach.
Responda APENAS com JSON válido sem markdown sem backticks:
{"percepcao_inicial": "como um visitante ve o perfil nos primeiros 3 segundos", "problemas": ["problema 1", "problema 2", "problema 3", "problema 4", "problema 5"], "impacto": "o que esses problemas estao custando ao perfil", "frase_gancho": "frase curta e impactante sobre o estado atual"}"""

SYSTEM_COMPLETO = """Você é uma estrategista de conteúdo especialista em Instagram.
Gere a ANÁLISE COMPLETA com soluções, estratégia e ideias de conteúdo.
Responda APENAS com JSON válido sem markdown sem backticks:
{"bio_reescrita": "nova bio sugerida", "solucoes": ["solucao 1", "solucao 2", "solucao 3", "solucao 4", "solucao 5"], "pilares_conteudo": ["pilar 1", "pilar 2", "pilar 3"], "ideias_conteudo": [{"titulo": "titulo", "formato": "Reel", "descricao": "descricao", "hook": "hook"}], "plano_acao": ["acao 1", "acao 2", "acao 3"]}"""


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/diagnostico', methods=['POST'])
def gerar_diagnostico():
    try:
        data = request.json
        arroba = data.get('arroba', '')
        nicho = data.get('nicho', '')
        seguidores = data.get('seguidores', '')
        objetivo = data.get('objetivo', '')
        obs = data.get('obs', '')
        imagens = data.get('imagens', [])

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        content = []

        for img_data in imagens[:5]:
            if ',' in img_data:
                header, b64 = img_data.split(',', 1)
                media_type = header.split(':')[1].split(';')[0]
            else:
                b64 = img_data
                media_type = 'image/jpeg'
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})

        content.append({"type": "text", "text": f"Perfil: {arroba}\nNicho: {nicho}\nSeguidores: {seguidores}\nObjetivo: {objetivo}\n{f'Obs: {obs}' if obs else ''}\nGere o diagnostico."})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_DIAGNOSTICO,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        analise = json.loads(raw[start:end])

        session_id = str(uuid.uuid4())
        analises_cache[session_id] = {
            'arroba': arroba, 'nicho': nicho, 'seguidores': seguidores,
            'objetivo': objetivo, 'obs': obs, 'imagens': imagens, 'diagnostico': analise
        }

        return jsonify({'success': True, 'analise': analise, 'session_id': session_id})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/criar-pagamento', methods=['POST'])
def criar_pagamento():
    try:
        data = request.json
        session_id = data.get('session_id')
        base_url = request.host_url.rstrip('/')

        preference_data = {
            "items": [{"title": "Analise Estrategica Completa Instagram", "quantity": 1, "unit_price": 19.90, "currency_id": "BRL"}],
            "back_urls": {
                "success": f"{base_url}/sucesso?session={session_id}",
                "failure": f"{base_url}/erro",
                "pending": f"{base_url}/pendente"
            },
            "auto_return": "approved",
            "external_reference": session_id,
            "notification_url": f"{base_url}/api/webhook"
        }

        preference = sdk.preference().create(preference_data)
        return jsonify({'success': True, 'checkout_url': preference["response"]["sandbox_init_point"]})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if data and data.get('type') == 'payment':
            payment_id = data['data']['id']
            payment_info = sdk.payment().get(payment_id)
            if payment_info['response']['status'] == 'approved':
                pagamentos_aprovados.add(payment_info['response']['external_reference'])
    except:
        pass
    return jsonify({'status': 'ok'})


@app.route('/api/verificar-pagamento', methods=['POST'])
def verificar_pagamento():
    data = request.json
    session_id = data.get('session_id')
    payment_id = data.get('payment_id')

    if payment_id:
        try:
            payment_info = sdk.payment().get(payment_id)
            if payment_info['response']['status'] == 'approved':
                ref = payment_info['response'].get('external_reference', session_id)
                pagamentos_aprovados.add(ref)
                return jsonify({'aprovado': True, 'session_id': ref})
        except:
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
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})

        diag = sessao['diagnostico']
        content.append({"type": "text", "text": f"Perfil: {sessao['arroba']}\nNicho: {sessao['nicho']}\nProblemas: {', '.join(diag['problemas'])}\nGere a analise completa com solucoes."})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_COMPLETO,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text.strip()
        start = raw.find('{')
        end = raw.rfind('}') + 1
        resultado = json.loads(raw[start:end])

        return jsonify({'success': True, 'diagnostico': diag, 'analise_completa': resultado, 'arroba': sessao['arroba']})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
