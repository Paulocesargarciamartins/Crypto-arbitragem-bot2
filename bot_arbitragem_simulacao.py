import os
import asyncio
import logging
import random
import time
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt
import nest_asyncio

nest_asyncio.apply()

# --- 1. Configurações e Parâmetros ---

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("A variável de ambiente 'TELEGRAM_BOT_TOKEN' não foi encontrada. Por favor, configure-a no Heroku.")

# Usando as letras sugeridas para representar as exchanges
EXCHANGES_LIST = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J']

PAIRS = ["BTC/USDT", "ETH/USDT", "ADA/USDT", "SOL/USDT", "XRP/USDT"]

# Configurações do bot de arbitragem
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = 2.0
DEFAULT_TRADE_AMOUNT_USD = 50.0
DEFAULT_FEE_PERCENTAGE = 0.1
DRY_RUN_MODE = True

COOLDOWN_SECONDS = 300

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. Gerenciadores ---

class ExchangeManager:
    def __init__(self, dry_run=True):
        self.exchanges = {}
        self.dry_run = dry_run
        logging.info("ExchangeManager iniciado. Conexões simuladas.")

class TradingManager:
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        
        # Lógica de caixas (simulada)
        self.caixa_principal = 100.0  # USDT
        self.caixa_reserva = 100.0    # USDT
        self.caixa_seguro = 100.0     # USDT
        self.moedas_travadas = {}
        
        logging.info(f"TradingManager iniciado. Dry Run: {self.dry_run}")
    
    def executar_arbitragem_simulada(self, lucro_liquido):
        
        # Simula uma chance de dar errado e travar a moeda
        if random.random() < 0.25: # 25% de chance de dar errado na venda
            moeda = random.choice(PAIRS).split('/')[0]
            corretora = random.choice(EXCHANGES_LIST)
            perda_simulada = random.uniform(3.0, 10.0) # Perda de 3% a 10%
            
            self.moedas_travadas[moeda] = {
                'corretora': corretora,
                'prejuizo_maximo': perda_simulada
            }
            
            # Tenta repor o caixa principal com o reserva
            if self.caixa_reserva >= self.caixa_principal:
                self.caixa_reserva -= self.caixa_principal
                self.caixa_principal = 100.0 # Repõe para um valor fixo para a próxima operação
                return f"⚠️ Arbitragem falhou. Moeda {moeda} travada na exchange {corretora}. Saldo do caixa principal foi reposto com o reserva."
            else:
                return f"❌ Arbitragem falhou. Moeda {moeda} travada na exchange {corretora}. Não há saldo suficiente no caixa reserva."
        
        else:
            # Simulação de arbitragem bem-sucedida
            lucro_valor = self.caixa_principal * (lucro_liquido / 100)
            self.caixa_principal += lucro_valor
            return f"✅ Arbitragem bem-sucedida! Lucro de {lucro_valor:.2f} USDT."


# --- 3. Instâncias Globais ---

global_exchanges_instances = {}
markets_loaded = {}
last_alert_times = {}

# O TradingManager agora gerencia os saldos e as moedas travadas
trading_manager = TradingManager(dry_run=DRY_RUN_MODE)

# --- 4. Funções de Arbitragem e WebSockets (Integradas) ---

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0

            # --- Lógica de Simulação de Oportunidade ---
            buy_ex_id = random.choice(EXCHANGES_LIST)
            sell_ex_id = random.choice([ex for ex in EXCHANGES_LIST if ex != buy_ex_id])
            pair = random.choice(PAIRS)

            best_buy_price = random.uniform(10, 20)
            best_sell_price = best_buy_price * (1 + random.uniform(0.01, 0.05))

            gross_profit_percentage = ((best_sell_price - best_buy_price) / best_buy_price) * 100
            net_profit_percentage = gross_profit_percentage - (2 * fee * 100)
            
            transfer_fee = 1.0 # Simulação de taxa de transferência
            net_profit_percentage -= (transfer_fee / trading_manager.caixa_principal) * 100

            if net_profit_percentage >= lucro_minimo:
                arbitrage_key = f"{pair}-{buy_ex_id}-{sell_ex_id}"
                current_time = time.time()

                if arbitrage_key in last_alert_times and (current_time - last_alert_times[arbitrage_key]) < COOLDOWN_SECONDS:
                    logger.debug(f"Alerta para {arbitrage_key} em cooldown.")
                    continue

                # Executa a arbitragem simulada com a nova lógica
                resultado_simulacao = trading_manager.executar_arbitragem_simulada(net_profit_percentage)
                
                msg = (f"🔍 Oportunidade encontrada!\n"
                    f"💰 Arbitragem para {pair}!\n"
                    f"Compre em {buy_ex_id} | Venda em {sell_ex_id}\n"
                    f"Lucro Líquido: {net_profit_percentage:.2f}%\n"
                    f"--- Simulação ---\n"
                    f"{resultado_simulacao}"
                )

                await bot.send_message(chat_id=chat_id, text=msg)
                last_alert_times[arbitrage_key] = current_time

        except Exception as e:
            logger.error(f"Erro no loop de arbitragem: {e}", exc_info=True)

        await asyncio.sleep(5)

