#!/usr/bin/env python3
"""
Ручной процессор сигналов - имитирует работу Telegram бота
Позволяет тестировать всю логику без Telegram
"""

import asyncio
import sys
import os
import httpx
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import (
    _post_level_search, _line_to_ticker_and_squares, 
    _is_correction_combo, _is_resistance_combo,
    _get_latest_key_levels, _find_closest_levels,
    _get_pivot_support_levels, _calculate_grid_orders,
    _send_webhook_from_level, build_payload, send_payload
)

async def process_manual_message(message_text):
    """Обрабатывает сообщение вручную, имитируя работу бота"""
    
    print(f"\n📨 Обрабатываем сообщение: {message_text}")
    print("=" * 60)
    
    # Парсим сообщение
    result = _line_to_ticker_and_squares(message_text)
    if not result:
        print("❌ Не удалось распарсить сообщение")
        return False
        
    ticker, squares, origin_tf = result
    print(f"   Тикер: {ticker}")
    print(f"   Эмодзи: {squares}")
    print(f"   Таймфрейм: {origin_tf}")
    
    # Проверяем тип сигнала
    if _is_correction_combo(squares):
        print("🟢 НАЙДЕН LONG СИГНАЛ")
        signal_type = "LONG"
        context = "long"
    elif _is_resistance_combo(squares):
        print("🔴 НАЙДЕН SHORT СИГНАЛ") 
        signal_type = "SHORT"
        context = "short"
    else:
        print("⚪ Нейтральный сигнал - пропускаем")
        return False
        
    # Отправляем запрос к API
    try:
        print(f"   🔄 Отправляем запрос к API для {ticker}...")
        symbol_usdt = f"{ticker}USDT"
        
        resp = await _post_level_search(symbol_usdt, context=context, origin_tf=origin_tf)
        
        if not resp or not isinstance(resp, dict):
            print(f"   ❌ API не ответил для {ticker}")
            return False
            
        decision = resp.get("decision", "")
        reason = resp.get("reason", "")
        level = resp.get("level", {})
        orders = resp.get("orders", {})
        last_price = resp.get("last_price")
        trade_setup = resp.get("trade_setup", {})
        
        print(f"   📊 API ответ:")
        print(f"      Решение: {decision}")
        print(f"      Причина: {reason}")
        
        if decision.startswith("no_trade"):
            print(f"   ⚠️ {signal_type} сигнал для {ticker} ОТКЛОНЕН API")
            return False
            
        if not level or not orders:
            print(f"   ❌ Недостаточно данных для формирования сигнала")
            return False
            
        # Извлекаем данные
        level_price = level.get("price", 0)
        level_score = level.get("score", 0)
        level_tol = level.get("tol", 0.02)
        
        entry = orders.get("entry", {})
        sl = orders.get("sl", {})
        tp = orders.get("tp", [])
        
        entry_price = entry.get("price", 0)
        sl_price = sl.get("price", 0)
        tp_price = tp[0].get("price", 0) if tp else 0
        
        print(f"   📈 Данные сигнала:")
        print(f"      Уровень: {level_price:.6f} (оценка: {level_score:.2f})")
        print(f"      Entry: {entry_price:.6f}")
        print(f"      SL: {sl_price:.6f}")
        print(f"      TP: {tp_price:.6f}")
        print(f"      Текущая цена: {last_price:.6f}")
        
        # Ищем Key Levels
        print(f"   🔍 Ищем Key Levels для {ticker}...")
        key_levels = _get_latest_key_levels(ticker)
        
        real_support = None
        real_resistance = None
        
        if key_levels:
            print(f"   ✅ Найдены Key Levels для {ticker}")
            
            if signal_type == "LONG":
                real_support = _find_closest_levels(
                    key_levels.get("support", []), 
                    float(last_price), 
                    "support"
                )
                if real_support:
                    print(f"   📊 Реальная поддержка: {real_support['zone'][0]:.5f} - {real_support['zone'][1]:.5f}")
            else:  # SHORT
                real_resistance = _find_closest_levels(
                    key_levels.get("resistance", []), 
                    float(last_price), 
                    "resistance"
                )
                if real_resistance:
                    print(f"   📊 Реальное сопротивление: {real_resistance['zone'][0]:.5f} - {real_resistance['zone'][1]:.5f}")
        else:
            print(f"   ⚠️ Key Levels не найдены, используем pivot уровни")
            
        # Определяем финальные уровни
        if signal_type == "LONG":
            if real_support:
                level_low, level_high = real_support['zone']
                print(f"   ✅ Используем реальную поддержку: {level_low:.5f} - {level_high:.5f}")
            else:
                # Fallback к pivot уровням
                pivot_levels = await _get_pivot_support_levels(symbol_usdt, "long")
                if pivot_levels:
                    level_low, level_high = pivot_levels
                    print(f"   ✅ Используем pivot поддержку: {level_low:.5f} - {level_high:.5f}")
                else:
                    # Статический расчет
                    level_low = float(last_price) * 0.95
                    level_high = float(last_price) * 1.05
                    print(f"   ⚠️ Используем статический расчет: {level_low:.5f} - {level_high:.5f}")
        else:  # SHORT
            if real_resistance:
                level_low, level_high = real_resistance['zone']
                print(f"   ✅ Используем реальное сопротивление: {level_low:.5f} - {level_high:.5f}")
            else:
                # Fallback к pivot уровням
                pivot_levels = await _get_pivot_support_levels(symbol_usdt, "short")
                if pivot_levels:
                    level_low, level_high = pivot_levels
                    print(f"   ✅ Используем pivot сопротивление: {level_low:.5f} - {level_high:.5f}")
                else:
                    # Статический расчет
                    level_low = float(last_price) * 0.95
                    level_high = float(last_price) * 1.05
                    print(f"   ⚠️ Используем статический расчет: {level_low:.5f} - {level_high:.5f}")
        
        # Рассчитываем сетку ордеров
        level_zone = (level_low, level_high)
        grid_data = _calculate_grid_orders(
            entry_price=float(last_price),
            level_zone=level_zone,
            side='buy' if signal_type == "LONG" else 'sell',
            qty_orders=5,
            max_risk=50.0
        )
        
        if grid_data:
            print(f"   📊 Сетка ордеров:")
            print(f"      Первый ордер: {grid_data['first_order_price']:.5f}")
            print(f"      Последний ордер: {grid_data['last_order_price']:.5f}")
            print(f"      SL: {grid_data['sl_price']:.5f}")
            print(f"      Общий объем: {grid_data['total_volume']:.2f}")
            print(f"      Общая сумма: ${grid_data['total_amount']:.2f}")
        
        # Формируем сообщение
        trend_emojis = ''.join(squares)
        
        if signal_type == "LONG":
            message = f"""🟢 **LONG** {ticker} {trend_emojis}

📊 **Текущая цена:** {last_price:.5f}
🎯 **Entry:** {entry_price:.5f}
🛡️ **SL:** {sl_price:.5f}
🎯 **TP:** {tp_price:.5f}

📈 **Key levels:** SUPPORT {level_low:.5f} - {level_high:.5f}

📊 **Сетка:** 5 ордеров (${grid_data['total_amount']:.2f})
   Первый: {grid_data['first_order_price']:.5f} (текущая)
   Последний: {grid_data['last_order_price']:.5f} (граница поддержки)
   SL: {grid_data['sl_price']:.5f} (за поддержкой)

⏰ **Таймфрейм:** {origin_tf.upper()}
🔢 **Оценка уровня:** {level_score:.2f}"""
        else:  # SHORT
            message = f"""🔴 **SHORT** {ticker} {trend_emojis}

📊 **Текущая цена:** {last_price:.5f}
🎯 **Entry:** {entry_price:.5f}
🛡️ **SL:** {sl_price:.5f}
🎯 **TP:** {tp_price:.5f}

📈 **Key levels:** RESISTANCE {level_low:.5f} - {level_high:.5f}

📊 **Сетка:** 5 ордеров (${grid_data['total_amount']:.2f})
   Первый: {grid_data['first_order_price']:.5f} (текущая)
   Последний: {grid_data['last_order_price']:.5f} (граница сопротивления)
   SL: {grid_data['sl_price']:.5f} (за сопротивлением)

⏰ **Таймфрейм:** {origin_tf.upper()}
🔢 **Оценка уровня:** {level_score:.2f}"""
        
        print(f"\n📤 СФОРМИРОВАННОЕ СООБЩЕНИЕ:")
        print("=" * 60)
        print(message)
        print("=" * 60)
        
        # Отправляем webhook
        print(f"\n🌐 Отправляем webhook...")
        webhook_success = await _send_webhook_from_level(
            symbol_usdt, 
            "buy" if signal_type == "LONG" else "sell",
            last_price, 
            level_zone, 
            tp_price
        )
        
        if webhook_success:
            print(f"   ✅ Webhook отправлен успешно")
        else:
            print(f"   ❌ Ошибка отправки webhook")
        
        print(f"\n✅ {signal_type} сигнал для {ticker} ОБРАБОТАН УСПЕШНО!")
        return True
        
    except Exception as e:
        print(f"   ❌ Ошибка обработки: {e}")
        return False

async def main():
    """Главная функция для интерактивного ввода"""
    
    print("🤖 РУЧНОЙ ПРОЦЕССОР СИГНАЛОВ")
    print("=" * 60)
    print("Введите сообщения из TRENDS Cryptovizor для обработки")
    print("Пример: $BB 🟥🟢🟢🟢🟢 FRAME:30M")
    print("Введите 'quit' для выхода")
    print("=" * 60)
    
    while True:
        try:
            message = input("\n📨 Введите сообщение: ").strip()
            
            if message.lower() in ['quit', 'exit', 'q']:
                print("👋 До свидания!")
                break
                
            if not message:
                continue
                
            await process_manual_message(message)
            
        except KeyboardInterrupt:
            print("\n👋 До свидания!")
            break
        except Exception as e:
            print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
