# ==============================================================================
# 5. CONTROLE VIA TELEGRAM (WEBHOOK FLASK) - REFATORADO
# ==============================================================================
# Dicionário para mapear comandos para funções.
# As funções de comando são encapsuladas para evitar a execução imediata.
COMMAND_HANDLERS = {}

def register_command(command_name):
    def decorator(func):
        COMMAND_HANDLERS[command_name] = func
        return func
    return decorator

@register_command("/ajuda")
def handle_help(chat_id, parts):
    help_message = (
        "🤖 *Lista de Comandos:*\n\n"
        "*Análise e Diagnóstico*\n"
        "`/status_geral`\n"
        "`/testar_conexoes`\n"
        "`/comparar_preco <MOEDA>` (Ex: `/comparar_preco btc`)\n\n"
        "*Controles Triangular (OKX Spot)*\n"
        "`/status_triangular`\n"
        "`/setprofit_triangular <valor>` (Ex: `/setprofit_triangular 0.2`)\n"
        "`/pausar_triangular`\n"
        "`/retomar_triangular`\n"
        "`/historico_triangular`\n"
        "`/simulacao_triangular_on`\n"
        "`/simulacao_triangular_off`\n\n"
        "*Controles Futuros (Multi-Exchange)*\n"
        "`/status_futuros`\n"
        "`/setprofit_futuros <valor>` (Ex: `/setprofit_futuros 0.4`)\n"
        "`/pausar_futuros`\n"
        "`/retomar_futuros`\n"
        "`/fechar_posicao <exc> <par> <lado> <qtd>` (Ex: `/fechar_posicao bybit btc/usdt:usdt buy 0.01`)"
    )
    send_telegram_message(help_message, chat_id)

@register_command("/status_geral")
def handle_general_status(chat_id, parts):
    triangular_status = "Ativo ✅" if triangular_running else "Pausado ⏸️"
    futures_status = "Ativo ✅" if futures_running else "Pausado ⏸️"
    msg = (
        f"📊 *Status Geral do Bot*\n\n"
        f"**Bot Triangular:** `{triangular_status}`\n"
        f"**Bot de Futuros:** `{futures_status}`\n"
        f"**Lucro Mín. Triangular:** `{triangular_min_profit_threshold * 100:.2f}%`\n"
        f"**Lucro Mín. Futuros:** `{futures_min_profit_threshold:.2f}%`"
    )
    send_telegram_message(msg, chat_id)

@register_command("/testar_conexoes")
def handle_test_connections(chat_id, parts):
    async def run_test_and_send_result():
        send_telegram_message("🔍 *Verificando conexões...*", chat_id)
        results = await test_all_connections()
        
        status_msg = "✅ *Status das Conexões:*\n\n"
        for ex, status in results.items():
            status_msg += f"`{ex.upper()}`: `{status}`\n"
        send_telegram_message(status_msg, chat_id)
    
    asyncio.run(run_test_and_send_result())

