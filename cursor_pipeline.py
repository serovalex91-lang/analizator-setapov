from __future__ import annotations
import os, asyncio, random, math
from typing import Any, Dict, Optional, Tuple
import time
from llm_prompt import PROMPT
import json

import httpx

# === CONFIG ===
TAAPI_KEY = os.getenv("TAAPI_KEY", "")
TAAPI_BASE = "https://api.taapi.io"
TAAPI_EXCHANGE = os.getenv("TAAPI_EXCHANGE", "binancefutures")
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUT = "https://fapi.binance.com"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Тайм-ауты/ретраи
HTTP_TIMEOUT = 10.0
RETRIES = 3

TF_BALLS = ["30m", "60m", "120m", "240m", "720m"]


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

async def taapi_get(client: httpx.AsyncClient, ind: str, symbol_slash: str, interval: str, extra: dict | None = None) -> dict | None:
    # TAAPI чаще любит символ формата BTC/USDT
    secret = _get_taapi_key()
    if not secret:
        return None
    params = {"secret": secret, "exchange": TAAPI_EXCHANGE, "symbol": symbol_slash, "interval": interval}
    if extra: params.update(extra)
    return await _aget_json(client, f"{TAAPI_BASE}/{ind}", params)

async def fetch_taapi_bundle(symbol_usdt: str) -> dict:
    # параллельные запросы (4h/12h)
    base = symbol_usdt.upper()
    # для TAAPI используем BTC/USDT
    if base.endswith("USDT"):
        slash = base[:-4] + "/USDT"
    else:
        slash = base

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        adx4   = asyncio.create_task(taapi_get(client, "adx",   slash, "4h"))
        adx12  = asyncio.create_task(taapi_get(client, "adx",   slash, "12h"))
        dmi4   = asyncio.create_task(taapi_get(client, "dmi",   slash, "4h"))
        dmi12  = asyncio.create_task(taapi_get(client, "dmi",   slash, "12h"))
        # фолбэк на отдельные DI эндпоинты
        plusdi4   = asyncio.create_task(taapi_get(client, "plusdi",  slash, "4h"))
        minusdi4  = asyncio.create_task(taapi_get(client, "minusdi", slash, "4h"))
        plusdi12  = asyncio.create_task(taapi_get(client, "plusdi",  slash, "12h"))
        minusdi12 = asyncio.create_task(taapi_get(client, "minusdi", slash, "12h"))
        macd4  = asyncio.create_task(taapi_get(client, "macd",  slash, "4h",  extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}))
        macd12 = asyncio.create_task(taapi_get(client, "macd",  slash, "12h", extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9}))
        macd4_bt1  = asyncio.create_task(taapi_get(client, "macd",  slash, "4h",  extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9, "backtrack": 1}))
        macd12_bt1 = asyncio.create_task(taapi_get(client, "macd",  slash, "12h", extra={"optInFastPeriod": 12, "optInSlowPeriod": 26, "optInSignalPeriod": 9, "backtrack": 1}))
        atr4   = asyncio.create_task(taapi_get(client, "atr",   slash, "4h"))
        atr12  = asyncio.create_task(taapi_get(client, "atr",   slash, "12h"))
        mfi4   = asyncio.create_task(taapi_get(client, "mfi",   slash, "4h"))
        bb4    = asyncio.create_task(taapi_get(client, "bbands",slash, "4h"))
        obv_now= asyncio.create_task(taapi_get(client, "obv",   slash, "4h"))
        obv_bt = asyncio.create_task(taapi_get(client, "obv",   slash, "4h", extra={"backtrack": 20}))
        rsi12  = asyncio.create_task(taapi_get(client, "rsi",   slash, "12h", extra={"optInMAType": 1, "backtrack": 0, "close": 1}))
        ema200 = asyncio.create_task(taapi_get(client, "ema",   slash, "12h", extra={"period": 200}))

        # ждем
        adx4, adx12, dmi4, dmi12, macd4, macd12, atr4, atr12, mfi4, bb4, obv_now, obv_bt, rsi12, ema200, \
        plusdi4, minusdi4, plusdi12, minusdi12, macd4_bt1, macd12_bt1 = await asyncio.gather(
            adx4, adx12, dmi4, dmi12, macd4, macd12, atr4, atr12, mfi4, bb4, obv_now, obv_bt, rsi12, ema200,
            plusdi4, minusdi4, plusdi12, minusdi12, macd4_bt1, macd12_bt1
        )

    # нормализация
    # ADX могут приходить как value / valueAdx
    def _adx_val(x: dict | None) -> Optional[float]:
        if not x: return None
        return _to_float(x.get("value") or x.get("valueAdx") or x.get("adx") or x.get("result"))

    adx = {"4h": _adx_val(adx4), "12h": _adx_val(adx12)}
    dmi = {"4h": map_dmi(dmi4), "12h": map_dmi(dmi12)}
    # фолбэк, если dmi пустой или без значений
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
    if not dmi["4h"] or dmi["4h"].get("di_plus") is None or dmi["4h"].get("di_minus") is None:
        dmi["4h"] = {
            "di_plus":  pick(plusdi4,  "value", "plusdi", "valuePlusDi", "valuePlusDI"),
            "di_minus": pick(minusdi4, "value", "minusdi", "valueMinusDi", "valueMinusDI"),
        }
    if not dmi["12h"] or dmi["12h"].get("di_plus") is None or dmi["12h"].get("di_minus") is None:
        dmi["12h"] = {
            "di_plus":  pick(plusdi12,  "value", "plusdi", "valuePlusDi", "valuePlusDI"),
            "di_minus": pick(minusdi12, "value", "minusdi", "valueMinusDi", "valueMinusDI"),
        }
    if dmi["4h"].get("di_plus") is None or dmi["4h"].get("di_minus") is None:
        try:
            print("TAAPI DMI 4h raw:", dmi4, plusdi4, minusdi4)
        except Exception:
            pass
    if dmi["12h"].get("di_plus") is None or dmi["12h"].get("di_minus") is None:
        try:
            print("TAAPI DMI 12h raw:", dmi12, plusdi12, minusdi12)
        except Exception:
            pass
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

