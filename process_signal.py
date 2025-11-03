#!/usr/bin/env python3
"""
Простой процессор сигналов - обрабатывает одно сообщение
"""

import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import (
    _post_level_search, _line_to_ticker_and_squares, 
    _is_correction_combo, _is_resistance_combo,
    _get_latest_key_levels, _find_closest_levels,
    _get_pivot_support_levels, _calculate_grid_orders,
    _send_webhook_from_level
)

async def process_signal(message_text):
    """Обрабатывает одно сообщение"""
    
    print(f"📨 Обрабатываем: {message_text}")
    print("=" * 50)
    
    # Парсим сообщение
    result = _line_to_ticker_and_squares(message_text)
    if not result:
        print("❌ Не удалось распарсить")
        return False
        
    ticker, squares, origin_tf = result
    print(f"Тикер: {ticker}, Эмодзи: {squares}, Таймфрейм: {origin_tf}")
    
    # Проверяем тип сигнала
    if _is_correction_combo(squares):
        print("🟢 LONG СИГНАЛ")
        signal_type = "LONG"
        context = "long"
    elif _is_resistance_combo(squares):
        print("🔴 SHORT СИГНАЛ") 
        signal_type = "SHORT"
        context = "short"
    else:
        print("⚪ Нейтральный - пропускаем")
        return False
        
    # API запрос
    try:
        print(f"🔄 Запрос к API для {ticker}...")
        symbol_usdt = f"{ticker}USDT"
        
        resp = await _post_level_search(symbol_usdt, context=context, origin_tf=origin_tf)
        
        if not resp or not isinstance(resp, dict):
            print(f"❌ API не ответил")
            return False
            
        decision = resp.get("decision", "")
        if decision.startswith("no_trade"):
            print(f"⚠️ Сигнал отклонен: {resp.get('reason', '')}")
            return False
            
        level = resp.get("level", {})
        orders = resp.get("orders", {})
        last_price = resp.get("last_price")
        
        if not level or not orders or not last_price:
            print(f"❌ Недостаточно данных")
            return False
            
        # Данные сигнала
        level_price = level.get("price", 0)
        entry_price = orders.get("entry", {}).get("price", 0)
        sl_price = orders.get("sl", {}).get("price", 0)
        tp_price = orders.get("tp", [{}])[0].get("price", 0)
        
        print(f"📊 Данные:")
        print(f"   Уровень: {level_price:.6f}")
        print(f"   Entry: {entry_price:.6f}")
        print(f"   SL: {sl_price:.6f}")
        print(f"   TP: {tp_price:.6f}")
        print(f"   Цена: {last_price:.6f}")
        
        # Key Levels
        print(f"🔍 Ищем Key Levels...")
        key_levels = _get_latest_key_levels(ticker)
        
        if key_levels:
            print(f"✅ Key Levels найдены")
            if signal_type == "LONG":
                real_support = _find_closest_levels(
                    key_levels.get("support", []), 
                    float(last_price), 
                    "support"
                )
                if real_support:
                    level_low, level_high = real_support['zone']
                    print(f"📊 Поддержка: {level_low:.5f} - {level_high:.5f}")
                else:
                    level_low, level_high = float(last_price) * 0.95, float(last_price) * 1.05
            else:
                real_resistance = _find_closest_levels(
                    key_levels.get("resistance", []), 
                    float(last_price), 
                    "resistance"
                )
                if real_resistance:
                    level_low, level_high = real_resistance['zone']
                    print(f"📊 Сопротивление: {level_low:.5f} - {level_high:.5f}")
                else:
                    level_low, level_high = float(last_price) * 0.95, float(last_price) * 1.05
        else:
            print(f"⚠️ Key Levels не найдены, используем статический расчет")
            level_low, level_high = float(last_price) * 0.95, float(last_price) * 1.05
        
        # Сетка ордеров
        level_zone = (level_low, level_high)
        grid_data = _calculate_grid_orders(
            entry_price=float(last_price),
            level_zone=level_zone,
            side='buy' if signal_type == "LONG" else 'sell',
            qty_orders=5,
            max_risk=50.0
        )
        
        if grid_data:
            print(f"📊 Сетка:")
            print(f"   Первый: {grid_data['first_order_price']:.5f}")
            print(f"   Последний: {grid_data['last_order_price']:.5f}")
            print(f"   SL: {grid_data['sl_price']:.5f}")
            print(f"   Сумма: ${grid_data['total_amount']:.2f}")
        
        # Формируем сообщение
        trend_emojis = ''.join(squares)
        level_type = "SUPPORT" if signal_type == "LONG" else "RESISTANCE"
        
        message = f"""{'🟢' if signal_type == 'LONG' else '🔴'} **{signal_type}** {ticker} {trend_emojis}

📊 **Текущая цена:** {last_price:.5f}
🎯 **Entry:** {entry_price:.5f}
🛡️ **SL:** {sl_price:.5f}
🎯 **TP:** {tp_price:.5f}

📈 **Key levels:** {level_type} {level_low:.5f} - {level_high:.5f}

📊 **Сетка:** 5 ордеров (${grid_data['total_amount']:.2f})
   Первый: {grid_data['first_order_price']:.5f} (текущая)
   Последний: {grid_data['last_order_price']:.5f} (граница {level_type.lower()})
   SL: {grid_data['sl_price']:.5f} (за {level_type.lower()})

⏰ **Таймфрейм:** {origin_tf.upper()}"""
        
        print(f"\n📤 СООБЩЕНИЕ:")
        print("=" * 50)
        print(message)
        print("=" * 50)
        
        # Webhook
        print(f"🌐 Отправляем webhook...")
        webhook_success = await _send_webhook_from_level(
            symbol_usdt, 
            "buy" if signal_type == "LONG" else "sell",
            last_price, 
            level_zone, 
            tp_price
        )
        
        if webhook_success:
            print(f"✅ Webhook отправлен")
        else:
            print(f"❌ Ошибка webhook")
        
        print(f"✅ {signal_type} сигнал для {ticker} ОБРАБОТАН!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

async def main():
    if len(sys.argv) < 2:
        print("Использование: python process_signal.py '$TICKER 🟥🟢🟢🟢🟢 FRAME:30M'")
        return
        
    message = ' '.join(sys.argv[1:])
    await process_signal(message)

if __name__ == "__main__":
    asyncio.run(main())