@register_command("/comparar_preco")
def handle_compare_price(chat_id, parts):
    if len(parts) < 2:
        send_telegram_message("❌ *Uso incorreto:* `/comparar_preco <MOEDA>`", chat_id)
        return
    
    symbol_base = parts[1].upper()
    target_symbol = f"{symbol_base}/USDT:USDT"
    
    async def compare_and_send():
        send_telegram_message(f"📈 *Buscando preços de {symbol_base}...*", chat_id)
        tasks = {name: ex.fetch_ticker(target_symbol) for name, ex in active_futures_exchanges.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        
        price_msg = f"📉 *Preços de {symbol_base}/USDT:*\n\n"
        for (name, _), res in zip(tasks.items(), results):
            if isinstance(res, Exception):
                price_msg += f"`{name.upper()}`: Erro\n"
            else:
                price_msg += f"`{name.upper()}`: *BID* `{res.get('bid')}` | *ASK* `{res.get('ask')}`\n"
        send_telegram_message(price_msg, chat_id)
    
    asyncio.run(compare_and_send())
    
@register_command("/status_triangular")
def handle_triangular_status(chat_id, parts):
    triangular_status = "Ativo ✅" if triangular_running else "Pausado ⏸️"
    msg = (
        f"📊 *Status Triangular (OKX Spot)*\n\n"
        f"**Status:** `{triangular_status}`\n"
        f"**Pares Monitorados:** `{triangular_monitored_cycles_count}`\n"
        f"**Lucro Mínimo:** `{triangular_min_profit_threshold * 100:.2f}%`\n"
        f"**Modo:** `{'SIMULAÇÃO' if TRIANGULAR_SIMULATE else 'REAL'}`\n"
        f"**Lucro Total:** `{triangular_lucro_total_usdt:.4f} USDT`"
    )
    send_telegram_message(msg, chat_id)

@register_command("/setprofit_triangular")
def handle_set_triangular_profit(chat_id, parts):
    global triangular_min_profit_threshold
    if len(parts) < 2 or not parts[1].replace('.', '', 1).isdigit():
        send_telegram_message("❌ *Uso incorreto:* `/setprofit_triangular <valor>` (Ex: 0.2)", chat_id)
        return
    new_value = Decimal(parts[1]) / 100
    triangular_min_profit_threshold = new_value
    send_telegram_message(f"✅ *Bot Triangular:* Lucro mínimo ajustado para `{new_value * 100:.2f}%`", chat_id)

@register_command("/pausar_triangular")
def handle_pause_triangular(chat_id, parts):
    global triangular_running
    triangular_running = False
    send_telegram_message("⏸️ *Bot Triangular pausado.*", chat_id)
    
@register_command("/retomar_triangular")
def handle_resume_triangular(chat_id, parts):
    global triangular_running
    triangular_running = True
    send_telegram_message("▶️ *Bot Triangular retomado.*", chat_id)

@register_command("/historico_triangular")
def handle_triangular_history(chat_id, parts):
    with sqlite3.connect(TRIANGULAR_DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM ciclos ORDER BY timestamp DESC LIMIT 10")
        records = c.fetchall()
    
    if not records:
        send_telegram_message("Nenhum ciclo de arbitragem triangular registrado ainda.", chat_id)
        return
    
    history_msg = "📜 *Últimos 10 Ciclos de Arbitragem:*\n\n"
    for rec in records:
        history_msg += (
            f"**Hora:** `{datetime.fromisoformat(rec[0]).strftime('%H:%M:%S')}`\n"
            f"**Lucro:** `{rec[2]:.3%}` (`{rec[3]:.4f} USDT`)\n"
            f"**Pares:** `{json.loads(rec[1])}`\n"
            f"**Modo:** `{rec[4]}`\n"
            f"**Status:** `{rec[5]}`\n\n"
        )
    send_telegram_message(history_msg, chat_id)

@register_command("/simulacao_triangular_on")
def handle_triangular_sim_on(chat_id, parts):
    global TRIANGULAR_SIMULATE
    TRIANGULAR_SIMULATE = True
    send_telegram_message("✅ *Modo de SIMULAÇÃO ativado* para o bot triangular.", chat_id)

@register_command("/simulacao_triangular_off")
def handle_triangular_sim_off(chat_id, parts):
    global TRIANGULAR_SIMULATE
    TRIANGULAR_SIMULATE = False
    send_telegram_message("⚠️ *Modo de SIMULAÇÃO desativado.* O bot triangular agora pode executar ordens reais.", chat_id)

@register_command("/status_futuros")
def handle_futures_status(chat_id, parts):
    futures_status = "Ativo ✅" if futures_running else "Pausado ⏸️"
    active_exchanges_str = ', '.join([ex.upper() for ex in active_futures_exchanges.keys()])
    msg = (
        f"📊 *Status Futuros (Multi-Exchange)*\n\n"
        f"**Status:** `{futures_status}`\n"
        f"**Exchanges Ativas:** `{active_exchanges_str}`\n"
        f"**Pares Monitorados:** `{futures_monitored_pairs_count}`\n"
        f"**Lucro Mínimo:** `{futures_min_profit_threshold:.2f}%`\n"
        f"**Modo:** `{'SIMULAÇÃO' if FUTURES_DRY_RUN else 'REAL'}`"
    )
    send_telegram_message(msg, chat_id)

@register_command("/setprofit_futuros")
def handle_set_futures_profit(chat_id, parts):
    global futures_min_profit_threshold
    if len(parts) < 2 or not parts[1].replace('.', '', 1).isdigit():
        send_telegram_message("❌ *Uso incorreto:* `/setprofit_futuros <valor>` (Ex: 0.4)", chat_id)
        return
    new_value = Decimal(parts[1])
    futures_min_profit_threshold = new_value
    send_telegram_message(f"✅ *Bot de Futuros:* Lucro mínimo ajustado para `{new_value:.2f}%`", chat_id)

@register_command("/fechar_posicao")
def handle_close_position(chat_id, parts):
    if len(parts) != 5:
        send_telegram_message("❌ *Uso incorreto:* `/fechar_posicao <exc> <par> <lado> <qtd>`", chat_id)
        return
    
    exchange_name, symbol, side, amount = parts[1:]
    
    async def close_position_and_send():
        result = await close_futures_position_command(exchange_name, symbol, side, amount)
        send_telegram_message(result, chat_id)
        
    asyncio.run(close_position_and_send())
    
@register_command("/pausar_futuros")
def handle_pause_futures(chat_id, parts):
    global futures_running
    futures_running = False
    send_telegram_message("⏸️ *Bot de Futuros pausado.*", chat_id)

@register_command("/retomar_futuros")
def handle_resume_futures(chat_id, parts):
    global futures_running
    futures_running = True
    send_telegram_message("▶️ *Bot de Futuros retomado.*", chat_id)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    msg = data.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_text = msg.get("text", "").strip().lower()

    if str(chat_id) != TELEGRAM_CHAT_ID:
        send_telegram_message(f"Alerta de segurança: Tentativa de acesso não autorizada de `{chat_id}`.")
        return "Não autorizado", 403

    def handle_command_thread():
        parts = msg_text.split()
        command = parts[0]
        handler = COMMAND_HANDLERS.get(command)
        
        if handler:
            handler(chat_id, parts)
        else:
            send_telegram_message(f"Comando `{command}` não reconhecido. Use `/ajuda` para ver os comandos disponíveis.", chat_id)
            
    if executor:
        executor.submit(handle_command_thread)

    return "OK", 200
