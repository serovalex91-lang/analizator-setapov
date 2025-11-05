from __future__ import annotations
import os
import re
import json
import asyncio
import logging
from typing import Dict, List

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
import time
import random

try:
    # OpenAI SDK v1.x
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


load_dotenv()
# Extra: also load ai.env if present (so starting without the helper script still works)
try:
    _ai_env_path = os.path.join(os.path.dirname(__file__), "ai.env")
    if os.path.exists(_ai_env_path):
        from dotenv import dotenv_values

        for _k, _v in (dotenv_values(_ai_env_path) or {}).items():
            if _v and not os.environ.get(_k):
                os.environ[_k] = _v
except Exception:
    pass

# Import pipeline and prompt after environment is loaded, so they see fresh env vars
from cursor_pipeline import orchestrate_setup_flow, call_llm  # noqa: E402
from llm_prompt import PROMPT  # noqa: E402
from watcher import REG, WatchItem, _watch_loop  # registry and watch loop

# Telegram credentials
# Prefer environment variables, but fall back to values used elsewhere in the repo if present
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", os.getenv("API_ID", "29129135")))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", os.getenv("API_HASH", "4f2fb26f0b7f24551bd1759cb78af30c"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", ""))

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "AI_SYSTEM_PROMPT",
    (
        "Ты — полезный русскоязычный AI-ассистент в Telegram. "
        "Отвечай кратко и по делу, сохраняй контекст диалога, форматируй код в блоках."
    ),
)
TAAPI_KEY = os.getenv("TAAPI_KEY", "")
TAAPI_EXCHANGE = os.getenv("TAAPI_EXCHANGE", "binancefutures")

# === Forward/Watch config ===
FORWARD_TARGET_ID = int(os.getenv("FORWARD_TARGET_ID", "0"))
FORWARD_PREFIX = os.getenv("FORWARD_PREFIX", "📡 AUTO")
FORWARD_THRESHOLD = int(os.getenv("FORWARD_THRESHOLD", "70"))
WATCH_THRESHOLD = int(os.getenv("WATCH_THRESHOLD", "70"))
WATCH_WINDOW_HOURS = int(os.getenv("WATCH_WINDOW_HOURS", "12"))
WATCH_INTERVAL_MIN = int(os.getenv("WATCH_INTERVAL_MIN", "60"))
SHOW_DEBUG = os.getenv("SHOW_DEBUG", "0") == "1"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai_agent_bot")


class ConversationStore:
    """Very simple in-memory conversation storage per chat."""

    def __init__(self, max_messages_per_chat: int = 12) -> None:
        self._store: Dict[int, List[dict]] = {}
        self._max = max_messages_per_chat

    def reset(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)

    def append(self, chat_id: int, role: str, content: str) -> None:
        messages = self._store.setdefault(
            chat_id,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
            ],
        )
        messages.append({"role": role, "content": content})
        # Keep tail
        if len(messages) > (self._max + 1):
            # keep system + last N pairs
            # ensure we never drop the system message at index 0
            head = messages[0:1]
            tail = messages[-self._max :]
            self._store[chat_id] = head + tail

    def get(self, chat_id: int) -> List[dict]:
        return self._store.get(chat_id, [{"role": "system", "content": SYSTEM_PROMPT}])


conv_store = ConversationStore(max_messages_per_chat=12)


def _require_config() -> None:
    missing: List[str] = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_API_ID:
        missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if missing:
        raise RuntimeError(
            "Отсутствуют обязательные переменные окружения: " + ", ".join(missing)
        )


def _init_openai_client() -> OpenAI | None:
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY не задан — использую fallback-режим ответов.")
        return None
    if OpenAI is None:
        raise RuntimeError(
            "Пакет 'openai' не установлен. Добавьте его в requirements и установите зависимости."
        )
    try:
        safe_suffix = OPENAI_API_KEY[-6:] if len(OPENAI_API_KEY) > 6 else "***"
        logger.info("OpenAI client initialized (model=%s, key=***%s)", OPENAI_MODEL, safe_suffix)
    except Exception:
        pass
    return OpenAI(api_key=OPENAI_API_KEY)


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", "."))
    except Exception:
        return None


