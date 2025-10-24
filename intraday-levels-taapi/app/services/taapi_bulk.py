import os
import json
import asyncio
import math
import httpx
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timezone
from app.config import (
    TAAPI_KEY, 
    HTTP_TIMEOUT_SECONDS, 
    MAX_RETRIES, 
    TAAPI_BASE_URL,
    RESULTS
)

def to_ta_symbol(sym: str) -> str:
    """Конвертация символа в формат Taapi.io"""
    if "/" in sym: 
        return sym
    if sym.endswith("USDT"): 
        return sym[:-4] + "/USDT"
    return sym  # простая эвристика

def session_vwap(ohlcv_rows):
    """Расчет VWAP (Volume Weighted Average Price)"""
    num, den = 0.0, 0.0
    for r in ohlcv_rows:
        p = (float(r['high']) + float(r['low']) + float(r['close'])) / 3.0
        v = float(r['volume'])
        num += p * v; den += v
    return num/den if den > 0 else None

def classic_pivots(prev_high, prev_low, prev_close):
    """Расчет классических пивотных точек"""
    pp = (prev_high + prev_low + prev_close)/3.0
    r1 = 2*pp - prev_low
    s1 = 2*pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2*(pp - prev_low)
    s3 = prev_low - 2*(prev_high - pp)
    r4 = prev_high + 3*(pp - prev_low)
    s4 = prev_low - 3*(prev_high - pp)
    return dict(PP=pp,R1=r1,S1=s1,R2=r2,S2=s2,R3=r3,S3=s3,R4=r4,S4=s4)

def swing_points(ohlcv, left=2, right=2):
    """Поиск свинг-точек (локальных максимумов и минимумов)"""
    highs, lows = [], []
    for i in range(left, len(ohlcv)-right):
        h = float(ohlcv[i]['high'])
        if all(h>=float(ohlcv[j]['high']) for j in range(i-left, i+right+1) if j!=i):
            highs.append((i, h))
        l = float(ohlcv[i]['low'])
        if all(l<=float(ohlcv[j]['low']) for j in range(i-left, i+right+1) if j!=i):
            lows.append((i, l))
    return highs, lows

def cluster_levels(points, tolerance):
    """Кластеризация уровней по толерантности"""
    if not points: 
        return []
    points = sorted(points)
    clusters, cur = [], [points[0]]
    for p in points[1:]:
        if abs(p - cur[-1]) <= tolerance:
            cur.append(p)
        else:
            clusters.append(sum(cur)/len(cur))
            cur = [p]
    clusters.append(sum(cur)/len(cur))
    return clusters

def compute_tolerance(price, atr, tick):
    """Расчет толерантности для уровней на основе цены, ATR и тика"""
    atr = atr or 0.0
    # Увеличенная зона: более широкий диапазон уровня и стопа
    return max(0.25 * atr, price * 0.0015, 5 * tick)

def score_level(level_price, confluence, rr, smashed_recent=False):
    """Оценка силы уровня на основе конвергенции факторов"""
    weights = {
        "htf_swing": 0.18,      # Свинг-точки на старших таймфреймах
        "pivot": 0.12,          # Пивотные точки
        "ema200_near": 0.12,    # Близость к EMA200
        "vwap": 0.10,           # VWAP
        "round": 0.06,          # Круглые числа
        "touches": 0.18,        # Количество касаний
        "volume_rejection": 0.12, # Отклонение объемом
        "trend_ok": 0.12        # Соответствие тренду
    }
    s = sum(weights[k] for k in confluence if k in weights)
    # Не обнуляем уровень по RR на этапе детекции — пользователь приоритизирует диапазон/SL
    if smashed_recent: 
        return 0.0  # Уровень недавно пробит
    return min(s, 1.0)

def construct_candles(symbol: str, interval: str, results: int) -> Dict[str, Any]:
    """Конструктор для запроса свечей"""
    return {
        "indicator": "candles",
        "exchange": "binance",
        "symbol": to_ta_symbol(symbol),
        "interval": interval,
        "results": results
    }

