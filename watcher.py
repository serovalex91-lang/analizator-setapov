from __future__ import annotations
import os
import asyncio
import time
from dataclasses import dataclass
from typing import Literal
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
FORWARD_THRESHOLD = int(os.getenv("FORWARD_THRESHOLD", "60"))

# Hysteresis & cooldowns (minutes)
ENTRY_HYSTERESIS = int(os.getenv("ENTRY_HYSTERESIS", "5"))  # open ≥60, close ≤60-5
ENTRY_ALERT_COOLDOWN_MIN = int(os.getenv("ENTRY_ALERT_COOLDOWN_MIN", "60"))
HOLD_ALERT_COOLDOWN_MIN = int(os.getenv("HOLD_ALERT_COOLDOWN_MIN", "120"))
TP_EXTEND_COOLDOWN_MIN = int(os.getenv("TP_EXTEND_COOLDOWN_MIN", "180"))
DANGER_ALERT_COOLDOWN_MIN = int(os.getenv("DANGER_ALERT_COOLDOWN_MIN", "60"))

# Post-entry score gates
HOLD_MIN_SCORE = int(os.getenv("HOLD_MIN_SCORE", "60"))
DOWNGRADE_SCORE = int(os.getenv("DOWNGRADE_SCORE", "45"))
EXIT_SCORE = int(os.getenv("EXIT_SCORE", "35"))