async def watch_all_exchanges():
    # Apenas um placeholder para o loop de WebSockets
    logger.info("Iniciando simulação de WebSockets...")
    while True:
        await asyncio.sleep(60)

# --- 5. Funções de Comando do Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = update.message.chat_id
    await update.message.reply_text(
        "Olá! Bot de Arbitragem Ativado (Modo de Simulação).\n"
        "Estou monitorando oportunidades de arbitragem e simulando a execução.\n"
        f"Lucro mínimo atual: {context.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)}%\n"
        f"Volume de trade para simulação: ${DEFAULT_TRADE_AMOUNT_USD:.2f}\n"
        f"Taxa de negociação por lado: {context.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE)}%\n\n"
        "Use /stop para parar de receber alertas."
    )
    logger.info(f"Bot iniciado por chat_id: {update.message.chat_id}")

async def setlucro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor < 0:
            await update.message.reply_text("O lucro mínimo não pode ser negativo.")
            return
        context.bot_data['lucro_minimo_porcentagem'] = valor
        await update.message.reply_text(f"Lucro mínimo atualizado para {valor:.2f}%")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setlucro 2.5")

async def setfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor < 0:
            await update.message.reply_text("A taxa de negociação não pode ser negativa.")
            return
        context.bot_data['fee_percentage'] = valor
        await update.message.reply_text(f"Taxa de negociação por lado atualizada para {valor:.3f}%")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setfee 0.075")

async def stop_arbitrage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Alertas e simulações desativados. Use /start para reativar.")
    logger.info(f"Alertas e simulações desativados por {update.message.chat_id}")
    
# --- Novos Comandos de Caixa ---

async def get_saldo_principal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = trading_manager.caixa_principal
    await update.message.reply_text(f"📦 Saldo do Caixa Principal: {saldo:.2f} USDT")

async def get_saldo_reserva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = trading_manager.caixa_reserva
    await update.message.reply_text(f"📦 Saldo do Caixa Reserva: {saldo:.2f} USDT")

async def get_saldo_seguro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = trading_manager.caixa_seguro
    await update.message.reply_text(f"📦 Saldo do Caixa Segurança: {saldo:.2f} USDT")

async def get_moedas_travadas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if trading_manager.moedas_travadas:
        msg = "Moedas travadas:\n"
        for moeda, info in trading_manager.moedas_travadas.items():
            msg += f" - {moeda} na exchange {info['corretora']} com prejuízo de até {info['prejuizo_maximo']:.2f}%.\n"
    else:
        msg = "Nenhuma moeda está travada no momento."
    await update.message.reply_text(msg)


# --- 6. Função Principal (main) ---

async def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlucro", setlucro))
    application.add_handler(CommandHandler("setfee", setfee))
    application.add_handler(CommandHandler("stop", stop_arbitrage))
    
    # Novos handlers para os comandos dos caixas
    application.add_handler(CommandHandler("saldoprincipal", get_saldo_principal))
    application.add_handler(CommandHandler("saldoreseerva", get_saldo_reserva))
    application.add_handler(CommandHandler("saldoseguro", get_saldo_seguro))
    application.add_handler(CommandHandler("moedastravadas", get_moedas_travadas))

    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar o bot e ver configurações"),
        BotCommand("setlucro", "Definir lucro mínimo em % (Ex: /setlucro 2.5)"),
        BotCommand("setfee", "Definir taxa de negociação por lado em % (Ex: /setfee 0.075)"),
        BotCommand("saldoprincipal", "Ver o saldo do caixa principal de arbitragem"),
        BotCommand("saldoreseerva", "Ver o saldo do caixa de reserva"),
        BotCommand("saldoseguro", "Ver o saldo do caixa de segurança"),
        BotCommand("moedastravadas", "Ver a lista de moedas travadas em alguma exchange"),
        BotCommand("stop", "Parar de receber alertas")
    ])

    logger.info("Bot iniciado com sucesso e aguardando mensagens...")

    try:
        asyncio.create_task(check_arbitrage_opportunities(application))
        await application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

    except Exception as e:
        logger.error(f"Erro no loop principal do bot: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
