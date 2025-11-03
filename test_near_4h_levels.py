#!/usr/bin/env python3
"""
Тестовый скрипт для проверки уровней NEARUSDT на 4h таймфрейме
"""
import sys
import os
import asyncio
import httpx

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

async def test_near_4h_levels():
    """Тестируем уровни NEARUSDT на 4h"""
    
    print("=" * 80)
    print("ТЕСТИРОВАНИЕ УРОВНЕЙ NEARUSDT НА 4H")
    print("=" * 80)
    
    # URL локального API
    api_url = "http://localhost:8001/levels/intraday-search"
    
    # Данные для запроса
    data = {
        "symbol": "NEARUSDT",
        "context": "long",
        "origin_tf": "30m"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            print(f"🔍 Запрашиваем уровни для {data['symbol']} в контексте {data['context']}...")
            
            response = await client.post(api_url, json=data)
            
            if response.status_code == 200:
                response_data = response.json()
                print("✅ Успешный ответ от API")
                
                # Выводим структуру ответа
                print(f"\n📊 Структура ответа:")
                print(f"   - decision: {response_data.get('decision')}")
                print(f"   - reason: {response_data.get('reason')}")
                print(f"   - last_price: {response_data.get('last_price')}")
                
                # Проверяем level
                level = response_data.get('level')
                if level:
                    print(f"\n🎯 Найденный уровень:")
                    print(f"   - type: {level.get('type')}")
                    print(f"   - price: {level.get('price')}")
                    print(f"   - strength: {level.get('strength')}")
                    print(f"   - age: {level.get('age')}")
                    
                    # Проверяем range
                    if 'range' in level:
                        print(f"   - range: {level.get('range')}")
                    
                    # Проверяем debug_pivots
                    if 'debug_pivots' in level:
                        debug_pivots = level.get('debug_pivots', {})
                        print(f"   - debug_pivots: {debug_pivots}")
                        if 'S1' in debug_pivots:
                            print(f"     - S1 (поддержка): {debug_pivots['S1']}")
                        if 'R1' in debug_pivots:
                            print(f"     - R1 (сопротивление): {debug_pivots['R1']}")
                
                # Проверяем trade_setup
                trade_setup = response_data.get('trade_setup')
                if trade_setup:
                    print(f"\n📈 Trade Setup:")
                    print(f"   - trade_setup: {trade_setup}")
                    
                    # Проверяем debug_pivots в trade_setup
                    if 'debug_pivots' in trade_setup:
                        debug_pivots = trade_setup.get('debug_pivots', {})
                        print(f"   - debug_pivots: {debug_pivots}")
                        if 'S1' in debug_pivots:
                            print(f"     - S1 (поддержка): {debug_pivots['S1']}")
                        if 'R1' in debug_pivots:
                            print(f"     - R1 (сопротивление): {debug_pivots['R1']}")
                
                else:
                    print(f"❌ Ошибка API: {response_data.get('reason')}")
                    
            else:
                print(f"❌ HTTP ошибка: {response.status_code}")
                print(f"   Ответ: {response.text}")
                
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
    
    print("\n" + "=" * 80)
    print("ТЕСТ ЗАВЕРШЕН")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_near_4h_levels())
