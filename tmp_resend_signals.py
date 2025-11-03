import asyncio, re
from telethon import TelegramClient
import httpx
from userbot import api_id, api_hash, BOT_TOKEN, BOT_CHAT_ID

SIG_RE = re.compile(r'^✅\s*([A-Z0-9]+)\s*\|\s*(LONG|SHORT)\s*Signal Found', re.M)

async def main():
    client = TelegramClient('userbot_session', api_id, api_hash)
    await client.start()
    try:
        entity = await client.get_entity('serovserov_bot')
    except Exception:
        print('ERR: cannot resolve serovserov_bot')
        return
    found = []
    async for m in client.iter_messages(entity, limit=800):
        t = (m.text or m.message or m.raw_text) or ''
        if SIG_RE.search(t):
            found.append(t.strip())
        if len(found) >= 10:
            break
    found = list(reversed(found))
    text = 'Нет найденных сигналов.'
    if found:
        text = '📬 Последние 10 сигналов (исходный текст)

' + '

'.join(found)
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', json={'chat_id': BOT_CHAT_ID, 'text': text})
        print('sendMessage status:', r.status_code)
        print('sendMessage body:', r.text[:500])

asyncio.run(main())