# TP extension rules
TP_EXTENSION_MAX = float(os.getenv("TP_EXTENSION_MAX", "0.20"))  # +20% cap
TP_EXTENSION_STEP_ATR4H = float(os.getenv("TP_EXTENSION_STEP_ATR4H", "1.0"))
TP_EXTENSION_MIN_DELTA_ATR = float(os.getenv("TP_EXTENSION_MIN_DELTA_ATR", "0.5"))


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
    # New state for extended watcher flow
    phase: Literal["pre_entry", "post_entry"] = "pre_entry"
    entry_price: Optional[float] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    holding_since: Optional[float] = None
    # entry-window state
    entry_window_open: bool = False
    _entry_ok_streak: int = 0
    _entry_bad_streak: int = 0
    # TP extension state
    _last_tp_extend_ts: float = 0.0
    _base_tp_initial: Optional[float] = None


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
    from cursor_pipeline import orchestrate_setup_flow  # lazy import to avoid cycles
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

    async def fetch_futures_price(symbol_usdt: str) -> Optional[float]:
        """Prefer Binance Futures mark price; fallback to last price."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Mark price (premiumIndex)
                r = await client.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": symbol_usdt.upper()})
                if r.status_code == 200:
                    jd = r.json() or {}
                    mp = jd.get("markPrice")
                    if mp is not None:
                        return float(mp)
                # Fallback: futures last price
                r2 = await client.get("https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": symbol_usdt.upper()})
                if r2.status_code == 200:
                    jd2 = r2.json() or {}
                    px = jd2.get("price")
                    if px is not None:
                        return float(px)
        except Exception:
            return None
        return None

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
            # cache baseline levels to WatchItem
            try:
                if wi.tp is None:
                    wi.tp = parsed.get("tp")
                if wi.sl is None:
                    wi.sl = parsed.get("sl")
            except Exception:
                pass
            # Prepare recent indicator history for this watch (last 16 points)
            wi.step_count += 1
            skip_heavy = (wi.step_count % 3) != 0  # обновляем тяжёлые ТФ раз в 3 тика (~45 мин)
            hist = (REG.history.get(wi.key) or [])[-16:]
            # SL auto-stop check before heavy work (use Futures mark/last price)
            symbol = (parsed.get("ticker") or "").upper()
            direction = (parsed.get("direction") or "").lower()
            hard_sl = (wi.sl if wi.sl is not None else parsed.get("sl"))
            px_now = None
            if symbol and hard_sl is not None:
                try:
                    px_now = await fetch_futures_price(f"{symbol}USDT")
                except Exception:
                    px_now = None
            if px_now is not None and direction in ("long", "short"):
                hit = (direction == "long" and px_now <= hard_sl) or (direction == "short" and px_now >= hard_sl)
                if hit:
                    try:
                        src_chat = parsed.get("_meta", {}).get("src_chat_id")
                        if src_chat:
                            await tg.send_message(src_chat, f"⛔️ {symbol}: цена {px_now:.4f} пробила жёсткий SL {float(hard_sl):.4f}. Наблюдение остановлено.")
                        if FORWARD_TARGET_ID:
                            try:
                                kshort = wi.key[:6]
                                await tg.send_message(FORWARD_TARGET_ID, f"⛔️ [{symbol}] k={kshort} — пробит жёсткий SL ({px_now:.4f} vs {float(hard_sl):.4f}).")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    REG.watches.pop(wi.key, None)
                    return
            # TP auto-finish in post_entry
            try:
                hard_tp = (wi.tp if wi.tp is not None else parsed.get("tp"))
                if px_now is not None and hard_tp is not None and direction in ("long","short") and wi.phase == "post_entry":
                    tp_hit = (direction == "long" and px_now >= hard_tp) or (direction == "short" and px_now <= hard_tp)
                    if tp_hit:
                        try:
                            src_chat = parsed.get("_meta", {}).get("src_chat_id")
                            if src_chat:
                                await tg.send_message(src_chat, f"🎯 {symbol}: цель {float(hard_tp):.4f} достигнута. Наблюдение завершено.")
                        except Exception:
                            pass
                        REG.watches.pop(wi.key, None)
                        return
            except Exception:
                pass

            # 5m volume spike detector
            vol_sym = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
            vol_ctx = await detect_volume_spike(vol_sym) if symbol else {"spike": False}

            # LLM cadence: call rarely to avoid bursts
            LLM_TICK_STRIDE = int(os.getenv("LLM_TICK_STRIDE", "3"))
            use_llm = (wi.step_count % max(1, LLM_TICK_STRIDE) == 0)
            preview, llm_payload, llm_res = await orchestrate_setup_flow(
                parsed, PROMPT, with_llm=use_llm, history_data=hist, skip_heavy_tf=skip_heavy, volume_context=vol_ctx
            )
            # Update score only when LLM actually ran; otherwise keep previous value (may be None initially)
            sc: Optional[int] = wi.last_score
            if use_llm and isinstance(llm_res, dict):
                try:
                    val = llm_res.get("score")
                    if isinstance(val, (int, float)):
                        sc = int(val)
                except Exception:
                    pass
            wi.last_score = sc
            # fast-TF snapshot for telemetry
            try:
                tf_fast = (llm_payload or {}).get("taapi") or {}
                adx1h = ((tf_fast.get("adx_fast") or {}).get("1h"))
                adx15 = ((tf_fast.get("adx_fast") or {}).get("15m"))
                macd1h = ((tf_fast.get("macd_fast") or {}).get("1h") or {}).get("hist")
                macd15 = ((tf_fast.get("macd_fast") or {}).get("15m") or {}).get("hist")
                rsi15 = ((tf_fast.get("rsi_fast") or {}).get("15m"))
            except Exception:
                adx1h = adx15 = macd1h = macd15 = rsi15 = None

            # RR feasibility
            rr_ok = None
            try:
                cp = parsed.get("current_price") or px_now
                if cp is not None and wi.sl is not None and wi.tp is not None:
                    num = abs(float(wi.tp) - float(cp))
                    den = max(1e-9, abs(float(cp) - float(wi.sl)))
                    rr_ok = (num / den) >= 3.0
            except Exception:
                rr_ok = None

            logger.info(
                "[watcher] tick: key=%s phase=%s last_score=%s score_known=%s rr_ok=%s entry_window=%s",
                wi.key,
                wi.phase,
                (wi.last_score if wi.last_score is not None else "n/a"),
                (wi.last_score is not None),
                rr_ok,
                ("open" if getattr(wi, "entry_window_open", False) else "closed"),
            )
            try:
                logger.info(
                    "[watcher] fast_tf: m15(adx=%s, macd_hist=%s, rsi=%s) h1(adx=%s, macd_hist=%s)",
                    adx15, macd15, rsi15, adx1h, macd1h,
                )
            except Exception:
                pass
            # keep a small buffer of scores for trend checks
            try:
                score_series = (wi.payload.get("_score_series") or [])
                if wi.last_score is not None:
                    score_series.append(int(wi.last_score))
                    if len(score_series) > 16:
                        score_series = score_series[-16:]
                wi.payload["_score_series"] = score_series
            except Exception:
                pass
            # act-based alerts from LLM (phase-aware)
            try:
                act = (llm_res or {}).get("action") or {}
                rec = (act.get("recommendation") or "").lower()
                phase = (act.get("phase") or wi.phase or "pre_entry").lower()
                src_chat = parsed.get("_meta", {}).get("src_chat_id")
                if wi.phase == "post_entry" and rec == "hold" and src_chat and _should_alert("hold", cooldown_sec=HOLD_ALERT_COOLDOWN_MIN*60):
                    try:
                        await tg.send_message(src_chat, f"📈 {symbol}: тренд усиливается, держим позицию. Score {wi.last_score}/100.")
                    except Exception:
                        pass
                    try:
                        if FORWARD_TARGET_ID:
                            kshort = wi.key[:6]
                            await tg.send_message(FORWARD_TARGET_ID, f"📈 [{symbol}] k={kshort} — держим позицию. Score {wi.last_score}/100.")
                    except Exception:
                        pass
                if rec in ("avoid", "exit_immediate") and src_chat and _should_alert(rec, cooldown_sec=DANGER_ALERT_COOLDOWN_MIN*60):
                    if rec == "avoid":
                        msg = f"⚠️ {symbol}: условия ухудшились — вход ОТМЕНИТЬ. Причина: {act.get('reason','')}"
                        await tg.send_message(src_chat, msg)
                        try:
                            if FORWARD_TARGET_ID:
                                kshort = wi.key[:6]
                                await tg.send_message(FORWARD_TARGET_ID, f"⚠️ [{symbol}] k={kshort} — вход ОТМЕНИТЬ. {act.get('reason','')}")
                        except Exception:
                            pass
                    else:
                        msg = f"⛔️ {symbol}: выход ИЗ ПОЗИЦИИ (немедленно). Причина: {act.get('reason','')}"
                        await tg.send_message(src_chat, msg)
                        try:
                            if FORWARD_TARGET_ID:
                                kshort = wi.key[:6]
                                await tg.send_message(FORWARD_TARGET_ID, f"⛔️ [{symbol}] k={kshort} — выход ИЗ ПОЗИЦИИ. {act.get('reason','')}")
                        except Exception:
                            pass
                        REG.watches.pop(wi.key, None)
                        return
                # promote phase if LLM signals post_entry explicitly
                try:
                    if wi.phase == "pre_entry" and phase == "post_entry":
                        wi.phase = "post_entry"
                        if wi.entry_price is None:
                            wi.entry_price = parsed.get("current_price") or px_now
                        wi.holding_since = wi.holding_since or time.time()
                except Exception:
                    pass
            except Exception:
                pass
            # score-based cancel/danger hint (anti-spam cooldown)
            try:
                if wi.phase == "post_entry" and wi.last_score is not None and wi.last_score <= DOWNGRADE_SCORE and _should_alert("danger", cooldown_sec=DANGER_ALERT_COOLDOWN_MIN*60):
                    src_chat = parsed.get("_meta", {}).get("src_chat_id")
                    if src_chat:
                        await tg.send_message(src_chat, f"⚠️ {symbol}: тренд ослаб (score {wi.last_score}/100). Рекомендуется частичная фиксация.")
                    try:
                        if FORWARD_TARGET_ID:
                            kshort = wi.key[:6]
                            await tg.send_message(FORWARD_TARGET_ID, f"⚠️ [{symbol}] k={kshort} — тренд ослаб (score {wi.last_score}/100).")
                    except Exception:
                        pass
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
            # block entry on opposite volume spike (pre_entry only)
            try:
                block_by_volume = False
                if wi.phase == "pre_entry" and (vol_ctx or {}).get("spike") and wi.last_score is not None:
                    if direction == "short" and (vol_ctx or {}).get("dir") == "up":
                        block_by_volume = True
                    if direction == "long" and (vol_ctx or {}).get("dir") == "down":
                        block_by_volume = True
                if block_by_volume:
                    # soften by reducing score to avoid forwarding
                    wi.last_score = max(0, (wi.last_score or 0) - 10)
                try:
                    logger.info(
                        "[watcher] opp_spike_block=%s (5m ret=%.2f%%, v_now=%.2f, median=%.2f)",
                        bool(block_by_volume),
                        float((vol_ctx or {}).get("ret5m") or 0.0),
                        float((vol_ctx or {}).get("v_now") or 0.0),
                        float((vol_ctx or {}).get("median_v") or 0.0),
                    )
                except Exception:
                    pass
            except Exception:
                pass

            # Phase logic with anti-spam transitions and hysteresis
            try:
                if wi.phase == "pre_entry":
                    # entry-window open/close with hysteresis and debounce (2 bars)
                    open_gate = FORWARD_THRESHOLD
                    close_gate = max(0, FORWARD_THRESHOLD - ENTRY_HYSTERESIS)
                    llm_rec = ((llm_res or {}).get("action") or {}).get("recommendation", "").lower()
                    cond_ok = (wi.last_score or 0) >= open_gate and llm_rec != "avoid" and not ((vol_ctx or {}).get("spike") and ((direction=="long" and (vol_ctx or {}).get("dir")=="down") or (direction=="short" and (vol_ctx or {}).get("dir")=="up")))
                    if cond_ok:
                        wi._entry_ok_streak = min(10, (wi._entry_ok_streak or 0) + 1)
                        wi._entry_bad_streak = 0
                    else:
                        wi._entry_bad_streak = min(10, (wi._entry_bad_streak or 0) + 1)
                        wi._entry_ok_streak = 0
                    debounce_ok = wi._entry_ok_streak >= 2
                    if not wi.entry_window_open and debounce_ok:
                        wi.entry_window_open = True
                        # announce entry window open (not more often than cooldown)
                        cooldown_ok = _should_alert("entry_open", cooldown_sec=ENTRY_ALERT_COOLDOWN_MIN*60)
                        try:
                            logger.info(
                                "[watcher] entry_window open (reason=score>=%, debounce_ok=%s, hysteresis_gate=%s, cooldown_ok=%s)",
                                open_gate, debounce_ok, open_gate, cooldown_ok,
                            )
                        except Exception:
                            pass
                        if cooldown_ok:
                            try:
                                src_chat = parsed.get("_meta", {}).get("src_chat_id")
                                rr = None
                                try:
                                    cp = parsed.get("current_price") or px_now
                                    if cp is not None and wi.sl is not None and wi.tp is not None:
                                        num = abs(float(wi.tp) - float(cp))
                                        den = max(1e-9, abs(float(cp) - float(wi.sl)))
                                        rr = num / den
                                except Exception:
                                    rr = None
                                rr_txt = f" RR={round(rr,2)}" if rr is not None else ""
                                if src_chat:
                                    await tg.send_message(src_chat, f"✅ {symbol} {direction.upper()} — окно входа открыто.{rr_txt}")
                            except Exception:
                                pass
                    # close window if conditions fade out (hysteresis close gate or avoid)
                    cond_close = (wi.entry_window_open and (((wi.last_score or 0) <= close_gate) or llm_rec == "avoid"))
                    debounce_bad_ok = wi._entry_bad_streak >= 2
                    if cond_close and debounce_bad_ok:
                        wi.entry_window_open = False
                        cooldown_ok = _should_alert("entry_close", cooldown_sec=ENTRY_ALERT_COOLDOWN_MIN*60)
                        try:
                            logger.info(
                                "[watcher] entry_window close (reason=cond_close, debounce_ok=%s, hysteresis_gate=%s, cooldown_ok=%s)",
                                debounce_bad_ok, close_gate, cooldown_ok,
                            )
                        except Exception:
                            pass
                        if cooldown_ok:
                            try:
                                src_chat = parsed.get("_meta", {}).get("src_chat_id")
                                if src_chat:
                                    await tg.send_message(src_chat, f"ℹ️ {symbol} — окно входа закрыто (условия рассеялись).")
                            except Exception:
                                pass
                    # promote to post_entry when window is open and LLM explicitly says enter
                    if wi.entry_window_open and llm_rec == "enter":
                        src_chat = parsed.get("_meta", {}).get("src_chat_id")
                        cooldown_ok = _should_alert("enter_confirm", cooldown_sec=ENTRY_ALERT_COOLDOWN_MIN*60)
                        try:
                            logger.info("[watcher] notify enter_confirm (cooldown_ok=%s)", cooldown_ok)
                        except Exception:
                            pass
                        if src_chat and cooldown_ok:
                            await tg.send_message(src_chat, f"✅ {symbol}: сетап подтверждён, можно входить.")
                        try:
                            from ai_agent_bot import forward_to_channel
                            await forward_to_channel(tg, parsed, (llm_res or {}))
                            REG.last_forwarded_keys[wi.key] = time.time()
                            wi.forwarded = True
                        except Exception:
                            pass
                        wi.phase = "post_entry"
                        if wi.entry_price is None:
                            wi.entry_price = parsed.get("current_price") or px_now
                        wi.holding_since = wi.holding_since or time.time()
                elif wi.phase == "post_entry":
                    # Weakening alert (not more than DANGER cooldown)
                    cooldown_ok = _should_alert("weak_post", cooldown_sec=DANGER_ALERT_COOLDOWN_MIN*60)
                    try:
                        logger.info("[watcher] danger_check (score=%s < %s) cooldown_ok=%s", (wi.last_score or 0), DOWNGRADE_SCORE, cooldown_ok)
                    except Exception:
                        pass
                    if (wi.last_score or 0) < DOWNGRADE_SCORE and cooldown_ok:
                        src_chat = parsed.get("_meta", {}).get("src_chat_id")
                        if src_chat:
                            await tg.send_message(src_chat, f"⚠️ {symbol}: тренд ослаб, стоит подумать о частичной фиксации.")
                        try:
                            if FORWARD_TARGET_ID:
                                kshort = wi.key[:6]
                                await tg.send_message(FORWARD_TARGET_ID, f"⚠️ [{symbol}] k={kshort} — тренд ослаб, возможна частичная фиксация.")
                        except Exception:
                            pass
                    # Exit by score gate
                    exit_cd_ok = _should_alert("exit_score", cooldown_sec=10)
                    try:
                        logger.info("[watcher] exit_check (score=%s <= %s) cooldown_ok=%s", (wi.last_score or 0), EXIT_SCORE, exit_cd_ok)
                    except Exception:
                        pass
                    if (wi.last_score or 100) <= EXIT_SCORE and exit_cd_ok:
                        src_chat = parsed.get("_meta", {}).get("src_chat_id")
                        if src_chat:
                            await tg.send_message(src_chat, f"⛔️ {symbol}: устойчивое ухудшение (score {wi.last_score}/100). Выход из позиции.")
                        if FORWARD_TARGET_ID:
                            try:
                                kshort = wi.key[:6]
                                await tg.send_message(FORWARD_TARGET_ID, f"⛔️ [{symbol}] k={kshort} — выход по ухудшению score={wi.last_score}/100.")
                            except Exception:
                                pass
                REG.watches.pop(wi.key, None)
                return
                    # TP extension logic (3h cooldown)
                    try:
                        cd_tp_ok = _should_alert("tp_extend_chk", cooldown_sec=TP_EXTEND_COOLDOWN_MIN*60)
                        atr4 = (((llm_payload or {}).get("taapi") or {}).get("atr") or {}).get("4h")
                        if isinstance(atr4, (int, float)) and atr4 > 0 and px_now is not None:
                                # establish base tp cap
                                if wi._base_tp_initial is None and wi.tp is not None:
                                    wi._base_tp_initial = float(wi.tp)
                                base_cap = None
                                try:
                                    if wi._base_tp_initial is not None:
                                        base_cap = float(wi._base_tp_initial) * (1.0 + (TP_EXTENSION_MAX if direction=="long" else -TP_EXTENSION_MAX))
                                except Exception:
                                    base_cap = None
                                step = float(atr4) * TP_EXTENSION_STEP_ATR4H
                            if direction == "long":
                                    proposed = max(float(wi.tp or px_now), float(px_now) + step)
                                    if base_cap is not None:
                                        proposed = min(proposed, base_cap)
                            else:
                                    proposed = min(float(wi.tp or px_now), float(px_now) - step)
                                    if base_cap is not None:
                                        proposed = max(proposed, base_cap)
                                min_delta = float(atr4) * TP_EXTENSION_MIN_DELTA_ATR
                                can_raise = False
                                try:
                                    if wi.tp is not None:
                                        can_raise = (direction=="long" and (proposed - float(wi.tp)) >= min_delta) or (direction=="short" and (float(wi.tp) - proposed) >= min_delta)
                                except Exception:
                                    can_raise = False
                                # only if trend is healthy
                                score_series = wi.payload.get("_score_series") or []
                                healthy = False
                                try:
                                    if len(score_series) >= 8:
                                        a = sum(score_series[-8:]) / 8.0
                                        b = sum(score_series[-16:-8]) / 8.0 if len(score_series) >= 16 else a
                                        healthy = (a >= 70 and a >= b and (wi.last_score or 0) >= HOLD_MIN_SCORE)
                                except Exception:
                                    healthy = False
                                try:
                                    logger.info(
                                        "[watcher] tp_extend: atr4h=%.6f step=%.6f proposed=%.6f prev_tp=%s cap_ok=%s min_delta_ok=%s cooldown_ok=%s healthy=%s",
                                        float(atr4), float(step), float(proposed), str(wi.tp),
                                        (base_cap is None or (direction=="long" and proposed <= base_cap) or (direction=="short" and proposed >= base_cap)),
                                        bool(can_raise), bool(cd_tp_ok), bool(healthy)
                                    )
                                except Exception:
                                    pass
                                if can_raise and healthy and cd_tp_ok:
                                    old_tp = wi.tp
                                    wi.tp = float(proposed)
                                    src_chat = parsed.get("_meta", {}).get("src_chat_id")
                                    try:
                                        if src_chat:
                                            await tg.send_message(src_chat, f"🎯 {symbol} — TP повышен до {wi.tp} (+{TP_EXTENSION_STEP_ATR4H}×ATR4h).")
                                    except Exception:
                                        pass
                                    if FORWARD_TARGET_ID:
                                        try:
                                            kshort = wi.key[:6]
                                            await tg.send_message(FORWARD_TARGET_ID, f"🎯 [{symbol}] k={kshort} — TP повышен до {wi.tp}.")
                                        except Exception:
                                            pass
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            logger.exception("WATCH tick error: key=%s", wi.key)
        await asyncio.sleep(wi.interval_sec)