def construct_indicator(ind: str, symbol: str, interval: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Конструктор для запроса индикатора"""
    c = {
        "indicator": ind,
        "exchange": "binance",
        "symbol": to_ta_symbol(symbol),
        "interval": interval,
    }
    if inputs:
        c["inputs"] = inputs
    return c

async def taapi_bulk(constructs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Taapi Bulk (per latest docs):
    POST /bulk
    Body: {
      "secret": "<API_KEY>",
      "construct": {
         "exchange": "binance",
         "symbol": "BTC/USDT",
         "interval": "5m",
         "indicators": [ {"indicator":"rsi", "period":14}, ... ]
      }
    }

    Мы эмулируем массив запросов, выполняя несколько последовательных вызовов (по одному на каждый construct),
    и возвращаем агрегированный ответ вида {"results": [ ... ]} в исходном порядке.
    """
    if not TAAPI_KEY:
        raise RuntimeError("TAAPI_KEY is not set")

    url = f"{TAAPI_BASE_URL}/bulk"
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    results: List[Any] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        last_err = None
        for idx, c in enumerate(constructs):
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    base = {
                        "exchange": c.get("exchange", "binance"),
                        "symbol": c.get("symbol"),
                        "interval": c.get("interval"),
                    }
                    ind_name = c.get("indicator")
                    indicator_obj: Dict[str, Any] = {"indicator": ind_name}
                    # candles: use "results" parameter
                    if ind_name == "candles":
                        if "results" in c:
                            # Bulk API restriction: max 20 results per construct
                            try:
                                indicator_obj["results"] = max(1, min(int(c["results"]), 20))
                            except Exception:
                                indicator_obj["results"] = 20
                    else:
                        # other indicators: flatten inputs as fields (e.g., period)
                        inputs = c.get("inputs") or {}
                        for k, v in inputs.items():
                            indicator_obj[k] = v

                    body = {
                        "secret": TAAPI_KEY,
                        "construct": {
                            **base,
                            "indicators": [indicator_obj],
                        },
                    }

                    r = await client.post(url, json=body, headers={"Content-Type": "application/json"})
                    r.raise_for_status()
                    results.append(r.json())
                    break
                except Exception as e:
                    last_err = e
                    if attempt == MAX_RETRIES:
                        raise last_err
                    await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))

    return {"results": results}

async def get_candles_direct(symbol: str, interval: str, results: int) -> List[Dict[str, Any]]:
    """
    Fetch candles via Direct Method:
    GET /candle?secret=...&exchange=binance&symbol=BTC/USDT&interval=5m&results=...&addResultTimestamp=true
    Returns normalized list of candles using parse_bulk_candles.
    """
    if not TAAPI_KEY:
        raise RuntimeError("TAAPI_KEY is not set")

    url = f"{TAAPI_BASE_URL}/candle"
    params = {
        "secret": TAAPI_KEY,
        "exchange": "binance",
        "symbol": to_ta_symbol(symbol),
        "interval": interval,
        "results": results,
        "addResultTimestamp": "true",
    }

    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                # Direct returns object with arrays: timestamp/open/high/low/close/volume
                if isinstance(data, dict) and all(k in data for k in ["timestamp","open","high","low","close","volume"]):
                    candles_list: List[Dict[str, Any]] = []
                    ts_list = data.get("timestamp", []) or []
                    o_list = data.get("open", []) or []
                    h_list = data.get("high", []) or []
                    l_list = data.get("low", []) or []
                    c_list = data.get("close", []) or []
                    v_list = data.get("volume", []) or []
                    n = min(len(ts_list), len(o_list), len(h_list), len(l_list), len(c_list), len(v_list))
                    for i in range(n):
                        ts = ts_list[i]
                        t_iso = datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc).isoformat()
                        candles_list.append({
                            "t": t_iso,
                            "open": float(o_list[i]),
                            "high": float(h_list[i]),
                            "low": float(l_list[i]),
                            "close": float(c_list[i]),
                            "volume": float(v_list[i]),
                        })
                    return candles_list
                # Fallback: parse generic shapes
                if isinstance(data, list):
                    payload = {"data": data}
                elif isinstance(data, dict):
                    payload = data
                else:
                    payload = {"data": []}
                return parse_bulk_candles(payload)
            except Exception as e:
                last_err = e
                if attempt == MAX_RETRIES:
                    raise last_err
                await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))