def parse_setup_message(text: str) -> dict | None:
    """Parse screener message into the requested minimal fields.
    Returns dict with keys: ticker, trend_balls, direction, tp, sl, current_price, key_levels{type,min,max}.
    Returns None if message doesn't look like a setup payload.
    """
    if not text:
        return None
    if text.lstrip().startswith(FORWARD_PREFIX):
        return None
    if "#setup" not in text.lower():
        return None

    # ticker
    m_ticker = re.search(r"\$([A-Z]{2,15})\b", text)
    ticker = m_ticker.group(1) if m_ticker else None

    # trend balls: take a line starting with TREND and collect 5 balls
    balls = []
    m_trend_line = re.search(r"^\s*TREND\s+(.+)$", text, flags=re.MULTILINE)
    if m_trend_line:
        segment = m_trend_line.group(1)
        balls = re.findall(r"[🟢🔴]", segment)[:5]

    # direction
    m_dir = re.search(r"\b(Short|Long)\b", text, flags=re.IGNORECASE)
    direction = m_dir.group(1).upper() if m_dir else None

    # TP/SL (numbers)
    m_tp = re.search(r"TP:\s*([0-9]+(?:[\.,][0-9]+)?)", text, flags=re.IGNORECASE)
    m_sl = re.search(r"SL:\s*([0-9]+(?:[\.,][0-9]+)?)", text, flags=re.IGNORECASE)
    tp = _to_float(m_tp.group(1)) if m_tp else None
    sl = _to_float(m_sl.group(1)) if m_sl else None

    # Current price (may appear inside Comment section)
    m_cur = re.search(r"Current:\s*([0-9]+(?:[\.,][0-9]+)?)", text, flags=re.IGNORECASE)
    current_price = _to_float(m_cur.group(1)) if m_cur else None

    # Key levels
    m_levels = re.search(
        r"Key\s+levels:\s*(RESISTANCE|SUPPORT)\s*([0-9\.,]+)\s*-\s*([0-9\.,]+)",
        text,
        flags=re.IGNORECASE,
    )
    key_levels = None
    if m_levels:
        k_type = m_levels.group(1).upper()
        k_min = _to_float(m_levels.group(2))
        k_max = _to_float(m_levels.group(3))
        key_levels = {"type": k_type, "min": k_min, "max": k_max}

    # Ensure minimal payload
    payload = {
        "ticker": ticker,
        "trend_balls": balls if balls else None,
        "direction": direction,
        "tp": tp,
        "sl": sl,
        "current_price": current_price,
        "key_levels": key_levels,
    }
    # If nothing meaningful found, return None
    if not any(v is not None for v in payload.values()):
        return None
    return payload


# stable key for dedup (ticker+levels+direction+key_levels)
import hashlib


def setup_key(parsed: dict) -> str:
    base = {
        "ticker": parsed.get("ticker"),
        "direction": parsed.get("direction"),
        "tp": parsed.get("tp"),
        "sl": parsed.get("sl"),
        "kl": parsed.get("key_levels"),
    }
    s = json.dumps(base, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


async def forward_to_channel(tg: TelegramClient, parsed: dict, llm_result: dict) -> None:
    if FORWARD_TARGET_ID == 0:
        return
    meta = parsed.get("_meta") or {}
    src_chat = meta.get("src_chat_id")
    src_msg = meta.get("src_msg_id")
    key = setup_key(parsed)
    logger.info("forward_to_channel: to=%s key=%s", FORWARD_TARGET_ID, key)
    head = f"{FORWARD_PREFIX} | {parsed.get('ticker')} {parsed.get('direction')} | score {llm_result.get('score')}"
    try:
        await tg.send_message(FORWARD_TARGET_ID, head)
        if src_chat and src_msg:
            await tg.forward_messages(FORWARD_TARGET_ID, src_msg, src_chat)
        else:
            raw = meta.get("raw_text") or "—"
            await tg.send_message(FORWARD_TARGET_ID, raw)
    except Exception:
        logger.exception("forward_to_channel failed")


async def maybe_forward_or_watch(tg: TelegramClient, parsed: dict, llm_result: dict) -> None:
    try:
        score = int(llm_result.get("score", 0))
    except Exception:
        score = 0
    key = setup_key(parsed)
    # prevent duplicates
    try:
        if REG.last_forwarded_keys.get(key):
            return
    except Exception:
        pass
    if score >= FORWARD_THRESHOLD:
        await forward_to_channel(tg, parsed, llm_result)
        try:
            REG.last_forwarded_keys[key] = time.time()
        except Exception:
            pass
        return
    if 40 <= score < FORWARD_THRESHOLD:
        # enqueue watch if not exists
        try:
            if key not in REG.watches:
                now = time.time()
                wi = WatchItem(
                    key=key,
                    payload={"parsed": parsed, "last_llm": llm_result},
                    started_ts=now,
                    deadline_ts=now + WATCH_WINDOW_HOURS * 3600,
                    interval_sec=WATCH_INTERVAL_MIN * 60,
                    threshold=WATCH_THRESHOLD,
                )
                REG.watches[key] = wi
                asyncio.create_task(_watch_loop(tg, wi))
        except Exception:
            pass


async def fetch_binance_price(symbol_usdt: str) -> float | None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol_usdt.upper()},
            )
            if r.status_code != 200:
                return None
            jd = r.json() or {}
            return _to_float(str(jd.get("price")))
    except Exception:
        return None


