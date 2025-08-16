# app.py (Versão Funcional Completa)
import os
import requests
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = Flask(__name__)

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    msg_text = data.get("message", {}).get("text", "").strip().lower()
    
    # Comandos simples para confirmar que está funcionando
    if msg_text == "/ping":
        send_telegram_message("Pong! 🏓 O servidor web está respondendo.")
    elif msg_text == "/ajuda":
        send_telegram_message("🤖 *Bot Online!* O processo de arbitragem está rodando em segundo plano. Comandos de controle detalhados serão adicionados em breve.")
    else:
        # Apenas confirma o recebimento, sem tentar controlar os outros bots
        send_telegram_message(f"Comando `{msg_text}` recebido. O robô de análise está trabalhando em segundo plano.")

    return "OK", 200