def parse_bulk_candles(construct_result: Any) -> List[Dict[str, Any]]:
    """
    Приводим свечи к единому виду:
    [{'t': iso8601, 'open': float, 'high':..., 'low':..., 'close':..., 'volume': float}]
    TAAPI обычно возвращает {'data':[{'timestamp':..., 'open':..., ...},...]}
    """
    out = []
    # Support multiple TAAPI response shapes
    # 1) Bulk wrapper: { "data": [ { "indicator":"candles", "result": { ... } } ] }
    # 2) Direct result object: { "timestamp": [...], "open": [...], ... }
    # 3) Generic list of candle dicts under data/value/result
    if isinstance(construct_result, dict) and "data" in construct_result and isinstance(construct_result["data"], list):
        # Try to extract first candles payload
        for item in construct_result["data"]:
            if not isinstance(item, dict):
                continue
            res = item.get("result") or {}
            # Shape 2a: arrays per field
            if all(k in res for k in ["timestamp","open","high","low","close","volume"]):
                ts_list = res.get("timestamp", []) or []
                o_list = res.get("open", []) or []
                h_list = res.get("high", []) or []
                l_list = res.get("low", []) or []
                c_list = res.get("close", []) or []
                v_list = res.get("volume", []) or []
                n = min(len(ts_list), len(o_list), len(h_list), len(l_list), len(c_list), len(v_list))
                for i in range(n):
                    ts = ts_list[i]
                    t_iso = datetime.utcfromtimestamp(ts/1000 if ts > 10**12 else ts).replace(tzinfo=timezone.utc).isoformat()
                    out.append({
                        "t": t_iso,
                        "open": float(o_list[i]),
                        "high": float(h_list[i]),
                        "low": float(l_list[i]),
                        "close": float(c_list[i]),
                        "volume": float(v_list[i]),
                    })
                break
            # Shape 2b: list of dicts under result.data
            data_list = res.get("data") if isinstance(res, dict) else None
            if isinstance(data_list, list) and data_list:
                construct_result = {"data": data_list}
                break

    data = (
        construct_result.get("data")
        or construct_result.get("value")
        or construct_result.get("result")
        or []
    )

    for r in data:
        if isinstance(r, dict):
            ts = r.get("timestamp") or r.get("time") or r.get("t")
            if isinstance(ts, (int, float)):
                t_iso = datetime.utcfromtimestamp(ts/1000 if ts > 10**12 else ts).replace(tzinfo=timezone.utc).isoformat()
            elif ts is not None:
                t_iso = str(ts)
            else:
                continue
            open_v = r.get("open", r.get("o"))
            high_v = r.get("high", r.get("h"))
            low_v = r.get("low", r.get("l"))
            close_v = r.get("close", r.get("c"))
            vol_v = r.get("volume", r.get("v", 0.0))
            out.append({
                "t": t_iso,
                "open": float(open_v if open_v is not None else 0.0),
                "high": float(high_v if high_v is not None else 0.0),
                "low": float(low_v if low_v is not None else 0.0),
                "close": float(close_v if close_v is not None else 0.0),
                "volume": float(vol_v if vol_v is not None else 0.0),
            })

    out.sort(key=lambda x: x["t"])
    return out