async def fetch_taapi_adx(symbol_usdt: str) -> dict | None:
    """Fetch ADX 4h and 12h from TAAPI for given USDT symbol (e.g., BTCUSDT)."""
    if not TAAPI_KEY:
        return None
    try:
        import httpx

        def to_ta_symbol(sym: str) -> str:
            s = sym.upper()
            return f"{s[:-4]}/USDT" if s.endswith("USDT") else s

        ta_sym = to_ta_symbol(symbol_usdt)
        async with httpx.AsyncClient(timeout=12.0) as client:
            u4 = "https://api.taapi.io/adx"
            u12 = "https://api.taapi.io/adx"
            p4 = {"secret": TAAPI_KEY, "exchange": TAAPI_EXCHANGE, "symbol": ta_sym, "interval": "4h"}
            p12 = {"secret": TAAPI_KEY, "exchange": TAAPI_EXCHANGE, "symbol": ta_sym, "interval": "12h"}
            r4, r12 = await asyncio.gather(client.get(u4, params=p4), client.get(u12, params=p12))
            if r4.status_code != 200 or r12.status_code != 200:
                return None
            j4 = r4.json() or {}
            j12 = r12.json() or {}
            return {
                "adx": {
                    "4h": j4.get("value"),
                    "12h": j12.get("value"),
                }
            }
    except Exception:
        return None


def _to_taapi_symbol(sym: str) -> str:
    s = sym.upper()
    return f"{s[:-4]}/USDT" if s.endswith("USDT") else s


