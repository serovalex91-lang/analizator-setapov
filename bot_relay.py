import os
import asyncio
from typing import Set
from dotenv import load_dotenv
from telethon import TelegramClient, events
from signal_webhook.service import try_process_screener_message
import httpx


load_dotenv()


def _parse_int_set(env_name: str) -> Set[int]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            pass
    return out


API_ID = int(os.environ.get("API_ID", "29129135"))
API_HASH = os.environ.get("API_HASH", "4f2fb26f0b7f24551bd1759cb78af30c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Кто имеет право писать боту для ретрансляции (оставьте пусто, чтобы разрешить всем)
ALLOWED_USER_IDS = _parse_int_set("ALLOWED_USER_IDS")

# Куда пересылать:
#  - "sender" (по умолчанию) — в тот же чат, откуда пришло сообщение
#  - числовой chat_id (например, -1001234567890) — фиксированная цель
#  - строка (username/@link) — если указано явно
RELAY_TARGET = os.environ.get("RELAY_TARGET", "sender").strip() or "sender"
try:
    RELAY_TARGET = int(RELAY_TARGET)
except Exception:
    pass

# Включение/выключение стратегической обработки (по умолчанию выключено)
RELAY_STRATEGY_ENABLED = os.environ.get("RELAY_STRATEGY_ENABLED", "0").strip() == "1"

# HTTP-ретрансляция на ваш сервер (если задан URL — отправляем POST каждого сообщения)
RELAY_HTTP_URL = os.environ.get("RELAY_HTTP_URL", "").strip()


client = TelegramClient(
    "bot_relay_session",
    API_ID,
    API_HASH,
)


@client.on(events.NewMessage(incoming=True))
async def on_message(event):
    sender_id = getattr(event, "sender_id", None)
    if ALLOWED_USER_IDS and sender_id not in ALLOWED_USER_IDS:
        return
    # Определяем целевой чат динамически, если задано "sender"
    target = event.chat_id if RELAY_TARGET == "sender" else RELAY_TARGET
    # 1) Попытка стратегической обработки текста (если включено)
    text = event.raw_text or ""
    if RELAY_STRATEGY_ENABLED and text:
        try:
            res = await try_process_screener_message(text)
            if res is True:
                print("[bot_relay] strategy: parsed & sent webhook OK")
                try:
                    await event.reply("✅ Сетап распознан и отправлен на вебхук")
                except Exception:
                    pass
            elif res is False:
                print("[bot_relay] strategy: parsed but skipped (stale/invalid)")
                try:
                    await event.reply("⚠️ Сетап распознан, но пропущен (устарел/некорректен)")
                except Exception:
                    pass
            else:
                print("[bot_relay] strategy: not a setup")
        except Exception as e:
            print(f"[bot_relay] strategy: error {e}")

    # 2) Ретрансляция на ваш сервер по HTTP
    if RELAY_HTTP_URL:
        try:
            payload = {
                "message_id": getattr(event.message, 'id', None),
                "chat_id": getattr(event, 'chat_id', None),
                "sender_id": sender_id,
                "date": getattr(event, 'date', None).isoformat() if getattr(event, 'date', None) else None,
                "text": text,
                "has_media": bool(getattr(event.message, 'media', None)),
            }
            async with httpx.AsyncClient(timeout=15.0) as hc:
                resp = await hc.post(RELAY_HTTP_URL, json=payload)
                ok = 200 <= resp.status_code < 300
                print(f"[bot_relay] http relay: status={resp.status_code} ok={ok}")
        except Exception as e:
            print(f"[bot_relay] http relay error: {e}")
    try:
        # Пытаемся переслать оригинальное сообщение (содержит вложения/медиа)
        await client.forward_messages(target, event.message)
    except Exception:
        # Фолбэк: просто отправим текст, если он есть
        if text:
            try:
                await client.send_message(target, text)
            except Exception:
                pass


async def main():
    if not BOT_TOKEN:
        print("[bot_relay] Требуется BOT_TOKEN в переменных окружения")
        return
    print("[bot_relay] Запуск...")
    print(f"[bot_relay] target={RELAY_TARGET}")
    allow_info = ",".join(str(i) for i in sorted(ALLOWED_USER_IDS)) if ALLOWED_USER_IDS else "ANY"
    print(f"[bot_relay] allowed_user_ids={allow_info}")

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[bot_relay] Бот авторизован: @{getattr(me, 'username', None)} (id={getattr(me, 'id', None)})")
    print("[bot_relay] Ожидаю сообщения...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())


