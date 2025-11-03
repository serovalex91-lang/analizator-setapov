#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простая QR-авторизация для Telegram
Сохраняет сессию в userbot_session.session
"""

import asyncio
from telethon import TelegramClient
import qrcode

# API данные
api_id = 29789016
api_hash = "08f02604da51a96029d07cdd644303a4"
session_name = "userbot_session"

async def main():
    print("🔐 Запуск QR-авторизации Telegram...")
    print("=" * 50)
    
    client = TelegramClient(session_name, api_id, api_hash)
    
    await client.connect()
    
    if not await client.is_user_authorized():
        print("\n📱 Сканируй QR-код в Telegram:")
        print("   Настройки → Устройства → Подключить устройство\n")
        
        # QR-авторизация
        qr_login = await client.qr_login()
        
        # Генерация QR-кода
        qr = qrcode.QRCode(border=2)
        qr.add_data(qr_login.url)
        qr.print_ascii(invert=True)
        
        print(f"\n🔗 Или открой ссылку: {qr_login.url}\n")
        
        # Ожидание авторизации
        try:
            await qr_login.wait(timeout=300)  # 5 минут
            print("\n✅ Авторизация успешна!")
        except TimeoutError:
            print("\n❌ Таймаут! QR-код истек.")
            await client.disconnect()
            return
    else:
        print("✅ Уже авторизован!")
    
    # Проверка подключения
    me = await client.get_me()
    print(f"\n👤 Подключен как: {me.first_name}")
    if me.username:
        print(f"   Username: @{me.username}")
    print(f"   Phone: {me.phone}")
    print(f"\n💾 Сессия сохранена: {session_name}.session")
    print("=" * 50)
    
    await client.disconnect()
    print("\n✨ Готово! Теперь можешь запустить userbot.py")

if __name__ == "__main__":
    asyncio.run(main())