def parse_indicator_value(construct_result: Any) -> Optional[float]:
    """Извлечение значения индикатора из результата"""
    # для простых индикаторов TAAPI может класть
    # 1) {'value': ...}
    # 2) {'data': [{'value': ...}, ...]}
    # 3) {'data': [{'result': {'value': ...}}]}
    # 4) {'result': {'value': ...}}
    if isinstance(construct_result, dict):
        if "value" in construct_result:
            try:
                return float(construct_result["value"])  # type: ignore[arg-type]
            except Exception:
                return None
        # 4) Nested result.value
        if isinstance(construct_result.get("result"), dict):
            res = construct_result.get("result") or {}
            if "value" in res:
                try:
                    return float(res.get("value"))  # type: ignore[arg-type]
                except Exception:
                    return None
        # 2) and 3) data list
        data = construct_result.get("data")
        if isinstance(data, list) and data:
            last = data[-1]
            if isinstance(last, dict):
                # 2)
                if "value" in last:
                    try:
                        return float(last.get("value"))  # type: ignore[arg-type]
                    except Exception:
                        return None
                # 3)
                if isinstance(last.get("result"), dict) and "value" in last.get("result", {}):
                    try:
                        return float(last.get("result", {}).get("value"))  # type: ignore[arg-type]
                    except Exception:
                        return None
    return None

def parse_indicator_data(construct_result: Any) -> List[Dict[str, Any]]:
    """Извлечение данных индикатора из результата"""
    # Возможные формы:
    # A) {"data": [{"timestamp":..., "value":...}, ...]}
    # B) {"data": [{"result": {"data": [ {...}, ... ] }}]}
    # C) {"result": {"data": [ {...}, ... ] }}
    # D) уже список
    if isinstance(construct_result, dict):
        # C)
        if isinstance(construct_result.get("result"), dict) and isinstance(construct_result.get("result", {}).get("data"), list):
            data_list = construct_result.get("result", {}).get("data")  # type: ignore[assignment]
        else:
            data_list = construct_result.get("data") or construct_result.get("value")
        if isinstance(data_list, list):
            # B) когда каждый элемент имеет result.data
            if data_list and isinstance(data_list[0], dict) and isinstance(data_list[0].get("result"), dict) and isinstance(data_list[0].get("result", {}).get("data"), list):
                data_list = data_list[0].get("result", {}).get("data")  # type: ignore[assignment]
    else:
        data_list = []

    if not isinstance(data_list, list):
        return []

    result: List[Dict[str, Any]] = []
    for item in data_list:
        if isinstance(item, dict):
            if "timestamp" in item:
                ts = item["timestamp"]
                if isinstance(ts, (int, float)):
                    item["timestamp"] = datetime.utcfromtimestamp(
                        ts/1000 if ts > 10**12 else ts
                    ).replace(tzinfo=timezone.utc).isoformat()
            result.append(item)

    return result

