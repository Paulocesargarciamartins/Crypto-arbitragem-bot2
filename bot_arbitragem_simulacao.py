import os
import asyncio
import logging
import random
import time
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import nest_asyncio

nest_asyncio.apply()

# --- 1. Configurações e Parâmetros ---

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("A variável de ambiente 'TELEGRAM_BOT_TOKEN' não foi encontrada. Por favor, configure-a no Heroku.")

EXCHANGES_LIST = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J']
PAIRS = ["BTC/USDT", "ETH/USDT", "ADA/USDT", "SOL/USDT", "XRP/USDT"]

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

class TradingManager:
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        
        self.caixa_principal = 100.0
        self.caixa_reserva = 100.0
        self.caixa_seguro = 100.0
        self.moedas_travadas = {}
        self.operacoes_hoje = 0
        self.lucro_hoje = 0.0
        
        logging.info(f"TradingManager iniciado. Dry Run: {self.dry_run}")
    
    def transferir_fundos(self, de, para, valor):
        try:
            valor = float(valor)
        except ValueError:
            return "❌ Valor inválido. A transferência deve ser um número."

        if valor <= 0:
            return "❌ Valor inválido. A transferência deve ser um valor positivo."

        caixas_map = {
            'cx1': 'caixa_principal',
            'cx2': 'caixa_reserva',
            'cx3': 'caixa_seguro'
        }
        
        if de not in caixas_map or para not in caixas_map:
            return "❌ Caixas inválidos. Use cx1, cx2 ou cx3."
            
        origem = caixas_map[de]
        destino = caixas_map[para]

        if getattr(self, origem) < valor:
            return f"❌ Saldo insuficiente no caixa de origem ({origem}). Saldo atual: {getattr(self, origem):.2f}"

        setattr(self, origem, getattr(self, origem) - valor)
        setattr(self, destino, getattr(self, destino) + valor)
        
        return f"✅ Transferência de {valor:.2f} USDT de {origem} para {destino} realizada com sucesso."

    def executar_arbitragem_simulada(self, lucro_liquido):
        self.operacoes_hoje += 1
        
        if random.random() < 0.25:
            moeda = random.choice(PAIRS).split('/')[0]
            corretora = random.choice(EXCHANGES_LIST)
            perda_simulada = random.uniform(3.0, 10.0)
            
            self.moedas_travadas[moeda] = {
                'corretora': corretora,
                'prejuizo_maximo': perda_simulada
            }
            
            if self.caixa_reserva >= DEFAULT_TRADE_AMOUNT_USD:
                self.caixa_reserva -= DEFAULT_TRADE_AMOUNT_USD
                self.caixa_principal = DEFAULT_TRADE_AMOUNT_USD
                return f"⚠️ Arbitragem falhou. Moeda {moeda} travada na exchange {corretora}. Saldo do caixa principal foi reposto com o reserva."
            else:
                return f"❌ Arbitragem falhou. Moeda {moeda} travada na exchange {corretora}. Não há saldo suficiente no caixa reserva."
        
        else:
            lucro_valor = self.caixa_principal * (lucro_liquido / 100)
            self.caixa_principal += lucro_valor
            self.lucro_hoje += lucro_valor
            return f"✅ Arbitragem bem-sucedida! Lucro de {lucro_valor:.2f} USDT. Novo saldo: {self.caixa_principal:.2f}"

# --- 3. Instâncias Globais e correção do erro ---
trading_manager = TradingManager(dry_run=DRY_RUN_MODE)
last_alert_times = {} # CORREÇÃO: Variável inicializada aqui.

# --- 4. Funções de Arbitragem e WebSockets ---

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

            buy_ex_id = random.choice(EXCHANGES_LIST)
            sell_ex_id = random.choice([ex for ex in EXCHANGES_LIST if ex != buy_ex_id])
            pair = random.choice(PAIRS)

            best_buy_price = random.uniform(10, 20)
            best_sell_price = best_buy_price * (1 + random.uniform(0.01, 0.05))

            gross_profit_percentage = ((best_sell_price - best_buy_price) / best_buy_price) * 100
            net_profit_percentage = gross_profit_percentage - (2 * fee * 100)
            
            transfer_fee = 1.0
            net_profit_percentage -= (transfer_fee / trading_manager.caixa_principal) * 100

            if net_profit_percentage >= lucro_minimo:
                arbitrage_key = f"{pair}-{buy_ex_id}-{sell_ex_id}"
                current_time = time.time()

                if arbitrage_key in last_alert_times and (current_time - last_alert_times[arbitrage_key]) < COOLDOWN_SECONDS:
                    continue

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
    logger.info("Iniciando simulação de WebSockets...")
    while True:
        await asyncio.sleep(60)

