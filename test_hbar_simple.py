#!/usr/bin/env python3
"""
Упрощенный тест HBAR без API (только фильтры RSI/EMA)
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import (
    _line_to_ticker_and_squares, 
    _is_correction_combo, 
    _is_resistance_combo,
    _check_12h_filters
)

async def test_hbar_simple():
    """Тестируем обработку сообщения HBAR без API"""
    
    # Тестовое сообщение
    test_message = "$HBAR 🟥🟢🟢🟢🟢 frame:30M"
    
    print("=" * 60)
    print("ТЕСТИРОВАНИЕ HBAR СООБЩЕНИЯ (БЕЗ API)")
    print("=" * 60)
    print(f"Сообщение: {test_message}")
    print()
    
    # 1. Парсинг сообщения
    print("1. ПАРСИНГ СООБЩЕНИЯ:")
    ticker, squares, origin_tf = _line_to_ticker_and_squares(test_message)
    print(f"   Тикер: {ticker}")
    print(f"   Эмодзи: {squares}")
    print(f"   Таймфрейм: {origin_tf}")
    print()
    
    if not ticker:
        print("❌ Ошибка парсинга тикера")
        return
    
    symbol_usdt = ticker if ticker.endswith("USDT") else f"{ticker}USDT"
    print(f"   Символ: {symbol_usdt}")
    print()
    
    # 2. Проверка комбинации эмодзи
    print("2. ПРОВЕРКА КОМБИНАЦИИ ЭМОДЗИ:")
    is_long = _is_correction_combo(squares)
    is_short = _is_resistance_combo(squares)
    print(f"   LONG комбинация: {is_long}")
    print(f"   SHORT комбинация: {is_short}")
    print()
    
    if not is_long and not is_short:
        print("❌ Неизвестная комбинация эмодзи")
        return
    
    context = "long" if is_long else "short"
    print(f"   Контекст: {context}")
    print()
    
    # 3. Проверка фильтров RSI/EMA
    print("3. ПРОВЕРКА ФИЛЬТРОВ RSI/EMA 12h:")
    filters_ok = await _check_12h_filters(symbol_usdt, context)
    print(f"   Фильтры пройдены: {filters_ok}")
    print()
    
    if not filters_ok:
        print("❌ Сигнал заблокирован фильтрами RSI/EMA")
        return
    
    # 4. Симуляция успешного сигнала
    print("4. СИМУЛЯЦИЯ УСПЕШНОГО СИГНАЛА:")
    print("   ✅ Все проверки пройдены")
    print("   ✅ Сигнал будет отправлен в Telegram")
    print("   ✅ Webhook будет отправлен на Finandy")
    print()
    
    # 5. Формирование сообщения для Telegram
    print("5. СООБЩЕНИЕ ДЛЯ TELEGRAM:")
    trend_emojis = ''.join(squares)
    current_time = "22:50 11.09.2025"  # Примерное время
    
    msg = (
        f"${symbol_usdt.replace('USDT', '')} {origin_tf} Binance #Futures\n"
        f"TREND {trend_emojis}\n"
        f"MA 🟢 RSI 🟢 {current_time}\n"
        f"Volume 1D       0.0 M\n"
        f"CD Week         +0.00 M\n"
        f"Long 📈\n\n"
        f"⌛️ Entry: 0.234880\n"
        f"☑️ TP: 0.250000 +6.45%\n"
        f"✖️ SL: 0.220000 -6.32%\n"
        f"🎲 Risk-reward: 1.0\n\n"
        f"Comment: Получен сигнал о начале коррекции, сгенерирован торговый сетап и отправлен в терминал. | "
        f"Key levels: SUPPORT 0.230000 - 0.240000 | "
        f"Current: 0.234880 (+0.00%)"
    )
    
    print(msg)
    print()
    
    print("=" * 60)
    print("ТЕСТ ЗАВЕРШЕН УСПЕШНО")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_hbar_simple())
