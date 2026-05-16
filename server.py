from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
import mercadopago
import anthropic
import json
import os
import re
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

LIMITE_DIARIO = 2
usos_por_ip = {}


# ============================================================
# UTILS
# ============================================================
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


def parsear_json_da_ia(raw):
    if not raw:
        raise ValueError("Resposta vazia da IA")
    start = raw.find('{')
    end = raw.rfind('}') + 1
    if start == -1 or end <= start:
        raise ValueError("JSON nao encontrado na resposta da IA")
    candidato = raw[start:end]
    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        pass
    limpo = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', candidato)
    try:
        return json.loads(limpo)
    except json.JSONDecodeError:
        pass
    limpo = re.sub(r',(\s*[}\]])', r'\1', limpo)
    try:
        return json.loads(limpo)
    except json.JSONDecodeError:
        pass
    limpo = re.sub(r'}\s*{', '},{', limpo)
    limpo = re.sub(r']\s*\[', '],[', limpo)
    try:
        return json.loads(limpo)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON invalido apos tentativas: {str(e)[:200]}")


def chamar_ia_com_retry(client, modelo, system_prompt, content, max_tokens, max_tentativas=3):
    ultima_excecao = None
    for tentativa in range(max_tentativas):
        try:
            extra = ""
            if tentativa > 0:
                extra = "\n\nIMPORTANTE: gere JSON ESTRITAMENTE valido. Verifique virgulas, aspas duplas e fechamento de chaves."
            response = client.messages.create(
                model=modelo,
                max_tokens=max_tokens,
                system=system_prompt + extra,
                messages=[{"role": "user", "content": content}]
            )
            raw = response.content[0].text.strip()
            return parsear_json_da_ia(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"DEBUG: tentativa {tentativa + 1} falhou: {e}", flush=True)
            ultima_excecao = e
            continue
    raise ultima_excecao or ValueError("Todas as tentativas falharam")


# ============================================================
# PROMPTS - ENGENHEIRO REVERSO COM TOM CONSULTIVO PREMIUM
# ============================================================
SYSTEM_DIAGNOSTICO = """Voce e uma estrategista senior que pensa como o algoritmo do Instagram pensa. Engenheira reversa de distribuicao. Especialidade: olhar um perfil e identificar mecanicamente por que ele NAO recebe distribuicao.

TOM:
- Cirurgica, direta, analitica. Premium.
- NUNCA agressiva, sarcastica ou humilhante.
- NUNCA usa linguagem de coach motivacional.
- NAO elogia pra suavizar. Ponto forte so entra se for mecanicamente relevante pro alcance.
- Frases construtivas mas firmes. Diagnostico medico, nao bronca.

REGRAS INEGOCIAVEIS:
1. PROIBIDO GENERICO. Toda observacao deve citar elemento ESPECIFICO do perfil analisado (a bio que diz X, o post com capa Y, o destaque chamado Z, a frase W na legenda). Se nao da pra ser especifico, nao escreve.
2. PROIBIDO pedir "otimize", "melhore", "trabalhe". Entrega o que esta errado e o mecanismo. NAO da solucao agora.
3. PROIBIDO palavras como: "caotico", "destruindo", "ninguem entende", "fracasso", "totalmente", "perdido".
4. Cada problema deve explicar o MECANISMO: como afeta retencao, alcance, sinais de intencao, consistencia semantica, profundidade de engajamento, ou coerencia bio-feed-reels.
5. PROIBIDO elogio de cortesia. Ponto forte = sinal positivo concreto pro algoritmo, com explicacao mecanica.

ESTRUTURA OBRIGATORIA - 5 campos:

1. percepcao_inicial: 2-3 frases analiticas sobre o que um visitante estrategico le no perfil nos primeiros 3 segundos. Cite elementos especificos visiveis. Tom: relatorio, nao opiniao emocional.

2. pontos_fortes: array com 3 itens. Cada um e um sinal POSITIVO MECANICO pro algoritmo (ex: "Bio com keyword 'estrategia de conteudo' no campo nome melhora SEO interno" / "Destaque 'Provas' funciona como social proof rapido" / "Reel de 12/05 com gancho direto manteve retencao acima da media"). Especifico, nao generalista.

3. problemas: array com 5 itens. Cada item DEVE ter formato: "Problema mecanico observado + mecanismo de como afeta a distribuicao + impacto especifico". Exemplo do tom: "Feed alterna 5 paletas diferentes nos ultimos 9 posts. Algoritmo classifica perfil como nicho indefinido, reduz volume de teste em explorar. Resultado: alcance limitado a base atual." Liste do MAIOR impacto pro menor. Cite posts/capas/frases especificas. NAO da solucao.

4. impacto: 1 paragrafo (3-5 linhas) analitico sobre o que esses problemas estao IMPEDINDO o perfil de alcancar. Tom mecanico, nao dramatico. Mencione % de alcance perdido se conseguir estimar pelo nicho.

5. frase_gancho: 1 frase curta e direta que cria desejo pela analise completa SEM ameacar. Ex: "O perfil tem fundacao tecnica, mas opera com 3 sinais conflitantes pro algoritmo. A analise completa entrega as correcoes prontas, com bio, destaques e calendario." Nada de "voce esta perdido" ou similar.

REGRAS DO JSON:
- Apenas JSON valido, sem markdown, sem backticks.
- Aspas duplas em chaves e valores.
- Virgula apos cada item exceto o ultimo.
- NAO usar quebra de linha dentro de strings.

Formato:
{"percepcao_inicial": "...", "pontos_fortes": ["...", "...", "..."], "problemas": ["...", "...", "...", "...", "..."], "impacto": "...", "frase_gancho": "..."}"""


