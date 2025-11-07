from __future__ import annotations
import os, asyncio, random, math
from typing import Any, Dict, Optional, Tuple
import time
from llm_prompt import PROMPT
import json

import httpx
import logging

# === CONFIG ===
TAAPI_KEY = os.getenv("TAAPI_KEY", "")
TAAPI_BASE = "https://api.taapi.io"
TAAPI_EXCHANGE = os.getenv("TAAPI_EXCHANGE", "binancefutures")
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUT = "https://fapi.binance.com"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
FAST_TF_ENABLED = int(os.getenv("FAST_TF_ENABLED", "1"))
FAST_TF_SCORE_CAP = int(os.getenv("FAST_TF_SCORE_CAP", "10"))  # max ±10 influence from fast TF

# Тайм-ауты/ретраи
HTTP_TIMEOUT = 10.0
RETRIES = 3

TF_BALLS = ["30m", "60m", "120m", "240m", "720m"]

logger = logging.getLogger("cursor_pipeline")

# ===== Simple in-memory cache for TAAPI calls =====
_TAAPI_CACHE: dict[tuple[str, str, str, str], dict] = {}
_TAAPI_TTL_SEC = int(os.getenv("TAAPI_CACHE_TTL", "300"))  # 5 minutes default
_cache_hits = 0
_cache_misses = 0

def _rough_rr(direction: str, entry: float | None, sl: float | None, tp: float | None) -> float | None:
    try:
        if None in (entry, sl, tp):
            return None
        if direction == "long" and not (sl < entry < tp):
            return None
        if direction == "short" and not (tp < entry < sl):
            return None
        num = abs(tp - entry)
        den = max(1e-9, abs(entry - sl))
        return float(num / den)
    except Exception:
        return None

def compute_fallback_score(llm_payload: dict) -> int:
    """Дешёвый детерминированный скор 0..100 на базе тренда, ADX, MACD, RSI и RR."""
    sp = (llm_payload.get("setup_parsed") or {})
    ta = (llm_payload.get("taapi") or {})
    dirn = (sp.get("direction") or "")
    cp = sp.get("current_price")
    sl = sp.get("sl"); tp = sp.get("tp")
    rr = _rough_rr(dirn, cp, sl, tp) or 0.0
    tb = sp.get("trend_balls") or []
    trend_ok = 0.0
    if dirn == "short":
        trend_ok = (tb.count("down") / max(1, len(tb)))
    elif dirn == "long":
        trend_ok = (tb.count("up") / max(1, len(tb)))
    adx4 = ((ta.get("adx") or {}).get("4h") or 0) or 0
    adx12 = ((ta.get("adx") or {}).get("12h") or 0) or 0
    macd4 = ((ta.get("macd") or {}).get("4h") or {}).get("hist")
    macd12 = ((ta.get("macd") or {}).get("12h") or {}).get("hist")
    rsi12 = ((ta.get("filters") or {}).get("rsi_12h"))
    w_trend, w_rr, w_adx, w_macd, w_rsi = 0.25, 0.25, 0.2, 0.2, 0.1
    s_trend = 100.0 * float(trend_ok)
    s_rr = 100.0 * min(1.0, max(0.0, (rr - 1.0) / 3.0))
    s_adx = 100.0 * min(1.0, max(0.0, (max(adx4 or 0, adx12 or 0) - 15) / 20))
    s_macd = 50.0
    try:
        if macd4 is not None and macd12 is not None:
            if dirn == "short":
                s_macd = 75.0 if (macd4 < 0 and macd12 <= 0) else 35.0
            else:
                s_macd = 75.0 if (macd4 > 0 and macd12 >= 0) else 35.0
    except Exception:
        pass
    s_rsi = 50.0
    try:
        if rsi12 is not None:
            if dirn == "short":
                s_rsi = 75.0 if rsi12 < 50 else 35.0
            else:
                s_rsi = 75.0 if rsi12 > 50 else 35.0
    except Exception:
        pass
    base = w_trend*s_trend + w_rr*s_rr + w_adx*s_adx + w_macd*s_macd + w_rsi*s_rsi
    return int(max(0, min(100, round(base))))

# ========== низкоуровневые утилиты ==========

async def _aget_json(client: httpx.AsyncClient, url: str, params: dict, retries: int = RETRIES) -> dict | None:
    for i in range(retries):
        try:
            r = await client.get(url, params=params)
            if r.status_code == 429:
                await asyncio.sleep(0.7 * (2**i) + random.random())
                continue
            if r.status_code >= 500:
                await asyncio.sleep(0.5 * (2**i) + random.random())
                continue
            if r.status_code != 200:
                # вернем пусто, но не падаем
                return None
            return r.json()
        except Exception:
            if i == retries - 1:
                return None
            await asyncio.sleep(0.4 * (2**i) + random.random())
    return None

def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        s = str(x).replace(",", ".").strip()
        return float(s)
    except Exception:
        return None

# ========== маппинг ключей TAAPI ==========

def map_dmi(jd: dict | None) -> dict | None:
    if not jd:
        return None
    src = jd.get("result") if isinstance(jd.get("result"), dict) else jd
    def first_val(d: dict, keys: list[str]) -> Optional[float]:
        for k in keys:
            if k in d and d[k] is not None:
                return _to_float(d[k])
        # case-insensitive fallback
        try:
            lower_map = {str(k).lower(): v for k, v in d.items() if v is not None}
            for k in keys:
                if k.lower() in lower_map:
                    return _to_float(lower_map[k.lower()])
        except Exception:
            pass
        return None
    di_plus = first_val(src, [
        "+di", "+DI", "pdi", "PDI", "plusdi", "PlusDI", "valuePlusDI", "valuePlusDi", "di_plus"
    ]) 
    di_minus = first_val(src, [
        "-di", "-DI", "mdi", "MDI", "minusdi", "MinusDI", "valueMinusDI", "valueMinusDi", "di_minus"
    ]) 
    return {"di_plus": di_plus, "di_minus": di_minus}

def map_macd(jd: dict | None) -> dict | None:
    if not jd: return None
    macd   = jd.get("valueMACD")       or jd.get("macd")
    signal = jd.get("valueMACDSignal") or jd.get("signal")
    # запасные ключи включены для устойчивости к разным версиям TAAPI
    hist   = (
        jd.get("valueMACDHist")
        or jd.get("hist")
        or jd.get("valueHistogram")
        or jd.get("histogram")
        or jd.get("valueMACDHistogram")
    )
    return {"macd": _to_float(macd), "signal": _to_float(signal), "hist": _to_float(hist)}

def map_bb_width(bb: dict | None) -> Optional[float]:
    if not bb: return None
    upper = bb.get("valueUpper")  or bb.get("valueUpperBand")  or bb.get("upper")
    lower = bb.get("valueLower")  or bb.get("valueLowerBand")  or bb.get("lower")
    basis = bb.get("valueMiddle") or bb.get("valueMiddleBand") or bb.get("basis")
    upper = _to_float(upper); lower = _to_float(lower); basis = _to_float(basis)
    if upper is None or lower is None or (basis is None) or basis == 0:
        return None
    return (upper - lower) / abs(basis)

