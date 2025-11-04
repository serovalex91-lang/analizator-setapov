from __future__ import annotations
import os
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

# === Forward target (Telegram channel) ===
FORWARD_TARGET_ID = int(os.getenv("FORWARD_TARGET_ID", "0"))
FORWARD_PREFIX = os.getenv("FORWARD_PREFIX", "📡 AUTO")

# === Watch policy ===
WATCH_INTERVAL_MIN = int(os.getenv("WATCH_INTERVAL_MIN", "60"))
WATCH_THRESHOLD = int(os.getenv("WATCH_THRESHOLD", "70"))


@dataclass
class WatchItem:
    key: str
    payload: dict
    started_ts: float
    deadline_ts: float
    interval_sec: int
    threshold: int
    forwarded: bool = False
    last_score: Optional[int] = None


class _Registry:
    def __init__(self) -> None:
        self._last_parsed: Dict[int, Dict[str, Any]] = {}
        # legacy numeric watches for /watch command compatibility
        self._watches_legacy: Dict[int, Any] = {}
        self._next_id: int = 1
        # new key-based registry
        self.watches: Dict[str, WatchItem] = {}
        self.last_forwarded_keys: Dict[str, float] = {}

    def remember_last_parsed(self, chat_id: int, parsed: Dict[str, Any]) -> None:
        self._last_parsed[chat_id] = parsed

    def get_last_parsed(self, chat_id: int) -> Optional[Dict[str, Any]]:
        return self._last_parsed.get(chat_id)

    # legacy helpers (used by /watch command elsewhere)
    def new_watch(self, chat_id: int, parsed: Dict[str, Any], threshold: float):
        wid = self._next_id
        self._next_id += 1
        item = type("LegacyWatch", (), {})()
        item.id = wid
        item.chat_id = chat_id
        item.parsed = parsed
        item.threshold = threshold
        item.forwarded = False
        item.iter = 0
        item.deadline_ts = 0.0
        item.last_score = None
        self._watches_legacy[wid] = item
        return item

    def stop(self, watch_id: int) -> None:
        self._watches_legacy.pop(watch_id, None)

    def list(self, chat_id: int) -> list[Any]:
        return [w for w in self._watches_legacy.values() if getattr(w, "chat_id", None) == chat_id]


REG = _Registry()


async def _watch_loop(tg, wi: WatchItem) -> None:
    from cursor_pipeline import orchestrate_setup_flow  # lazy import to avoid cycles
    from llm_prompt import PROMPT
    while True:
        if time.time() >= wi.deadline_ts:
            try:
                src_chat = wi.payload["parsed"].get("_meta", {}).get("src_chat_id")
                if src_chat:
                    await tg.send_message(src_chat, "⛔️ Сетап не прошёл порог за время наблюдения. Скип.")
            except Exception:
                pass
            REG.watches.pop(wi.key, None)
            return
        try:
            parsed = wi.payload["parsed"]
            preview, llm_payload, llm_res = await orchestrate_setup_flow(parsed, PROMPT, with_llm=True)
            sc = int(llm_res.get("score", 0)) if isinstance(llm_res, dict) else 0
            wi.last_score = sc
            if sc >= wi.threshold:
                try:
                    from ai_agent_bot import forward_to_channel
                    await forward_to_channel(tg, parsed, llm_res)
                except Exception:
                    pass
                wi.forwarded = True
                REG.last_forwarded_keys[wi.key] = time.time()
                REG.watches.pop(wi.key, None)
                return
        except Exception:
            pass
        await asyncio.sleep(wi.interval_sec)