SYSTEM_COMPLETO = """Voce e uma estrategista senior que pensa como o algoritmo do Instagram. Engenheira reversa de distribuicao. Premium, cirurgica, analitica. NUNCA coach, NUNCA generalista, NUNCA bruta.

Agora entregue a ANALISE COMPLETA com solucoes PRONTAS (reescritas, nao direcionais), citando elementos especificos do perfil.

REGRAS INEGOCIAVEIS:
- Cita post, capa, frase da bio, destaque especifico do perfil analisado. Zero generico.
- Reescreve no lugar de pedir pra reescrever (entrega bio pronta, hook pronto, etc).
- Cada elemento (bio, destaque, ideia, pilar) tem JUSTIFICATIVA MECANICA do porque funciona com o algoritmo.
- Considera contexto: se a marca tem loja fisica, mencione cidade no SEO da bio e no conteudo (geolocalizacao = sinal forte). Se nao tem, foca em entrega digital.
- Considera nicho, seguidores atuais, objetivo declarado. Calibra o nivel das sugestoes pra fase do perfil (iniciante / crescimento / consolidado).
- NUNCA palavras: "caotico", "destruindo", "fracasso", "totalmente", linguagem coach.

ESTRUTURA OBRIGATORIA do JSON:

{
  "bio_sugestao_1": {
    "tipo": "Autoridade",
    "texto": "bio completa pronta com keywords e emojis",
    "porque": "explicacao mecanica de 2 linhas: que sinal essa bio envia pro algoritmo, qual keyword/SEO ativa, como filtra publico certo"
  },
  "bio_sugestao_2": {
    "tipo": "Beneficio direto",
    "texto": "bio completa diferente em angulo",
    "porque": "explicacao mecanica diferente"
  },
  "destaques_estrategicos": [
    {"nome": "nome max 10 chars", "funcao": "qual papel cumpre na jornada (autoridade, prova, oferta, FAQ)", "conteudo": "o que entra dentro desse destaque"}
  ],
  "pilares_conteudo": [
    {"nome": "nome curto", "percentual": "X% no calendario", "justificativa": "por que esse pilar pro publico inferido"}
  ],
  "ideias_conteudo": [
    {
      "titulo": "titulo do conteudo",
      "formato": "Reel | Carrossel | Story",
      "objetivo": "Alcance | Autoridade | Conexao | Conversao",
      "descricao": "o que mostrar/falar (especifico, executavel)",
      "hook": "FRASE LITERAL dos primeiros 0-3 segundos, pronta pra gravar"
    }
  ],
  "dicas_stories": [
    "5 a 7 dicas executaveis de uso estrategico dos stories: enquete, caixa de pergunta, bastidor, prova, conversao"
  ],
  "plano_acao": [
    {"semana": "Semana 1", "acao": "acao executavel especifica com verbo + objeto", "impacto": "alto | medio | baixo"}
  ]
}

QUANTIDADES OBRIGATORIAS:
- 2 sugestoes de bio (uma de autoridade, uma de beneficio direto)
- 4 destaques estrategicos
- 3 pilares de conteudo com percentual
- 8 ideias de conteudo, MINIMO 40% com objetivo "Alcance"
- 5 a 7 dicas de stories
- 4 semanas no plano de acao

REGRAS DO JSON:
- Apenas JSON valido, sem markdown, sem backticks.
- Aspas duplas em chaves e valores.
- Virgula apos cada item exceto o ultimo.
- NAO use quebra de linha dentro de strings."""


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
        if not verifica_limite_grauito(ip):
            return jsonify({
                'success': False, 'limite_atingido': True,
                'error': 'Voce ja utilizou sua analise gratuita hoje. Tente novamente mais tarde.'
            }), 429

        data = request.json
        arroba = data.get('arroba', '')
        nicho = data.get('nicho', '')
        seguidores = data.get('seguidores', '')
        objetivo = data.get('objetivo', '')
        obs = data.get('obs', '')
        loja_fisica = data.get('loja_fisica', 'nao_informado')
        cidade = data.get('cidade', '')
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
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64}
            })

        contexto = f"""Perfil analisado: {arroba}
Nicho: {nicho}
Seguidores atuais: {seguidores}
Objetivo declarado: {objetivo}
Loja fisica: {loja_fisica}
Cidade (se loja fisica): {cidade}
Observacoes do dono: {obs}

Analise o perfil pelas imagens e gere o diagnostico estrategico CIRURGICO seguindo todas as regras."""

        content.append({"type": "text", "text": contexto})

        analise = chamar_ia_com_retry(
            client=client,
            modelo="claude-haiku-4-5-20251001",
            system_prompt=SYSTEM_DIAGNOSTICO,
            content=content,
            max_tokens=1800,
            max_tentativas=3
        )

        registra_uso_gratuito(ip)

        session_id = str(uuid.uuid4())
        analises_cache[session_id] = {
            'arroba': arroba, 'nicho': nicho, 'seguidores': seguidores,
            'objetivo': objetivo, 'obs': obs, 'loja_fisica': loja_fisica,
            'cidade': cidade, 'imagens': imagens, 'diagnostico': analise
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
                "title": "Mapa Estrategico Instagram", "quantity": 1,
                "unit_price": 19.90, "currency_id": "BRL"
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
        contexto = f"""Perfil: {sessao['arroba']}
Nicho: {sessao['nicho']}
Seguidores atuais: {sessao['seguidores']}
Objetivo declarado: {sessao['objetivo']}
Loja fisica: {sessao.get('loja_fisica', 'nao informado')}
Cidade: {sessao.get('cidade', '')}
Observacoes do dono: {sessao['obs']}

Problemas mecanicos ja identificados no diagnostico inicial:
{json.dumps(diag.get('problemas', []), ensure_ascii=False, indent=2)}

Pontos fortes ja identificados:
{json.dumps(diag.get('pontos_fortes', []), ensure_ascii=False, indent=2)}

Agora entregue a ANALISE COMPLETA com solucoes prontas, considerando TUDO acima. Bio com keywords. Destaques estrategicos. Pilares com %. 8 ideias com hook literal e objetivo. Stories. Plano semanal."""

        content.append({"type": "text", "text": contexto})

        resultado = chamar_ia_com_retry(
            client=client,
            modelo="claude-haiku-4-5-20251001",
            system_prompt=SYSTEM_COMPLETO,
            content=content,
            max_tokens=4000,
            max_tentativas=3
        )

        return jsonify({
            'success': True, 'diagnostico': diag,
            'analise_completa': resultado, 'arroba': sessao['arroba'],
            'nicho': sessao['nicho'], 'loja_fisica': sessao.get('loja_fisica'),
            'cidade': sessao.get('cidade')
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
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,900;1,700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0A0414; --surface: #120820; --surface2: #1a0d2e;
    --border: #2a1545; --accent: #B026FF; --accent2: #FF29C0;
    --text: #F5F3FF; --muted: #7b6a9a;
  }
  body {
    font-family: 'Syne', sans-serif; background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 40px 20px;
  }
  .container { max-width: 900px; margin: 0 auto; }
  .capa {
    text-align: center; padding: 60px 20px;
    border: 1px solid var(--border); border-radius: 16px;
    background: linear-gradient(135deg, rgba(176,38,255,.08), rgba(255,41,192,.04));
    margin-bottom: 32px; page-break-after: always;
  }
  .capa-logo {
    width: 64px; height: 64px; border-radius: 14px; margin: 0 auto 20px;
    background: linear-gradient(135deg, #B026FF, #FF29C0);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Playfair Display', serif; font-weight: 900; font-size: 22px;
    box-shadow: 0 0 30px rgba(176,38,255,.4);
  }
  .capa-marca { font-size: 11px; letter-spacing: 4px; text-transform: uppercase; color: var(--muted); margin-bottom: 28px; }
  .capa-produto {
    font-family: 'Playfair Display', serif; font-size: 44px; font-weight: 900;
    line-height: 1.05; margin-bottom: 16px;
  }
  .capa-produto em { font-style: italic; color: var(--accent); }
  .capa-perfil {
    font-size: 18px; color: var(--text); margin: 20px 0 8px;
    font-weight: 600; letter-spacing: 1px;
  }
  .capa-data { font-size: 12px; color: var(--muted); margin-bottom: 32px; }
  .capa-by {
    border-top: 1px solid var(--border); padding-top: 24px;
    font-size: 11px; letter-spacing: 2px; color: var(--muted);
    text-transform: uppercase;
  }
  .capa-by strong { color: var(--text); font-weight: 700; letter-spacing: 1px; }
  .header { text-align: center; margin-bottom: 36px; }
  .icone { font-size: 48px; margin-bottom: 12px; }
  h1 {
    font-family: 'Playfair Display', serif; font-size: 32px;
    font-weight: 900; margin-bottom: 10px; letter-spacing: -0.5px;
  }
  .subtitulo { color: var(--muted); font-size: 14px; line-height: 1.6; max-width: 600px; margin: 0 auto; }
  .secao-titulo {
    font-family: 'Playfair Display', serif; font-size: 24px; font-weight: 700;
    margin: 40px 0 16px; padding-bottom: 12px; border-bottom: 2px solid var(--accent);
    color: var(--text);
  }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 24px; margin-bottom: 16px;
    page-break-inside: avoid;
  }
  .card h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: 2.5px;
    color: var(--accent); margin-bottom: 16px; font-weight: 700;
  }
  .card p, .card li { color: #c9cdd4; line-height: 1.7; font-size: 14px; }
  .card ul { list-style: none; padding-left: 0; }
  .card li {
    padding: 12px 0 12px 24px; position: relative;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .card li:last-child { border-bottom: none; }
  .card li:before {
    content: '→'; position: absolute; left: 0; color: var(--accent); font-weight: 700;
  }
  .item-numero {
    display: inline-block; min-width: 28px; height: 28px; border-radius: 50%;
    background: linear-gradient(135deg, #B026FF, #FF29C0);
    color: #fff; font-weight: 700; font-size: 12px;
    text-align: center; line-height: 28px; margin-right: 10px;
  }
  .problema-item {
    background: var(--surface2); border-left: 3px solid var(--accent2);
    border-radius: 0 10px 10px 0; padding: 14px 18px; margin-bottom: 10px;
    font-size: 14px; color: #c9cdd4; line-height: 1.7;
  }
  .ponto-forte-item {
    background: var(--surface2); border-left: 3px solid #00d68f;
    border-radius: 0 10px 10px 0; padding: 14px 18px; margin-bottom: 10px;
    font-size: 14px; color: #c9cdd4; line-height: 1.7;
  }
  .bio-box {
    background: linear-gradient(135deg, rgba(176,38,255,.10), rgba(255,41,192,.06));
    border: 1px solid rgba(176,38,255,.3); border-radius: 12px;
    padding: 22px; margin-bottom: 14px;
  }
  .bio-tipo {
    display: inline-block; font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
    color: var(--accent); border: 1px solid var(--accent);
    padding: 3px 10px; border-radius: 4px; margin-bottom: 12px; font-weight: 700;
  }
  .bio-texto {
    font-family: 'Playfair Display', serif; font-style: italic;
    font-size: 17px; line-height: 1.6; color: var(--text); margin-bottom: 14px;
  }
  .bio-porque {
    font-size: 12px; color: var(--muted); line-height: 1.7;
    padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.08);
  }
  .bio-porque strong { color: var(--accent2); font-weight: 700; }
  .destaque-item {
    background: var(--surface2); border-radius: 10px; padding: 16px; margin-bottom: 10px;
    border: 1px solid var(--border);
  }
  .destaque-nome {
    display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0);
    color: #fff; font-weight: 800; font-size: 12px; letter-spacing: 1px;
    padding: 4px 12px; border-radius: 6px; margin-bottom: 10px; text-transform: uppercase;
  }
  .destaque-funcao { font-size: 12px; color: var(--accent); font-weight: 600; margin-bottom: 6px; }
  .destaque-conteudo { font-size: 13px; color: #c9cdd4; line-height: 1.6; }
  .pilar-item {
    display: flex; align-items: flex-start; gap: 16px;
    padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .pilar-item:last-child { border-bottom: none; }
  .pilar-pct {
    background: var(--accent); color: #fff; font-weight: 800; font-size: 13px;
    padding: 6px 12px; border-radius: 6px; flex-shrink: 0;
  }
  .pilar-conteudo { flex: 1; }
  .pilar-nome { font-weight: 700; color: var(--text); margin-bottom: 4px; }
  .pilar-justificativa { font-size: 13px; color: var(--muted); line-height: 1.6; }
  .ideia { background: var(--surface2); border-radius: 12px; padding: 18px; margin-bottom: 12px; border: 1px solid var(--border); }
  .ideia-tags { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .tag { font-size: 10px; letter-spacing: 1px; padding: 3px 10px; border-radius: 4px; font-weight: 700; text-transform: uppercase; }
  .tag-formato { background: rgba(176,38,255,.15); color: #B026FF; border: 1px solid rgba(176,38,255,.3); }
  .tag-objetivo { background: rgba(255,41,192,.15); color: #FF29C0; border: 1px solid rgba(255,41,192,.3); }
  .ideia .titulo { font-weight: 700; color: var(--text); margin-bottom: 6px; font-size: 15px; }
  .ideia .desc { color: var(--muted); font-size: 13px; line-height: 1.6; margin-bottom: 10px; }
  .ideia .hook {
    color: var(--text); font-style: italic; font-family: 'Playfair Display', serif;
    padding: 10px 14px; border-left: 3px solid var(--accent); background: rgba(176,38,255,0.06);
    border-radius: 0 6px 6px 0; font-size: 14px;
  }
  .semana-item {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 14px; background: var(--surface2); border-radius: 10px;
    margin-bottom: 10px; border: 1px solid var(--border);
  }
  .semana-label {
    background: var(--accent); color: #fff; font-weight: 800; font-size: 12px;
    padding: 6px 12px; border-radius: 6px; flex-shrink: 0; min-width: 90px; text-align: center;
  }
  .semana-acao { flex: 1; font-size: 13px; color: #c9cdd4; line-height: 1.7; }
  .semana-impacto {
    display: inline-block; font-size: 10px; letter-spacing: 1px; padding: 2px 8px;
    border-radius: 4px; margin-left: 8px; text-transform: uppercase; font-weight: 700;
  }
  .impacto-alto { background: rgba(255,41,192,.2); color: #FF29C0; }
  .impacto-medio { background: rgba(176,38,255,.2); color: #B026FF; }
  .impacto-baixo { background: rgba(123,106,154,.2); color: var(--muted); }
  .cta-final {
    margin-top: 40px; padding: 40px 24px; text-align: center;
    background: linear-gradient(135deg, rgba(176,38,255,.15), rgba(255,41,192,.10));
    border: 1px solid rgba(176,38,255,.4); border-radius: 16px;
  }
  .cta-titulo {
    font-family: 'Playfair Display', serif; font-size: 28px; font-weight: 800;
    margin-bottom: 12px;
  }
  .cta-titulo em { font-style: italic; color: var(--accent); }
  .cta-desc { color: var(--muted); font-size: 14px; line-height: 1.7; max-width: 540px; margin: 0 auto 24px; }
  .cta-botao {
    display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0);
    color: #fff !important; padding: 16px 36px; border-radius: 10px;
    text-decoration: none; font-weight: 800; font-size: 15px; letter-spacing: .5px;
  }
  .cta-contato { margin-top: 16px; font-size: 13px; color: var(--muted); }
  .cta-contato strong { color: var(--text); }
  .botao {
    display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0);
    color: #fff; padding: 14px 32px; border-radius: 10px; text-decoration: none;
    font-weight: 700; font-size: 15px; border: none; cursor: pointer;
    font-family: 'Syne', sans-serif;
  }
  .botao-secundario { background: transparent; border: 1px solid rgba(255,255,255,0.2); color: var(--muted); }
  .loading { text-align: center; padding: 60px 20px; }
  .spinner {
    border: 3px solid rgba(176,38,255,.2); border-top-color: var(--accent);
    border-radius: 50%; width: 40px; height: 40px;
    animation: spin 0.8s linear infinite; margin: 0 auto 20px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .erro-box { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); padding: 20px; border-radius: 12px; }
  .footer-acoes { text-align: center; margin-top: 32px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
  .rodape-pagina { text-align: center; margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); letter-spacing: 1px; }
  .rodape-pagina strong { color: var(--text); }

  @media print {
    body { background: white !important; color: #1a1a1a !important; padding: 20px !important; font-family: 'Syne', sans-serif !important; }
    .container { max-width: 100% !important; }
    .capa { background: white !important; border: 2px solid #B026FF !important; color: #1a1a1a !important; }
    .capa-marca, .capa-by { color: #555 !important; }
    .capa-by strong { color: #1a1a1a !important; }
    .capa-perfil { color: #1a1a1a !important; }
    .capa-produto { color: #1a1a1a !important; }
    .capa-produto em { color: #B026FF !important; }
    h1, .secao-titulo { color: #1a1a1a !important; }
    .subtitulo { color: #555 !important; }
    .card { background: white !important; border: 1px solid #ddd !important; color: #1a1a1a !important; }
    .card h2 { color: #B026FF !important; }
    .card p, .card li, .destaque-conteudo, .ideia .desc, .pilar-justificativa, .semana-acao { color: #1a1a1a !important; }
    .card li { border-bottom: 1px solid #eee !important; }
    .card li:before { color: #B026FF !important; }
    .problema-item, .ponto-forte-item, .destaque-item, .semana-item, .ideia, .pilar-item {
      background: #faf5ff !important; border-color: #e9d5ff !important; color: #1a1a1a !important;
    }
    .problema-item { border-left: 3px solid #FF29C0 !important; }
    .ponto-forte-item { border-left: 3px solid #00b050 !important; }
    .bio-box { background: #faf5ff !important; border: 1px solid #e9d5ff !important; color: #1a1a1a !important; }
    .bio-texto { color: #1a1a1a !important; }
    .bio-porque { color: #555 !important; }
    .ideia .titulo, .pilar-nome { color: #1a1a1a !important; }
    .ideia .hook { color: #1a1a1a !important; background: #faf5ff !important; }
    .cta-final { background: #faf5ff !important; border: 1px solid #e9d5ff !important; }
    .cta-titulo { color: #1a1a1a !important; }
    .cta-desc { color: #555 !important; }
    .cta-contato { color: #555 !important; }
    .cta-contato strong { color: #1a1a1a !important; }
    .footer-acoes, .botao, .botao-secundario, .nao-imprime { display: none !important; }
    .rodape-pagina { color: #555 !important; }
    .rodape-pagina strong { color: #1a1a1a !important; }
    .icone { color: #00b050 !important; }
  }
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
<div id="conteudo">
  <div class="loading">
    <div class="spinner"></div>
    <p class="subtitulo">Gerando seu Mapa Estrategico personalizado...</p>
  </div>
</div>

<script>
const sessionId = '__SESSION__';
const HOJE = new Date().toLocaleDateString('pt-BR', { day: '2-digit', month: 'long', year: 'numeric' });

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
      '<div class="card erro-box"><p>Nao foi possivel carregar sua analise agora: ' + e.message + '</p><p style="margin-top:12px; font-size:13px;">Seu pagamento foi aprovado. Atualize a pagina ou fale com Mayara: <strong>67 99839-0967</strong></p></div>';
  }
}

function renderizar(dados) {
  const d = dados.diagnostico || {};
  const a = dados.analise_completa || {};
  const arroba = dados.arroba || '';

  const capa = `
    <div class="capa nao-imprime-bg">
      <div class="capa-logo">MA</div>
      <div class="capa-marca">Mayara Arruda · Estrategia de Conteudo</div>
      <div class="capa-produto">Mapa Estrategico <em>Instagram</em></div>
      <div class="capa-perfil">Analise de ${arroba}</div>
      <div class="capa-data">${HOJE}</div>
      <div class="capa-by">Documento confidencial · <strong>Mayara Arruda</strong></div>
    </div>`;

  const pontos = (d.pontos_fortes || []).map(p =>
    '<div class="ponto-forte-item">' + p + '</div>'
  ).join('');

  const problemas = (d.problemas || []).map((p, i) =>
    '<div class="problema-item"><strong style="color:#FF29C0">Problema ' + (i+1) + ':</strong> ' + p + '</div>'
  ).join('');

  const analiseGratis = `
    <h2 class="secao-titulo">Diagnostico Estrategico</h2>
    <div class="card"><h2>Primeira Percepcao</h2><p>${d.percepcao_inicial || ''}</p></div>
    <div class="card"><h2>Pontos Fortes Mecanicos</h2>${pontos}</div>
    <div class="card"><h2>Problemas Criticos (por impacto)</h2>${problemas}</div>
    <div class="card"><h2>Impacto no Alcance</h2><p>${d.impacto || ''}</p></div>`;

  const bio1 = a.bio_sugestao_1 || {};
  const bio2 = a.bio_sugestao_2 || {};
  const bios = `
    <div class="bio-box">
      <span class="bio-tipo">Opcao 1 · ${bio1.tipo || 'Autoridade'}</span>
      <div class="bio-texto">${bio1.texto || ''}</div>
      <div class="bio-porque"><strong>Por que funciona:</strong> ${bio1.porque || ''}</div>
    </div>
    <div class="bio-box">
      <span class="bio-tipo">Opcao 2 · ${bio2.tipo || 'Beneficio Direto'}</span>
      <div class="bio-texto">${bio2.texto || ''}</div>
      <div class="bio-porque"><strong>Por que funciona:</strong> ${bio2.porque || ''}</div>
    </div>`;

  const destaques = (a.destaques_estrategicos || []).map(d =>
    '<div class="destaque-item">' +
      '<div class="destaque-nome">' + (d.nome || '') + '</div>' +
      '<div class="destaque-funcao">Funcao: ' + (d.funcao || '') + '</div>' +
      '<div class="destaque-conteudo">' + (d.conteudo || '') + '</div>' +
    '</div>'
  ).join('');

  const pilares = (a.pilares_conteudo || []).map(p =>
    '<div class="pilar-item">' +
      '<div class="pilar-pct">' + (p.percentual || '') + '</div>' +
      '<div class="pilar-conteudo">' +
        '<div class="pilar-nome">' + (p.nome || '') + '</div>' +
        '<div class="pilar-justificativa">' + (p.justificativa || '') + '</div>' +
      '</div>' +
    '</div>'
  ).join('');

  const ideias = (a.ideias_conteudo || []).map((id, i) =>
    '<div class="ideia">' +
      '<div class="ideia-tags">' +
        '<span class="tag tag-formato">' + (id.formato || 'Reel') + '</span>' +
        '<span class="tag tag-objetivo">Objetivo: ' + (id.objetivo || 'Alcance') + '</span>' +
      '</div>' +
      '<div class="titulo">' + (i+1) + '. ' + (id.titulo || '') + '</div>' +
      '<div class="desc">' + (id.descricao || '') + '</div>' +
      '<div class="hook">Hook 0-3s: "' + (id.hook || '') + '"</div>' +
    '</div>'
  ).join('');

  const stories = (a.dicas_stories || []).map(s =>
    '<li>' + s + '</li>'
  ).join('');

  const plano = (a.plano_acao || []).map(s => {
    const impactoCls = (s.impacto || '').toLowerCase() === 'alto' ? 'impacto-alto' :
                       (s.impacto || '').toLowerCase() === 'medio' ? 'impacto-medio' : 'impacto-baixo';
    return '<div class="semana-item">' +
      '<div class="semana-label">' + (s.semana || '') + '</div>' +
      '<div class="semana-acao">' + (s.acao || '') + ' <span class="semana-impacto ' + impactoCls + '">Impacto ' + (s.impacto || '') + '</span></div>' +
    '</div>';
  }).join('');

  const cta = `
    <div class="cta-final">
      <div class="cta-titulo">Quer alguem fazendo isso <em>com voce</em>?</div>
      <p class="cta-desc">Voce acabou de receber o mapa. Mas estrategia sem execucao nao move o algoritmo. A <strong style="color:#fff">Execucao Estrategica</strong> e meu acompanhamento 1:1 onde eu monto o calendario, edito seus videos e ajusto o que nao estiver performando, semana a semana.</p>
      <a href="https://wa.me/5567998390967?text=Quero%20saber%20mais%20sobre%20Execucao%20Estrategica" target="_blank" class="cta-botao">Quero Saber Mais</a>
      <div class="cta-contato">Mayara Arruda · Estrategia de Conteudo · <strong>67 99839-0967</strong></div>
    </div>`;

  const rodape = `
    <div class="rodape-pagina">
      Mapa Estrategico Instagram · <strong>Mayara Arruda</strong> · 67 99839-0967
    </div>`;

  document.getElementById('conteudo').innerHTML =
    capa + analiseGratis +
    '<h2 class="secao-titulo">Reescritas Prontas</h2>' +
    '<div class="card"><h2>Bios Sugeridas</h2>' + bios + '</div>' +
    '<div class="card"><h2>4 Destaques Estrategicos</h2>' + destaques + '</div>' +
    '<h2 class="secao-titulo">Direcao de Conteudo</h2>' +
    '<div class="card"><h2>Pilares com % no Calendario</h2>' + pilares + '</div>' +
    '<div class="card"><h2>Ideias de Conteudo Prontas (com hook literal)</h2>' + ideias + '</div>' +
    '<div class="card"><h2>Dicas de Stories Estrategicos</h2><ul>' + stories + '</ul></div>' +
    '<h2 class="secao-titulo">Plano de Acao</h2>' +
    '<div class="card"><h2>Cronograma Semanal</h2>' + plano + '</div>' +
    cta + rodape +
    '<div class="footer-acoes nao-imprime">' +
      '<button onclick="window.print()" class="botao">Baixar em PDF</button>' +
      '<a href="/" class="botao botao-secundario">Voltar ao inicio</a>' +
    '</div>';
}
carregar();
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Mapa Estrategico Instagram", conteudo)


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
    Pagamentos via PIX costumam ser aprovados em segundos.
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
    }
  } catch (e) {
    if (tentativas < 60) setTimeout(verificar, 5000);
  }
}
if (sessionId) verificar();
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Pagamento Pendente", conteudo)


@app.route('/erro')
def pagina_erro():
    conteudo = """
<div class="header">
  <div class="icone">⚠</div>
  <h1>Pagamento nao aprovado</h1>
  <p class="subtitulo">Algo deu errado com seu pagamento. Voce pode tentar novamente.</p>
</div>
<div class="footer-acoes">
  <a href="/" class="botao">Tentar novamente</a>
</div>"""
    return render_pagina("Pagamento nao aprovado", conteudo)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