class TaapiBulkService:
    """Сервис для работы с Taapi.io bulk API"""
    
    def __init__(self):
        self.base_url = TAAPI_BASE_URL
        self.api_key = TAAPI_KEY
        self.timeout = HTTP_TIMEOUT_SECONDS
        self.max_retries = MAX_RETRIES
    
    async def get_klines_bulk(self, symbols: List[str], intervals: List[str]) -> Dict[str, Dict[str, List[Dict]]]:
        """Получение свечных данных для нескольких символов и таймфреймов"""
        constructs = []
        
        for symbol in symbols:
            for interval in intervals:
                results = RESULTS.get(interval, 1000)
                constructs.append(construct_candles(symbol, interval, results))
        
        if not constructs:
            return {}
        
        # Разбиваем на батчи по 10 запросов (ограничение Taapi.io)
        batch_size = 10
        all_results = {}
        
        for i in range(0, len(constructs), batch_size):
            batch = constructs[i:i + batch_size]
            try:
                response = await taapi_bulk(batch)
                results = response.get("results", [])
                
                # Обрабатываем результаты
                for j, result in enumerate(results):
                    if i + j < len(constructs):
                        construct = constructs[i + j]
                        symbol = construct["symbol"].replace("/", "")
                        interval = construct["interval"]
                        
                        if symbol not in all_results:
                            all_results[symbol] = {}
                        
                        candles = parse_bulk_candles(result)
                        all_results[symbol][interval] = candles
                        
            except Exception as e:
                print(f"Error in batch {i//batch_size + 1}: {e}")
                continue
        
        return all_results
    
    async def get_indicators_bulk(self, symbol: str, interval: str, indicators: List[str], 
                                indicator_params: Dict[str, Dict] = None) -> Dict[str, Any]:
        """Получение нескольких индикаторов для одного символа"""
        constructs = []
        
        for indicator in indicators:
            params = indicator_params.get(indicator, {}) if indicator_params else {}
            constructs.append(construct_indicator(indicator, symbol, interval, params))
        
        if not constructs:
            return {}
        
        try:
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            indicator_results = {}
            for i, result in enumerate(results):
                if i < len(indicators):
                    indicator_name = indicators[i]
                    
                    # Для простых индикаторов (RSI, MACD и т.д.)
                    if indicator_name in ["rsi", "macd", "bbands", "sma", "ema"]:
                        value = parse_indicator_value(result)
                        if value is not None:
                            indicator_results[indicator_name] = value
                        else:
                            # Если не удалось получить простое значение, сохраняем данные
                            data = parse_indicator_data(result)
                            indicator_results[indicator_name] = data
                    else:
                        # Для сложных индикаторов сохраняем все данные
                        data = parse_indicator_data(result)
                        indicator_results[indicator_name] = data
            
            return indicator_results
            
        except Exception as e:
            print(f"Error getting indicators for {symbol} {interval}: {e}")
            return {}
    
    async def get_vwap(self, symbol: str, interval: str, limit: int = 100) -> Optional[float]:
        """Получение VWAP для символа"""
        try:
            # Получаем свечные данные
            constructs = [construct_candles(symbol, interval, limit)]
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                candles = parse_bulk_candles(results[0])
                if candles:
                    return session_vwap(candles)
            
            return None
            
        except Exception as e:
            print(f"Error calculating VWAP for {symbol} {interval}: {e}")
            return None
    
    async def get_pivot_points(self, symbol: str, interval: str, limit: int = 100) -> Optional[Dict[str, float]]:
        """Получение пивотных точек для символа"""
        try:
            # Получаем свечные данные
            constructs = [construct_candles(symbol, interval, limit)]
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                candles = parse_bulk_candles(results[0])
                if len(candles) >= 2:
                    # Берем предыдущую свечу для расчета пивотов
                    prev_candle = candles[-2]
                    prev_high = float(prev_candle['high'])
                    prev_low = float(prev_candle['low'])
                    prev_close = float(prev_candle['close'])
                    
                    return classic_pivots(prev_high, prev_low, prev_close)
            
            return None
            
        except Exception as e:
            print(f"Error calculating pivot points for {symbol} {interval}: {e}")
            return None
    
    async def get_swing_points(self, symbol: str, interval: str, limit: int = 100, 
                             left: int = 2, right: int = 2, tolerance: float = 0.001) -> Optional[Dict[str, Any]]:
        """Получение свинг-точек и кластеризованных уровней"""
        try:
            # Получаем свечные данные
            constructs = [construct_candles(symbol, interval, limit)]
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                candles = parse_bulk_candles(results[0])
                if candles:
                    # Находим свинг-точки
                    highs, lows = swing_points(candles, left, right)
                    
                    # Извлекаем цены для кластеризации
                    high_prices = [h[1] for h in highs]
                    low_prices = [l[1] for l in lows]
                    
                    # Кластеризуем уровни
                    resistance_clusters = cluster_levels(high_prices, tolerance)
                    support_clusters = cluster_levels(low_prices, tolerance)
                    
                    return {
                        "swing_highs": highs,
                        "swing_lows": lows,
                        "resistance_clusters": resistance_clusters,
                        "support_clusters": support_clusters,
                        "parameters": {
                            "left": left,
                            "right": right,
                            "tolerance": tolerance
                        }
                    }
            
            return None
            
        except Exception as e:
            print(f"Error calculating swing points for {symbol} {interval}: {e}")
            return None
    
    async def get_atr(self, symbol: str, interval: str, period: int = 14) -> Optional[float]:
        """Получение ATR (Average True Range) для расчета толерантности"""
        try:
            constructs = [construct_indicator("atr", symbol, interval, {"period": period})]
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                return parse_indicator_value(results[0])
            
            return None
            
        except Exception as e:
            print(f"Error getting ATR for {symbol} {interval}: {e}")
            return None
    
    async def get_ema200(self, symbol: str, interval: str) -> Optional[float]:
        """Получение EMA200 для оценки близости к уровню"""
        try:
            constructs = [construct_indicator("ema", symbol, interval, {"period": 200})]
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                return parse_indicator_value(results[0])
            
            return None
            
        except Exception as e:
            print(f"Error getting EMA200 for {symbol} {interval}: {e}")
            return None
    
    def analyze_level_confluence(self, level_price: float, current_price: float, 
                               vwap: Optional[float], pivot_points: Optional[Dict], 
                               ema200: Optional[float], touches: int = 0) -> Dict[str, Any]:
        """Анализ конвергенции факторов для уровня"""
        confluence = []
        
        # Проверяем близость к VWAP
        if vwap and abs(level_price - vwap) / vwap < 0.01:  # В пределах 1% от VWAP
            confluence.append("vwap")
        
        # Проверяем близость к пивотным точкам
        if pivot_points:
            for key, value in pivot_points.items():
                if abs(level_price - value) / value < 0.005:  # В пределах 0.5% от пивотов
                    confluence.append("pivot")
                    break
        
        # Проверяем близость к EMA200
        if ema200 and abs(level_price - ema200) / ema200 < 0.02:  # В пределах 2% от EMA200
            confluence.append("ema200_near")
        
        # Проверяем круглые числа
        if level_price > 1:
            # Для цен > 1 проверяем круглые числа
            rounded = round(level_price, -int(math.log10(level_price)) + 1)
            if abs(level_price - rounded) / level_price < 0.01:
                confluence.append("round")
        else:
            # Для цен < 1 проверяем круглые числа с точностью до 2 знаков
            rounded = round(level_price, 2)
            if abs(level_price - rounded) / level_price < 0.01:
                confluence.append("round")
        
        # Добавляем количество касаний
        if touches > 0:
            confluence.append("touches")
        
        # Проверяем соответствие тренду (простая эвристика)
        if level_price < current_price:
            confluence.append("trend_ok")  # Уровень поддержки ниже текущей цены
        
        return {
            "confluence": confluence,
            "confluence_count": len(confluence),
            "factors": {
                "vwap_near": vwap and abs(level_price - vwap) / vwap < 0.01,
                "pivot_near": pivot_points and any(abs(level_price - v) / v < 0.005 for v in pivot_points.values()),
                "ema200_near": ema200 and abs(level_price - ema200) / ema200 < 0.02,
                "round_number": "round" in confluence,
                "touches": touches,
                "trend_ok": "trend_ok" in confluence
            }
        }
    
    async def get_support_resistance_bulk(self, symbols: List[str], intervals: List[str]) -> Dict[str, Dict]:
        """Получение уровней поддержки и сопротивления для нескольких символов"""
        # Сначала получаем свечные данные
        klines_data = await self.get_klines_bulk(symbols, intervals)
        
        results = {}
        for symbol, interval_data in klines_data.items():
            results[symbol] = {}
            
            for interval, candles in interval_data.items():
                if not candles:
                    continue
                
                # Простой алгоритм поиска уровней
                highs = [float(c['high']) for c in candles]
                lows = [float(c['low']) for c in candles]
                
                resistance_levels = self._find_peaks(highs, window=5)
                support_levels = self._find_peaks(lows, window=5, find_min=True)
                
                # Рассчитываем VWAP
                vwap = session_vwap(candles)
                
                # Рассчитываем пивотные точки
                pivot_points = None
                if len(candles) >= 2:
                    prev_candle = candles[-2]
                    prev_high = float(prev_candle['high'])
                    prev_low = float(prev_candle['low'])
                    prev_close = float(prev_candle['close'])
                    pivot_points = classic_pivots(prev_high, prev_low, prev_close)
                
                # Рассчитываем свинг-точки
                swing_data = None
                if len(candles) >= 5:  # Минимум для свинг-точек
                    highs_swing, lows_swing = swing_points(candles, left=2, right=2)
                    high_prices = [h[1] for h in highs_swing]
                    low_prices = [l[1] for l in lows_swing]
                    
                    # Кластеризуем с толерантностью 0.1%
                    tolerance = float(candles[-1]['close']) * 0.001
                    resistance_clusters = cluster_levels(high_prices, tolerance)
                    support_clusters = cluster_levels(low_prices, tolerance)
                    
                    swing_data = {
                        "swing_highs": highs_swing,
                        "swing_lows": lows_swing,
                        "resistance_clusters": resistance_clusters,
                        "support_clusters": support_clusters
                    }
                
                # Получаем ATR и EMA200 для анализа конвергенции
                atr = await self.get_atr(symbol, interval)
                ema200 = await self.get_ema200(symbol, interval)
                current_price = float(candles[-1]['close'])
                
                # Анализируем конвергенцию для каждого уровня
                enhanced_resistance_levels = []
                for level in resistance_levels:
                    confluence_analysis = self.analyze_level_confluence(
                        level['price'], current_price, vwap, pivot_points, ema200, 
                        int(level.get('strength', 1))
                    )
                    
                    # Рассчитываем толерантность
                    tick_size = current_price * 0.0001  # Примерный размер тика
                    tolerance = compute_tolerance(level['price'], atr, tick_size)
                    
                    # Оцениваем уровень
                    score = score_level(level['price'], confluence_analysis['confluence'], None, False)
                    
                    enhanced_resistance_levels.append({
                        **level,
                        "confluence": confluence_analysis,
                        "tolerance": tolerance,
                        "score": score
                    })
                
                enhanced_support_levels = []
                for level in support_levels:
                    confluence_analysis = self.analyze_level_confluence(
                        level['price'], current_price, vwap, pivot_points, ema200, 
                        int(level.get('strength', 1))
                    )
                    
                    # Рассчитываем толерантность
                    tick_size = current_price * 0.0001  # Примерный размер тика
                    tolerance = compute_tolerance(level['price'], atr, tick_size)
                    
                    # Оцениваем уровень
                    score = score_level(level['price'], confluence_analysis['confluence'], None, False)
                    
                    enhanced_support_levels.append({
                        **level,
                        "confluence": confluence_analysis,
                        "tolerance": tolerance,
                        "score": score
                    })
                
                results[symbol][interval] = {
                    'support_levels': enhanced_support_levels,
                    'resistance_levels': enhanced_resistance_levels,
                    'current_price': current_price,
                    'vwap': vwap,
                    'pivot_points': pivot_points,
                    'swing_points': swing_data,
                    'atr': atr,
                    'ema200': ema200,
                    'timestamp': datetime.now().isoformat()
                }
        
        return results
    
    def _find_peaks(self, data: List[float], window: int = 5, find_min: bool = False) -> List[Dict]:
        """Поиск пиков в данных"""
        peaks = []
        
        for i in range(window, len(data) - window):
            if find_min:
                # Поиск минимумов (поддержка)
                if all(data[i] <= data[j] for j in range(i - window, i + window + 1) if j != i):
                    peaks.append({
                        'price': data[i],
                        'index': i,
                        'strength': self._calculate_level_strength(data, i, window)
                    })
            else:
                # Поиск максимумов (сопротивление)
                if all(data[i] >= data[j] for j in range(i - window, i + window + 1) if j != i):
                    peaks.append({
                        'price': data[i],
                        'index': i,
                        'strength': self._calculate_level_strength(data, i, window)
                    })
        
        # Сортируем по силе уровня
        peaks.sort(key=lambda x: x['strength'], reverse=True)
        
        return peaks[:5]  # Возвращаем топ-5 уровней
    
    def _calculate_level_strength(self, data: List[float], index: int, window: int) -> float:
        """Расчет силы уровня"""
        # Простой алгоритм: чем больше касаний уровня, тем он сильнее
        level_price = data[index]
        tolerance = level_price * 0.001  # 0.1% толерантность
        
        touches = 0
        for i in range(len(data)):
            if abs(data[i] - level_price) <= tolerance:
                touches += 1
        
        return touches 