async def fetch_taapi_bundle(symbol_usdt: str) -> dict | None:
    """Full TA bundle per spec for 4h/12h (ADX/DMI/MACD/ATR/MFI/BBANDS width/OBV)."""
    if not TAAPI_KEY:
        return None
    import httpx

    ta_sym = _to_taapi_symbol(symbol_usdt)

    async def get(ind: str, params: dict) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.get(f"https://api.taapi.io/{ind}", params=params)
                if r.status_code != 200:
                    return None
                return r.json() or {}
        except Exception:
            return None

    p4 = {"secret": TAAPI_KEY, "exchange": TAAPI_EXCHANGE, "symbol": ta_sym, "interval": "4h"}
    p12 = {**p4, "interval": "12h"}

    adx4, adx12 = await asyncio.gather(
        get("adx", p4),
        get("adx", p12),
    )
    dmi4, dmi12 = await asyncio.gather(
        get("dmi", p4),
        get("dmi", p12),
    )
    macd4, macd12 = await asyncio.gather(
        get("macd", {**p4, "optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}),
        get("macd", {**p12, "optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}),
    )
    atr4, atr12 = await asyncio.gather(
        get("atr", {**p4, "period": 14}),
        get("atr", {**p12, "period": 14}),
    )
    mfi4 = await get("mfi", {**p4, "period": 14})
    bb4 = await get("bbands", {**p4, "period": 20, "matype": 0})
    obv_now, obv_bt20 = await asyncio.gather(
        get("obv", p4),
        get("obv", {**p4, "backtrack": 20}),
    )

    def g(d: dict | None, *keys: str):
        if not d:
            return None
        for k in keys:
            if k in d and d[k] is not None:
                try:
                    return float(d[k])
                except Exception:
                    return d[k]
        return None

    bb_width4 = None
    try:
        upper = g(bb4, "valueUpper", "upper")
        lower = g(bb4, "valueLower", "lower")
        basis = g(bb4, "valueMiddle", "middle", "basis")
        if upper is not None and lower is not None and basis not in (None, 0):
            bb_width4 = (float(upper) - float(lower)) / float(basis)
    except Exception:
        bb_width4 = None

    obv_trend = "flat"
    try:
        v_now = float(g(obv_now, "value") or 0)
        v_bt = float(g(obv_bt20, "value") or 0)
        if v_bt != 0:
            delta_pct = (v_now - v_bt) / abs(v_bt) * 100.0
            if delta_pct >= 2.0:
                obv_trend = "up"
            elif delta_pct <= -2.0:
                obv_trend = "down"
    except Exception:
        obv_trend = "flat"

    return {
        "adx": {"4h": g(adx4, "value", "adx"), "12h": g(adx12, "value", "adx")},
        "dmi": {
            "4h": {"di_plus": g(dmi4, "+di", "plusdi", "valuePlusDI"), "di_minus": g(dmi4, "-di", "minusdi", "valueMinusDI")},
            "12h": {"di_plus": g(dmi12, "+di", "plusdi", "valuePlusDI"), "di_minus": g(dmi12, "-di", "minusdi", "valueMinusDI")},
        },
        "macd": {
            "4h": {"hist": g(macd4, "valueMACDHistogram", "hist", "valueHistogram"), "macd": g(macd4, "valueMACD", "macd"), "signal": g(macd4, "valueMACDSignal", "signal")},
            "12h": {"hist": g(macd12, "valueMACDHistogram", "hist", "valueHistogram"), "macd": g(macd12, "valueMACD", "macd"), "signal": g(macd12, "valueMACDSignal", "signal")},
        },
        "atr": {"4h": g(atr4, "value"), "12h": g(atr12, "value")},
        "mfi": {"4h": g(mfi4, "value")},
        "bb_width": {"4h": bb_width4},
        "obv": {"4h_trend": obv_trend},
    }

async def call_cursor_api(parsed: dict, base_url: str = "http://127.0.0.1:8010") -> dict | None:
    """Send parsed setup to Cursor API /cursor/run and return its JSON result or None on failure."""
    try:
        import httpx

        payload = {
            "ticker": parsed.get("ticker"),
            "trend_balls": parsed.get("trend_balls") or [],
            "direction": (parsed.get("direction") or "").upper(),
            "tp": parsed.get("tp"),
            "sl": parsed.get("sl"),
            "current_price": parsed.get("current_price"),
            "key_levels": parsed.get("key_levels"),
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{base_url}/cursor/run", json=payload)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def _generate_ai_response(client_oa: OpenAI | None, chat_id: int, user_text: str) -> str:
    conv_store.append(chat_id, "user", user_text)
    messages = conv_store.get(chat_id)
    # If OpenAI client available, use it
    if client_oa is not None:
        try:
            completion = client_oa.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.5,
                max_tokens=700,
            )
            text = completion.choices[0].message.content or ""
        except Exception as e:
            logger.exception("OpenAI completion error: %s", e)
            text = (
                "Не удалось получить ответ от модели. Попробуйте ещё раз позже."
            )
    else:
        # Fallback: short helpful echo with instruction to set OPENAI_API_KEY
        text = (
            "У меня нет доступа к модели (не задан OPENAI_API_KEY).\n"
            "Ваше сообщение: " + user_text[:1000]
        )
    conv_store.append(chat_id, "assistant", text)
    return text


def build_client() -> TelegramClient:
    client = TelegramClient(
        "ai_bot_session",
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        device_model="ai-agent",
        system_version="1.0",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru_RU",
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
        timeout=30,
        request_retries=5,
    )
    # wait up to 12 hours on FloodWait
    client.flood_sleep_threshold = 12 * 60 * 60
    return client


async def main() -> None:
    _require_config()
    oa = _init_openai_client()

    tg = build_client()
    await tg.start(bot_token=TELEGRAM_BOT_TOKEN)

    logger.info("AI Telegram bot started. Waiting for messages…")

    @tg.on(events.NewMessage(pattern=r"^/start(?:@.*)?$"))
    async def _(event):  # noqa: ANN001
        await event.respond(
            "Привет! Я AI-ассистент. Напишите ваш вопрос. Команды: /reset — очистить контекст."
        )

    @tg.on(events.NewMessage(pattern=r"^/reset(?:@.*)?$"))
    async def _(event):  # noqa: ANN001
        conv_store.reset(event.chat_id)
        await event.respond("Контекст очищен. Продолжим.")

    @tg.on(events.NewMessage(pattern=r"^/taapi(?:@.*)?\s*(.*)$"))
    async def _(event):  # noqa: ANN001
        ticker = (event.pattern_match.group(1) or "").strip().upper()
        if not ticker:
            await event.respond("Укажите тикер: /taapi BTC или /taapi BTCUSDT")
            return
        if not ticker.endswith("USDT"):
            ticker += "USDT"
        bundle = await fetch_taapi_bundle(ticker)
        if bundle is None:
            await event.respond("TAAPI недоступен или отсутствует TAAPI_KEY")
            return
        # also attach last price
        last_price = await fetch_binance_price(ticker)
        resp = {"symbol": ticker, "last_price": last_price, "taapi": bundle}
        await event.respond("TAAPI bundle:\n" + json.dumps(resp, ensure_ascii=False, indent=2))

    @tg.on(events.NewMessage(pattern=r"^/llmtest(?:@.*)?$"))
    async def _llmtest(event):  # noqa: ANN001
        dummy = {
            "setup_parsed": {
                "symbol": "TESTUSDT",
                "direction": "short",
                "trend_balls": ["down", "down", "down", "down", "down"],
                "tf_balls": ["30m", "60m", "120m", "240m", "720m"],
                "current_price": 1.0,
                "tp": 0.9,
                "sl": 1.1,
                "key_levels": None,
                "levels_invalid": False,
            },
            "taapi": {
                "adx": {"4h": 25, "12h": 25},
                "dmi": {"4h": {"di_plus": 10, "di_minus": 20}, "12h": {"di_plus": 10, "di_minus": 20}},
                "macd": {"4h": {"macd": -1, "signal": -1, "hist": -0.1}, "12h": {"macd": -1, "signal": -1, "hist": -0.1}},
                "atr": {"4h": 0.01, "12h": 0.02},
                "mfi": {"4h": 40},
                "bb_width": {"4h": 0.05},
                "obv": {"4h_trend": "down"},
                "filters": {"ema200_12h": 1.2, "rsi_12h": 40, "price_above_ma200_12h": False, "rsi_12h_gt_50": False},
            },
            "market_context": {"last_price": 1.0, "funding_rate": 0.0, "open_interest_change_24h_pct": 0.0, "btc_context": {"adx_4h": 20, "macd_hist_4h": -0.1}},
            "meta": {"source": "diag", "timestamp_utc": None},
        }
        try:
            # add echo guard for test too
            dummy2 = dict(dummy)
            dummy2["_echo_guard"] = "DO_NOT_RETURN"
            res = await call_llm(dummy2)
        except Exception:
            res = None
        await event.respond("LLM TEST RESULT:\n" + (json.dumps(res, ensure_ascii=False, indent=2) if res else "None"))

    @tg.on(events.NewMessage(pattern=r"^/watch_status(?:@.*)?$"))
    async def _watch_status(event):  # noqa: ANN001
        try:
            lines = []
            for k, wi in (REG.watches or {}).items():
                try:
                    left = int((wi.deadline_ts or 0) - time.time())
                    lines.append(
                        f"• {k[:6]}… score={wi.last_score} next≈{(wi.interval_sec or 0)//60}m left={left//60}m thr={wi.threshold}"
                    )
                except Exception:
                    continue
            if not lines:
                await event.respond("Нет активных наблюдений.")
            else:
                await event.respond("Наблюдения:\n" + "\n".join(lines))
        except Exception:
            await event.respond("Нет активных наблюдений.")

    @tg.on(events.NewMessage(pattern=r"^/watch(?:@.*)?(?:\s+(\d+))?(?:\s+(\d+))?(?:\s+(\d+(?:\.\d+)?))?$"))
    async def _watch(event):  # noqa: ANN001
        try:
            hours = event.pattern_match.group(1)
            interval_min = event.pattern_match.group(2)
            threshold = event.pattern_match.group(3)
            hours_i = int(hours) if hours else WATCH_DEFAULT_HOURS
            interval_i = int(interval_min) if interval_min else WATCH_INTERVAL_MIN
            threshold_f = float(threshold) if threshold else WATCH_ENTER_SCORE
        except Exception:
            hours_i, interval_i, threshold_f = WATCH_DEFAULT_HOURS, WATCH_INTERVAL_MIN, WATCH_ENTER_SCORE

        parsed = None
        try:
            parsed = REG.get_last_parsed(event.chat_id)
        except Exception:
            parsed = None
        if not parsed:
            await event.respond("Нет последнего распознанного сетапа. Пришлите сетап перед /watch.")
            return

        # anti-duplicate: do not start a second watch for same source message
        try:
            src_id = (parsed.get("_meta") or {}).get("source_msg_id")
            if src_id is not None:
                active = REG.list(event.chat_id)
                if any((w.parsed.get("_meta") or {}).get("source_msg_id") == src_id for w in active):
                    await event.respond("⚠️ Наблюдение по этому сетапу уже запущено.")
                    return
        except Exception:
            pass

        watch = REG.new_watch(event.chat_id, parsed, threshold_f)
        watch.deadline_ts = time.time() + hours_i * 3600
        await event.respond(f"Запущено наблюдение #{watch.id}: {hours_i}ч, интервал {interval_i}м, порог {threshold_f}")

        async def _watch_loop() -> None:
            while True:
                now = time.time()
                if now >= watch.deadline_ts:
                    try:
                        await tg.send_message(watch.chat_id, "⛔️ Сетап не прошёл за отведённое время: SKIP")
                    except Exception:
                        pass
                    try:
                        REG.stop(watch.id)
                    except Exception:
                        pass
                    return

                # build payload and call LLM via pipeline to ensure consistent logic
                try:
                    preview, llm_payload, llm_result = await orchestrate_setup_flow(watch.parsed, PROMPT, with_llm=True)
                except Exception:
                    llm_result = None

                score = None
                verdict = None
                try:
                    if isinstance(llm_result, dict):
                        score = llm_result.get("score")
                        verdict = llm_result.get("verdict")
                        watch.last_score = float(score) if score is not None else None
                except Exception:
                    pass

                success = False
                try:
                    if isinstance(score, (int, float)) and score >= watch.threshold:
                        success = True
                except Exception:
                    success = False

                if success and not watch.forwarded:
                    raw = (watch.parsed.get("_meta") or {}).get("raw_text") or ""
                    fwd_text = f"{FORWARD_PREFIX}\n{raw}".strip()
                    try:
                        await tg.send_message(FORWARD_TARGET_ID, fwd_text)
                        watch.forwarded = True
                    except Exception:
                        pass
                    try:
                        REG.stop(watch.id)
                    except Exception:
                        pass
                    return

                # sleep until next iteration
                await asyncio.sleep(interval_i * 60 + random.uniform(0, 0.5))

        asyncio.create_task(_watch_loop())

    @tg.on(events.NewMessage())
    async def _(event):  # noqa: ANN001
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return
        # ignore our auto-forwards and messages from the target channel
        if text.lstrip().startswith(FORWARD_PREFIX):
            return
        try:
            if getattr(event, "chat_id", None) == FORWARD_TARGET_ID:
                return
        except Exception:
            pass
        try:
            # Attempt to parse screener/setup message first
            parsed = parse_setup_message(text)
            if parsed and parsed.get("ticker") and parsed.get("trend_balls"):
                # attach meta
                try:
                    parsed["_meta"] = {
                        "src_chat_id": event.chat_id,
                        "src_msg_id": getattr(event, "message", None).id if getattr(event, "message", None) else None,
                        "raw_text": getattr(event, "message", None).raw_text if getattr(event, "message", None) else text,
                        "ts_utc": None,
                    }
                    try:
                        REG.remember_last_parsed(event.chat_id, parsed)
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    _preview, llm_payload, llm_result = await orchestrate_setup_flow(parsed, PROMPT, with_llm=True)
                    if llm_result:
                        try:
                            # 1) Итог
                            def render_conclusion(res: dict) -> str:
                                c = res.get("conclusion") or {}
                                head = c.get("headline") or "Итог недоступен"
                                bullets = c.get("bullets") or []
                                inv = c.get("invalidation") or ""
                                lines = [f"🧾 *Итог:* {head}"]
                                for b in bullets[:6]:
                                    lines.append(f"• {b}")
                                if inv:
                                    lines.append(f"_Инвалидация:_ {inv}")
                                return "\n".join(lines)

                            concl_text = render_conclusion(llm_result)
                            await tg.send_message(event.chat_id, concl_text, parse_mode="Markdown")

                            # 2) Тактика
                            def render_tactical(tc: dict | None) -> str:
                                if not tc:
                                    return ""
                                lines = ["🎯 *Тактика:*", tc.get("summary", "—")]
                                grid_levels = tc.get("grid_levels") if isinstance(tc, dict) else None
                                if isinstance(grid_levels, list) and grid_levels:
                                    lines.append("📐 *Сетка:* " + " / ".join(map(str, grid_levels)))
                                adv = tc.get("advice")
                                if isinstance(adv, list):
                                    for a in adv[:5]:
                                        lines.append(f"• {a}")
                                return "\n".join(lines)

                            tact = llm_result.get("tactical_comment") if isinstance(llm_result, dict) else None
                            tact_text = render_tactical(tact)
                            if tact_text:
                                await tg.send_message(event.chat_id, tact_text, parse_mode="Markdown")

                            # 3) Сетка
                            g = llm_result.get("grid") if isinstance(llm_result, dict) else None
                            if isinstance(g, dict):
                                entries = g.get("entries") or []
                                if isinstance(entries, list) and entries:
                                    lines = ["🧱 *Сетка (RR≥3, SL фикс):*"]
                                    for idx, e in enumerate(entries[:5], start=1):
                                        price = e.get("price")
                                        tpv = e.get("tp")
                                        rr = e.get("rr")
                                        ok = bool(e.get("eligible"))
                                        mark = "✅" if ok else "✖️"
                                        lines.append(f"{idx}) {price} → TP {tpv} (RR {rr}) {mark}")
                                    blended = g.get("blended_rr")
                                    sl_v = g.get("hard_stop")
                                    cons = g.get("constraints") or {}
                                    stepv = cons.get("step_atr_4h")
                                    band = cons.get("used_key_level_band")
                                    lines.append(f"Blended RR: {blended}")
                                    tail = f"SL: {sl_v}"
                                    if stepv is not None:
                                        tail += f" | шаг ≈ {stepv}×ATR"
                                    if isinstance(band, list) and len(band) == 2:
                                        tail += f" | Band: {band[0]}–{band[1]}"
                                    lines.append(tail)
                                await tg.send_message(event.chat_id, "\n".join(lines), parse_mode="Markdown")
                            # auto decision: forward or watch
                            try:
                                await maybe_forward_or_watch(tg, parsed, llm_result)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    else:
                        await event.respond("❕ LLM недоступен или ответ пуст.")
                except Exception:
                    # fallback: just show parsed
                    await event.respond("Parsed setup:\n" + json.dumps(parsed, ensure_ascii=False, indent=2))
                return

            await event.respond("Думаю…")
            reply = await _generate_ai_response(oa, event.chat_id, text)
            # Edit previous status message to keep chat tidy
            try:
                await asyncio.sleep(0.1)
                async for msg in tg.iter_messages(event.chat_id, limit=1):
                    if msg and msg.message == "Думаю…":
                        await msg.edit(reply)
                        return
            except Exception:
                pass
            await event.respond(reply)
        except FloodWaitError as fw:
            logger.warning("Flood wait: sleeping for %s seconds", fw.seconds)
            await asyncio.sleep(int(fw.seconds) + 1)
        except Exception:
            logger.exception("Failed to handle message")

    await tg.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