async def call_llm(llm_payload: dict) -> dict | None:
    if not OPENAI_API_KEY:
        print("[LLM] OPENAI_API_KEY missing")
        return None
    base = (OPENAI_BASE_URL or "https://api.openai.com").rstrip("/")
    path = "/chat/completions" if base.endswith("/v1") else "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    user_content = PROMPT + "\n" + json.dumps(llm_payload, ensure_ascii=False)
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.25,
        "messages": [
            {"role": "system", "content": "Ты опытный крипто-аналитик. Отвечай строго JSON, без текста вне JSON."},
            {"role": "user", "content": user_content},
        ],
    }

    try:
        print(f"[LLM] base_url={base!r}, path={path!r}, model={OPENAI_MODEL!r}")
    except Exception:
        pass

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

async def orchestrate_setup_flow(parsed: dict, PROMPT: str, with_llm: bool = True) -> tuple[str, dict, Optional[dict]]:
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
    ta = await fetch_taapi_bundle(symbol)

    # 3) деривативы и BTC фон (не критично, но полезно)
    funding, oi_chg = await fetch_derivatives_context(symbol)
    btc_ctx = await fetch_btc_context()

    # 4) сбор LLM payload
    llm_payload = build_llm_payload(parsed, ta, last_price, funding, oi_chg, btc_ctx)

    # 5) превью для глаза
    preview = make_human_preview(llm_payload)

    # 6) LLM (опц.)
    llm_result = None
    if with_llm:
        # добавим echo-guard, чтобы отлавливать эхо входа
        payload_for_llm = dict(llm_payload)
        payload_for_llm["_echo_guard"] = "DO_NOT_RETURN"
        llm_result = await call_llm(payload_for_llm)
        # если пришли subscores — считаем итоговый score в коде
        try:
            WEIGHTS = {
                "trend_align":0.15, "dmi_spread":0.10, "adx_strength":0.15, "macd_momentum":0.15,
                "rsi_state":0.10, "vol_regime":0.10, "obv_flow":0.05, "levels_quality":0.10,
                "market_ctx":0.07, "staleness":0.03
            }
            if isinstance(llm_result, dict) and isinstance(llm_result.get("subscores"), dict):
                sub = llm_result["subscores"]
                base = 0.0
                for k, w in WEIGHTS.items():
                    try:
                        base += w * float(sub.get(k, 0) or 0)
                    except Exception:
                        pass
                score = int(round(base))
                flags = llm_result.get("flags") or []
                if isinstance(flags, list):
                    if "counter_trend" in flags:
                        score -= 7
                    if "adx_low" in flags:
                        score -= 5
                score = max(0, min(100, score))
                llm_result["score"] = score
                llm_result["confidence"] = max(1, min(10, int(round(score/10))))
        except Exception:
            pass
        # Нормализация confidence и confidence_text в любом случае
        try:
            if isinstance(llm_result, dict) and isinstance(llm_result.get("score"), (int, float)):
                _conf = int(max(1, min(10, round(float(llm_result.get("score")) / 10))))
                llm_result["confidence"] = _conf
                llm_result["confidence_text"] = f"{_conf*10}% уверенности"
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

    return preview, llm_payload, llm_result


