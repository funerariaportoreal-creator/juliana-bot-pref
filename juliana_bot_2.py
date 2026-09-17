"""
Juliana Lima — bot de vendas do PREF (Previdência Familiar / GFSF)
====================================================================

Webhook para WhatsApp Cloud API (Meta), usando a API da Anthropic com
tool use (function calling) pra eliminar de vez o risco de link de
pagamento errado/truncado — em vez do modelo digitar a URL, ele só
pede pra ferramenta `enviar_link_pagamento` mandar o link certo, e o
código garante que é sempre a string exata cadastrada.

REQUISITOS
    pip install flask anthropic requests

VARIÁVEIS DE AMBIENTE OBRIGATÓRIAS
    ANTHROPIC_API_KEY        chave da API da Anthropic (nunca hardcode)
    WHATSAPP_TOKEN           token de acesso da WhatsApp Cloud API (Meta)
    WHATSAPP_PHONE_ID        ID do número de telefone configurado na Meta
    WHATSAPP_VERIFY_TOKEN    token que você escolhe pra validar o webhook
    PLANTAO_WHATSAPP         número do plantão, formato internacional (ex: 5524999249490)
    PLANTAO_ALERT_URL        (opcional) webhook interno pra notificar a equipe
                             em caso de escalada (Slack, e-mail, etc.)

Isso É UM PONTO DE PARTIDA, não um sistema de produção completo. Antes
de rodar com tráfego real, resolva pelo menos:
    - Persistência de conversa em banco de dados (aqui é só um dict em
      memória — reinicia o processo, perde tudo)
    - Fila/retry de mensagens (WhatsApp pode reenviar webhooks)
    - Logging estruturado e monitoramento de erros
    - Rate limiting e proteção contra abuso
"""

import os
import json
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
import anthropic
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("juliana_bot")

app = Flask(__name__)
client = anthropic.Anthropic(http_client=httpx.Client(proxies=None)) # if ANTHROPIC_API_KEY do ambiente automaticamente
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_PHONE_ID = os.environ["WHATSAPP_PHONE_ID"]
WHATSAPP_VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
PLANTAO_WHATSAPP = os.environ.get("PLANTAO_WHATSAPP", "5524999249490")
PLANTAO_ALERT_URL = os.environ.get("PLANTAO_ALERT_URL")  # opcional

MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Fonte única de verdade dos links de pagamento — o modelo NUNCA escreve
# esses valores, só escolhe o nome do plano; o código faz o resto.
# ---------------------------------------------------------------------------
PLAN_LINKS = {
    "Migração": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=3cfd3609-2cc6-4a1f-a449-90d09b12c758",
    "Casal": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=c6fe3866-7742-4c2c-b8a4-30bbe1fcfeb2",
    "Individual": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=13661e9d-a5ca-4608-ba2b-d4a946a2b101",
    "Familiar": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=5e4752cf-9e9e-486f-ada5-7f6c2201ba57",
    "Mais": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=3584e905-0dd9-4b9e-9891-0dcc4e73c069",
    "Ideal": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=9a3f5a73-77b5-4f2d-997d-32bb3e18f2a5",
    "Individual c/ Jazigo": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=54aea747-49c7-4516-a32a-8292e912bfac",
    "Solução Total": "https://pref.tenex.com.br/contratar/previdencia_familiar?plano=8ad5373b-c1a5-45a9-a9b8-14c0a927ed0e",
}

with open(os.path.join(os.path.dirname(__file__), "system_prompt_for_script.txt"), encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

TOOLS = [
    {
        "name": "enviar_link_pagamento",
        "description": (
            "Envia o link de pagamento correto do Tenx pro plano que o cliente "
            "confirmou. O sistema busca a URL exata cadastrada — você só escolhe "
            "o nome do plano, nunca digite a URL você mesma."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plano": {
                    "type": "string",
                    "enum": list(PLAN_LINKS.keys()),
                    "description": "Nome exato do plano confirmado pelo cliente.",
                }
            },
            "required": ["plano"],
        },
    },
    {
        "name": "escalar_atendimento_humano",
        "description": (
            "Interrompe o atendimento automático e aciona um humano. Use "
            "SEMPRE que houver menção a óbito recente, luto em andamento ou "
            "urgência agora, ou quando a pergunta estiver fora do escopo de "
            "vendas de plano (jurídico, reclamação, algo que você não sabe)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "enum": ["emergência", "fora_do_escopo"],
                    "description": "Categoria da escalada.",
                },
                "resumo": {
                    "type": "string",
                    "description": "Resumo curto da situação, pra equipe humana entender o contexto rapidamente.",
                },
            },
            "required": ["motivo", "resumo"],
        },
    },
]

