from __future__ import annotations

import os
import time
import json
import math
import asyncio
import random
from typing import Any, Dict, Tuple, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from dotenv import load_dotenv

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

load_dotenv()

TAAPI_KEY = os.getenv("TAAPI_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PROMPT = os.getenv(
    "CURSOR_PROMPT",
    "Ты опытный крипто-аналитик. Отвечай строго JSON.",
)

app = FastAPI(title="Cursor API", version="1.0.0")


class KeyLevels(BaseModel):
    type: str
    min: float
    max: float


class CursorInput(BaseModel):
    ticker: str
    trend_balls: list[str] = Field(..., min_items=5, max_items=5)
    direction: str
    tp: float
    sl: float
    current_price: Optional[float] = None
    key_levels: Optional[KeyLevels] = None


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _balls_to_updown(balls: list[str]) -> list[str]:
    out = []
    for b in balls:
        out.append("up" if b == "🟢" else "down")
    return out


def _normalize(input_payload: CursorInput) -> Dict[str, Any]:
    symbol = f"{input_payload.ticker.upper()}USDT"
    direction = (input_payload.direction or "").lower()
    tf_balls = ["30m", "60m", "120m", "240m", "720m"]
    balls = _balls_to_updown(input_payload.trend_balls)

    levels_invalid = False
    cp = input_payload.current_price
    if direction == "short" and cp is not None:
        levels_invalid = not (input_payload.tp < cp < input_payload.sl)
    if direction == "long" and cp is not None:
        levels_invalid = not (input_payload.sl < cp < input_payload.tp)

    return {
        "symbol": symbol,
        "direction": direction,
        "trend_balls": balls,
        "tf_balls": tf_balls,
        "current_price": cp,
        "tp": input_payload.tp,
        "sl": input_payload.sl,
        "key_levels": input_payload.key_levels.dict() if input_payload.key_levels else None,
        "levels_invalid": levels_invalid,
    }


def _to_taapi_symbol(symbol_usdt: str) -> str:
    if symbol_usdt.upper().endswith("USDT"):
        base = symbol_usdt.upper()[:-4]
        return f"{base}/USDT"
    return symbol_usdt


_cache: Dict[Tuple[str, str, str, str], Tuple[float, Any]] = {}


