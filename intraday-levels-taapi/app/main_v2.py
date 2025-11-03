from fastapi import FastAPI, HTTPException
from typing import Dict, Any
import pandas as pd
import httpx
import logging
import json

logger = logging.getLogger(__name__)

from app.schemas import LevelSearchRequest, BallFlip, IntradaySearchResponse
from app.schemas import CursorRunRequest, CursorRunResponse
from app.config import SYMBOLS, TF_LIST, RESULTS, CACHE_TTL_SECONDS
from app.services.cache import TTLCache
# Use the unified TAAPI bulk client that matches constructs and response format
from app.services.taapi_bulk import (
    taapi_bulk,
    construct_candles,
    construct_indicator,
    parse_bulk_candles,
    parse_indicator_value,
    get_candles_direct,
)
from app.services.indicators_12h import indicators_12h_service
from app.services.vwap import session_vwap
from app.services.pivots import classic_pivots
from app.services.levels import find_best_level
from app.services.utils import infer_tick_from_price
from app.services.binance_fallback import binance_fallback
from app.services.cursor import fetch_taapi_cursor_pack, call_llm
from app.services.cursor import maybe_notify_telegram

app = FastAPI(title="Intraday Levels (TAAPI-only)")
cache = TTLCache(ttl_seconds=CACHE_TTL_SECONDS)

@app.get("/health")
async def health(): 
    return {"ok": True}

@app.post("/ball_flip")
async def ball_flip(evt: BallFlip):
    # Заглушка: можно вешать автоматический вызов поиска.
    return {"received": True}

def _cache_key(symbol: str) -> str:
    return f"pack:{symbol}"

async def fetch_pack(symbol: str) -> Dict[str, Any]:
    """
    Тянем свечи 5m/15m/30m/1h/4h + индикаторы (ATR/EMA20/EMA50/EMA200/ADX) через TAAPI bulk.
    При ошибке Taapi.io используем fallback на Binance API.
    Кэшируем результат на CACHE_TTL_SECONDS.
    """
    key = _cache_key(symbol)
    cached = cache.get(key)
    if cached: 
        return cached

    # Пробуем сначала Taapi.io
    try:
        # Свечи — напрямую через Direct Method (без ограничений 20)
        klines: Dict[str, Any] = {}
        for tf in TF_LIST:
            klines[tf] = await get_candles_direct(symbol, tf, RESULTS[tf])

        # Индикаторы 30m через Bulk (не более 20 результатов)
        constructs = [
            construct_indicator("atr", symbol, "30m", {"period": 14}),
            construct_indicator("ema", symbol, "30m", {"period": 20}),
            construct_indicator("ema", symbol, "30m", {"period": 50}),
            construct_indicator("ema", symbol, "30m", {"period": 200}),
            construct_indicator("adx", symbol, "30m", {"period": 14}),
        ]
        data = await taapi_bulk(constructs)
        rows = data.get("results") or data.get("data") or []
        atr30 = parse_indicator_value(rows[0]) if len(rows) > 0 else None
        ema20 = parse_indicator_value(rows[1]) if len(rows) > 1 else None
        ema50 = parse_indicator_value(rows[2]) if len(rows) > 2 else None
        ema200 = parse_indicator_value(rows[3]) if len(rows) > 3 else None
        adx30 = parse_indicator_value(rows[4]) if len(rows) > 4 else None

        pack = {
            "symbol": symbol,
            "klines": klines,
            "indicators_30m": {
                "atr": atr30, 
                "ema20": ema20, 
                "ema50": ema50, 
                "ema200": ema200, 
                "adx": adx30
            },
            "source": "taapi"
        }
        
    except Exception as taapi_error:
        logger.warning(f"Taapi.io failed for {symbol}, using Binance fallback: {taapi_error}")
        
        # Fallback на Binance API
        try:
            klines: Dict[str, Any] = {}
            indicators_30m = {}
            
            # Получаем свечи через Binance
            for tf in TF_LIST:
                klines[tf] = await binance_fallback.get_candles(symbol, tf, RESULTS[tf])
            
            # Рассчитываем индикаторы для 30m
            if "30m" in klines and len(klines["30m"]) > 0:
                indicators_30m = binance_fallback.calculate_simple_indicators(klines["30m"])
            
            pack = {
                "symbol": symbol,
                "klines": klines,
                "indicators_30m": indicators_30m,
                "source": "binance_fallback"
            }
            
        except Exception as binance_error:
            logger.error(f"Both Taapi.io and Binance fallback failed for {symbol}: {binance_error}")
            raise HTTPException(status_code=502, detail=f"All data sources failed for {symbol}")

    cache.set(key, pack)
    return pack

