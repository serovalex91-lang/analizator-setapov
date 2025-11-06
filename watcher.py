from __future__ import annotations
import os
import asyncio
import time
from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional
import statistics
import httpx

# === Forward target (Telegram channel) ===
FORWARD_TARGET_ID = int(os.getenv("FORWARD_TARGET_ID", "0"))
FORWARD_PREFIX = os.getenv("FORWARD_PREFIX", "📡 AUTO")

# === Watch policy ===
# Default polling interval is 15 minutes (can be overridden by env in creator of WatchItem)
WATCH_INTERVAL_MIN = int(os.getenv("WATCH_INTERVAL_MIN", "15"))
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
    step_count: int = 0


class _Registry:
    def __init__(self) -> None:
        self._last_parsed: Dict[int, Dict[str, Any]] = {}
        # legacy numeric watches for /watch command compatibility
        self._watches_legacy: Dict[int, Any] = {}
        self._next_id: int = 1
        # new key-based registry
        self.watches: Dict[str, WatchItem] = {}
        self.last_forwarded_keys: Dict[str, float] = {}
        # indicator history per watch-key (or symbol) to enable dynamic analysis in LLM
        # REG.history[watch_key] = [{"timestamp": iso, "adx4h": float, ...}, ...]
        self.history: Dict[str, list[dict]] = {}

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
    from cursor_pipeline import orchestrate_setup_flow, fetch_last_price  # lazy import to avoid cycles
    from llm_prompt import PROMPT
    logger = logging.getLogger("watcher")
    logger.info("WATCH started: key=%s", wi.key)
    # simple alert cooldown state
    last_alert = {"type": None, "ts": 0.0}

    def _should_alert(kind: str, cooldown_sec: int = None) -> bool:
        try:
            cd = int(os.getenv("CANCEL_COOLDOWN_SEC", "600")) if cooldown_sec is None else cooldown_sec
        except Exception:
            cd = 600
        now = time.time()
        if last_alert.get("type") != kind or (now - float(last_alert.get("ts") or 0.0) > cd):
            last_alert["type"] = kind
            last_alert["ts"] = now
            return True
        return False

    async def fetch_candles(symbol_usdt: str, interval: str, limit: int = 36) -> list[dict]:
        try:
            url = "https://api.binance.com/api/v3/klines"
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params={"symbol": symbol_usdt.upper(), "interval": interval, "limit": limit})
                if r.status_code != 200:
                    return []
                rows = r.json() or []
                out = []
                for k in rows:
                    # [ openTime, open, high, low, close, volume, closeTime, ... ]
                    out.append({
                        "ts": int(k[0])//1000,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
                return out
        except Exception:
            return []

    def _ema(vals: list[float], span: int = 12) -> float:
        try:
            if not vals:
                return 0.0
            alpha = 2.0 / (span + 1)
            ema = vals[0]
            for v in vals[1:]:
                ema = alpha * v + (1 - alpha) * ema
            return float(ema)
        except Exception:
            return 0.0

    async def detect_volume_spike(symbol_usdt: str) -> dict:
        c5 = await fetch_candles(symbol_usdt, "5m", limit=36)
        if len(c5) < 3:
            return {"spike": False}
        try:
            vol = [c["volume"] for c in c5[:-1]]
            v_now = float(c5[-1]["volume"])
            px_prev = float(c5[-2]["close"])
            px_now = float(c5[-1]["close"])
            ret5m = (px_now / px_prev - 1.0) * 100.0
            median_v = statistics.median(vol)
            ema_v = _ema(vol, span=12)
            spike = (v_now >= max(3.0 * median_v, 2.0 * ema_v)) and (abs(ret5m) >= 1.2)
            direction = "up" if ret5m > 0 else "down"
            return {"spike": spike, "dir": direction, "ret5m": ret5m, "v_now": v_now, "median_v": median_v, "ema_v": ema_v}
        except Exception:
            return {"spike": False}
    while True:
        if time.time() >= wi.deadline_ts:
            logger.info("WATCH deadline reached: key=%s", wi.key)
            try:
                src_chat = wi.payload["parsed"].get("_meta", {}).get("src_chat_id")
                symbol = (wi.payload.get("parsed") or {}).get("ticker") or "?"
                if src_chat:
                    await tg.send_message(src_chat, f"⛔️ {symbol}: сетап не прошёл порог за время наблюдения. Скип.")
            except Exception:
                pass
            REG.watches.pop(wi.key, None)
            return
        try:
            parsed = wi.payload["parsed"]
            # Prepare recent indicator history for this watch (last 16 points)
            wi.step_count += 1
            skip_heavy = (wi.step_count % 3) != 0  # обновляем тяжёлые ТФ раз в 3 тика (~45 мин)
            hist = (REG.history.get(wi.key) or [])[-16:]
            # SL auto-stop check before heavy work
            symbol = (parsed.get("ticker") or "").upper()
            direction = (parsed.get("direction") or "").lower()
            hard_sl = parsed.get("sl")
            px_now = None
            if symbol and hard_sl is not None:
                try:
                    px_now = await fetch_last_price(f"{symbol}USDT")
                except Exception:
                    px_now = None
            if px_now is not None and direction in ("long", "short"):
                hit = (direction == "long" and px_now <= hard_sl) or (direction == "short" and px_now >= hard_sl)
                if hit:
                    try:
                        src_chat = parsed.get("_meta", {}).get("src_chat_id")
                        if src_chat:
                            await tg.send_message(src_chat, f"⛔️ {symbol}: цена {px_now:.4f} пробила жёсткий SL {float(hard_sl):.4f}. Наблюдение остановлено.")
                    except Exception:
                        pass
                    REG.watches.pop(wi.key, None)
                    return

            # 5m volume spike detector
            vol_sym = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
            vol_ctx = await detect_volume_spike(vol_sym) if symbol else {"spike": False}

            # LLM cadence: call rarely to avoid bursts
            LLM_TICK_STRIDE = int(os.getenv("LLM_TICK_STRIDE", "3"))
            use_llm = (wi.step_count % max(1, LLM_TICK_STRIDE) == 0)
            preview, llm_payload, llm_res = await orchestrate_setup_flow(
                parsed, PROMPT, with_llm=use_llm, history_data=hist, skip_heavy_tf=skip_heavy, volume_context=vol_ctx
            )
            sc = int(llm_res.get("score", 0)) if (use_llm and isinstance(llm_res, dict)) else (wi.last_score or 0)
            wi.last_score = sc
            logger.info("WATCH tick: key=%s last_score=%s threshold=%s", wi.key, wi.last_score, wi.threshold)
            # act-based alerts from LLM
            try:
                act = (llm_res or {}).get("action") or {}
                rec = (act.get("recommendation") or "").lower()
                src_chat = parsed.get("_meta", {}).get("src_chat_id")
                if rec in ("avoid", "exit_immediate") and src_chat and _should_alert(rec):
                    if rec == "avoid":
                        msg = f"⚠️ {symbol}: условия ухудшились — вход ОТМЕНИТЬ. Причина: {act.get('reason','')}"
                        await tg.send_message(src_chat, msg)
                    else:
                        msg = f"⛔️ {symbol}: выход ИЗ ПОЗИЦИИ (немедленно). Причина: {act.get('reason','')}"
                        await tg.send_message(src_chat, msg)
                        REG.watches.pop(wi.key, None)
                        return
            except Exception:
                pass
            # score-based cancel hint
            try:
                cancel_score = int(os.getenv("CANCEL_SCORE", "35"))
                if (wi.last_score or 0) <= cancel_score and _should_alert("score_low"):
                    src_chat = parsed.get("_meta", {}).get("src_chat_id")
                    if src_chat:
                        await tg.send_message(src_chat, f"⚠️ {symbol}: score упал до {wi.last_score}/100 — идею лучше ОТМЕНИТЬ.")
            except Exception:
                pass
            # Update history with latest indicators snapshot for dynamics
            try:
                ta = (llm_payload or {}).get("taapi") or {}
                filters = (llm_payload or {}).get("taapi", {}).get("filters") or {}
                record = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "adx4h": ((ta.get("adx") or {}).get("4h")),
                    "adx12h": ((ta.get("adx") or {}).get("12h")),
                    "macd4h": ((ta.get("macd") or {}).get("4h") or {}).get("hist"),
                    "macd12h": ((ta.get("macd") or {}).get("12h") or {}).get("hist"),
                    "rsi12h": filters.get("rsi_12h"),
                    "obv_trend": (ta.get("obv") or {}).get("4h_trend"),
                }
                buf = REG.history.get(wi.key) or []
                buf.append(record)
                # clamp to 16
                if len(buf) > 16:
                    buf = buf[-16:]
                REG.history[wi.key] = buf
            except Exception:
                pass
            # block entry on opposite volume spike
            try:
                block_by_volume = False
                if (vol_ctx or {}).get("spike"):
                    if direction == "short" and (vol_ctx or {}).get("dir") == "up":
                        block_by_volume = True
                    if direction == "long" and (vol_ctx or {}).get("dir") == "down":
                        block_by_volume = True
                if block_by_volume:
                    # soften by reducing score to avoid forwarding
                    wi.last_score = max(0, (wi.last_score or 0) - 10)
            except Exception:
                pass

            if (wi.last_score or 0) >= wi.threshold:
                logger.info("WATCH promote to FORWARD: key=%s score=%s", wi.key, wi.last_score)
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
            logger.exception("WATCH tick error: key=%s", wi.key)
        await asyncio.sleep(wi.interval_sec)
