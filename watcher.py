from __future__ import annotations
import os
from typing import Any, Dict, Optional

# === Forward target (Telegram channel) ===
FORWARD_TARGET_ID = int(os.getenv("FORWARD_TARGET_ID", "-1003249126475"))
FORWARD_PREFIX = os.getenv("FORWARD_PREFIX", "[AUTO-FWD]")

# === Watch policy ===
WATCH_DEFAULT_HOURS = int(os.getenv("WATCH_DEFAULT_HOURS", "12"))
WATCH_INTERVAL_MIN = int(os.getenv("WATCH_INTERVAL_MIN", "60"))
WATCH_ENTER_SCORE = float(os.getenv("WATCH_ENTER_SCORE", "70"))


class WatchItem:
    def __init__(self, watch_id: int, chat_id: int, parsed: Dict[str, Any], threshold: float) -> None:
        self.id = watch_id
        self.chat_id = chat_id
        self.parsed = parsed
        self.threshold = threshold
        self.forwarded: bool = False
        self.iter: int = 0
        self.deadline_ts: float = 0.0
        self.last_score: Optional[float] = None


class _Registry:
    def __init__(self) -> None:
        self._last_parsed: Dict[int, Dict[str, Any]] = {}
        self._watches: Dict[int, WatchItem] = {}
        self._next_id: int = 1

    def remember_last_parsed(self, chat_id: int, parsed: Dict[str, Any]) -> None:
        self._last_parsed[chat_id] = parsed

    def get_last_parsed(self, chat_id: int) -> Optional[Dict[str, Any]]:
        return self._last_parsed.get(chat_id)

    def new_watch(self, chat_id: int, parsed: Dict[str, Any], threshold: float) -> WatchItem:
        wid = self._next_id
        self._next_id += 1
        item = WatchItem(wid, chat_id, parsed, threshold)
        self._watches[wid] = item
        return item

    def stop(self, watch_id: int) -> None:
        self._watches.pop(watch_id, None)

    def list(self, chat_id: int) -> list[WatchItem]:
        return [w for w in self._watches.values() if w.chat_id == chat_id]


REG = _Registry()
