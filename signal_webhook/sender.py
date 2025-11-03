from typing import Dict, Any
import httpx
from .config import WEBHOOK_URL, DRY_RUN, DEBUG, LONG_WEBHOOK_URL


async def send_payload(payload: Dict[str, Any]) -> bool:
    side = str(payload.get("side", "")).lower()
    # Маршрутизация с возможностью принудительного выбора URL через поле _route
    route = str(payload.get("_route", ""))
    if route == 'long':
        url = LONG_WEBHOOK_URL
    elif route == 'short':
        url = WEBHOOK_URL
    else:
        # по умолчанию: buy → LONG, sell → SHORT
        url = LONG_WEBHOOK_URL if side == "buy" else WEBHOOK_URL
    dry = DRY_RUN
    if dry:
        print(f"[DRY_RUN] Would POST to {url}: {payload}")
        return True
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload)
            if DEBUG:
                try:
                    print("[webhook] url:", url)
                    print("[webhook] response status:", r.status_code)
                    print("[webhook] response body:", r.text[:500])
                except Exception:
                    pass
            r.raise_for_status()
            return True
    except httpx.HTTPStatusError as e:
        resp = e.response
        body = ''
        try:
            body = resp.text
        except Exception:
            pass
        print(f"Webhook POST failed: {e}; status={getattr(resp,'status_code',None)} body={body[:500]}")
        return False
    except Exception as e:
        print(f"Webhook POST failed: {e}")
        return False


