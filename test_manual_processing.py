#!/usr/bin/env python3
"""
Тестовый скрипт для ручной обработки сообщений из TRENDS Cryptovizor
Показывает как работает логика без Telegram
"""

import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import _process_event, _line_to_ticker_and_squares, _is_correction_combo, _is_resistance_combo

async def test_message_processing():
    """Тестирует обработку сообщений вручную"""
    
    # Тестовые сообщения из TRENDS Cryptovizor
    test_messages = [
        # LONG сигналы
        "$BB       🟥🟢🟢🟢🟢     FRAME:30M",
        "$GRT      🟥🟢🟢🟢🟢     FRAME:30M", 
        "$STG      🟥🟢🟢🟢🟢     FRAME:30M",
        "$BOME     🟥🟢🟢🟢🟢     FRAME:30M",
        "$NOT      🟥🟢🟢🟢🟢     FRAME:30M",
        "$IO       🔴🟥🟢🟢🟢     FRAME:60M",
        
        # SHORT сигналы  
        "$ATOM     🟥🟢🟢🟢🔴     FRAME:30M",
        "$XTZ      🟥🟢🟢🟢🔴     FRAME:30M",
        "$HBAR     🟥🟢🟢🟢🔴     FRAME:30M",
        "$ZEN      🟥🟢🟢🟢🔴     FRAME:30M",
        "$SAND     🟥🟢🟢🟢🔴     FRAME:30M",
        "$SUSHI    🟥🟢🟢🟢🔴     FRAME:30M",
        "$INJ      🟥🟢🟢🟢🔴     FRAME:30M",
        "$1INCH    🟥🟢🟢🟢🔴     FRAME:30M",
        "$CAKE     🟥🟢🟢🟢🔴     FRAME:30M",
        "$OM       🟥🟢🟢🟢🔴     FRAME:30M",
        "$ICP      🟥🟢🟢🟢🔴     FRAME:30M",
        "$MBOX     🟥🟢🟢🟢🔴     FRAME:30M",
        "$GALA     🟥🟢🟢🟢🔴     FRAME:30M",
        "$SYS      🟥🟢🟢🟢🔴     FRAME:30M",
        "$ENS      🟥🟢🟢🟢🔴     FRAME:30M",
        "$AMP      🟥🟢🟢🟢🔴     FRAME:30M",
        "$GMX      🟥🟢🟢🟢🔴     FRAME:30M",
        "$USTC     🟥🟢🟢🟢🔴     FRAME:30M",
        "$ID       🟥🟢🟢🟢🔴     FRAME:30M",
        "$CYBER    🟥🟢🟢🟢🔴     FRAME:30M",
        "$CATI     🟥🟢🟢🟢🔴     FRAME:30M",
        "$USUAL    🟥🟢🟢🟢🔴     FRAME:30M",
        "$VELODROME 🟥🟢🟢🟢🔴     FRAME:30M",
        
        # Смешанные сигналы
        "$CRV      🟥🔴🔴🟢🔴     FRAME:30M",
        "$RSR      🟥🔴🟢🟢🔴     FRAME:30M",
        "$FORTH    🟥🟢🔴🔴🟢     FRAME:30M",
        "$QUICK    🟥🟢🔴🔴🟢     FRAME:30M",
        "$ACH      🟥🟢🔴🔴🔴     FRAME:30M",
        "$HOOK     🟥🟢🔴🟢🟢     FRAME:30M",
        "$SSV      🟥🟢🔴🟢🟢     FRAME:30M",
        "$XAI      🟥🔴🟢🟢🔴     FRAME:30M",
        "$MANTA    🟥🔴🔴🟢🔴     FRAME:30M",
        "$TURBO    🟥🔴🟢🟢🔴     FRAME:30M",
        "$CETUS    🟥🔴🔴🟢🔴     FRAME:30M",
    ]
    
    print("🧪 ТЕСТИРОВАНИЕ ОБРАБОТКИ СООБЩЕНИЙ")
    print("=" * 50)
    
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
        elif _is_resistance_combo(squares):
            print("🔴 НАЙДЕН SHORT СИГНАЛ") 
            signal_type = "SHORT"
        else:
            print("⚪ Нейтральный сигнал")
            continue
            
        # Симулируем обработку события
        try:
            print(f"   Обрабатываем {signal_type} сигнал для {ticker}...")
            # Здесь бы вызывался _process_event, но он требует Telegram клиент
            # Вместо этого показываем что сигнал был бы обработан
            print(f"   ✅ {signal_type} сигнал для {ticker} готов к обработке")
            
        except Exception as e:
            print(f"   ❌ Ошибка обработки: {e}")
    
    print("\n" + "=" * 50)
    print("✅ Тестирование завершено!")

if __name__ == "__main__":
    asyncio.run(test_message_processing())