def build_session_info(k30: list, k5: list) -> Dict[str, Any]:
    df = pd.DataFrame(k30)
    df["t"] = pd.to_datetime(df["t"])
    dates = sorted(set(df["t"].dt.date))
    if len(dates) < 2:
        raise HTTPException(503, "Not enough data for pivots")
    prev = dates[-2]; today = dates[-1]
    dprev = df[df["t"].dt.date == prev]
    PDH = float(dprev["high"].max()); PDL = float(dprev["low"].min()); PDC = float(dprev["close"].iloc[-1])
    # Session VWAP по текущим суткам (30m достаточно, но можно и 5m)
    dcur = df[df["t"].dt.date == today]
    vwap_val = session_vwap(dcur.to_dict("records")) if len(dcur) > 0 else None
    piv = classic_pivots(PDH, PDL, PDC)
    return {"PDH": PDH, "PDL": PDL, "PDC": PDC, "pivots_daily": piv, "vwap_session": vwap_val}

async def get_origin_indicators(symbol: str, origin_tf: str) -> Dict[str, float]:
    """Если origin_tf=60m — дотянем ATR/EMA/ADX для 60m отдельным bulk."""
    if origin_tf == "30m":
        return {}  # уже есть в pack['indicators_30m']
    
    try:
        constructs = [
            construct_indicator("atr", symbol, "60m", {"period": 14}),
            construct_indicator("ema", symbol, "60m", {"period": 20}),
            construct_indicator("ema", symbol, "60m", {"period": 50}),
            construct_indicator("ema", symbol, "60m", {"period": 200}),
            construct_indicator("adx", symbol, "60m", {"period": 14}),
        ]
        data = await taapi_bulk(constructs)
        d = data.get("results") or data.get("data") or []
        def v(i):
            return parse_indicator_value(d[i]) if i < len(d) else None
        return {"atr": v(0), "ema20": v(1), "ema50": v(2), "ema200": v(3), "adx": v(4)}
    except Exception as taapi_error:
        logger.warning(f"Taapi.io failed for {symbol} 60m indicators, using Binance fallback: {taapi_error}")
        try:
            # Fallback на Binance для 60m
            candles_60m = await binance_fallback.get_candles(symbol, "60m", 100)
            indicators = binance_fallback.calculate_simple_indicators(candles_60m)
            return indicators
        except Exception as binance_error:
            logger.error(f"Both Taapi.io and Binance fallback failed for {symbol} 60m: {binance_error}")
            return {"atr": None, "ema20": None, "ema50": None, "ema200": None, "adx": None}

async def get_filters_12h(symbol: str) -> Dict[str, float]:
    """Получить фильтры по RSI 12h и EMA200 12h через Binance/CoinGecko API."""
    try:
        rsi12h, ema200_12h = await indicators_12h_service.get_rsi_ema200_12h(symbol)
        return {"rsi12h": rsi12h, "ema200_12h": ema200_12h}
    except Exception as e:
        logger.error(f"Failed to fetch 12h indicators for {symbol}: {e}")
        return {"rsi12h": None, "ema200_12h": None}

