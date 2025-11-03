#!/usr/bin/env python3
"""
Тестовый скрипт для проверки обработки BTC сообщения
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import (
    _line_to_ticker_and_squares, 
    _is_correction_combo, 
    _is_resistance_combo,
    _check_12h_filters,
    _post_level_search,
    _send_webhook_from_level
)

async def test_btc_message():
    """Тестируем обработку сообщения BTC"""
    
    # Тестовое сообщение
    test_message = "$BTC 🟥🟢🟢🟢🟢 frame:30M"
    
    print("=" * 60)
    print("ТЕСТИРОВАНИЕ BTC СООБЩЕНИЯ")
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
    
    # 4. Запрос к API
    print("4. ЗАПРОС К API:")
    print(f"   URL: http://127.0.0.1:8000/levels/intraday-search")
    print(f"   Payload: {{'symbol': '{symbol_usdt}', 'context': '{context}', 'origin_tf': '{origin_tf}'}}")
    
    resp = await _post_level_search(symbol_usdt, context=context, origin_tf=origin_tf)
    print(f"   Ответ API: {resp}")
    print()
    
    if not resp or not isinstance(resp, dict):
        print("❌ Ошибка получения ответа от API")
        return
    
    if not resp.get("decision", "").startswith("enter_"):
        print(f"❌ API не рекомендует вход: {resp.get('decision', 'unknown')}")
        return
    
    # 5. Анализ ответа
    print("5. АНАЛИЗ ОТВЕТА API:")
    lvl = resp["level"]
    orders = resp.get("orders", {}) or {}
    sl = orders.get("sl", {}).get("price")
    tp_arr = orders.get("tp", [])
    tp = tp_arr[0].get("price") if tp_arr else None
    tol = float(lvl.get("tolerance") or 0.0)
    rng = lvl.get("range") or {"low": None, "high": None}
    last_price = resp.get("last_price")
    
    print(f"   Решение: {resp.get('decision')}")
    print(f"   Уровень: {lvl.get('price')}")
    print(f"   Толерантность: {tol}")
    print(f"   Диапазон: {rng.get('low')} - {rng.get('high')}")
    print(f"   Текущая цена: {last_price}")
    print(f"   SL: {sl}")
    print(f"   TP: {tp}")
    print()
    
    # 6. Проверка цены
    print("6. ПРОВЕРКА ЦЕНЫ:")
    rng_low = float(rng.get('low')) if rng.get('low') is not None else None
    rng_high = float(rng.get('high')) if rng.get('high') is not None else None
    ok_to_send = True
    
    if last_price is not None and rng_low is not None and rng_high is not None:
        inside_range = rng_low <= last_price <= rng_high
        near_low = abs(last_price - rng_low) <= tol
        near_high = abs(last_price - rng_high) <= tol
        
        print(f"   Цена в диапазоне: {inside_range}")
        print(f"   Близко к нижней границе: {near_low}")
        print(f"   Близко к верхней границе: {near_high}")
        
        if not (inside_range or near_low or near_high):
            ok_to_send = False
            print("❌ Цена не подходит для входа")
        else:
            print("✅ Цена подходит для входа")
    else:
        print("⚠️ Не удалось проверить цену")
    
    print()
    
    if not ok_to_send:
        print("❌ Сигнал не будет отправлен")
        return
    
    # 7. Отправка webhook
    print("7. ОТПРАВКА WEBHOOK:")
    try:
        await _send_webhook_from_level(symbol_usdt, "buy" if context == "long" else "sell", 
                                     orders.get('entry',{}).get('price'), sl, tp)
        print("✅ Webhook отправлен успешно")
    except Exception as e:
        print(f"❌ Ошибка отправки webhook: {e}")
    
    print()
    print("=" * 60)
    print("ТЕСТ ЗАВЕРШЕН")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_btc_message())