def balls_to_dirs(emoji_balls: list[str]) -> list[str]:
    # 🟢 -> "up", 🔴 -> "down"; дополняем до 5
    res = []
    for b in (emoji_balls or [])[:5]:
        res.append("up" if b == "🟢" else "down")
    while len(res) < 5: res.append("down")
    return res

def compute_levels_invalid(direction: str, current_price: Optional[float], tp: Optional[float], sl: Optional[float]) -> bool:
    if current_price is None or tp is None or sl is None:
        return False
    if direction == "short":
        return not (tp < current_price < sl)
    if direction == "long":
        return not (sl < current_price < tp)
    return False


# ========== фетчеры TAAPI/биржа ==========

def _get_taapi_key() -> str:
    return os.getenv("TAAPI_KEY", "")

async def taapi_get(
    client: httpx.AsyncClient,
    ind: str,
    symbol_slash: str,
    interval: str,
    extra: dict | None = None,
    use_cache_only: bool = False,
) -> dict | None:
    """Fetch TAAPI indicator with simple TTL cache.
    If use_cache_only=True, return cached value or None (no network).
    """
    secret = _get_taapi_key()
    if not secret:
        return None
    params = {"secret": secret, "exchange": TAAPI_EXCHANGE, "symbol": symbol_slash, "interval": interval}
    if extra:
        params.update(extra)

    # cache key (indicator + symbol + interval)
    try:
        key = (ind, symbol_slash, interval, str(sorted(params.items())))
        rec = _TAAPI_CACHE.get(key)
        now = time.time()
        if rec and (now - rec.get("ts", 0)) < _TAAPI_TTL_SEC:
            globals()["_cache_hits"] += 1
            return rec.get("data")
        if use_cache_only:
            return None
        globals()["_cache_misses"] += 1
    except Exception:
        pass

    data = await _aget_json(client, f"{TAAPI_BASE}/{ind}", params)
    try:
        _TAAPI_CACHE[key] = {"ts": time.time(), "data": data}
        if (_cache_hits + _cache_misses) % 50 == 0:
            logger.info("[TAAPI_CACHE] size=%s hits=%s misses=%s", len(_TAAPI_CACHE), _cache_hits, _cache_misses)
    except Exception:
        pass
    return data

async def fetch_taapi_bundle(symbol_usdt: str, skip_heavy_tf: bool = False) -> dict:
    # параллельные запросы (4h/12h)
    base = symbol_usdt.upper()
    # для TAAPI используем BTC/USDT
    if base.endswith("USDT"):
        slash = base[:-4] + "/USDT"
    else:
        slash = base

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # use_cache_only for heavy TF when skip_heavy_tf is True
        uco = skip_heavy_tf
        adx4   = asyncio.create_task(taapi_get(client, "adx",   slash, "4h",  use_cache_only=uco))
        adx12  = asyncio.create_task(taapi_get(client, "adx",   slash, "12h", use_cache_only=uco))
        dmi4   = asyncio.create_task(taapi_get(client, "dmi",   slash, "4h",  use_cache_only=uco))
        dmi12  = asyncio.create_task(taapi_get(client, "dmi",   slash, "12h", use_cache_only=uco))
        macd4  = asyncio.create_task(taapi_get(client, "macd",  slash, "4h",  extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}, use_cache_only=uco))
        macd12 = asyncio.create_task(taapi_get(client, "macd",  slash, "12h", extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}, use_cache_only=uco))
        macd4_bt1  = asyncio.create_task(taapi_get(client, "macd",  slash, "4h",  extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9, "backtrack": 1}, use_cache_only=uco))
        macd12_bt1 = asyncio.create_task(taapi_get(client, "macd",  slash, "12h", extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9, "backtrack": 1}, use_cache_only=uco))
        atr4   = asyncio.create_task(taapi_get(client, "atr",   slash, "4h",  use_cache_only=uco))
        atr12  = asyncio.create_task(taapi_get(client, "atr",   slash, "12h", use_cache_only=uco))
        mfi4   = asyncio.create_task(taapi_get(client, "mfi",   slash, "4h",  use_cache_only=False))
        bb4    = asyncio.create_task(taapi_get(client, "bbands",slash, "4h",  use_cache_only=False))
        obv_now= asyncio.create_task(taapi_get(client, "obv",   slash, "4h",  use_cache_only=False))
        obv_bt = asyncio.create_task(taapi_get(client, "obv",   slash, "4h", extra={"backtrack": 20}, use_cache_only=False))
        rsi12  = asyncio.create_task(taapi_get(client, "rsi",   slash, "12h", extra={"optInMAType": 1, "backtrack": 0, "close": 1}, use_cache_only=uco))
        ema200 = asyncio.create_task(taapi_get(client, "ema",   slash, "12h", extra={"period": 200}, use_cache_only=uco))

        # ждем
        adx4, adx12, dmi4, dmi12, macd4, macd12, atr4, atr12, mfi4, bb4, obv_now, obv_bt, rsi12, ema200, \
        macd4_bt1, macd12_bt1 = await asyncio.gather(
            adx4, adx12, dmi4, dmi12, macd4, macd12, atr4, atr12, mfi4, bb4, obv_now, obv_bt, rsi12, ema200,
            macd4_bt1, macd12_bt1
        )

    # нормализация
    # ADX могут приходить как value / valueAdx
    def _adx_val(x: dict | None) -> Optional[float]:
        if not x: return None
        return _to_float(x.get("value") or x.get("valueAdx") or x.get("adx") or x.get("result"))

    adx = {"4h": _adx_val(adx4), "12h": _adx_val(adx12)}
    dmi = {"4h": map_dmi(dmi4), "12h": map_dmi(dmi12)}
    # фолбэк, если dmi пустой или без значений (не дергаем plusdi/minusdi — используем dmi только)
    def pick(x, *keys):
        if not x:
            return None
        # check both root and nested 'result'
        dicts = [x]
        try:
            if isinstance(x.get("result"), dict):
                dicts.append(x.get("result"))
        except Exception:
            pass
        for d in dicts:
            for k in keys:
                if d and d.get(k) is not None:
                    return _to_float(d.get(k))
            # case-insensitive in this dict
            try:
                lower_map = {str(k).lower(): v for k, v in (d or {}).items() if v is not None}
                for k in keys:
                    if k.lower() in lower_map:
                        return _to_float(lower_map[k.lower()])
            except Exception:
                pass
        return None
    # no extra calls to plusdi/minusdi; if DMI missing values, keep None
    macd = {"4h": map_macd(macd4), "12h": map_macd(macd12)}
    atr = {"4h": _to_float(atr4.get("value") if atr4 else None), "12h": _to_float(atr12.get("value") if atr12 else None)}
    mfi = {"4h": _to_float(mfi4.get("value") if mfi4 else None)}
    bb_width = {"4h": map_bb_width(bb4)}
    # extract bb levels for percent_b calculation later
    bb_upper = (bb4 or {}).get("valueUpper") or (bb4 or {}).get("valueUpperBand") or (bb4 or {}).get("upper")
    bb_lower = (bb4 or {}).get("valueLower") or (bb4 or {}).get("valueLowerBand") or (bb4 or {}).get("lower")
    bb_middle = (bb4 or {}).get("valueMiddle") or (bb4 or {}).get("valueMiddleBand") or (bb4 or {}).get("middle") or (bb4 or {}).get("basis")
    bb_upper = _to_float(bb_upper); bb_lower = _to_float(bb_lower); bb_middle = _to_float(bb_middle)

    # obv_trend
    now_v = _to_float((obv_now or {}).get("value"))
    bt_v  = _to_float((obv_bt or {}).get("value"))
    obv_trend = None
    if now_v is not None and bt_v is not None and abs(bt_v) > 1e-9:
        pct = (now_v - bt_v) / abs(bt_v)
        obv_trend = "up" if pct > 0.02 else ("down" if pct < -0.02 else "flat")

    # capture optional TAAPI timestamps for RSI to verify closed candle
    def _ts(d: dict | None) -> Optional[int]:
        if not d:
            return None
        for k in ("timestamp", "time", "unix"):
            v = d.get(k)
            try:
                if v is not None:
                    return int(v)
            except Exception:
                continue
        return None

    filters = {
        "ema200_12h": _to_float((ema200 or {}).get("value")),
        "rsi_12h": _to_float((rsi12 or {}).get("value")),
        "rsi_12h_ts": _ts(rsi12),
    }

    return {
        "adx": adx,
        "dmi": dmi,
        "macd": macd,
        "macd_prev": {"4h": {"hist": (map_macd(macd4_bt1) or {}).get("hist")}, "12h": {"hist": (map_macd(macd12_bt1) or {}).get("hist")}},
        "atr": atr,
        "mfi": mfi,
        "bb_width": bb_width,
        "bb": {"4h": {"upper": bb_upper, "lower": bb_lower, "middle": bb_middle}},
        "obv": {"4h_trend": obv_trend},
        "filters": filters,
    }