@app.post("/levels/intraday-search")
async def intraday_search(req: LevelSearchRequest) -> IntradaySearchResponse:
    symbol = req.symbol.upper()
    if symbol not in [s.upper() for s in SYMBOLS]:
        raise HTTPException(400, f"Symbol {symbol} is not allowed")

    pack = await fetch_pack(symbol)
    kl = pack["klines"]
    if not kl["5m"] or not kl["30m"] or not kl["1h"] or not kl["4h"]:
        raise HTTPException(503, "Candles not ready")

    # Сессионные агрегаты
    session_info = build_session_info(kl["30m"], kl["5m"])

    # Индикаторы на origin_tf
    if req.origin_tf == "30m":
        inds = pack["indicators_30m"]
    else:
        inds = await get_origin_indicators(symbol, "60m")

    # tickSize: приближение от текущей цены
    last_price = float(kl["5m"][-1]["close"])
    tick_size = infer_tick_from_price(last_price)

    # Фильтры по RSI 12h и EMA200 12h
    filters = await get_filters_12h(symbol)
    rsi12h = filters.get("rsi12h")
    ema200_12h = filters.get("ema200_12h")
    if req.origin_tf not in ("30m", "60m", "120m"):
        pass  # формально не требуется, но оставим для совместимости

    # Условия пользователя:
    # long: rsi12h > 52 и price >= ema200_12h
    # short: rsi12h < 48 и price <= ema200_12h
    def _flt_ok_long() -> bool:
        try:
            return (rsi12h is not None and rsi12h > 52.0) and (ema200_12h is not None and last_price >= float(ema200_12h))
        except Exception:
            return False

    def _flt_ok_short() -> bool:
        try:
            return (rsi12h is not None and rsi12h < 48.0) and (ema200_12h is not None and last_price <= float(ema200_12h))
        except Exception:
            return False

    # Apply 12h filters with Binance/CoinGecko data
    filters_ok = _flt_ok_long() if req.context == "long" else _flt_ok_short()
    if not filters_ok:
        return IntradaySearchResponse(
            decision="no_trade",
            reason="filters_12h_blocked",
        )

    # Отладка session_info
    print(f"DEBUG: session_info type: {type(session_info)}")
    print(f"DEBUG: session_info content: {session_info}")
    
    # Поиск лучшего уровня
    res = find_best_level(
        kl["5m"], kl["15m"], kl["30m"], kl["1h"], kl["4h"], session_info,
        tick_size, {"atr": inds.get("atr"), "ema200": inds.get("ema200")}, 
        side=req.context, origin_tf=req.origin_tf
    )
    if not res:
        return IntradaySearchResponse(
            decision="no_trade",
            reason="no valid level found at current criteria"
        )

    # Расчет ордеров с реальными уровнями сопротивления/поддержки
    atr = float(inds.get("atr") or 0.0)
    level = res["price"]; tol = res["tolerance"]
    current_price = float(inds.get("close") or level)
    
    # Получаем пивоты из session_info
    pivots = session_info.get("pivots_daily", {}) if isinstance(session_info, dict) else {}
    
    # Отладочная информация
    print(f"DEBUG: Available pivots for {req.symbol}: {pivots}")
    
    if req.context == "long":
        entry = level
        # SL: ближайший уровень поддержки ниже (S1, S2, S3, S4)
        sl_candidates = []
        for key in ["S1", "S2", "S3", "S4"]:
            if key in pivots and pivots[key] is not None:
                try:
                    sl_val = float(pivots[key])
                    if sl_val < entry:
                        sl_candidates.append(sl_val)
                        print(f"DEBUG: SL candidate {key}: {sl_val}")
                except:
                    continue
        
        if sl_candidates:
            sl = max(sl_candidates)  # ближайший уровень поддержки
            print(f"DEBUG: Selected SL: {sl}")
        else:
            sl = entry - max(atr * 2, 3*tick_size)  # fallback на ATR
            print(f"DEBUG: SL fallback: {sl}")
        
        # TP: ближайший уровень сопротивления выше (R1, R2, R3, R4)
        tp_candidates = []
        for key in ["R1", "R2", "R3", "R4"]:
            if key in pivots and pivots[key] is not None:
                try:
                    tp_val = float(pivots[key])
                    if tp_val > entry:
                        tp_candidates.append(tp_val)
                        print(f"DEBUG: TP candidate {key}: {tp_val}")
                except:
                    continue
        
        if tp_candidates:
            tp1 = min(tp_candidates)  # ближайший уровень сопротивления
            print(f"DEBUG: Selected TP: {tp1}")
        else:
            # Fallback: минимальное соотношение 1:1.5
            risk = entry - sl
            tp1 = entry + (risk * 1.5)
            print(f"DEBUG: TP fallback: {tp1}")
            
    else:  # short
        entry = level
        # SL: ближайший уровень сопротивления выше (R1, R2, R3, R4)
        sl_candidates = []
        for key in ["R1", "R2", "R3", "R4"]:
            if key in pivots and pivots[key] is not None:
                try:
                    sl_val = float(pivots[key])
                    if sl_val > entry:
                        sl_candidates.append(sl_val)
                except:
                    continue
        
        if sl_candidates:
            sl = min(sl_candidates)  # ближайший уровень сопротивления
        else:
            sl = entry + max(atr * 2, 3*tick_size)  # fallback на ATR
        
        # TP: ближайший уровень поддержки ниже (S1, S2, S3, S4)
        tp_candidates = []
        for key in ["S1", "S2", "S3", "S4"]:
            if key in pivots and pivots[key] is not None:
                try:
                    tp_val = float(pivots[key])
                    if tp_val < entry:
                        tp_candidates.append(tp_val)
                except:
                    continue
        
        if tp_candidates:
            tp1 = max(tp_candidates)  # ближайший уровень поддержки
        else:
            # Fallback: минимальное соотношение 1:1.5
            risk = sl - entry
            tp1 = entry - (risk * 1.5)
    
    # Рассчитываем проценты и соотношение риск/прибыль
    if req.context == "long":
        risk_pct = ((entry - sl) / entry) * 100
        reward_pct = ((tp1 - entry) / entry) * 100
        risk_reward = (tp1 - entry) / (entry - sl) if (entry - sl) > 0 else 0
    else:
        risk_pct = ((sl - entry) / entry) * 100
        reward_pct = ((entry - tp1) / entry) * 100
        risk_reward = (entry - tp1) / (sl - entry) if (sl - entry) > 0 else 0

    # Добавляем key_levels для userbot
    key_levels = {}
    if req.context == "long":
        # Для LONG используем пивоты как key_levels
        key_levels = {
            "support": pivots.get("S1") or pivots.get("S2"),
            "resistance": pivots.get("R1") or pivots.get("R2")
        }
    else:
        # Для SHORT используем пивоты как key_levels
        key_levels = {
            "support": pivots.get("S1") or pivots.get("S2"),
            "resistance": pivots.get("R1") or pivots.get("R2")
        }

    return IntradaySearchResponse(
        decision=f"enter_{req.context}",
        reason="valid level found",
        level={
            "price": level,
            "score": res["score"],
            "confluence": res["confluence"],
            "tolerance": tol
        },
        orders={
            "entry": {"type": "limit", "price": entry},
            "sl": {"price": sl},
            "tp": [{"price": tp1, "portion": 1.0}]
        },
        # Добавляем key_levels для userbot
        key_levels=key_levels,
        last_price=current_price,
        # Дополнительная информация для торгового сетапа
        trade_setup={
            "entry_price": entry,
            "sl_price": sl,
            "tp_price": tp1,
            "risk_percent": round(risk_pct, 2),
            "reward_percent": round(reward_pct, 2),
            "risk_reward_ratio": round(risk_reward, 2),
            "current_price": current_price,
            "price_change_percent": round(((current_price - entry) / entry) * 100, 2),
            "debug_pivots": pivots,  # Добавляем пивоты для отладки
            "debug_tp_candidates": tp_candidates if req.context == "long" else []
        }
    )


