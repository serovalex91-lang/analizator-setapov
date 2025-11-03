from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_BASE_URL,
    OPENAI_TIMEOUT_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from app.services.taapi_bulk import (
    taapi_bulk,
    construct_indicator,
    parse_indicator_value,
    parse_indicator_data,
    construct_candles,
    parse_bulk_candles,
)


def _now_sec() -> int:
    return int(time.time())


def compute_obv_trend(values: List[float], lookback: int = 20) -> str:
    if not values:
        return "flat"
    arr = values[-lookback:]
    if len(arr) < 2:
        return "flat"
    slope = arr[-1] - arr[0]
    if slope > 0:
        return "up"
    if slope < 0:
        return "down"
    return "flat"


async def fetch_taapi_cursor_pack(symbol: str) -> Dict[str, Any]:
    """Fetch required indicators for 4h and 12h via TAAPI bulk API.
    We keep it resilient: if any indicator fails, we return None values and record missing fields in quality.
    """
    constructs: List[Dict[str, Any]] = []
    # 4h set
    constructs += [
        construct_indicator("adx", symbol, "4h", {"period": 14}),
        construct_indicator("plusdi", symbol, "4h", {"period": 14}),
        construct_indicator("minusdi", symbol, "4h", {"period": 14}),
        construct_indicator("macd", symbol, "4h", {}),
        construct_indicator("atr", symbol, "4h", {"period": 14}),
        construct_indicator("obv", symbol, "4h", {}),
        construct_indicator("mfi", symbol, "4h", {"period": 14}),
        construct_indicator("bbands", symbol, "4h", {"period": 20, "stddev": 2}),
    ]
    # 12h set
    constructs += [
        construct_indicator("adx", symbol, "12h", {"period": 14}),
        construct_indicator("plusdi", symbol, "12h", {"period": 14}),
        construct_indicator("minusdi", symbol, "12h", {"period": 14}),
        construct_indicator("macd", symbol, "12h", {}),
        construct_indicator("atr", symbol, "12h", {"period": 14}),
    ]
    # 4h candles for swing structure (20 results enough for last swings)
    constructs += [construct_candles(symbol, "4h", 200)]

    missing: List[str] = []

    try:
        bulk = await taapi_bulk(constructs)
        results = bulk.get("results", [])
    except Exception:
        # If TAAPI completely failed, return stub with everything missing
        return {
            "taapi": {},
            "bb_width": {},
            "structure": {},
            "quality": {"missing_fields": [
                "adx.4h","dmi.4h","macd.4h","atr.4h","obv.4h","mfi.4h","bbands.4h",
                "adx.12h","dmi.12h","macd.12h","atr.12h"
            ]}
        }

    def safe_indicator_value(idx: int) -> Optional[float]:
        if idx >= len(results):
            return None
        try:
            return parse_indicator_value(results[idx])
        except Exception:
            return None

    def last_from_data(idx: int) -> Optional[Dict[str, Any]]:
        if idx >= len(results):
            return None
        try:
            data = parse_indicator_data(results[idx])
            return data[-1] if data else None
        except Exception:
            return None

    idx = 0
    # 4h
    adx_4h = safe_indicator_value(idx); idx += 1
    plus_4h = safe_indicator_value(idx); idx += 1
    minus_4h = safe_indicator_value(idx); idx += 1
    macd4 = last_from_data(idx); idx += 1
    atr_4h = safe_indicator_value(idx); idx += 1
    obv4 = parse_indicator_data(results[idx]) if idx < len(results) else []
    idx += 1
    mfi_4h = safe_indicator_value(idx); idx += 1
    bb4 = last_from_data(idx); idx += 1

    # 12h
    adx_12h = safe_indicator_value(idx); idx += 1
    plus_12h = safe_indicator_value(idx); idx += 1
    minus_12h = safe_indicator_value(idx); idx += 1
    macd12 = last_from_data(idx); idx += 1
    atr_12h = safe_indicator_value(idx); idx += 1
    # 4h candles
    candles_4h = []
    last_4h_ts: Optional[str] = None
    if idx < len(results):
        try:
            candles_4h = parse_bulk_candles(results[idx])
            if candles_4h:
                last_4h_ts = str(candles_4h[-1].get("t"))
        except Exception:
            candles_4h = []
    idx += 1

    # Derived
    obv_series = []
    if isinstance(obv4, list):
        # parse_indicator_data returns list of dicts
        for row in obv4[-20:]:
            val = row.get("value") if isinstance(row, dict) else None
            if isinstance(val, (int, float)):
                obv_series.append(float(val))
    obv_trend = compute_obv_trend(obv_series)

    bb_width_4h = None
    if isinstance(bb4, dict):
        try:
            upper = float(bb4.get("upper"))
            basis = float(bb4.get("basis"))
            lower = float(bb4.get("lower"))
            if basis != 0:
                bb_width_4h = (upper - lower) / basis
        except Exception:
            bb_width_4h = None

    dmi_4h = {"di_plus": plus_4h, "di_minus": minus_4h}
    dmi_12h = {"di_plus": plus_12h, "di_minus": minus_12h}

    # MACD fields are provider-specific; try common keys
    def macd_fields(d: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
        if not isinstance(d, dict):
            return {"hist": None, "signal": None, "macd": None, "signal_cross": None}
        hist = d.get("hist") or d.get("valueMACDHist") or d.get("histogram")
        signal = d.get("signal") or d.get("valueMACDSignal")
        macd = d.get("macd") or d.get("valueMACD")
        out: Dict[str, Optional[float]] = {
            "hist": float(hist) if isinstance(hist, (int, float)) else None,
            "signal": float(signal) if isinstance(signal, (int, float)) else None,
            "macd": float(macd) if isinstance(macd, (int, float)) else None,
        }
        try:
            if out["macd"] is not None and out["signal"] is not None:
                out_cross = "bullish" if out["macd"] >= out["signal"] else "bearish"
            else:
                out_cross = None
        except Exception:
            out_cross = None
        out["signal_cross"] = out_cross
        return out

    macd_4h = macd_fields(macd4)
    macd_12h = macd_fields(macd12)

    # Freshness placeholders (we don't have exact timestamps for every indicator)
    quality = {"missing_fields": missing}

    # Structure: last swing high/low via simple fractal 5/5
    def last_swings_4h(candles: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        try:
            highs = [float(c.get("high")) for c in candles]
            lows = [float(c.get("low")) for c in candles]
        except Exception:
            return None, None
        n = len(candles)
        if n < 11:
            return None, None
        def is_pivot_high(i: int) -> bool:
            return all(highs[i] >= highs[j] for j in range(i-5, i+6) if 0 <= j < n and j != i)
        def is_pivot_low(i: int) -> bool:
            return all(lows[i] <= lows[j] for j in range(i-5, i+6) if 0 <= j < n and j != i)
        last_h, last_l = None, None
        for i in range(n-6, 5, -1):
            if last_h is None and is_pivot_high(i):
                last_h = highs[i]
            if last_l is None and is_pivot_low(i):
                last_l = lows[i]
            if last_h is not None and last_l is not None:
                break
        return last_h, last_l

    s_high, s_low = last_swings_4h(candles_4h)

    return {
        "taapi": {
            "adx": {"4h": adx_4h, "12h": adx_12h},
            "dmi": {"4h": dmi_4h, "12h": dmi_12h},
            "macd": {"4h": macd_4h, "12h": macd_12h},
            "atr": {"4h": atr_4h, "12h": atr_12h},
            "obv": {"4h_trend": obv_trend},
            "mfi": {"4h": mfi_4h},
            "bb_width": {"4h": bb_width_4h},
        },
        "structure": {"last_swing_high_4h": s_high, "last_swing_low_4h": s_low},
        "quality": quality,
        "freshness": {"last_4h_ts": last_4h_ts},
    }


async def call_llm(payload: Dict[str, Any], prompt: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    def build_body(extra_instruction: str | None = None) -> Dict[str, Any]:
        user_content = prompt + "\n\n" + json.dumps(payload, ensure_ascii=False)
        if extra_instruction:
            user_content = user_content + "\n\n" + extra_instruction
        return {
            "model": OPENAI_MODEL,
            "temperature": 0.2,
            "max_tokens": 1800,
            "messages": [
                {"role": "system", "content": "Ты — опытный крипто-трейдер. Отвечай СТРОГО JSON без текста."},
                {"role": "user", "content": user_content},
            ],
        }

    def try_parse(content: str) -> Optional[Dict[str, Any]]:
        # Strip code fences ```json ... ``` if present
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
        if fence:
            content = fence.group(1)
        # Try direct JSON
        try:
            return json.loads(content)
        except Exception:
            pass
        # Try to extract first JSON object substring
        try:
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(content[start : end + 1])
        except Exception:
            pass
        return None

    timeout = httpx.Timeout(OPENAI_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # First attempt
        r = await client.post(url, headers=headers, json=build_body())
        r.raise_for_status()
        data = r.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = try_parse(content) if isinstance(content, str) else None
        if parsed is not None:
            return parsed

        # One retry with explicit instruction
        retry_body = build_body("Верни ТОЛЬКО JSON, без пояснений и без ```.")
        rr = await client.post(url, headers=headers, json=retry_body)
        rr.raise_for_status()
        data2 = rr.json()
        content2 = data2.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed2 = try_parse(content2) if isinstance(content2, str) else None
        if parsed2 is not None:
            return parsed2

    # If still not parsable, return raw for debugging
    return {"raw": data}


async def maybe_notify_telegram(text: str) -> None:
    """Send a short message to Telegram if BOT_TOKEN and CHAT_ID are set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:3800], "disable_web_page_preview": True}
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.post(api, json=payload)
    except Exception:
        pass


