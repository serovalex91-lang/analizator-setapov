import httpx
import asyncio
import time
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
import logging
from app.config import (
    TAAPI_KEY, 
    HTTP_TIMEOUT_SECONDS, 
    MAX_RETRIES, 
    CACHE_TTL_SECONDS,
    TAAPI_BASE_URL,
    SYMBOLS,
    TF_LIST,
    RESULTS
)
from .cache import TTLCache
from .taapi_bulk import TaapiBulkService, taapi_bulk, construct_candles, construct_indicator, parse_bulk_candles, parse_indicator_value, session_vwap, classic_pivots, swing_points, cluster_levels, compute_tolerance, score_level

logger = logging.getLogger(__name__)

class TaapiService:
    """Сервис для работы с Taapi.io API"""
    
    def __init__(self):
        self.api_key = TAAPI_KEY
        self.base_url = TAAPI_BASE_URL
        self.timeout = HTTP_TIMEOUT_SECONDS
        self.max_retries = MAX_RETRIES
        self.cache_ttl = CACHE_TTL_SECONDS
        
        # Используем новый TTLCache
        self._cache = TTLCache(ttl_seconds=self.cache_ttl)
        
        # Инициализируем bulk сервис
        self.bulk_service = TaapiBulkService()
        
        if not self.api_key:
            raise ValueError("TAAPI_KEY не настроен в переменных окружения")
    
    async def _make_request(self, endpoint: str, params: Dict[str, Any]) -> Dict:
        """Выполнение запроса к Taapi.io API (legacy метод)"""
        url = f"{self.base_url}/{endpoint}"
        
        # Добавляем API ключ
        params['secret'] = self.api_key
        
        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, params=params)
                    
                    if response.status_code == 200:
                        data = response.json()
                        return data
                    elif response.status_code == 429:
                        # Rate limit - ждем и повторяем
                        wait_time = 2 ** attempt
                        logger.warning(f"Rate limit hit, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        error_text = response.text
                        logger.error(f"API error {response.status_code}: {error_text}")
                        raise Exception(f"API error {response.status_code}: {error_text}")
                        
            except httpx.TimeoutException:
                logger.warning(f"Timeout on attempt {attempt + 1}")
                if attempt == self.max_retries - 1:
                    raise Exception("Request timeout")
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Request error: {e}")
                if attempt == self.max_retries - 1:
                    raise Exception(str(e))
                await asyncio.sleep(1)
        
        raise Exception("Max retries exceeded")
    
    def _get_cache_key(self, symbol: str, interval: str, indicator: str, **kwargs) -> str:
        """Генерация ключа кэша"""
        params_str = "_".join([f"{k}_{v}" for k, v in sorted(kwargs.items())])
        return f"{symbol}_{interval}_{indicator}_{params_str}"
    
    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List[Dict]:
        """Получение свечных данных (использует bulk API)"""
        cache_key = self._get_cache_key(symbol, interval, "klines", limit=limit)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached klines for {symbol} {interval}")
            return cached_data
        
        # Используем bulk API
        constructs = [construct_candles(symbol, interval, limit)]
        
        try:
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                data = parse_bulk_candles(results[0])
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return []
                
        except Exception as e:
            logger.error(f"Error getting klines for {symbol} {interval}: {e}")
            return []
    
    async def get_rsi(self, symbol: str, interval: str, period: int = 14) -> Dict:
        """Получение RSI (использует bulk API)"""
        cache_key = self._get_cache_key(symbol, interval, "rsi", period=period)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached RSI for {symbol} {interval}")
            return cached_data
        
        # Используем bulk API
        constructs = [construct_indicator("rsi", symbol, interval, {"period": period})]
        
        try:
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                value = parse_indicator_value(results[0])
                data = {
                    "value": value,
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"value": None, "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error getting RSI for {symbol} {interval}: {e}")
            return {"value": None, "timestamp": datetime.now().isoformat()}
    
    async def get_macd(self, symbol: str, interval: str, 
                      fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Dict:
        """Получение MACD (использует bulk API)"""
        cache_key = self._get_cache_key(symbol, interval, "macd", 
                                       fast=fast_period, slow=slow_period, signal=signal_period)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached MACD for {symbol} {interval}")
            return cached_data
        
        # Используем bulk API
        constructs = [construct_indicator("macd", symbol, interval, {
            "fast": fast_period,
            "slow": slow_period,
            "signal": signal_period
        })]
        
        try:
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                result = results[0]
                data = {
                    "macd": parse_indicator_value(result),
                    "signal": None,  # MACD возвращает только одно значение
                    "histogram": None,
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"macd": None, "signal": None, "histogram": None, "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error getting MACD for {symbol} {interval}: {e}")
            return {"macd": None, "signal": None, "histogram": None, "timestamp": datetime.now().isoformat()}
    
    async def get_bollinger_bands(self, symbol: str, interval: str, period: int = 20, std_dev: float = 2) -> Dict:
        """Получение Bollinger Bands (использует bulk API)"""
        cache_key = self._get_cache_key(symbol, interval, "bbands", period=period, std=std_dev)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached Bollinger Bands for {symbol} {interval}")
            return cached_data
        
        # Используем bulk API
        constructs = [construct_indicator("bbands", symbol, interval, {
            "period": period,
            "std": std_dev
        })]
        
        try:
            response = await taapi_bulk(constructs)
            results = response.get("results", [])
            
            if results:
                result = results[0]
                data = {
                    "upper": parse_indicator_value(result),
                    "middle": None,  # BB возвращает только одно значение
                    "lower": None,
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"upper": None, "middle": None, "lower": None, "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error getting Bollinger Bands for {symbol} {interval}: {e}")
            return {"upper": None, "middle": None, "lower": None, "timestamp": datetime.now().isoformat()}
    
    async def get_vwap(self, symbol: str, interval: str, limit: int = 100) -> Dict:
        """Получение VWAP (Volume Weighted Average Price)"""
        cache_key = self._get_cache_key(symbol, interval, "vwap", limit=limit)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached VWAP for {symbol} {interval}")
            return cached_data
        
        try:
            # Получаем свечные данные
            klines = await self.get_klines(symbol, interval, limit)
            
            if klines:
                vwap_value = session_vwap(klines)
                data = {
                    "value": vwap_value,
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"value": None, "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error calculating VWAP for {symbol} {interval}: {e}")
            return {"value": None, "timestamp": datetime.now().isoformat()}
    
    async def get_pivot_points(self, symbol: str, interval: str, limit: int = 100) -> Dict:
        """Получение пивотных точек"""
        cache_key = self._get_cache_key(symbol, interval, "pivot_points", limit=limit)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached pivot points for {symbol} {interval}")
            return cached_data
        
        try:
            # Получаем свечные данные
            klines = await self.get_klines(symbol, interval, limit)
            
            if len(klines) >= 2:
                # Берем предыдущую свечу для расчета пивотов
                prev_candle = klines[-2]
                prev_high = float(prev_candle['high'])
                prev_low = float(prev_candle['low'])
                prev_close = float(prev_candle['close'])
                
                pivot_data = classic_pivots(prev_high, prev_low, prev_close)
                data = {
                    "pivot_points": pivot_data,
                    "prev_high": prev_high,
                    "prev_low": prev_low,
                    "prev_close": prev_close,
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"pivot_points": None, "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error calculating pivot points for {symbol} {interval}: {e}")
            return {"pivot_points": None, "timestamp": datetime.now().isoformat()}
    
    async def get_swing_points(self, symbol: str, interval: str, limit: int = 100, 
                             left: int = 2, right: int = 2, tolerance: float = 0.001) -> Dict:
        """Получение свинг-точек и кластеризованных уровней"""
        cache_key = self._get_cache_key(symbol, interval, "swing_points", 
                                       limit=limit, left=left, right=right, tolerance=tolerance)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached swing points for {symbol} {interval}")
            return cached_data
        
        try:
            # Получаем свечные данные
            klines = await self.get_klines(symbol, interval, limit)
            
            if len(klines) >= 5:  # Минимум для свинг-точек
                # Находим свинг-точки
                highs, lows = swing_points(klines, left, right)
                
                # Извлекаем цены для кластеризации
                high_prices = [h[1] for h in highs]
                low_prices = [l[1] for l in lows]
                
                # Кластеризуем уровни
                resistance_clusters = cluster_levels(high_prices, tolerance)
                support_clusters = cluster_levels(low_prices, tolerance)
                
                data = {
                    "swing_highs": highs,
                    "swing_lows": lows,
                    "resistance_clusters": resistance_clusters,
                    "support_clusters": support_clusters,
                    "parameters": {
                        "left": left,
                        "right": right,
                        "tolerance": tolerance
                    },
                    "timestamp": datetime.now().isoformat()
                }
                # Кэшируем результат
                self._cache.set(cache_key, data)
                return data
            else:
                return {"swing_highs": [], "swing_lows": [], "timestamp": datetime.now().isoformat()}
                
        except Exception as e:
            logger.error(f"Error calculating swing points for {symbol} {interval}: {e}")
            return {"swing_highs": [], "swing_lows": [], "timestamp": datetime.now().isoformat()}
    
    async def get_atr(self, symbol: str, interval: str, period: int = 14) -> Dict:
        """Получение ATR (Average True Range)"""
        cache_key = self._get_cache_key(symbol, interval, "atr", period=period)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached ATR for {symbol} {interval}")
            return cached_data
        
        try:
            value = await self.bulk_service.get_atr(symbol, interval, period)
            data = {
                "value": value,
                "timestamp": datetime.now().isoformat()
            }
            # Кэшируем результат
            self._cache.set(cache_key, data)
            return data
        except Exception as e:
            logger.error(f"Error getting ATR for {symbol} {interval}: {e}")
            return {"value": None, "timestamp": datetime.now().isoformat()}
    
    async def get_ema200(self, symbol: str, interval: str) -> Dict:
        """Получение EMA200"""
        cache_key = self._get_cache_key(symbol, interval, "ema200")
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached EMA200 for {symbol} {interval}")
            return cached_data
        
        try:
            value = await self.bulk_service.get_ema200(symbol, interval)
            data = {
                "value": value,
                "timestamp": datetime.now().isoformat()
            }
            # Кэшируем результат
            self._cache.set(cache_key, data)
            return data
        except Exception as e:
            logger.error(f"Error getting EMA200 for {symbol} {interval}: {e}")
            return {"value": None, "timestamp": datetime.now().isoformat()}
    
    async def get_support_resistance(self, symbol: str, interval: str, limit: int = 100) -> Dict:
        """Получение уровней поддержки и сопротивления"""
        cache_key = self._get_cache_key(symbol, interval, "support_resistance", limit=limit)
        
        # Проверяем кэш
        cached_data = self._cache.get(cache_key)
        if cached_data:
            logger.info(f"Using cached S/R levels for {symbol} {interval}")
            return cached_data
        
        # Получаем свечные данные
        klines = await self.get_klines(symbol, interval, limit)
        
        if not klines:
            return {
                'symbol': symbol,
                'interval': interval,
                'support_levels': [],
                'resistance_levels': [],
                'current_price': None,
                'vwap': None,
                'pivot_points': None,
                'swing_points': None,
                'atr': None,
                'ema200': None,
                'timestamp': datetime.now().isoformat()
            }
        
        # Простой алгоритм поиска уровней поддержки и сопротивления
        highs = [float(k['high']) for k in klines]
        lows = [float(k['low']) for k in klines]
        
        # Находим локальные максимумы и минимумы
        resistance_levels = self._find_peaks(highs, window=5)
        support_levels = self._find_peaks(lows, window=5, find_min=True)
        
        # Рассчитываем VWAP
        vwap = session_vwap(klines)
        
        # Рассчитываем пивотные точки
        pivot_points = None
        if len(klines) >= 2:
            prev_candle = klines[-2]
            prev_high = float(prev_candle['high'])
            prev_low = float(prev_candle['low'])
            prev_close = float(prev_candle['close'])
            pivot_points = classic_pivots(prev_high, prev_low, prev_close)
        
        # Рассчитываем свинг-точки
        swing_data = None
        if len(klines) >= 5:  # Минимум для свинг-точек
            highs_swing, lows_swing = swing_points(klines, left=2, right=2)
            high_prices = [h[1] for h in highs_swing]
            low_prices = [l[1] for l in lows_swing]
            
            # Кластеризуем с толерантностью 0.1%
            tolerance = float(klines[-1]['close']) * 0.001
            resistance_clusters = cluster_levels(high_prices, tolerance)
            support_clusters = cluster_levels(low_prices, tolerance)
            
            swing_data = {
                "swing_highs": highs_swing,
                "swing_lows": lows_swing,
                "resistance_clusters": resistance_clusters,
                "support_clusters": support_clusters
            }
        
        # Получаем ATR и EMA200 для анализа конвергенции
        atr_data = await self.get_atr(symbol, interval)
        ema200_data = await self.get_ema200(symbol, interval)
        atr = atr_data.get("value") if atr_data else None
        ema200 = ema200_data.get("value") if ema200_data else None
        current_price = float(klines[-1]['close'])
        
        # Анализируем конвергенцию для каждого уровня
        enhanced_resistance_levels = []
        for level in resistance_levels:
            confluence_analysis = self.bulk_service.analyze_level_confluence(
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
            confluence_analysis = self.bulk_service.analyze_level_confluence(
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
        
        result = {
            'symbol': symbol,
            'interval': interval,
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
        
        # Кэшируем результат
        self._cache.set(cache_key, result)
        
        return result
    
    async def get_klines_bulk(self, symbols: List[str] = None, intervals: List[str] = None) -> Dict[str, Dict[str, List[Dict]]]:
        """Получение свечных данных для нескольких символов и таймфреймов"""
        symbols = symbols or SYMBOLS
        intervals = intervals or TF_LIST
        
        return await self.bulk_service.get_klines_bulk(symbols, intervals)
    
    async def get_indicators_bulk(self, symbol: str, interval: str, indicators: List[str], 
                                indicator_params: Dict[str, Dict] = None) -> Dict[str, Any]:
        """Получение нескольких индикаторов для одного символа"""
        return await self.bulk_service.get_indicators_bulk(symbol, interval, indicators, indicator_params)
    
    async def get_support_resistance_bulk(self, symbols: List[str] = None, intervals: List[str] = None) -> Dict[str, Dict]:
        """Получение уровней поддержки и сопротивления для нескольких символов"""
        symbols = symbols or SYMBOLS
        intervals = intervals or TF_LIST
        
        return await self.bulk_service.get_support_resistance_bulk(symbols, intervals)
    
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
    
    def clear_cache(self):
        """Очистка кэша"""
        self._cache.clear()
    
    def get_cache_stats(self) -> Dict:
        """Получение статистики кэша"""
        return self._cache.get_stats()
    
    def delete_cache_key(self, key: str) -> bool:
        """Удаление конкретного ключа из кэша"""
        return self._cache.delete(key)
    
    def update_cache_ttl(self, new_ttl: int):
        """Обновление TTL кэша"""
        self._cache.update_ttl(new_ttl)
        self.cache_ttl = new_ttl 