@app.post("/cursor/run", response_model=CursorRunResponse)
async def cursor_run(req: CursorRunRequest) -> CursorRunResponse:
    symbol = req.setup.symbol.upper()
    taapi_pack = await fetch_taapi_cursor_pack(symbol)

    payload = {
        "meta": {
            "request_id": "",
            "ts_utc": pd.Timestamp.utcnow().isoformat(),
            "language": "ru",
        },
        "setup": {
            "symbol": symbol,
            "correction_tf": req.setup.correction_tf,
            "htf_trend_up": req.setup.htf_trend_up,
            "price_above_ma200_12h": req.setup.price_above_ma200_12h,
            "rsi_12h_gt_50": req.setup.rsi_12h_gt_50,
        },
        **taapi_pack,
        "scoring_prefs": (req.scoring_prefs.dict() if req.scoring_prefs else {
            "threshold_enter": 7.0,
            "weights": None,
        })
    }

    # Отправим в Telegram краткую сводку TAAPI для проверки актуальности
    try:
        t = taapi_pack.get("taapi", {}) if isinstance(taapi_pack, dict) else {}
        struct = taapi_pack.get("structure", {}) if isinstance(taapi_pack, dict) else {}
        adx4 = t.get("adx", {}).get("4h")
        adx12 = t.get("adx", {}).get("12h")
        dmi4 = t.get("dmi", {}).get("4h", {})
        macd4 = t.get("macd", {}).get("4h", {})
        atr4 = t.get("atr", {}).get("4h")
        atr12 = t.get("atr", {}).get("12h")
        obv4 = t.get("obv", {}).get("4h_trend")
        mfi4 = t.get("mfi", {}).get("4h")
        bbw4 = t.get("bb_width", {}).get("4h")
        s_hi = struct.get("last_swing_high_4h")
        s_lo = struct.get("last_swing_low_4h")
        dbg = (
            f"TAAPI {symbol}: ADX4h={adx4}, ADX12h={adx12}, "
            f"DI+4h={dmi4.get('di_plus')}, DI-4h={dmi4.get('di_minus')}, "
            f"MACD4h(hist={macd4.get('hist')}, sig={macd4.get('signal')}, cross={macd4.get('signal_cross')}), "
            f"ATR4h={atr4}, ATR12h={atr12}, OBV4h={obv4}, MFI4h={mfi4}, BBW4h={bbw4}, "
            f"swingH4h={s_hi}, swingL4h={s_lo}"
        )
        await maybe_notify_telegram(dbg)
    except Exception:
        pass

    prompt = (
        "Ты — опытный крипто-трейдер.\n"
        "На основе переданных данных оцени вероятность ПРОДОЛЖЕНИЯ тренда после коррекции.\n"
        "Не рассчитывай стопы, тейк-профиты или объём позиции.\n"
        "Задача — провести рейтинговую оценку (1–10) по категориям и выдать итоговый вердикт.\n\n"
        "Категории: trend_strength (ADX/DMI), momentum (MACD), volume_flow (OBV+MFI), "
        "range_risk (BB width), vol_rr (ATR и пригодность), structure (свинги).\n"
        "Шкала: 1–3 против тренда; 4–5 слабое; 6 нейтрально; 7–8 продолжение; 9–10 сильное.\n"
        "Правила: total_score = Σ(score_i×weight_i); если total_score >= threshold_enter → verdict=continuation;\n"
        "если ADX<20 и BB width низкий → range_risk; если DI- > DI+ и MACD hist < 0 → reversal_risk; иначе no_trade.\n\n"
        "Выводи только JSON со структурами: symbol, verdict, scores{...}, aggregate{total_score, threshold_enter, confidence_pct}, reasoning_short, notes."
    )
    llm = await call_llm(payload, prompt)
    # Отправим сырое LLM-возвращённое JSON в Telegram (усечённо), чтобы можно было проверить фактический ответ
    try:
        llm_preview = json.dumps(llm, ensure_ascii=False) if isinstance(llm, dict) else str(llm)
        await maybe_notify_telegram(f"LLM raw ({symbol}):\n{llm_preview[:3500]}")
    except Exception:
        pass

    try:
        verdict = llm.get("verdict") if isinstance(llm, dict) else None
        agg = llm.get("aggregate") if isinstance(llm, dict) else {}
        order = llm.get("order") if isinstance(llm, dict) else {}
        passed = (
            verdict == "continuation"
            and (agg.get("total_score") or 0) >= (agg.get("threshold_enter") or 7.0)
            and order.get("action") == "place"
        )
        if passed:
            try:
                await maybe_notify_telegram(f"LLM verdict: {verdict}\nSymbol: {symbol}\nScore: {agg.get('total_score')} / {agg.get('threshold_enter')}")
            except Exception:
                pass
            return CursorRunResponse(status="placed", message="ok", payload_sent=order, llm_raw=llm)
        else:
            try:
                await maybe_notify_telegram(f"LLM verdict: {verdict or 'no_trade'}\nSymbol: {symbol}")
            except Exception:
                pass
            return CursorRunResponse(status="skipped", message=str(verdict or "no_trade"), llm_raw=llm if isinstance(llm, dict) else None)
    except Exception as e:
        return CursorRunResponse(status="error", message=str(e), llm_raw=llm if isinstance(llm, dict) else None)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 