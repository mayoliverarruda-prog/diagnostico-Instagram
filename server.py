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


def get_contexto_sazonal():
    hoje = datetime.utcnow()
    mes = hoje.month
    dia = hoje.day

    def dias_ate(m, d):
        alvo = datetime(hoje.year, m, d)
        if alvo < hoje:
            alvo = datetime(hoje.year + 1, m, d)
        return (alvo - hoje).days

    proximas = [
        (dias_ate(2, 14), "Dia dos Namorados (14 de fevereiro)"),
        (dias_ate(3, 8), "Dia da Mulher (8 de marco)"),
        (dias_ate(5, 11), "Dia das Maes (segundo domingo de maio)"),
        (dias_ate(6, 12), "Dia dos Namorados (12 de junho)"),
        (dias_ate(8, 10), "Dia dos Pais (segundo domingo de agosto)"),
        (dias_ate(10, 12), "Dia das Criancas (12 de outubro)"),
        (dias_ate(11, 28), "Black Friday (ultima sexta de novembro)"),
        (dias_ate(12, 25), "Natal (25 de dezembro)"),
        (dias_ate(1, 1), "Ano Novo (1 de janeiro)"),
    ]

    datas_proximas = [(dias, nome) for dias, nome in proximas if dias <= 45]

    if mes in [12, 1, 2]:
        estacao = "verao no Brasil"
    elif mes in [3, 4, 5]:
        estacao = "outono no Brasil"
    elif mes in [6, 7, 8]:
        estacao = "inverno no Brasil"
    else:
        estacao = "primavera no Brasil"

    meses_pt = {
        1: "janeiro", 2: "fevereiro", 3: "marco", 4: "abril",
        5: "maio", 6: "junho", 7: "julho", 8: "agosto",
        9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"
    }

    texto = f"Hoje e {dia} de {meses_pt[mes]} de {hoje.year}. Estacao atual: {estacao}."
    if datas_proximas:
        urgencias = []
        for dias, nome in datas_proximas:
            if dias <= 15:
                urgencias.append(f"{nome} — URGENTE, faltam apenas {dias} dias")
            elif dias <= 30:
                urgencias.append(f"{nome} — faltam {dias} dias, momento critico para comecar")
            else:
                urgencias.append(f"{nome} — faltam {dias} dias, bom momento para se preparar")
        texto += f" Datas importantes: {'; '.join(urgencias)}."
    else:
        texto += " Nenhuma data comemorativa grande nos proximos 45 dias — foco em crescimento organico."

    return texto


def fechar_chaves_truncadas(s):
    abertas_chave = 0
    abertas_colchete = 0
    dentro_string = False
    escape = False
    for c in s:
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"':
            dentro_string = not dentro_string
            continue
        if dentro_string:
            continue
        if c == '{':
            abertas_chave += 1
        elif c == '}':
            abertas_chave -= 1
        elif c == '[':
            abertas_colchete += 1
        elif c == ']':
            abertas_colchete -= 1
    if dentro_string:
        s += '"'
    s += ']' * max(0, abertas_colchete)
    s += '}' * max(0, abertas_chave)
    return s


def consertar_virgulas_faltantes(s):
    s = re.sub(r'"\s*\n\s*"([a-zA-Z_])', r'",\n"\1', s)
    s = re.sub(r'}\s*\n\s*{', r'},\n{', s)
    s = re.sub(r']\s*\n\s*\[', r'],\n[', s)
    s = re.sub(r'"\s*\n\s+"', r'",\n"', s)
    s = re.sub(r']\s*\n\s*"([a-zA-Z_])', r'],\n"\1', s)
    s = re.sub(r'}\s*\n\s*"([a-zA-Z_])', r'},\n"\1', s)
    return s