# --- 5. Funções de Comando do Telegram ---

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = update.message.chat_id
    await update.message.reply_text(
        "Olá! Bot de Arbitragem Ativado (Modo de Simulação).\n"
        "Estou monitorando oportunidades de arbitragem e simulando a execução.\n"
        "Use /config para ver as configurações atuais."
    )
    logger.info(f"Bot iniciado por chat_id: {update.message.chat_id}")

async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Alertas e simulações desativados. Use /start para reativar.")
    logger.info(f"Alertas e simulações desativados por {update.message.chat_id}")

async def get_saldo_cx1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📦 Saldo do Caixa Principal: {trading_manager.caixa_principal:.2f} USDT")

async def get_saldo_cx2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📦 Saldo do Caixa Reserva: {trading_manager.caixa_reserva:.2f} USDT")

async def get_saldo_cx3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📦 Saldo do Caixa Segurança: {trading_manager.caixa_seguro:.2f} USDT")

async def transferir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        de, para, valor = context.args
        msg = trading_manager.transferir_fundos(de, para, valor)
        await update.message.reply_text(msg)
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /transferir cx3 cx2 50")

async def moedas_travadas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if trading_manager.moedas_travadas:
        msg = "Moedas travadas:\n"
        for moeda, info in trading_manager.moedas_travadas.items():
            msg += f" - {moeda} na exchange {info['corretora']} com prejuízo de até {info['prejuizo_maximo']:.2f}%.\n"
    else:
        msg = "Nenhuma moeda está travada no momento."
    await update.message.reply_text(msg)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 **Estatísticas do Dia**\n"
        f"Operações de simulação: {trading_manager.operacoes_hoje}\n"
        f"Lucro acumulado: {trading_manager.lucro_hoje:.2f} USDT"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"⚙️ **Configurações Atuais**\n"
        f"Lucro Mínimo: {context.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)}%\n"
        f"Taxa de Negociação: {context.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE)}%\n"
        f"Modo de Operação: {'Simulação (DRY RUN)' if DRY_RUN_MODE else 'Real'}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

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

# --- 6. Função Principal (main) ---

async def main():
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("saldocx1", get_saldo_cx1))
    application.add_handler(CommandHandler("saldocx2", get_saldo_cx2))
    application.add_handler(CommandHandler("saldocx3", get_saldo_cx3))
    application.add_handler(CommandHandler("transferir", transferir))
    application.add_handler(CommandHandler("moedastravadas", moedas_travadas))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("config", config))
    application.add_handler(CommandHandler("setlucro", setlucro))
    application.add_handler(CommandHandler("setfee", setfee))

    await application.bot.set_my_commands([
        BotCommand("start", "Inicia o bot"),
        BotCommand("stop", "Para o bot"),
        BotCommand("saldocx1", "Saldo Caixa Principal"),
        BotCommand("saldocx2", "Saldo Caixa Reserva"),
        BotCommand("saldocx3", "Saldo Caixa Segurança"),
        BotCommand("transferir", "Transferir fundos entre caixas (Ex: /transferir cx3 cx2 50)"),
        BotCommand("moedastravadas", "Ver moedas travadas"),
        BotCommand("stats", "Estatísticas do dia"),
        BotCommand("config", "Ver configurações atuais"),
        BotCommand("setlucro", "Definir lucro mínimo (Ex: /setlucro 2.5)"),
        BotCommand("setfee", "Definir taxa de negociação (Ex: /setfee 0.075)")
    ])

    logger.info("Bot iniciado com sucesso e aguardando mensagens...")

    try:
        asyncio.create_task(check_arbitrage_opportunities(application))
        await application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
    except Exception as e:
        logger.error(f"Erro no loop principal do bot: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
