from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import mercadopago
import anthropic
import base64
import json
import os
import uuid

app = Flask(__name__, static_folder='static')
CORS(app)
@app.route("/")
def home():
    return app.send_static_file("index.html")
# Credenciais
MP_ACCESS_TOKEN = "APP_USR-5014720075912185-051421-1751eba1b3378583136757a507872f74-3403743588"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY
)
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

# Storage simples em memória (em produção usar banco de dados)
pagamentos_aprovados = set()
analises_cache = {}

SYSTEM_DIAGNOSTICO = """Você é uma estrategista de conteúdo especialista em Instagram. 
Analise o perfil e gere um diagnóstico HONESTO focado apenas nos PROBLEMAS — sem soluções.
Seja direta, específica e cirúrgica. Não use linguagem de coach.
Responda APENAS com JSON válido sem markdown:
{
  "percepcao_inicial": "como um visitante vê o perfil nos primeiros 3 segundos",
  "problemas": ["problema 1 específico", "problema 2", "problema 3", "problema 4", "problema 5"],
  "impacto": "o que esses problemas estão custando ao perfil",
  "frase_gancho": "frase curta e impactante sobre o estado atual"
}"""

SYSTEM_COMPLETO = """Você é uma estrategista de conteúdo especialista em Instagram.
Agora gere a ANÁLISE COMPLETA com soluções, estratégia e ideias de conteúdo.
Responda APENAS com JSON válido sem markdown:
{
  "bio_reescrita": "nova bio sugerida pronta para usar",
  "solucoes": ["solução 1 para o problema 1", "solução 2", "solução 3", "solução 4", "solução 5"],
  "pilares_conteudo": ["pilar 1", "pilar 2", "pilar 3"],
  "ideias_conteudo": [
    {"titulo": "título específico", "formato": "Reel", "descricao": "o que mostrar e como", "hook": "frase de abertura pronta"},
    {"titulo": "título 2", "formato": "Carrossel", "descricao": "o que mostrar", "hook": "hook 2"},
    {"titulo": "título 3", "formato": "Reel", "descricao": "o que mostrar", "hook": "hook 3"},
    {"titulo": "título 4", "formato": "Story", "descricao": "o que mostrar", "hook": "hook 4"},
    {"titulo": "título 5", "formato": "Carrossel", "descricao": "o que mostrar", "hook": "hook 5"},
    {"titulo": "título 6", "formato": "Reel", "descricao": "o que mostrar", "hook": "hook 6"}
  ],
  "plano_acao": ["ação 1 para fazer nos próximos 7 dias", "ação 2", "ação 3"]
}"""


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
        imagens = data.get('imagens', [])  # lista de base64

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        content = []
        
        # Adiciona imagens se houver
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
            "text": f"""Perfil: {arroba}
Nicho: {nicho}
Seguidores: {seguidores}
Objetivo: {objetivo}
{f'Observações: {obs}' if obs else ''}

Gere o diagnóstico estratégico focado apenas nos problemas."""
        })

        response = client.messages.create(
           model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=SYSTEM_DIAGNOSTICO,
            messages=[{"role": "user", "content": content}]
        )

      raw = response.content[0].text
clean = raw.replace('```json', '').replace('```', '').strip()
analise = json.loads(clean)

        # Gera ID único para essa sessão
        session_id = str(uuid.uuid4())
        analises_cache[session_id] = {
            'arroba': arroba,
            'nicho': nicho,
            'seguidores': seguidores,
            'objetivo': objetivo,
            'obs': obs,
            'imagens': imagens,
            'diagnostico': analise
        }

        return jsonify({'success': True, 'analise': analise, 'session_id': session_id})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/criar-pagamento', methods=['POST'])
def criar_pagamento():
    try:
        data = request.json
        session_id = data.get('session_id')

        preference_data = {
            "items": [{
                "title": "Análise Estratégica Completa de Perfil Instagram",
                "quantity": 1,
                "unit_price": 19.90,
                "currency_id": "BRL"
            }],
            "back_urls": {
                "success": f"https://instagram-content-mapper--mayoliverarruda.replit.app/sucesso?session={session_id}",
                "failure": f"https://instagram-content-mapper--mayoliverarruda.replit.app/erro",
                "pending": f"https://instagram-content-mapper--mayoliverarruda.replit.app/pendente"
            },
            "auto_return": "approved",
            "external_reference": session_id,
            "notification_url": "https://instagram-content-mapper--mayoliverarruda.replit.app/api/webhook"
        }

        preference = sdk.preference().create(preference_data)
        init_point = preference["response"]["init_point"]
        sandbox_init_point = preference["response"]["sandbox_init_point"]

        return jsonify({
            'success': True,
            'checkout_url': sandbox_init_point,  # usar sandbox para teste
            'preference_id': preference["response"]["id"]
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if data.get('type') == 'payment':
            payment_id = data['data']['id']
            payment_info = sdk.payment().get(payment_id)
            if payment_info['response']['status'] == 'approved':
                session_id = payment_info['response']['external_reference']
                pagamentos_aprovados.add(session_id)
        return jsonify({'status': 'ok'})
    except:
        return jsonify({'status': 'ok'})


@app.route('/api/verificar-pagamento', methods=['POST'])
def verificar_pagamento():
    data = request.json
    session_id = data.get('session_id')
    payment_id = data.get('payment_id')

    # Verifica pelo payment_id do Mercado Pago
    if payment_id:
        try:
            payment_info = sdk.payment().get(payment_id)
            status = payment_info['response']['status']
            if status == 'approved':
                session_id_ref = payment_info['response'].get('external_reference', session_id)
                pagamentos_aprovados.add(session_id_ref)
                return jsonify({'aprovado': True, 'session_id': session_id_ref})
        except:
            pass

    aprovado = session_id in pagamentos_aprovados
    return jsonify({'aprovado': aprovado})


@app.route('/api/analise-completa', methods=['POST'])
def analise_completa():
    try:
        data = request.json
        session_id = data.get('session_id')

        if session_id not in pagamentos_aprovados and session_id not in analises_cache:
            return jsonify({'success': False, 'error': 'Pagamento não encontrado'}), 403

        sessao = analises_cache.get(session_id)
        if not sessao:
            return jsonify({'success': False, 'error': 'Sessão não encontrada'}), 404

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
            "text": f"""Perfil: {sessao['arroba']}
Nicho: {sessao['nicho']}
Seguidores: {sessao['seguidores']}
Objetivo: {sessao['objetivo']}

Diagnóstico já feito:
- Percepção: {diag['percepcao_inicial']}
- Problemas: {', '.join(diag['problemas'])}

Agora gere a análise COMPLETA com soluções, bio reescrita, ideias de conteúdo e plano de ação."""
        })

        response = client.messages.create(
           model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=SYSTEM_COMPLETO,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text
        clean = raw.replace('```json', '').replace('```', '').strip()
        analise_completa = json.loads(clean)

        return jsonify({
            'success': True,
            'diagnostico': diag,
            'analise_completa': analise_completa,
            'arroba': sessao['arroba']
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 8080 ))
    app.run(host="0.0.0.0", port=port)
