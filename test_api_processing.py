#!/usr/bin/env python3
"""
Тестовый скрипт для полного тестирования обработки сообщений с API запросами
Показывает как работает вся логика без Telegram
"""

import asyncio
import sys
import os
import httpx
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import _post_level_search, _line_to_ticker_and_squares, _is_correction_combo, _is_resistance_combo

async def test_api_processing():
    """Тестирует полную обработку сообщений с API запросами"""
    
    # Тестовые сообщения из TRENDS Cryptovizor
    test_messages = [
        # LONG сигналы
        "$BB       🟥🟢🟢🟢🟢     FRAME:30M",
        "$GRT      🟥🟢🟢🟢🟢     FRAME:30M", 
        "$BOME     🟥🟢🟢🟢🟢     FRAME:30M",
        
        # SHORT сигналы  
        "$ATOM     🟥🟢🟢🟢🔴     FRAME:30M",
        "$SAND     🟥🟢🟢🟢🔴     FRAME:30M",
        "$SUSHI    🟥🟢🟢🟢🔴     FRAME:30M",
    ]
    
    print("🧪 ТЕСТИРОВАНИЕ ПОЛНОЙ ОБРАБОТКИ С API")
    print("=" * 60)
    
    for i, message in enumerate(test_messages, 1):
        print(f"\n📨 Сообщение {i}: {message}")
        
        # Парсим сообщение
        result = _line_to_ticker_and_squares(message)
        if not result:
            print("❌ Не удалось распарсить сообщение")
            continue
            
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
            print("⚪ Нейтральный сигнал")
            continue
            
        # Тестируем API запрос
        try:
            print(f"   🔄 Отправляем запрос к API для {ticker}...")
            symbol_usdt = f"{ticker}USDT"
            
            resp = await _post_level_search(symbol_usdt, context=context, origin_tf=origin_tf)
            
            if resp and isinstance(resp, dict):
                decision = resp.get("decision", "")
                reason = resp.get("reason", "")
                level = resp.get("level", {})
                orders = resp.get("orders", {})
                last_price = resp.get("last_price")
                trade_setup = resp.get("trade_setup", {})
                
                print(f"   📊 API ответ:")
                print(f"      Решение: {decision}")
                print(f"      Причина: {reason}")
                
                if level:
                    price = level.get("price", 0)
                    score = level.get("score", 0)
                    print(f"      Уровень: {price:.6f} (оценка: {score:.2f})")
                
                if orders:
                    entry = orders.get("entry", {})
                    sl = orders.get("sl", {})
                    tp = orders.get("tp", [])
                    
                    if entry:
                        print(f"      Entry: {entry.get('price', 'N/A')}")
                    if sl:
                        print(f"      SL: {sl.get('price', 'N/A')}")
                    if tp:
                        print(f"      TP: {tp[0].get('price', 'N/A')}")
                
                if last_price:
                    print(f"      Текущая цена: {last_price:.6f}")
                
                if trade_setup:
                    risk_percent = trade_setup.get("risk_percent", 0)
                    reward_percent = trade_setup.get("reward_percent", 0)
                    risk_reward = trade_setup.get("risk_reward_ratio", 0)
                    print(f"      Риск: {risk_percent:.2f}%")
                    print(f"      Награда: {reward_percent:.2f}%")
                    print(f"      R/R: {risk_reward:.2f}")
                
                if decision.startswith("enter_"):
                    print(f"   ✅ {signal_type} сигнал для {ticker} ПОДТВЕРЖДЕН API")
                else:
                    print(f"   ⚠️ {signal_type} сигнал для {ticker} ОТКЛОНЕН API")
                    
            else:
                print(f"   ❌ API не ответил для {ticker}")
                
        except Exception as e:
            print(f"   ❌ Ошибка API запроса: {e}")
    
    print("\n" + "=" * 60)
    print("✅ Тестирование завершено!")

if __name__ == "__main__":
    asyncio.run(test_api_processing())
