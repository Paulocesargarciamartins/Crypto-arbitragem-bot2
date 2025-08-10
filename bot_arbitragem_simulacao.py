async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue

            # A lógica para lidar com trades travados (stuck) pode ser implementada aqui.
            # Por exemplo, uma tentativa de venda emergencial ou alerta.

            if GLOBAL_ACTIVE_TRADES:
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
            trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0

            best_opportunity = None
            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA[pair]
                if len(market_data) < 2:
                    continue

                best_buy_price = float('inf')
                best_sell_price = 0
                buy_ex_id = None
                sell_ex_id = None
                for ex_id, data in market_data.items():
                    ask = data.get('ask')
                    bid = data.get('bid')
                    if ask and ask < best_buy_price:
                        best_buy_price = ask
                        buy_ex_id = ex_id
                    if bid and bid > best_sell_price:
                        best_sell_price = bid
                        sell_ex_id = ex_id

                if not buy_ex_id or not sell_ex_id or buy_ex_id == sell_ex_id:
                    continue

                try:
                    buy_exchange_rest = await get_exchange_instance(buy_ex_id, authenticated=False, is_rest=True)
                    sell_exchange_rest = await get_exchange_instance(sell_ex_id, authenticated=False, is_rest=True)

                    ticker_buy = await buy_exchange_rest.fetch_ticker(pair)
                    ticker_sell = await sell_exchange_rest.fetch_ticker(pair)

                    confirmed_buy_price = ticker_buy['ask']
                    confirmed_sell_price = ticker_sell['bid']

                except Exception as e:
                    logger.warning(f"Falha na consulta REST para o par {pair}: {e}")
                    continue

                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_percentage = gross_profit * 100
                net_profit_percentage = gross_profit_percentage - (2 * fee * 100)

                # Verifica o saldo disponível para executar o trade.
                if GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0) < trade_amount_usd + DEFAULT_MIN_USDT_BALANCE:
                    logger.info(f"Saldo insuficiente na exchange {buy_ex_id} para o par {pair}.")
                    continue

                if net_profit_percentage >= lucro_minimo:
                    if best_opportunity is None or net_profit_percentage > best_opportunity['net_profit']:
                        best_opportunity = {
                            'pair': pair,
                            'buy_ex_id': buy_ex_id,
                            'sell_ex_id': sell_ex_id,
                            'buy_price': confirmed_buy_price,
                            'sell_price': confirmed_sell_price,
                            'net_profit': net_profit_percentage,
                            'amount_usd': trade_amount_usd
                        }

            if best_opportunity:
                pair = best_opportunity['pair']
                amount_usdt = best_opportunity['amount_usd']
                buy_ex = best_opportunity['buy_ex_id']
                sell_ex = best_opportunity['sell_ex_id']
                buy_price = best_opportunity['buy_price']
                sell_price = best_opportunity['sell_price']

                # Pega a moeda base (ex: BTC de BTC/USDT).
                base_currency = pair.split('/')[0]
                amount_base = amount_usdt / buy_price

                # Executa a compra.
                buy_order = await execute_trade('buy', buy_ex, pair, amount_base, buy_price)
                if not buy_order:
                    logger.error("Falha ao executar a compra.")
                    GLOBAL_STATS['trade_outcomes']['failed'] += 1
                    await asyncio.sleep(5)
                    continue

                # Executa a venda.
                sell_order = await execute_trade('sell', sell_ex, pair, amount_base, sell_price)
                if not sell_order:
                    logger.error(f"Falha ao executar a venda na exchange {sell_ex} para o par {pair}. Posição travada.")
                    GLOBAL_STATS['trade_outcomes']['stuck'] += 1
                    # A posição fica "travada". Implementar uma lógica de recuperação pode ser necessário.
                    GLOBAL_STUCK_POSITIONS[pair] = {
                        'amount': amount_base,
                        'buy_price': buy_price,
                        'buy_ex': buy_ex,
                        'sell_ex': sell_ex,
                        'time': time.time()
                    }
                    await asyncio.sleep(5)
                    continue
                else:
                    GLOBAL_STATS['trade_outcomes']['success'] += 1
                    # Calcula e atualiza o lucro total.
                    GLOBAL_STATS['pair_opportunities'][pair]['total_profit'] += (sell_order['average'] - buy_order['average']) * amount_base
                    message = f"🟢 **Arbitragem bem-sucedida!**\n\n" \
                              f"**Par:** `{pair}`\n" \
                              f"**Compra:** `{buy_ex}` @ `{buy_order['average']:.8f}`\n" \
                              f"**Venda:** `{sell_ex}` @ `{sell_order['average']:.8f}`\n" \
                              f"**Valor:** `{amount_usdt:.2f}` USDT\n" \
                              f"**Lucro Líquido:** `{best_opportunity['net_profit']:.2f}`%"
                    await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

                await asyncio.sleep(60) # Espera 60 segundos para evitar trades repetidos muito rápido.
        except Exception as e:
            logger.error(f"Erro no loop principal: {e}")
            await asyncio.sleep(10)