def parsear_json_da_ia(raw):
    if not raw:
        raise ValueError("Resposta vazia da IA")
    start = raw.find('{')
    if start == -1:
        raise ValueError("JSON nao encontrado na resposta da IA")
    end = raw.rfind('}') + 1
    candidato = raw[start:end] if end > start else raw[start:]

    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        pass

    limpo = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', candidato)
    try:
        return json.loads(limpo)
    except json.JSONDecodeError:
        pass

    limpo3 = re.sub(r',(\s*[}\]])', r'\1', limpo)
    try:
        return json.loads(limpo3)
    except json.JSONDecodeError:
        pass

    limpo4 = consertar_virgulas_faltantes(limpo3)
    try:
        return json.loads(limpo4)
    except json.JSONDecodeError:
        pass

    limpo5 = fechar_chaves_truncadas(limpo4)
    try:
        return json.loads(limpo5)
    except json.JSONDecodeError:
        pass

    limpo6 = fechar_chaves_truncadas(consertar_virgulas_faltantes(limpo))
    try:
        return json.loads(limpo6)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON invalido apos tentativas: {str(e)[:200]}")


def chamar_ia_com_retry(client, modelo, system_prompt, content, max_tokens, max_tentativas=3):
    ultima_excecao = None
    for tentativa in range(max_tentativas):
        try:
            extra = ""
            if tentativa > 0:
                extra = "\n\nATENCAO: tentativa anterior falhou. Gere JSON estritamente valido. Virgulas no lugar. Aspas duplas em tudo. Feche todas as chaves e colchetes. NAO use quebra de linha dentro de strings."
            response = client.messages.create(
                model=modelo,
                max_tokens=max_tokens,
                system=system_prompt + extra,
                messages=[
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": "{"}
                ]
            )
            raw = "{" + response.content[0].text.strip()
            return parsear_json_da_ia(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"DEBUG: tentativa {tentativa + 1} falhou: {e}", flush=True)
            ultima_excecao = e
            continue
    raise ultima_excecao or ValueError("Todas as tentativas falharam")


SYSTEM_DIAGNOSTICO = """Voce e uma especialista em distribuicao algoritmica do Instagram. Sua funcao e fazer um diagnostico REAL de um perfil — como um medico que respeita o paciente mas nao esconde o que encontrou.

PRINCIPIOS INEGOCIAVEIS:

1. SEJA ESPECIFICA AO PONTO DE DOER
Nao escreva o que qualquer pessoa poderia dizer sobre qualquer perfil.
Escreva o que so voce poderia dizer sobre ESSE perfil especifico.
Cite o texto exato da bio. O nome dos destaques. O tema dos ultimos posts. A cor do feed. Se nao consegue ser especifica, nao escreve.

2. DIAGNOSTICO REAL, NAO RELATORIO EDUCADO
Se o perfil tem um problema serio, diga. Com clareza. Sem suavizar.
Se o perfil tem algo que realmente funciona, reconheca. Sem elogiar por educacao.
A frase "Voce vende estrategia mas seu proprio feed prova que nao tem uma" e o nivel de honestidade que buscamos.
Mas NUNCA seja cruel, agressiva ou humilhante. Tom de medico — firme, claro, respeitoso.

3. EXPLIQUE O MECANISMO
Nao diga apenas "falta consistencia visual". Diga o que isso causa na pratica para o crescimento do perfil.

4. CUBRA OBRIGATORIAMENTE:
- Destaques: existem? Os nomes fazem sentido para quem nunca viu o perfil? Estao organizados para guiar o visitante? Se nao existem, diga explicitamente.
- Alcance para nao-seguidores: o conteudo visivel tem potencial de chegar em quem nao segue ainda? Os videos prendem quem passa pelo feed sem seguir?
- Diferencial: o que esse perfil tem de unico que o algoritmo poderia usar a favor? Se nao tem nada que o diferencie, diga isso.
- Sazonalidade: se ha uma data comercial relevante nos proximos 30 a 45 dias e o perfil nao fez nada ainda, diga com urgencia real. Nao suavize.

5. PROIBIDO:
- Frases que qualquer IA usaria: "falta consistencia", "o feed poderia ser mais organizado", "e importante ter uma estrategia"
- Palavras em ingles ou jargao: hook, CTA, branding, KPI, insights, storytelling, copywriting, feedbacks
- Tom de coach motivacional
- Elogios por cortesia

SAZONALIDADE — use com urgencia quando relevante:
__CONTEXTO_SAZONAL__

ESTRUTURA OBRIGATORIA — 6 campos:

1. percepcao_inicial: O que um visitante desconhecido le e sente nos primeiros 3 segundos. Cite elementos especificos e reais: o texto exato da bio, o que aparece nos destaques, o que o feed transmite. Seja direta.

2. pontos_fortes: Lista com 3 itens. So entra o que REALMENTE funciona e impacta o crescimento. Cite o elemento real. Nao elogie o que e obrigacao basica.

3. problemas: Lista com 3 itens. Formato: [o que foi observado especificamente] + [como isso prejudica o crescimento ou as vendas na pratica]. Nao entregue solucao. Seja direta ao ponto de fazer a pessoa pensar "isso e exatamente o meu problema".

4. impacto: 1 paragrafo (3 a 5 linhas). O que esses problemas estao impedindo concretamente neste perfil especifico. Pode ser direto e um pouco desconfortavel. O objetivo e a pessoa sentir a urgencia de resolver.

5. oportunidade: 1 a 2 frases. Uma oportunidade real que existe AGORA — considerando o nicho, o mes e as datas proximas. Se ha uma data importante chegando e o perfil nao se preparou, diga isso com urgencia: "O Dia das Maes e em X dias e o perfil ainda nao fez nenhum post sobre o tema — cada dia perdido agora e alcance que nao volta."

6. frase_gancho: 1 frase que resume o estado atual do perfil de forma honesta e memoravel. Deve fazer a pessoa pensar "e exatamente isso". Nao pode ser generica. Nao pode ser cruel. Nivel certo: "O perfil tem presenca mas nao tem direcao — o visitante sai sem entender exatamente o que voce resolve."

REGRAS DO JSON:
Apenas JSON valido, sem markdown, sem backticks. Aspas duplas. Virgula apos cada item exceto o ultimo. NAO use quebra de linha dentro de strings.

Formato exato:
{"percepcao_inicial": "...", "pontos_fortes": ["...", "...", "..."], "problemas": ["...", "...", "..."], "impacto": "...", "oportunidade": "...", "frase_gancho": "..."}"""


SYSTEM_COMPLETO = """Voce e uma especialista em distribuicao algoritmica do Instagram. Agora entregue a ANALISE COMPLETA com solucoes prontas — nao sugestoes, nao direcoes. Solucoes prontas para executar.

PRINCIPIOS:
- Portugues direto e simples. Sem palavras em ingles. Sem jargao de marketing.
- NAO use: hook, CTA, branding, retencao, engajamento, KPI, insights, feedbacks, storytelling.
- Escreva a bio — nao diga como melhorar. Escreva ela pronta.
- Escreva a frase de abertura do video — pronta para gravar. Nao um modelo.
- Tudo especifico para esse perfil. Zero de resposta que serviria para qualquer outro perfil.
- Nos destaques: pense como um visitante que nunca viu esse perfil. O que ele precisa encontrar para confiar e agir?
- Considere o nicho, o numero de seguidores, o objetivo e a sazonalidade.

SAZONALIDADE — considere nas ideias e no plano de acao:
__CONTEXTO_SAZONAL__

ESTRUTURA OBRIGATORIA:

{
  "bio_sugestao_1": {
    "tipo": "Autoridade",
    "texto": "bio completa pronta com emojis e palavras especificas do nicho desse perfil",
    "porque": "2 linhas explicando por que essa bio funciona para esse perfil especifico"
  },
  "bio_sugestao_2": {
    "tipo": "Beneficio direto",
    "texto": "bio completa com angulo diferente — foca no resultado que o cliente tem",
    "porque": "2 linhas com logica diferente da primeira"
  },
  "destaques_estrategicos": [
    {
      "nome": "nome curto (max 10 letras, como aparece no Instagram)",
      "funcao": "qual papel cumpre na jornada do visitante — especifico: converter, gerar confianca, responder objecao, mostrar resultado",
      "conteudo": "o que entra dentro — especifico e executavel"
    }
  ],
  "pilares_conteudo": [
    {
      "nome": "nome do pilar",
      "percentual": "X% do conteudo",
      "justificativa": "por que esse percentual faz sentido para esse perfil especifico agora"
    }
  ],
  "ideias_conteudo": [
    {
      "titulo": "titulo especifico — nao generico",
      "formato": "Video curto | Carrossel | Story",
      "objetivo": "Alcance | Autoridade | Conexao | Venda",
      "descricao": "o que mostrar ou falar — executavel, especifico para esse nicho",
      "frase_abertura": "FRASE LITERAL para comecar — pronta para gravar ou digitar, nao um modelo"
    }
  ],
  "dicas_stories": [
    "dica especifica e executavel de como usar os stories para esse perfil e esse nicho"
  ],
  "plano_acao": [
    {
      "semana": "Semana 1",
      "acao": "acao especifica com verbo + o que fazer + onde fazer",
      "impacto": "alto | medio | baixo"
    }
  ]
}

QUANTIDADES OBRIGATORIAS:
- 2 sugestoes de bio
- 4 destaques estrategicos
- 3 pilares com percentual
- 8 ideias de conteudo, pelo menos 3 com objetivo Alcance
- 5 a 7 dicas de stories
- 4 semanas no plano de acao

REGRAS DO JSON:
Apenas JSON valido, sem markdown, sem backticks. Aspas duplas. Virgula apos cada item exceto o ultimo. NAO use quebra de linha dentro de strings."""


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

        contexto_sazonal = get_contexto_sazonal()
        system_diag = SYSTEM_DIAGNOSTICO.replace('__CONTEXTO_SAZONAL__', contexto_sazonal)

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
Nicho ou segmento: {nicho}
Numero de seguidores: {seguidores}
Objetivo do dono: {objetivo}
Tem loja fisica: {loja_fisica}
Cidade (se loja fisica): {cidade}
Observacoes do dono: {obs}

Analise as imagens e gere o diagnostico seguindo todas as regras. Seja especifica. Cite elementos reais do perfil."""

        content.append({"type": "text", "text": contexto})

        analise = chamar_ia_com_retry(
            client=client,
            modelo="claude-haiku-4-5-20251001",
            system_prompt=system_diag,
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
                'success': False, 'error': 'Erro ao criar preferencia de pagamento',
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

        contexto_sazonal = get_contexto_sazonal()
        system_comp = SYSTEM_COMPLETO.replace('__CONTEXTO_SAZONAL__', contexto_sazonal)

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
Numero de seguidores: {sessao['seguidores']}
Objetivo: {sessao['objetivo']}
Tem loja fisica: {sessao.get('loja_fisica', 'nao informado')}
Cidade: {sessao.get('cidade', '')}
Observacoes: {sessao['obs']}

Problemas encontrados no diagnostico:
{json.dumps(diag.get('problemas', []), ensure_ascii=False, indent=2)}

Pontos positivos ja identificados:
{json.dumps(diag.get('pontos_fortes', []), ensure_ascii=False, indent=2)}

Agora entregue a analise completa com todas as solucoes prontas para executar. Tudo especifico para esse perfil."""

        content.append({"type": "text", "text": contexto})

        resultado = chamar_ia_com_retry(
            client=client,
            modelo="claude-haiku-4-5-20251001",
            system_prompt=system_comp,
            content=content,
            max_tokens=6000,
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


PAGINA_BASE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITULO__</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,900;1,700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root { --bg: #0A0414; --surface: #120820; --surface2: #1a0d2e; --border: #2a1545; --accent: #B026FF; --accent2: #FF29C0; --text: #F5F3FF; --muted: #7b6a9a; }
  body { font-family: 'Syne', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 40px 20px; }
  .container { max-width: 900px; margin: 0 auto; }
  .capa { text-align: center; padding: 60px 20px; border: 1px solid var(--border); border-radius: 16px; background: linear-gradient(135deg, rgba(176,38,255,.08), rgba(255,41,192,.04)); margin-bottom: 32px; }
  .capa-logo { width: 64px; height: 64px; border-radius: 14px; margin: 0 auto 20px; background: linear-gradient(135deg, #B026FF, #FF29C0); display: flex; align-items: center; justify-content: center; font-family: 'Playfair Display', serif; font-weight: 900; font-size: 22px; color: #fff; box-shadow: 0 0 30px rgba(176,38,255,.4); }
  .capa-marca { font-size: 11px; letter-spacing: 4px; text-transform: uppercase; color: var(--muted); margin-bottom: 28px; }
  .capa-produto { font-family: 'Playfair Display', serif; font-size: 44px; font-weight: 900; line-height: 1.05; margin-bottom: 16px; }
  .capa-produto em { font-style: italic; color: var(--accent); }
  .capa-perfil { font-size: 18px; color: var(--text); margin: 20px 0 8px; font-weight: 600; }
  .capa-data { font-size: 12px; color: var(--muted); margin-bottom: 32px; }
  .capa-by { border-top: 1px solid var(--border); padding-top: 24px; font-size: 11px; letter-spacing: 2px; color: var(--muted); text-transform: uppercase; }
  .capa-by strong { color: var(--text); }
  .secao-titulo { font-family: 'Playfair Display', serif; font-size: 24px; font-weight: 700; margin: 40px 0 16px; padding-bottom: 12px; border-bottom: 2px solid var(--accent); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px; margin-bottom: 16px; }
  .card h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 2.5px; color: var(--accent); margin-bottom: 16px; font-weight: 700; }
  .card p, .card li { color: #c9cdd4; line-height: 1.7; font-size: 14px; }
  .card ul { list-style: none; }
  .card li { padding: 12px 0 12px 24px; position: relative; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .card li:last-child { border-bottom: none; }
  .card li:before { content: "->"; position: absolute; left: 0; color: var(--accent); font-weight: 700; }
  .problema-item { background: var(--surface2); border-left: 3px solid var(--accent2); border-radius: 0 10px 10px 0; padding: 14px 18px; margin-bottom: 10px; font-size: 14px; color: #c9cdd4; line-height: 1.7; }
  .ponto-forte-item { background: var(--surface2); border-left: 3px solid #00d68f; border-radius: 0 10px 10px 0; padding: 14px 18px; margin-bottom: 10px; font-size: 14px; color: #c9cdd4; line-height: 1.7; }
  .bio-box { background: linear-gradient(135deg, rgba(176,38,255,.10), rgba(255,41,192,.06)); border: 1px solid rgba(176,38,255,.3); border-radius: 12px; padding: 22px; margin-bottom: 14px; }
  .bio-tipo { display: inline-block; font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--accent); border: 1px solid var(--accent); padding: 3px 10px; border-radius: 4px; margin-bottom: 12px; font-weight: 700; }
  .bio-texto { font-family: 'Playfair Display', serif; font-style: italic; font-size: 17px; line-height: 1.6; color: var(--text); margin-bottom: 14px; }
  .bio-porque { font-size: 12px; color: var(--muted); line-height: 1.7; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.08); }
  .bio-porque strong { color: var(--accent2); }
  .destaque-item { background: var(--surface2); border-radius: 10px; padding: 16px; margin-bottom: 10px; border: 1px solid var(--border); }
  .destaque-nome { display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0); color: #fff; font-weight: 800; font-size: 12px; padding: 4px 12px; border-radius: 6px; margin-bottom: 10px; text-transform: uppercase; }
  .destaque-funcao { font-size: 12px; color: var(--accent); font-weight: 600; margin-bottom: 6px; }
  .destaque-conteudo { font-size: 13px; color: #c9cdd4; line-height: 1.6; }
  .pilar-item { display: flex; align-items: flex-start; gap: 16px; padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .pilar-item:last-child { border-bottom: none; }
  .pilar-pct { background: var(--accent); color: #fff; font-weight: 800; font-size: 13px; padding: 6px 12px; border-radius: 6px; flex-shrink: 0; }
  .pilar-nome { font-weight: 700; color: var(--text); margin-bottom: 4px; }
  .pilar-justificativa { font-size: 13px; color: var(--muted); line-height: 1.6; }
  .ideia { background: var(--surface2); border-radius: 12px; padding: 18px; margin-bottom: 12px; border: 1px solid var(--border); }
  .ideia-tags { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .tag { font-size: 10px; padding: 3px 10px; border-radius: 4px; font-weight: 700; text-transform: uppercase; }
  .tag-formato { background: rgba(176,38,255,.15); color: #B026FF; border: 1px solid rgba(176,38,255,.3); }
  .tag-objetivo { background: rgba(255,41,192,.15); color: #FF29C0; border: 1px solid rgba(255,41,192,.3); }
  .ideia .titulo { font-weight: 700; color: var(--text); margin-bottom: 6px; font-size: 15px; }
  .ideia .desc { color: var(--muted); font-size: 13px; line-height: 1.6; margin-bottom: 10px; }
  .ideia .frase-abertura { color: var(--text); font-style: italic; font-family: 'Playfair Display', serif; padding: 10px 14px; border-left: 3px solid var(--accent); background: rgba(176,38,255,0.06); border-radius: 0 6px 6px 0; font-size: 14px; }
  .semana-item { display: flex; align-items: flex-start; gap: 14px; padding: 14px; background: var(--surface2); border-radius: 10px; margin-bottom: 10px; border: 1px solid var(--border); }
  .semana-label { background: var(--accent); color: #fff; font-weight: 800; font-size: 12px; padding: 6px 12px; border-radius: 6px; flex-shrink: 0; min-width: 90px; text-align: center; }
  .semana-acao { flex: 1; font-size: 13px; color: #c9cdd4; line-height: 1.7; }
  .semana-impacto { display: inline-block; font-size: 10px; padding: 2px 8px; border-radius: 4px; margin-left: 8px; text-transform: uppercase; font-weight: 700; }
  .impacto-alto { background: rgba(255,41,192,.2); color: #FF29C0; }
  .impacto-medio { background: rgba(176,38,255,.2); color: #B026FF; }
  .impacto-baixo { background: rgba(123,106,154,.2); color: var(--muted); }
  .cta-final { margin-top: 40px; padding: 40px 24px; text-align: center; background: linear-gradient(135deg, rgba(176,38,255,.15), rgba(255,41,192,.10)); border: 1px solid rgba(176,38,255,.4); border-radius: 16px; }
  .cta-titulo { font-family: 'Playfair Display', serif; font-size: 28px; font-weight: 800; margin-bottom: 12px; }
  .cta-titulo em { font-style: italic; color: var(--accent); }
  .cta-desc { color: var(--muted); font-size: 14px; line-height: 1.7; max-width: 540px; margin: 0 auto 24px; }
  .cta-botao { display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0); color: #fff !important; padding: 16px 36px; border-radius: 10px; text-decoration: none; font-weight: 800; font-size: 15px; }
  .cta-contato { margin-top: 16px; font-size: 13px; color: var(--muted); }
  .cta-contato strong { color: var(--text); }
  .botao { display: inline-block; background: linear-gradient(135deg, #B026FF, #FF29C0); color: #fff; padding: 14px 32px; border-radius: 10px; text-decoration: none; font-weight: 700; font-size: 15px; border: none; cursor: pointer; font-family: 'Syne', sans-serif; }
  .botao-sec { background: transparent; border: 1px solid rgba(255,255,255,0.2); color: var(--muted); }
  .loading { text-align: center; padding: 60px 20px; }
  .spinner { border: 3px solid rgba(176,38,255,.2); border-top-color: var(--accent); border-radius: 50%; width: 40px; height: 40px; animation: spin 0.8s linear infinite; margin: 0 auto 20px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .footer-acoes { text-align: center; margin-top: 32px; display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
  .rodape { text-align: center; margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); }
  .rodape strong { color: var(--text); }
  @media print { .footer-acoes, .nao-imprime { display: none !important; } body { background: white !important; color: #1a1a1a !important; } }
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
    <p style="color:#7b6a9a;font-size:14px;">Gerando seu Mapa Estrategico personalizado...</p>
  </div>
</div>
<script>
const sessionId = '__SESSION__';
const HOJE = new Date().toLocaleDateString('pt-BR', {day:'2-digit',month:'long',year:'numeric'});

async function carregar() {
  if (!sessionId) {
    document.getElementById('conteudo').innerHTML = '<div class="card"><p>Sessao nao identificada. Volte ao site e refaca a analise.</p></div>';
    return;
  }
  try {
    const resp = await fetch('/api/analise-completa', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: sessionId})
    });
    const dados = await resp.json();
    if (!dados.success) throw new Error(dados.error || 'Erro ao carregar');
    renderizar(dados);
  } catch(e) {
    document.getElementById('conteudo').innerHTML =
      '<div class="card"><p>Nao foi possivel carregar sua analise: ' + e.message + '</p><p style="margin-top:12px;font-size:13px;">Seu pagamento foi aprovado. Atualize a pagina ou fale com Mayara: <strong>67 99839-0967</strong></p></div>';
  }
}

function renderizar(dados) {
  const d = dados.diagnostico || {};
  const a = dados.analise_completa || {};
  const arroba = dados.arroba || '';

  const capa = '<div class="capa"><div class="capa-logo">MA</div><div class="capa-marca">Mayara Arruda - Estrategia de Conteudo</div><div class="capa-produto">Mapa Estrategico <em>Instagram</em></div><div class="capa-perfil">Analise de ' + arroba + '</div><div class="capa-data">' + HOJE + '</div><div class="capa-by">Documento confidencial - <strong>Mayara Arruda</strong></div></div>';

  const pontos = (d.pontos_fortes||[]).map(p=>'<div class="ponto-forte-item">'+p+'</div>').join('');
  const problemas = (d.problemas||[]).map((p,i)=>'<div class="problema-item"><strong style="color:#FF29C0">Problema '+(i+1)+':</strong> '+p+'</div>').join('');

  const bio1 = a.bio_sugestao_1||{};
  const bio2 = a.bio_sugestao_2||{};
  const bios =
    '<div class="bio-box"><span class="bio-tipo">Opcao 1 - '+(bio1.tipo||'Autoridade')+'</span><div class="bio-texto">'+(bio1.texto||'')+'</div><div class="bio-porque"><strong>Por que funciona:</strong> '+(bio1.porque||'')+'</div></div>' +
    '<div class="bio-box"><span class="bio-tipo">Opcao 2 - '+(bio2.tipo||'Beneficio Direto')+'</span><div class="bio-texto">'+(bio2.texto||'')+'</div><div class="bio-porque"><strong>Por que funciona:</strong> '+(bio2.porque||'')+'</div></div>';

  const destaques = (a.destaques_estrategicos||[]).map(d=>'<div class="destaque-item"><div class="destaque-nome">'+(d.nome||'')+'</div><div class="destaque-funcao">Funcao: '+(d.funcao||'')+'</div><div class="destaque-conteudo">'+(d.conteudo||'')+'</div></div>').join('');

  const pilares = (a.pilares_conteudo||[]).map(p=>'<div class="pilar-item"><div class="pilar-pct">'+(p.percentual||'')+'</div><div><div class="pilar-nome">'+(p.nome||'')+'</div><div class="pilar-justificativa">'+(p.justificativa||'')+'</div></div></div>').join('');

  const ideias = (a.ideias_conteudo||[]).map((id,i)=>'<div class="ideia"><div class="ideia-tags"><span class="tag tag-formato">'+(id.formato||'Video curto')+'</span><span class="tag tag-objetivo">Objetivo: '+(id.objetivo||'Alcance')+'</span></div><div class="titulo">'+(i+1)+'. '+(id.titulo||'')+'</div><div class="desc">'+(id.descricao||'')+'</div><div class="frase-abertura">Frase de abertura: "'+(id.frase_abertura||id.hook||'')+'"</div></div>').join('');

  const stories = (a.dicas_stories||[]).map(s=>'<li>'+s+'</li>').join('');

  const plano = (a.plano_acao||[]).map(s=>{
    const cls=(s.impacto||'').toLowerCase()==='alto'?'impacto-alto':(s.impacto||'').toLowerCase()==='medio'?'impacto-medio':'impacto-baixo';
    return '<div class="semana-item"><div class="semana-label">'+(s.semana||'')+'</div><div class="semana-acao">'+(s.acao||'')+' <span class="semana-impacto '+cls+'">Impacto '+(s.impacto||'')+'</span></div></div>';
  }).join('');

  document.getElementById('conteudo').innerHTML =
    capa +
    '<h2 class="secao-titulo">Diagnostico Estrategico</h2>' +
    '<div class="card"><h2>Primeira Percepcao do Perfil</h2><p>'+(d.percepcao_inicial||'')+'</p></div>' +
    '<div class="card"><h2>O que ja esta funcionando</h2>'+pontos+'</div>' +
    '<div class="card"><h2>O que precisa melhorar</h2>'+problemas+'</div>' +
    '<div class="card"><h2>O que isso esta impedindo</h2><p>'+(d.impacto||'')+'</p></div>' +
    '<h2 class="secao-titulo">Solucoes Prontas</h2>' +
    '<div class="card"><h2>Sugestoes de Bio</h2>'+bios+'</div>' +
    '<div class="card"><h2>Destaques do Perfil</h2>'+destaques+'</div>' +
    '<h2 class="secao-titulo">Plano de Conteudo</h2>' +
    '<div class="card"><h2>Pilares de Conteudo</h2>'+pilares+'</div>' +
    '<div class="card"><h2>Ideias de Conteudo Prontas</h2>'+ideias+'</div>' +
    '<div class="card"><h2>Como Usar os Stories</h2><ul>'+stories+'</ul></div>' +
    '<h2 class="secao-titulo">Plano de Acao</h2>' +
    '<div class="card"><h2>O que fazer semana a semana</h2>'+plano+'</div>' +
    '<div class="cta-final"><div class="cta-titulo">Quer alguem fazendo isso <em>com voce</em>?</div><p class="cta-desc">Voce tem o mapa. Falta executar. A Execucao Estrategica e o acompanhamento 1:1 onde eu monto o calendario, ajusto o que nao performa e fico do seu lado semana a semana.</p><a href="https://wa.me/5567998390967?text=Quero%20saber%20mais%20sobre%20Execucao%20Estrategica" target="_blank" class="cta-botao">Quero Saber Mais</a><div class="cta-contato">Mayara Arruda - Estrategia de Conteudo - <strong>67 99839-0967</strong></div></div>' +
    '<div class="rodape">Mapa Estrategico Instagram - <strong>Mayara Arruda</strong> - 67 99839-0967</div>' +
    '<div class="footer-acoes nao-imprime"><button onclick="window.print()" class="botao">Baixar em PDF</button><a href="/" class="botao botao-sec">Voltar ao inicio</a></div>';
}
carregar();
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Mapa Estrategico Instagram", conteudo)


@app.route('/pendente')
def pagina_pendente():
    session_id = request.args.get('session', '')
    conteudo = """
<div style="text-align:center;padding:60px 20px;">
  <div style="font-size:48px;margin-bottom:16px;">⏳</div>
  <h1 style="font-family:'Playfair Display',serif;font-size:28px;margin-bottom:12px;">Aguardando confirmacao</h1>
  <p style="color:#7b6a9a;font-size:14px;margin-bottom:24px;">Seu pagamento esta sendo processado. Esta pagina atualiza automaticamente.</p>
  <p id="status" style="color:#7b6a9a;font-size:13px;">Verificando pagamento...</p>
</div>
<script>
const sessionId = '__SESSION__';
let tentativas = 0;
async function verificar() {
  tentativas++;
  try {
    const resp = await fetch('/api/verificar-pagamento', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: sessionId})
    });
    const dados = await resp.json();
    if (dados.aprovado) {
      document.getElementById('status').textContent = 'Pagamento aprovado! Redirecionando...';
      setTimeout(()=>window.location.href='/sucesso?session='+sessionId, 1500);
    } else {
      document.getElementById('status').textContent = 'Aguardando... (verificacao '+tentativas+')';
      if (tentativas < 60) setTimeout(verificar, 5000);
    }
  } catch(e) { if (tentativas < 60) setTimeout(verificar, 5000); }
}
if (sessionId) verificar();
</script>"""
    conteudo = conteudo.replace("__SESSION__", session_id)
    return render_pagina("Pagamento Pendente", conteudo)


@app.route('/erro')
def pagina_erro():
    conteudo = """
<div style="text-align:center;padding:60px 20px;">
  <div style="font-size:48px;margin-bottom:16px;">⚠</div>
  <h1 style="font-family:'Playfair Display',serif;font-size:28px;margin-bottom:12px;">Pagamento nao aprovado</h1>
  <p style="color:#7b6a9a;font-size:14px;margin-bottom:24px;">Algo deu errado. Voce pode tentar novamente.</p>
  <a href="/" style="display:inline-block;background:linear-gradient(135deg,#B026FF,#FF29C0);color:#fff;padding:14px 32px;border-radius:10px;text-decoration:none;font-weight:700;">Tentar novamente</a>
</div>"""
    return render_pagina("Pagamento nao aprovado", conteudo)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