async def fetch_last_price(symbol_usdt: str) -> Optional[float]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        jd = await _aget_json(client, f"{BINANCE_SPOT}/api/v3/ticker/price", {"symbol": symbol_usdt.upper()})
        return _to_float(jd.get("price")) if jd else None

async def fetch_derivatives_context(symbol_usdt: str) -> Tuple[Optional[float], Optional[float]]:
    """ funding_rate (last), oi_change_24h_pct (approx) """
    sym = symbol_usdt.upper()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        fr = await _aget_json(client, f"{BINANCE_FUT}/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
        funding = _to_float((fr[0] or {}).get("fundingRate")) if isinstance(fr, list) and fr else None

        oi_hist = await _aget_json(client, f"{BINANCE_FUT}/futures/data/openInterestHist", {"symbol": sym, "period": "1d", "limit": 2})
        oi_chg = None
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            v0 = _to_float(oi_hist[-2].get("sumOpenInterest"))
            v1 = _to_float(oi_hist[-1].get("sumOpenInterest"))
            if v0 and v1 and v0 != 0:
                oi_chg = (v1 - v0) / abs(v0) * 100.0
        return funding, oi_chg

async def fetch_btc_context() -> dict:
    """Небольшой фон по BTC."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        btc_adx4 = await taapi_get(client, "adx", "BTC/USDT", "4h")
        btc_macd4 = await taapi_get(client, "macd", "BTC/USDT", "4h")
    return {
        "adx_4h": _to_float((btc_adx4 or {}).get("valueAdx") or (btc_adx4 or {}).get("value")),
        "macd_hist_4h": _to_float((btc_macd4 or {}).get("valueMACDHist") or (btc_macd4 or {}).get("hist")),
    }


# ========== сборка LLM payload + превью ==========

def build_llm_payload(parsed: dict, taapi_bundle: dict, last_price: Optional[float],
                      funding: Optional[float] = None, oi_chg_pct: Optional[float] = None,
                      btc_ctx: Optional[dict] = None) -> dict:
    """
    parsed (из твоего парсера бота):
    {
      "ticker": "MANA",
      "trend_balls": ["🟢","🟢","🟢","🔴","🔴"],
      "direction": "SHORT",
      "tp": 0.2195, "sl": 0.2574, "current_price": 0.2309,
      "key_levels": {"type":"RESISTANCE","min":0.2496,"max":0.2521}
    }
    """
    symbol = f"{parsed['ticker'].upper()}USDT"
    direction = (parsed.get("direction") or "").lower()  # "short" | "long"
    tb = balls_to_dirs(parsed.get("trend_balls") or [])
    cp = parsed.get("current_price")
    if cp is None:
        cp = last_price
    tp = parsed.get("tp"); sl = parsed.get("sl")

    # derive filters: price_above_ma200_12h / rsi_12h_gt_50
    ema200 = ((taapi_bundle.get("filters") or {}).get("ema200_12h"))
    rsi12  = ((taapi_bundle.get("filters") or {}).get("rsi_12h"))
    price_above_ma200_12h = (cp > ema200) if (cp is not None and ema200 is not None) else None
    rsi_12h_gt_50 = (rsi12 > 50) if (rsi12 is not None) else None

    levels_invalid = compute_levels_invalid(direction, cp, tp, sl)

    payload = {
        "setup_parsed": {
            "symbol": symbol,
            "direction": direction,
            "trend_balls": tb,
            "tf_balls": TF_BALLS,
            "current_price": cp,
            "tp": tp,
            "sl": sl,
            "key_levels": parsed.get("key_levels"),
            "levels_invalid": levels_invalid
        },
        "derived": {
            "dmi_spread_4h": (abs((taapi_bundle.get("dmi") or {}).get("4h", {}).get("di_plus") - (taapi_bundle.get("dmi") or {}).get("4h", {}).get("di_minus"))
                               if (taapi_bundle.get("dmi") or {}).get("4h") and (taapi_bundle.get("dmi") or {}).get("4h", {}).get("di_plus") is not None and (taapi_bundle.get("dmi") or {}).get("4h", {}).get("di_minus") is not None else None),
            "dmi_spread_12h": (abs((taapi_bundle.get("dmi") or {}).get("12h", {}).get("di_plus") - (taapi_bundle.get("dmi") or {}).get("12h", {}).get("di_minus"))
                                if (taapi_bundle.get("dmi") or {}).get("12h") and (taapi_bundle.get("dmi") or {}).get("12h", {}).get("di_plus") is not None and (taapi_bundle.get("dmi") or {}).get("12h", {}).get("di_minus") is not None else None),
            "macd_hist_4h_slope": ( ((taapi_bundle.get("macd") or {}).get("4h", {}).get("hist") or 0) - (((taapi_bundle.get("macd_prev") or {}).get("4h", {}) or {}).get("hist") or 0) ) if (taapi_bundle.get("macd") or {}).get("4h") else None,
            "macd_hist_12h_slope": ( ((taapi_bundle.get("macd") or {}).get("12h", {}).get("hist") or 0) - (((taapi_bundle.get("macd_prev") or {}).get("12h", {}) or {}).get("hist") or 0) ) if (taapi_bundle.get("macd") or {}).get("12h") else None,
            "rsi12h_distance_50": (abs(((taapi_bundle.get("filters") or {}).get("rsi_12h") or 0) - 50) if (taapi_bundle.get("filters") or {}).get("rsi_12h") is not None else None),
            "percent_b_4h": ( ((cp if cp is not None else last_price) - ( (taapi_bundle.get("bb") or {}).get("4h", {}).get("lower") or 0 )) / (((taapi_bundle.get("bb") or {}).get("4h", {}).get("upper") or 0) - ((taapi_bundle.get("bb") or {}).get("4h", {}).get("lower") or 0))
                               if (taapi_bundle.get("bb") or {}).get("4h") and (taapi_bundle.get("bb") or {}).get("4h", {}).get("upper") not in (None, 0) and (taapi_bundle.get("bb") or {}).get("4h", {}).get("lower") is not None and ((taapi_bundle.get("bb") or {}).get("4h", {}).get("upper") - (taapi_bundle.get("bb") or {}).get("4h", {}).get("lower")) not in (None, 0) and (cp is not None or last_price is not None) else None),
            "atr_ratios": {
                "entry_px_ratio": (abs(((cp if cp is not None else 0) - (cp if cp is not None else 0)))/((taapi_bundle.get("atr") or {}).get("4h") or 1.0)) if (cp is not None and (taapi_bundle.get("atr") or {}).get("4h") not in (None, 0)) else None,
                "sl_entry_ratio": (abs(((sl or 0) - (cp or 0)))/((taapi_bundle.get("atr") or {}).get("4h") or 1.0)) if (sl is not None and cp is not None and (taapi_bundle.get("atr") or {}).get("4h") not in (None, 0)) else None,
                "tp_entry_ratio": (abs(((tp or 0) - (cp or 0)))/((taapi_bundle.get("atr") or {}).get("4h") or 1.0)) if (tp is not None and cp is not None and (taapi_bundle.get("atr") or {}).get("4h") not in (None, 0)) else None,
            },
            "staleness_hours": ((time.time() - float((taapi_bundle.get("filters") or {}).get("rsi_12h_ts") or 0)) / 3600.0) if ((taapi_bundle.get("filters") or {}).get("rsi_12h_ts") is not None) else None,
        },
        "taapi": {
            "adx": taapi_bundle.get("adx"),
            "dmi": taapi_bundle.get("dmi"),
            "macd": taapi_bundle.get("macd"),
            "atr": taapi_bundle.get("atr"),
            "mfi": taapi_bundle.get("mfi"),
            "bb_width": taapi_bundle.get("bb_width"),
            "obv": taapi_bundle.get("obv"),
            "filters": {
                "ema200_12h": ema200,
                "rsi_12h": rsi12,
                "price_above_ma200_12h": price_above_ma200_12h,
                "rsi_12h_gt_50": rsi_12h_gt_50
            }
        },
        "market_context": {
            "last_price": last_price,
            "funding_rate": funding,
            "open_interest_change_24h_pct": oi_chg_pct,
            "btc_context": btc_ctx or {"adx_4h": None, "macd_hist_4h": None}
        },
        "meta": {"source": "telegram_bot", "timestamp_utc": None}
    }
    # Передаём жёсткий стоп явно в payload для LLM (hint):
    try:
        if sl is not None:
            payload["grid"] = {"hard_stop": float(sl)}
    except Exception:
        pass
    return payload

def make_human_preview(payload: dict) -> str:
    """Короткий обзор для глаза в Telegram перед LLM-ответом."""
    sp = payload["setup_parsed"]; ta = payload["taapi"]; mc = payload["market_context"]
    tb = sp["trend_balls"]; tb_txt = " / ".join(f"{TF_BALLS[i]}:{'↑' if d=='up' else '↓'}" for i,d in enumerate(tb))
    adx4 = ta["adx"].get("4h"); adx12 = ta["adx"].get("12h")
    macd4 = ta["macd"]["4h"]; macd12 = ta["macd"]["12h"]
    bbw = ta["bb_width"].get("4h")
    bbw_note = ""
    try:
        if isinstance(bbw, (int, float)):
            if float(bbw) < 0.025:
                bbw_note = " (узко, риск флэта)"
            elif float(bbw) > 0.08:
                bbw_note = " (широко, волатильно)"
    except Exception:
        bbw_note = ""
    obv = ta["obv"].get("4h_trend")
    filt = ta.get("filters") or {}
    dmi4 = ta["dmi"].get("4h") or {}
    dmi12 = ta["dmi"].get("12h") or {}
    lines = [
        f"🔎 {sp['symbol']} — {sp['direction']}",
        f"TREND: {tb_txt}",
        f"Px: {sp.get('current_price')} | TP: {sp.get('tp')} | SL: {sp.get('sl')}",
        f"ADX 4h/12h: {adx4} / {adx12}",
        f"DMI 4h (DI+/DI-): { dmi4.get('di_plus') } / { dmi4.get('di_minus') }",
        f"DMI 12h (DI+/DI-): { dmi12.get('di_plus') } / { dmi12.get('di_minus') }",
        f"MACD_hist 4h/12h: { (macd4 or {}).get('hist') } / { (macd12 or {}).get('hist') }",
        f"ATR 4h/12h: { (ta.get('atr') or {}).get('4h') } / { (ta.get('atr') or {}).get('12h') }",
        f"BB width 4h: {bbw}{bbw_note} | OBV 4h: {obv}",
        f"RSI12h: {filt.get('rsi_12h')} | EMA200 12h: {filt.get('ema200_12h')} | Px>MA200?: {filt.get('price_above_ma200_12h')}",
        f"Funding: {mc.get('funding_rate')} | OI Δ24h%: {mc.get('open_interest_change_24h_pct')}",
        f"BTC ctx (ADX4h/MACD_Hist4h): { (mc.get('btc_context') or {}).get('adx_4h') } / { (mc.get('btc_context') or {}).get('macd_hist_4h') }",
        f"Levels valid?: {not sp.get('levels_invalid')}"
    ]
    return "\n".join(lines)


# ========== опционально: вызов LLM ==========

EXPECTED_KEYS = {"symbol", "direction", "verdict", "score", "flags", "corrections", "rationale_short", "confidence", "subscores", "debug"}

def _valid_llm(d: dict) -> bool:
    return isinstance(d, dict) and EXPECTED_KEYS.issubset(set(d.keys()))

_LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "1"))
_LLM_SEM = asyncio.Semaphore(_LLM_MAX_CONCURRENCY)


async def call_llm(llm_payload: dict, extra_text: str | None = None) -> dict | None:
    if not OPENAI_API_KEY:
        print("[LLM] OPENAI_API_KEY missing")
        return None
    base = (OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    path = "/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    # Compose user content: base prompt + optional history/dynamics block + payload JSON
    history_block = ("\nИсторические данные по индикаторам за период наблюдения:\n" + extra_text) if extra_text else ""
    user_content = PROMPT + history_block + "\n" + json.dumps(llm_payload, ensure_ascii=False)
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.25,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — трейдер-алгоритмист. Оценивай сетап по техническим данным и динамике индикаторов,"
                    " давай краткий, но содержательный анализ с акцентом на изменение силы сигнала."
                    " Всегда структурируй выводы и избегай воды. В конце верни ТОЛЬКО JSON строго по схеме."
                ),
            },
            {"role": "user", "content": user_content},
        ],
    }

    try:
        print(f"[LLM] base_url={base!r}, path={path!r}, model={OPENAI_MODEL!r}")
    except Exception:
        pass

    async with _LLM_SEM:
        async with httpx.AsyncClient(timeout=60.0, base_url=base) as client:
            r = await client.post(path, headers=headers, json=body)
            if r.status_code != 200:
                print("[LLM] HTTP", r.status_code, (r.text or "")[:500])
                return None
            raw = r.json()
            content = raw["choices"][0]["message"]["content"]
            try:
                data = json.loads(content)
            except Exception as e:
                print("[LLM] parse err:", e, content[:300])
                body["messages"][ -1]["content"] = "ВЕРНИ ТОЛЬКО JSON ПО СХЕМЕ.\n" + user_content
                r2 = await client.post(path, headers=headers, json=body)
                if r2.status_code != 200:
                    print("[LLM] HTTP(retry)", r2.status_code, (r2.text or "")[:500])
                    return None
                content2 = r2.json()["choices"][0]["message"]["content"]
                try:
                    data = json.loads(content2)
                except Exception as e2:
                    print("[LLM] parse err2:", e2, content2[:300])
                    return None

    if not _valid_llm(data):
        print("[LLM] invalid schema keys:", list(data.keys())[:20])
        return None
    return data


# ========== главная точка для хендлера ==========

async def orchestrate_setup_flow(
    parsed: dict,
    PROMPT: str,
    with_llm: bool = True,
    history_data: list[dict] | None = None,
    skip_heavy_tf: bool = False,
    volume_context: dict | None = None,
) -> tuple[str, dict, Optional[dict]]:
    """
    Вход: parsed из твоего бота (ticker, trend_balls, direction, tp, sl, current_price, key_levels)
    Выход: (human_preview_text, llm_payload, llm_result_or_none)
    """
    symbol = f"{parsed['ticker'].upper()}USDT"

    # 1) last price (если нет)
    last_price = parsed.get("current_price")
    if last_price is None:
        last_price = await fetch_last_price(symbol)

    # 2) TAAPI bundle (если у тебя уже есть свой — подставь сюда)
    ta = await fetch_taapi_bundle(symbol, skip_heavy_tf=skip_heavy_tf)

    # 3) деривативы и BTC фон (не критично, но полезно)
    funding, oi_chg = await fetch_derivatives_context(symbol)
    btc_ctx = await fetch_btc_context()

    # 4) сбор LLM payload
    llm_payload = build_llm_payload(parsed, ta, last_price, funding, oi_chg, btc_ctx)
    if volume_context:
        try:
            llm_payload["volume_context"] = volume_context
        except Exception:
            pass

    # 5) превью для глаза
    preview = make_human_preview(llm_payload)

    # 6) LLM (опц.)
    llm_result = None
    if with_llm:
        # добавим echo-guard, чтобы отлавливать эхо входа
        payload_for_llm = dict(llm_payload)
        payload_for_llm["_echo_guard"] = "DO_NOT_RETURN"
        # Prepare optional short history block for the prompt
        extra_txt = None
        try:
            if history_data:
                # keep last 12 items, flatten key names for readability
                tail = history_data[-12:]
                compact = []
                for r in tail:
                    compact.append({
                        "t": (r.get("timestamp") or "")[-8:],
                        "ADX4h": r.get("adx4h"),
                        "ADX12h": r.get("adx12h"),
                        "MACD4h": r.get("macd4h"),
                        "MACD12h": r.get("macd12h"),
                        "RSI12h": r.get("rsi12h"),
                        "OBV": r.get("obv_trend"),
                    })
                import json as _json
                extra_txt = _json.dumps(compact, ensure_ascii=False)
        except Exception:
            extra_txt = None
        llm_result = await call_llm(payload_for_llm, extra_text=extra_txt)
        try:
            if isinstance(llm_result, dict):
                logger.info("[PIPE] got llm_result keys: %s", list(llm_result.keys()))
        except Exception:
            pass
        # если пришли subscores — считаем итоговый score в коде
        try:
            WEIGHTS = {
                "trend_align":0.15, "dmi_spread":0.12, "adx_strength":0.12, "macd_momentum":0.15,
                "rsi_state":0.08, "vol_regime":0.10, "obv_flow":0.06, "levels_quality":0.12,
                "market_ctx":0.07, "structure":0.03
            }
            if isinstance(llm_result, dict) and isinstance(llm_result.get("subscores"), dict):
                sub = llm_result["subscores"]
                base = 0.0
                for k, w in WEIGHTS.items():
                    try:
                        base += w * float(sub.get(k, 50) or 50)
                    except Exception:
                        pass
                score = int(round(base))
                # мягкие штрафы с ограничением сверху
                flags = llm_result.get("flags") or []
                penalty = 0
                if isinstance(flags, list):
                    soft_map = {
                        "adx_low":5, "dmi_conflict":6, "macd_diverge":6, "range_risk":8,
                        "counter_trend":10, "entry_far":5, "sl_tight":6, "tp_optimistic":6,
                        "missing_data":4
                    }
                    for f in flags:
                        penalty += soft_map.get(f, 0)
                penalty = min(penalty, 20)
                score = int(round(base - penalty))
                score = max(0, min(100, score))
                llm_result["score"] = score
                llm_result["confidence"] = max(1, min(10, int(round(score/10))))
        except Exception:
            pass
        # Нормализация score/confidence в любом случае (гарантия чисел)
        try:
            if not isinstance(llm_result, dict):
                llm_result = {}
            if not isinstance(llm_result.get("score"), (int, float)):
                llm_result["score"] = compute_fallback_score(llm_payload)
            # Применяем кап влияния fast-TF: итоговый score не может отклоняться от HTF-базы > ±FAST_TF_SCORE_CAP
            if FAST_TF_ENABLED:
                try:
                    base_htf = compute_fallback_score(llm_payload)  # 4h/12h + RR/balls
                    s_llm = float(llm_result.get("score"))
                    delta = s_llm - float(base_htf)
                    cap = float(max(0, FAST_TF_SCORE_CAP))
                    clamped = base_htf + max(-cap, min(cap, delta))
                    final_score = int(max(0, min(100, round(clamped))))
                    llm_result["score"] = final_score
                except Exception:
                    pass
            _conf = int(max(1, min(10, round(float(llm_result.get("score")) / 10))))
            llm_result["confidence"] = _conf
            llm_result["confidence_text"] = f"{_conf*10}% уверенности"
        except Exception:
            pass
        # Маппинг вердикта по шкале
        try:
            if isinstance(llm_result, dict) and isinstance(llm_result.get("score"), (int, float)):
                sc = int(llm_result["score"])
                def _map_verdict(score: int) -> str:
                    if score >= 90:
                        return "high"
                    if score >= 75:
                        return "strong"
                    if score >= 60:
                        return "moderate"
                    if score >= 1:
                        return "weak"
                    return "invalid"
                llm_result["verdict"] = _map_verdict(sc)
        except Exception:
            pass
        # Коррекция range_risk по BB-width
        try:
            flags = llm_result.get("flags") or []
            if not isinstance(flags, list):
                flags = []
            bbw = ((llm_payload.get("taapi") or {}).get("bb_width") or {}).get("4h")
            if isinstance(bbw, (int, float)):
                if bbw < 0.025 and "range_risk" not in flags:
                    flags.append("range_risk")
                if bbw >= 0.025 and "range_risk" in flags:
                    flags = [f for f in flags if f != "range_risk"]
            llm_result["flags"] = flags
        except Exception:
            pass
        # Коррекция флага dmi_conflict (убираем ложноположительный)
        try:
            flags = llm_result.get("flags") or []
            if isinstance(flags, list) and ("dmi_conflict" in flags):
                dmi = (llm_payload.get("taapi") or {}).get("dmi") or {}
                d4 = dmi.get("4h") or {}
                d12 = dmi.get("12h") or {}
                dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
                def _sp(d: dict) -> float:
                    try:
                        return float((d.get("di_plus") or 0) - (d.get("di_minus") or 0))
                    except Exception:
                        return 0.0
                sp4 = _sp(d4)
                sp12 = _sp(d12)
                small4 = abs(sp4) < 3
                small12 = abs(sp12) < 3
                conflict = False
                if dirn == "long":
                    conflict = ((sp4 <= 0 and sp12 <= 0) or (small4 and small12))
                elif dirn == "short":
                    conflict = ((sp4 >= 0 and sp12 >= 0) or (small4 and small12))
                if not conflict:
                    llm_result["flags"] = [f for f in flags if f != "dmi_conflict"]
        except Exception:
            pass
        # entry_far → корректная заметка в corrections
        try:
            flags = llm_result.get("flags") or []
            corr = llm_result.get("corrections") or {}
            dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
            cp = (llm_payload.get("setup_parsed") or {}).get("current_price")
            kv = (llm_payload.get("setup_parsed") or {}).get("key_levels") or {}
            zone_min = kv.get("min"); zone_max = kv.get("max")
            if isinstance(flags, list) and ("entry_far" in flags):
                # держим entry, ждём отката
                corr["entry"] = "keep"
                if dirn == "long":
                    if zone_min is not None and zone_max is not None:
                        corr["entry_note"] = f"цена выше входа; ждём откат к зоне {round(float(zone_min),4)}–{round(float(zone_max),4)}"
                    else:
                        corr["entry_note"] = "цена выше входа; ждём откат к входной зоне"
                elif dirn == "short":
                    if zone_min is not None and zone_max is not None:
                        corr["entry_note"] = f"цена ниже входа; ждём ретест зоны {round(float(zone_min),4)}–{round(float(zone_max),4)}"
                    else:
                        corr["entry_note"] = "цена ниже входа; ждём ретест входной зоны"
                llm_result["corrections"] = corr
        except Exception:
            pass
        # Если proposals пусты — добавим консервативный ATR-вариант
        try:
            props = llm_result.get("proposals") or []
            if not props:
                atr4 = (llm_payload.get("taapi") or {}).get("atr", {}).get("4h")
                entry_val = (llm_payload.get("setup_parsed") or {}).get("current_price") or (llm_payload.get("market_context") or {}).get("last_price")
                dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
                if atr4 and entry_val:
                    ent = float(entry_val); a = float(atr4)
                    if dirn == "long":
                        sl = round(ent - 1.2*a, 6)
                        tp = round(ent + 2.4*a, 6)
                    else:
                        sl = round(ent + 1.2*a, 6)
                        tp = round(ent - 2.4*a, 6)
                    rr = abs(tp - ent) / max(1e-9, abs(ent - sl))
                    if rr >= 1.5:
                        llm_result["proposals"] = [{
                            "method": "ATR", "entry": "use_current", "sl": sl, "tp": tp,
                            "rr": round(rr, 2), "justification": "базовый ATR-профиль с RR≈2 на фоне текущей волатильности"
                        }]
            # если есть key_levels и ATR — попытаться дополнить KeyLevel вариантом, чтобы было не менее 2
            props = llm_result.get("proposals") or []
            if len(props) < 2:
                kv = (llm_payload.get("setup_parsed") or {}).get("key_levels") or {}
                atr4 = (llm_payload.get("taapi") or {}).get("atr", {}).get("4h")
                dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
                # сформировать pullback только если зона подходит по направлению
                if kv and atr4:
                    a = float(atr4)
                    ktype = (kv.get("type") or "").upper()
                    kmin = kv.get("min"); kmax = kv.get("max")
                    if kmin is not None and kmax is not None:
                        mid = float((kmin + kmax) / 2.0)
                        if dirn == "long" and ktype == "SUPPORT":
                            ent = mid
                            sl = min(float(kmin) - 0.2*a, ent - 1.2*a)
                            tp = ent + max(2.0*a, 2.4*a)
                        elif dirn == "short" and ktype == "RESISTANCE":
                            ent = mid
                            sl = max(float(kmax) + 0.2*a, ent + 1.2*a)
                            tp = ent - max(2.0*a, 2.4*a)
                        else:
                            ent = None
                        if ent is not None:
                            rr = abs(tp - ent) / max(1e-9, abs(ent - sl))
                            if rr >= 1.5:
                                props.append({
                                    "method": "KeyLevel", "entry": round(ent, 6), "sl": round(sl, 6), "tp": round(tp, 6),
                                    "rr": round(rr, 2), "justification": "pullback к ключевой зоне, SL ниже/выше зоны, RR≥1.5"
                                })
                                llm_result["proposals"] = props[:3]
        except Exception:
            pass
        # Обновление subscores.staleness по реальному возрасту (если есть ts)
        try:
            from datetime import datetime, timezone
            def _staleness_subscore(ts_setup: Optional[str], now_dt: datetime, tf_minutes: int = 240) -> int:
                try:
                    if not ts_setup:
                        return 50
                    # простая поддержка ISO 8601 с Z
                    ts = ts_setup
                    if ts.endswith("Z"):
                        ts = ts.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_min = (now_dt - dt.astimezone(timezone.utc)).total_seconds() / 60.0
                    if age_min <= tf_minutes:
                        return 80
                    if age_min <= 2 * tf_minutes:
                        return 60
                    if age_min <= 4 * tf_minutes:
                        return 45
                    return 30
                except Exception:
                    return 50

            now_dt = datetime.now(timezone.utc)
            subs = llm_result.get("subscores") or {}
            if not isinstance(subs, dict):
                subs = {}
            # попытка взять время сетапа из parsed меты
            ts_setup = (llm_payload.get("setup_parsed") or {}).get("_meta", {}).get("source_timestamp_utc")
            if not ts_setup:
                # fallback: по RSI ts
                rsi_ts = ((llm_payload.get("taapi") or {}).get("filters") or {}).get("rsi_12h_ts")
                if isinstance(rsi_ts, (int, float)):
                    ts_setup = datetime.fromtimestamp(float(rsi_ts), tz=timezone.utc).isoformat()
            subs["staleness"] = _staleness_subscore(ts_setup, now_dt, tf_minutes=240)
            llm_result["subscores"] = subs
        except Exception:
            pass
        # Коррекция debug.notes: убрать DMI-конфликт, подсветить MACD конфликт, если есть
        try:
            dbg = llm_result.get("debug") or {}
            notes = str(dbg.get("notes") or "")
            # удалить упоминание dmi_conflict, если его нет во flags
            flags = llm_result.get("flags") or []
            if isinstance(flags, list) and ("dmi_conflict" not in flags) and notes:
                notes = notes.replace("dmi_conflict", "")
            # добавить MACD конфликт, если знаки гистограмм 4h/12h различны
            macd4 = (llm_payload.get("taapi") or {}).get("macd", {}).get("4h", {})
            macd12 = (llm_payload.get("taapi") or {}).get("macd", {}).get("12h", {})
            h4 = macd4.get("hist"); h12 = macd12.get("hist")
            try:
                if h4 is not None and h12 is not None and (float(h4) * float(h12) < 0):
                    if "MACD 4h < 0 vs 12h > 0" not in notes and "MACD 12h < 0 vs 4h > 0" not in notes:
                        if float(h4) < 0 < float(h12):
                            notes = (notes + "; " if notes else "") + "MACD 4h < 0 vs 12h > 0"
                        elif float(h12) < 0 < float(h4):
                            notes = (notes + "; " if notes else "") + "MACD 12h < 0 vs 4h > 0"
            except Exception:
                pass
            if notes:
                dbg["notes"] = notes.strip().strip("; ")
                llm_result["debug"] = dbg
        except Exception:
            pass
        # Fallback conclusion если пусто
        try:
            if not llm_result.get("conclusion"):
                dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
                is_long = dirn == "long"
                llm_result["conclusion"] = {
                    "headline": "Картина смешанная; ждём подтверждения тайминга",
                    "bullets": [
                        "Плюсы: DI+>DI− на HTF, цена выше EMA200 12h" if is_long else "Плюсы: DI−>DI+ на HTF, цена ниже EMA200 12h",
                        "Риски: MACD_hist 4h < 0, ADX 4h на грани",
                        "Что улучшит: MACD_hist 4h → 0+, рост ADX12h; ретест поддержки и отбой" if is_long else "Что улучшит: MACD_hist 4h → 0−, рост ADX12h; ретест сопротивления и отбой",
                        "План: добор от зоны поддержки, SL за свинг/≥1.2×ATR, цель — ближайшее сопротивление" if is_long else "План: вход от зоны сопротивления, SL за свинг/≥1.2×ATR, цель — ближайшая поддержка",
                    ],
                    "invalidation": "Закрепление ниже ключевой поддержки и roll-over ADX12h" if is_long else "Закрепление выше ключевого сопротивления и roll-over ADX12h",
                }
        except Exception:
            pass
        # Fallback tactical_comment если пусто
        try:
            if not llm_result.get("tactical_comment"):
                dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
                kv = (llm_payload.get("setup_parsed") or {}).get("key_levels") or {}
                a4 = (llm_payload.get("taapi") or {}).get("atr", {}).get("4h")
                grid = []
                if kv:
                    kmin = kv.get("min"); kmax = kv.get("max")
                    if kmin is not None and kmax is not None:
                        mid = round((float(kmin) + float(kmax)) / 2.0, 2)
                        # простая сетка вокруг середины зоны
                        grid = [str(round(mid + d, 1)) for d in ([0.0, -2.4, -4.8] if dirn == "long" else [0.0, 2.4, 4.8])]
                adv = []
                if a4:
                    adv.append(f"SL: {'ниже' if dirn=='long' else 'выше'} зоны (~1.2×ATR)")
                    adv.append("TP: частичная фиксация у ближайшей цель/зоны")
                adv.append("Контроль: MACD_hist 4h → в сторону позиции, рост ADX12h")
                llm_result["tactical_comment"] = {
                    "summary": "Ждём отката к ключевой зоне и подтверждения тайминга (MACD/ADX), затем работаем от зоны сеткой.",
                    "grid_levels": grid[:3],
                    "advice": adv[:3],
                }
        except Exception:
            pass
        # Валидация предложений уровней (proposals)
        try:
            def _validate_proposals(res: dict, direction: str, entry_val: Optional[float], atr4: Optional[float]) -> dict:
                if not isinstance(res, dict):
                    return res
                proposals = res.get("proposals") or []
                if not isinstance(proposals, list):
                    res["proposals"] = []
                    return res
                ok = []
                ent0 = entry_val
                for p in proposals:
                    try:
                        ent = ent0
                        if p.get("entry") not in (None, "use_current"):
                            ent = float(p.get("entry"))
                        sl = float(p["sl"]) if p.get("sl") is not None else None
                        tp = float(p["tp"]) if p.get("tp") is not None else None
                        if ent is None or sl is None or tp is None:
                            continue
                        if direction == "long" and not (sl < ent < tp):
                            continue
                        if direction == "short" and not (tp < ent < sl):
                            continue
                        rr = abs(tp - ent) / max(1e-9, abs(ent - sl))
                        if rr < 1.5:
                            continue
                        if atr4:
                            dist_sl_atr = abs(ent - sl) / atr4
                            if not (0.5 <= dist_sl_atr <= 2.5):
                                continue
                        p["rr"] = round(rr, 2)
                        ok.append(p)
                    except Exception:
                        continue
                res["proposals"] = ok[:3]
                return res

            dirn = (llm_result or {}).get("direction") or llm_payload.get("setup_parsed", {}).get("direction")
            ent_fallback = llm_payload.get("setup_parsed", {}).get("current_price") or llm_payload.get("market_context", {}).get("last_price")
            atr4 = (llm_payload.get("taapi") or {}).get("atr", {}).get("4h")
            llm_result = _validate_proposals(llm_result, dirn, ent_fallback, atr4)
        except Exception:
            pass
        # Валидация и расчёт RR для сетки (grid)
        try:
            def rr_long(entry: float, tp: float, sl: float) -> float:
                return abs(tp - entry) / max(1e-9, abs(entry - sl))

            def rr_short(entry: float, tp: float, sl: float) -> float:
                return abs(entry - tp) / max(1e-9, abs(sl - entry))

            def validate_and_fix_grid(res: dict, direction: str, current_price: Optional[float], atr4: Optional[float], key_band: Optional[tuple[float, float]], hard_stop_value: Optional[float]):
                g = res.get("grid") or {}
                # Ensure single TP mode tied to setup TP if present
                try:
                    setup_tp = (llm_payload.get("setup_parsed") or {}).get("tp")
                    if setup_tp is not None:
                        g.setdefault("mode", "single_tp")
                        g["tp_single"] = float(setup_tp)
                except Exception:
                    pass
                sl = g.get("hard_stop")
                entries = g.get("entries") or []
                mode = g.get("mode") or "single_tp"
                tp_single = g.get("tp_single")
                # enforce hard stop from setup
                try:
                    if hard_stop_value is not None:
                        sl = float(hard_stop_value)
                        g["hard_stop"] = sl
                except Exception:
                    pass
                if sl is None:
                    res["grid"] = g
                    return res
                if not isinstance(entries, list):
                    entries = []
                fixed = []
                for e in entries:
                    try:
                        price = float(e.get("price"))
                    except Exception:
                        e["eligible"] = False
                        e["rr"] = 0.0
                        fixed.append(e)
                        continue
                    tp = e.get("tp")
                    eligible = bool(e.get("eligible", True))
                    if tp is None:
                        eligible = False
                    try:
                        slf = float(sl)
                        tpf = float(tp) if tp is not None else None
                    except Exception:
                        eligible = False
                        tpf = None
                    if direction == "long":
                        if current_price is not None and price > float(current_price):
                            eligible = False
                        if not (tpf is not None and slf < price < tpf):
                            eligible = False
                        rr = rr_long(price, tpf if tpf is not None else price, slf) if eligible else 0.0
                    else:
                        if current_price is not None and price < float(current_price):
                            eligible = False
                        if not (tpf is not None and tpf < price < slf):
                            eligible = False
                        rr = rr_short(price, tpf if tpf is not None else price, slf) if eligible else 0.0
                    if eligible and rr < 3.0:
                        eligible = False
                    e["eligible"] = eligible
                    e["rr"] = round(rr, 2) if rr else 0.0
                    fixed.append(e)
                # ensure exactly 5 entries (pad if needed)
                final_entries = list(fixed)
                # choose step
                step_px = None
                if atr4:
                    try:
                        step_px = float(atr4) * 0.45
                    except Exception:
                        step_px = None
                if step_px is None and key_band:
                    try:
                        band_min, band_max = float(key_band[0]), float(key_band[1])
                        band_span = max(0.0, band_max - band_min)
                        step_px = band_span / 4.0 if band_span > 0 else None
                    except Exception:
                        step_px = None
                while len(final_entries) < 5 and current_price is not None:
                    last_price = None
                    if final_entries:
                        try:
                            last_price = float(final_entries[-1].get("price"))
                        except Exception:
                            last_price = None
                    base_price = float(current_price) if last_price is None else last_price
                    if step_px is None:
                        # no ATR and no band -> cannot infer step, break
                        break
                    if direction == "long":
                        price_new = base_price - step_px
                    else:
                        price_new = base_price + step_px
                    tp_new = None
                    if mode == "single_tp" and tp_single is not None:
                        tp_new = tp_single
                    elif atr4:
                        tp_new = (price_new + 3.0 * float(atr4)) if direction == "long" else (price_new - 3.0 * float(atr4))
                    eligible_new = True
                    # band constraint
                    if key_band:
                        try:
                            if not (float(key_band[0]) <= price_new <= float(key_band[1])):
                                eligible_new = False
                            # clamp into band softly
                            price_new = max(float(key_band[0]), min(price_new, float(key_band[1])))
                        except Exception:
                            pass
                    # direction validity and RR
                    rr_new = 0.0
                    try:
                        if tp_new is None:
                            eligible_new = False
                        if direction == "long":
                            if not (tp_new is not None and sl < price_new < tp_new):
                                eligible_new = False
                            rr_new = rr_long(price_new, float(tp_new) if tp_new is not None else price_new, float(sl)) if eligible_new else 0.0
                        else:
                            if not (tp_new is not None and tp_new < price_new < sl):
                                eligible_new = False
                            rr_new = rr_short(price_new, float(tp_new) if tp_new is not None else price_new, float(sl)) if eligible_new else 0.0
                        if eligible_new and rr_new < 3.0:
                            eligible_new = False
                    except Exception:
                        eligible_new = False
                        rr_new = 0.0
                    final_entries.append({
                        "price": round(price_new, 6),
                        "tp": round(float(tp_new), 6) if tp_new is not None else None,
                        "eligible": bool(eligible_new),
                        "rr": round(rr_new, 2) if rr_new else 0.0,
                        "note": "auto-filled"
                    })
                # trim to 5
                if len(final_entries) > 5:
                    final_entries = final_entries[:5]
                g["entries"] = final_entries
                rr_vals = [e["rr"] for e in final_entries if e.get("eligible")]
                blended = round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0
                g["blended_rr"] = blended
                # compute average step in ATR units across final entries
                if atr4 and len(final_entries) >= 2:
                    steps = []
                    for i in range(1, len(final_entries)):
                        try:
                            steps.append(abs(float(final_entries[i]["price"]) - float(final_entries[i-1]["price"])) / float(atr4))
                        except Exception:
                            pass
                    g.setdefault("constraints", {})
                    g["constraints"]["step_atr_4h"] = round(sum(steps)/len(steps), 2) if steps else None
                if key_band:
                    g.setdefault("constraints", {})
                    g["constraints"]["used_key_level_band"] = [float(key_band[0]), float(key_band[1])]
                res["grid"] = g
                return res

            dirn = (llm_payload.get("setup_parsed") or {}).get("direction") or ""
            cp = (llm_payload.get("setup_parsed") or {}).get("current_price") or (llm_payload.get("market_context") or {}).get("last_price")
            atr4 = (llm_payload.get("taapi") or {}).get("atr", {}).get("4h")
            kv = (llm_payload.get("setup_parsed") or {}).get("key_levels") or {}
            key_band = None
            if kv and kv.get("min") is not None and kv.get("max") is not None:
                key_band = (float(kv.get("min")), float(kv.get("max")))
            sl_value = (llm_payload.get("setup_parsed") or {}).get("sl")
            # Force single TP grid from setup tp before validation
            try:
                setup_tp = (llm_payload.get("setup_parsed") or {}).get("tp")
                if setup_tp is not None:
                    llm_result = llm_result or {}
                    llm_result.setdefault("grid", {})
                    llm_result["grid"]["mode"] = "single_tp"
                    llm_result["grid"]["tp_single"] = float(setup_tp)
            except Exception:
                pass
            llm_result = validate_and_fix_grid(llm_result, dirn, cp, atr4, key_band, sl_value)
        except Exception:
            pass

    return preview, llm_payload, llm_result


