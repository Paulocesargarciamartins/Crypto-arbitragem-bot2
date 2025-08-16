# app.py (Versão de Diagnóstico com Logs Detalhados)
import os
import requests
import json
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()
print("APP.PY: Script iniciado.")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TokenNaoEncontrado")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ChatIdNaoEncontrado")

print(f"APP.PY: Token carregado - {'Sim' if TELEGRAM_TOKEN != 'TokenNaoEncontrado' else 'NÃO'}")
print(f"APP.PY: Chat ID carregado - {'Sim' if TELEGRAM_CHAT_ID != 'ChatIdNaoEncontrado' else 'NÃO'}")

app = Flask(__name__)
print("APP.PY: Aplicativo Flask criado.")

def send_telegram_message(text):
    print(f"SEND_MESSAGE: Tentando enviar texto: '{text}'")
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "TokenNaoEncontrado":
        print("SEND_MESSAGE: ERRO - Token do Telegram ausente.")
        return
    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "ChatIdNaoEncontrado":
        print("SEND_MESSAGE: ERRO - Chat ID do Telegram ausente.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    
    try:
        print("SEND_MESSAGE: Enviando requisição para a API do Telegram...")
        response = requests.post(url, json=payload, timeout=15)
        print(f"SEND_MESSAGE: Telegram respondeu com status {response.status_code} e conteúdo: {response.text}")
    except Exception as e:
        print(f"SEND_MESSAGE: ERRO CRÍTICO ao enviar requisição: {e}")

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    print("\nWEBHOOK: Rota do webhook foi acionada! Recebemos algo do Telegram.")
    try:
        data = request.get_json()
        print(f"WEBHOOK: Conteúdo recebido: {json.dumps(data, indent=2)}")
        send_telegram_message("Recebi sua mensagem. Verificando os logs...")
    except Exception as e:
        print(f"WEBHOOK: ERRO ao processar a requisição: {e}")
        
    return "OK", 200

@app.route("/")
def index():
    print("\nINDEX: Rota raiz '/' foi acessada.")
    return "Servidor de diagnóstico está no ar. Verifique os logs do Heroku."
