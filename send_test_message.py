#!/usr/bin/env python3
"""
Скрипт для отправки тестового сообщения в Telegram
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from userbot import client, notify

async def send_test_message():
    """Отправляем тестовое сообщение HBAR"""
    
    # Тестовое сообщение
    test_message = "$HBAR 🟥🟢🟢🟢🟢 frame:30M"
    
    print("Отправляем тестовое сообщение в Telegram...")
    print(f"Сообщение: {test_message}")
    
    try:
        # Отправляем сообщение себе
        await notify(test_message)
        print("✅ Сообщение отправлено успешно!")
        
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")

if __name__ == "__main__":
    asyncio.run(send_test_message())