async def _get_with_retry(url: str, params: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    import httpx

    backoff = 0.5
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        await asyncio.sleep(backoff + random.random() * 0.25)
        backoff *= 2
    return None


def _cache_get(key: Tuple[str, str, str, str], ttl_sec: int = 60) -> Optional[Any]:
    ts_val = _cache.get(key)
    if not ts_val:
        return None
    ts, val = ts_val
    if (time.time() - ts) <= ttl_sec:
        return val
    return None


def _cache_set(key: Tuple[str, str, str, str], val: Any) -> None:
    _cache[key] = (time.time(), val)


async def _taapi_indicator(symbol: str, interval: str, indicator: str, extra: Dict[str, Any] | None = None) -> Optional[Dict[str, Any]]:
    if not TAAPI_KEY:
        return None
    params = {
        "secret": TAAPI_KEY,
        "exchange": "binance",
        "symbol": _to_taapi_symbol(symbol),
        "interval": interval,
    }
    if extra:
        params.update(extra)

    key = (indicator, interval, symbol, json.dumps(extra or {}, sort_keys=True))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data = await _get_with_retry(f"https://api.taapi.io/{indicator}", params)
    if data is not None:
        _cache_set(key, data)
    return data


async def _taapi_bundle(symbol: str) -> Dict[str, Any]:
    """Fetch TA bundle per spec; tolerate failures with nulls."""
    out = {
        "adx": {"4h": None, "12h": None},
        "dmi": {"4h": {"di_plus": None, "di_minus": None}, "12h": {"di_plus": None, "di_minus": None}},
        "macd": {"4h": {"hist": None, "macd": None, "signal": None}, "12h": {"hist": None, "macd": None, "signal": None}},
        "atr": {"4h": None, "12h": None},
        "mfi": {"4h": None},
        "bb_width": {"4h": None},
        "obv": {"4h_trend": "flat"},
    }

    # helper to safely extract possible TAAPI keys variations
    def _g(d: Dict[str, Any], *keys: str) -> Optional[float]:
        for k in keys:
            if k in d and d[k] is not None:
                try:
                    return float(d[k])
                except Exception:
                    pass
        return None

    # ADX 4h/12h
    adx4 = await _taapi_indicator(symbol, "4h", "adx") or {}
    adx12 = await _taapi_indicator(symbol, "12h", "adx") or {}
    out["adx"]["4h"] = _g(adx4, "value", "result", "adx")
    out["adx"]["12h"] = _g(adx12, "value", "result", "adx")

    # DMI 4h/12h
    dmi4 = await _taapi_indicator(symbol, "4h", "dmi") or {}
    dmi12 = await _taapi_indicator(symbol, "12h", "dmi") or {}
    out["dmi"]["4h"] = {"di_plus": _g(dmi4, "+di", "plusdi", "valuePlusDI"), "di_minus": _g(dmi4, "-di", "minusdi", "valueMinusDI")}
    out["dmi"]["12h"] = {"di_plus": _g(dmi12, "+di", "plusdi", "valuePlusDI"), "di_minus": _g(dmi12, "-di", "minusdi", "valueMinusDI")}

    # MACD 4h/12h
    macd4 = await _taapi_indicator(symbol, "4h", "macd", {"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}) or {}
    macd12 = await _taapi_indicator(symbol, "12h", "macd", {"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}) or {}
    out["macd"]["4h"] = {
        "hist": _g(macd4, "valueMACDHistogram", "hist", "valueHistogram"),
        "macd": _g(macd4, "valueMACD", "macd"),
        "signal": _g(macd4, "valueMACDSignal", "signal"),
    }
    out["macd"]["12h"] = {
        "hist": _g(macd12, "valueMACDHistogram", "hist", "valueHistogram"),
        "macd": _g(macd12, "valueMACD", "macd"),
        "signal": _g(macd12, "valueMACDSignal", "signal"),
    }

    # ATR 4h/12h
    atr4 = await _taapi_indicator(symbol, "4h", "atr", {"period": 14}) or {}
    atr12 = await _taapi_indicator(symbol, "12h", "atr", {"period": 14}) or {}
    out["atr"]["4h"] = _g(atr4, "value")
    out["atr"]["12h"] = _g(atr12, "value")

    # MFI 4h
    mfi4 = await _taapi_indicator(symbol, "4h", "mfi", {"period": 14}) or {}
    out["mfi"]["4h"] = _g(mfi4, "value")

    # BBANDS 4h → width
    bb4 = await _taapi_indicator(symbol, "4h", "bbands", {"period": 20, "matype": 0}) or {}
    upper = _g(bb4, "valueUpper", "upper")
    lower = _g(bb4, "valueLower", "lower")
    basis = _g(bb4, "valueMiddle", "middle", "basis")
    out["bb_width"]["4h"] = ((upper - lower) / basis) if (upper and lower and basis and basis != 0) else None

    # OBV 4h: now and backtrack=20
    obv_now = await _taapi_indicator(symbol, "4h", "obv") or {}
    obv_20 = await _taapi_indicator(symbol, "4h", "obv", {"backtrack": 20}) or {}
    try:
        v_now = float(obv_now.get("value"))
        v_20 = float(obv_20.get("value"))
        delta = (v_now - v_20) / (abs(v_20) if v_20 != 0 else 1.0) * 100.0
        trend = "flat"
        if delta >= 2.0:
            trend = "up"
        elif delta <= -2.0:
            trend = "down"
        out["obv"]["4h_trend"] = trend
    except Exception:
        out["obv"]["4h_trend"] = "flat"

    return out


async def _fetch_last_price(symbol: str) -> Optional[float]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol})
            if r.status_code == 200:
                jd = r.json() or {}
                return float(jd.get("price"))
    except Exception:
        return None
    return None


async def _fetch_btc_context() -> Dict[str, Optional[float]]:
    if not TAAPI_KEY:
        return {"adx_4h": None, "macd_hist_4h": None}
    sym = "BTCUSDT"
    adx4 = await _taapi_indicator(sym, "4h", "adx") or {}
    macd4 = await _taapi_indicator(sym, "4h", "macd", {"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}) or {}
    def _g(d: Dict[str, Any], *keys: str) -> Optional[float]:
        for k in keys:
            if k in d and d[k] is not None:
                try:
                    return float(d[k])
                except Exception:
                    pass
        return None
    return {
        "adx_4h": _g(adx4, "value", "adx"),
        "macd_hist_4h": _g(macd4, "valueMACDHistogram", "hist", "valueHistogram"),
    }


def _init_openai() -> Optional[OpenAI]:
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _map_verdict(score: int) -> str:
    if score >= 80:
        return "strong"
    if score >= 65:
        return "moderate"
    if score >= 40:
        return "weak"
    return "invalid"


async def _call_llm(client: OpenAI, payload: Dict[str, Any]) -> Dict[str, Any]:
    user_text = json.dumps(payload, ensure_ascii=False)
    msgs = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": user_text},
    ]
    def _try_once() -> Optional[Dict[str, Any]]:
        try:
            r = client.chat.completions.create(model=OPENAI_MODEL, temperature=0.25, messages=msgs, max_tokens=800)
            txt = r.choices[0].message.content or ""
            return json.loads(txt)
        except Exception:
            return None

    out = _try_once()
    if out is not None:
        return out

    # one retry with explicit instruction
    msgs[-1]["content"] = user_text + "\nВЕРНИ ТОЛЬКО JSON, без текста, строго по схеме."
    out = _try_once()
    if out is None:
        raise HTTPException(status_code=502, detail="LLM response is not valid JSON")
    return out


@app.post("/cursor/run")
async def cursor_run(input_payload: CursorInput):
    setup_parsed = _normalize(input_payload)

    # TA bundle
    ta = await _taapi_bundle(setup_parsed["symbol"]) if TAAPI_KEY else {
        "adx": {"4h": None, "12h": None},
        "dmi": {"4h": {"di_plus": None, "di_minus": None}, "12h": {"di_plus": None, "di_minus": None}},
        "macd": {"4h": {"hist": None, "macd": None, "signal": None}, "12h": {"hist": None, "macd": None, "signal": None}},
        "atr": {"4h": None, "12h": None},
        "mfi": {"4h": None},
        "bb_width": {"4h": None},
        "obv": {"4h_trend": "flat"},
    }

    # Market context
    last_price = setup_parsed.get("current_price") or await _fetch_last_price(setup_parsed["symbol"]) or None
    btc_ctx = await _fetch_btc_context()
    market_context = {
        "last_price": last_price,
        "funding_rate": None,
        "open_interest_change_24h_pct": None,
        "btc_context": btc_ctx,
    }

    payload = {
        "setup_parsed": setup_parsed,
        "taapi": ta,
        "market_context": market_context,
        "meta": {"source": "telegram_bot", "timestamp_utc": _now_iso()},
    }

    # LLM call
    client = _init_openai()
    if client is None:
        # If no LLM available, return payload for debugging
        return JSONResponse({"debug_payload": payload})

    try:
        # _call_llm уже async → просто ждём результат
        res = await _call_llm(client, payload)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    # Post-process verdict if missing
    try:
        if not res.get("verdict") and isinstance(res.get("score"), int):
            res["verdict"] = _map_verdict(int(res["score"]))
    except Exception:
        pass

    return JSONResponse(res)