# ---------------------------------------------------------------------------
# Estado de conversa — EM MEMÓRIA. Trocar por Redis/Postgres antes de
# produção com volume real; um restart do processo apaga tudo.
# ---------------------------------------------------------------------------
conversations: dict[str, list[dict]] = {}


def get_history(phone: str) -> list[dict]:
    if phone not in conversations:
        conversations[phone] = [
            {"role": "user", "content": "(início da conversa)"},
            {
                "role": "assistant",
                "content": (
                    "Oi, tudo bem? Aqui é a Juliana, da Previdência Familiar. "
                    "Posso te ajudar a achar o plano ideal pra proteger sua "
                    "família. Como você se chama?"
                ),
            },
        ]
    return conversations[phone]


def send_whatsapp_message(phone: str, text: str) -> None:
    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code >= 300:
        log.error("Falha ao enviar mensagem WhatsApp: %s %s", resp.status_code, resp.text)


def notify_plantao(motivo: str, resumo: str, phone: str) -> None:
    """Alerta interno da equipe — adapte pro canal real (Slack, e-mail, etc.)."""
    log.warning("ESCALADA [%s] de %s: %s", motivo, phone, resumo)
    if PLANTAO_ALERT_URL:
        try:
            requests.post(
                PLANTAO_ALERT_URL,
                json={
                    "motivo": motivo,
                    "resumo": resumo,
                    "cliente_whatsapp": phone,
                    "quando": datetime.now(timezone.utc).isoformat(),
                },
                timeout=10,
            )
        except requests.RequestException:
            log.exception("Falha ao notificar plantão via PLANTAO_ALERT_URL")


def handle_tool_call(tool_name: str, tool_input: dict, phone: str) -> str:
    """Executa a ferramenta e devolve o texto que vira tool_result pro modelo."""
    if tool_name == "enviar_link_pagamento":
        plano = tool_input.get("plano")
        link = PLAN_LINKS.get(plano)
        if not link:
            return f"ERRO: plano '{plano}' não existe na tabela. Planos válidos: {', '.join(PLAN_LINKS)}."
        send_whatsapp_message(phone, link)
        return f"Link do plano {plano} enviado com sucesso pro cliente."

    if tool_name == "escalar_atendimento_humano":
        motivo = tool_input.get("motivo", "não especificado")
        resumo = tool_input.get("resumo", "")
        notify_plantao(motivo, resumo, phone)
        if motivo == "emergência":
            msg = (
                "Sinto muito. Pra te atender com toda a prioridade agora, fala "
                f"direto com nosso plantão 24h: WhatsApp https://wa.me/{PLANTAO_WHATSAPP} "
                "ou 0800 095 3353, gratuito. Eles vão te acolher imediatamente."
            )
        else:
            msg = "Vou chamar um colega da equipe pra te ajudar com isso, só um instante."
        send_whatsapp_message(phone, msg)
        return "Escalada registrada e cliente avisado."

    return f"ERRO: ferramenta desconhecida '{tool_name}'."


def run_conversation_turn(phone: str, user_text: str) -> None:
    history = get_history(phone)
    history.append({"role": "user", "content": user_text})

    # Loop de tool use: o modelo pode chamar uma ou mais ferramentas antes
    # de terminar a resposta; processamos até ele parar de pedir ferramentas.
    for _ in range(4):  # limite de segurança contra loop infinito
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        history.append({"role": "assistant", "content": response.content})

        text_blocks = [b.text for b in response.content if b.type == "text"]
        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        for block in text_blocks:
            if block.strip():
                send_whatsapp_message(phone, block.strip())

        if not tool_blocks:
            break  # resposta normal, sem chamada de ferramenta — fim do turno

        tool_results = []
        for tb in tool_blocks:
            result_text = handle_tool_call(tb.name, tb.input, phone)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tb.id, "content": result_text}
            )
        history.append({"role": "user", "content": tool_results})
        # volta pro topo do loop pra deixar o modelo continuar depois do tool_result


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificação inicial exigida pela Meta ao configurar o webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True) or {}
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages", [])
    except (KeyError, IndexError):
        return jsonify({"status": "ignored"}), 200

    for msg in messages:
        if msg.get("type") != "text":
            continue  # protótipo trata só texto por enquanto
        phone = msg["from"]
        text = msg["text"]["body"]
        try:
            run_conversation_turn(phone, text)
        except Exception:
            log.exception("Erro processando mensagem de %s", phone)
            send_whatsapp_message(
                phone, "Opa, desculpa, deu uma travadinha aqui do meu lado — pode mandar de novo? 🙏"
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Em produção, rode atrás de um servidor WSGI de verdade (gunicorn, etc.)
    # e HTTPS — a Meta exige HTTPS válido pro webhook.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
