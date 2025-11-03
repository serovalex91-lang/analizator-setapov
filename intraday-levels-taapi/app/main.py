from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from typing import List, Dict, Optional
from datetime import datetime
from app.services.taapi_service import TaapiService
from app.services.level_finder import find_levels_for_side
from app.config import SYMBOLS, TF_LIST
from app.models import (
    LevelSearchRequest, 
    BallFlip, 
    APIResponse,
    SupportResistanceResponse,
    CacheStats
)

app = FastAPI(
    title="Intraday Levels Taapi.io API",
    description="API для анализа внутридневных уровней с использованием Taapi.io",
    version="1.0.0"
)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация сервиса Taapi.io
taapi_service = TaapiService()

@app.get("/")
async def root():
    """Корневой эндпоинт"""
    return APIResponse(
        success=True,
        data={
            "message": "Intraday Levels Taapi.io API",
            "version": "1.0.0",
            "status": "running",
            "taapi_key_configured": bool(taapi_service.api_key)
        }
    )

@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return APIResponse(
        success=True,
        data={
            "status": "healthy",
            "taapi_key": "configured" if taapi_service.api_key else "missing",
            "symbols": SYMBOLS,
            "timeframes": TF_LIST
        }
    )

@app.get("/config")
async def get_config():
    """Получение текущей конфигурации"""
    return APIResponse(
        success=True,
        data={
            "symbols": SYMBOLS,
            "timeframes": TF_LIST,
            "cache_ttl": taapi_service.cache_ttl,
            "http_timeout": taapi_service.timeout,
            "max_retries": taapi_service.max_retries
        }
    )

@app.get("/symbols")
async def get_symbols():
    """Получение списка символов"""
    return APIResponse(
        success=True,
        data={"symbols": SYMBOLS}
    )

@app.get("/timeframes")
async def get_timeframes():
    """Получение списка таймфреймов"""
    return APIResponse(
        success=True,
        data={"timeframes": TF_LIST}
    )

@app.get("/klines/{symbol}/{interval}")
async def get_klines(symbol: str, interval: str, limit: int = 100):
    """Получение свечных данных"""
    try:
        data = await taapi_service.get_klines(symbol, interval, limit)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "data": data,
                "count": len(data)
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/rsi/{symbol}/{interval}")
async def get_rsi(symbol: str, interval: str, period: int = 14):
    """Получение RSI индикатора"""
    try:
        data = await taapi_service.get_rsi(symbol, interval, period)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "period": period,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/macd/{symbol}/{interval}")
async def get_macd(symbol: str, interval: str, 
                  fast_period: int = 12, slow_period: int = 26, signal_period: int = 9):
    """Получение MACD индикатора"""
    try:
        data = await taapi_service.get_macd(symbol, interval, fast_period, slow_period, signal_period)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "fast_period": fast_period,
                "slow_period": slow_period,
                "signal_period": signal_period,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/bbands/{symbol}/{interval}")
async def get_bollinger_bands(symbol: str, interval: str, period: int = 20, std_dev: float = 2):
    """Получение Bollinger Bands"""
    try:
        data = await taapi_service.get_bollinger_bands(symbol, interval, period, std_dev)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "period": period,
                "std_dev": std_dev,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/vwap/{symbol}/{interval}")
async def get_vwap(symbol: str, interval: str, limit: int = 100):
    """Получение VWAP (Volume Weighted Average Price)"""
    try:
        data = await taapi_service.get_vwap(symbol, interval, limit)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/pivots/{symbol}/{interval}")
async def get_pivot_points(symbol: str, interval: str, limit: int = 100):
    """Получение пивотных точек"""
    try:
        data = await taapi_service.get_pivot_points(symbol, interval, limit)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/swings/{symbol}/{interval}")
async def get_swing_points(symbol: str, interval: str, limit: int = 100, 
                          left: int = 2, right: int = 2, tolerance: float = 0.001):
    """Получение свинг-точек и кластеризованных уровней"""
    try:
        data = await taapi_service.get_swing_points(symbol, interval, limit, left, right, tolerance)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "left": left,
                "right": right,
                "tolerance": tolerance,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/atr/{symbol}/{interval}")
async def get_atr(symbol: str, interval: str, period: int = 14):
    """Получение ATR (Average True Range)"""
    try:
        data = await taapi_service.get_atr(symbol, interval, period)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "period": period,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/ema200/{symbol}/{interval}")
async def get_ema200(symbol: str, interval: str):
    """Получение EMA200"""
    try:
        data = await taapi_service.get_ema200(symbol, interval)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/levels/{symbol}/{interval}")
async def get_support_resistance(symbol: str, interval: str, limit: int = 100):
    """Получение уровней поддержки и сопротивления"""
    try:
        data = await taapi_service.get_support_resistance(symbol, interval, limit)
        return APIResponse(
            success=True,
            data=data
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/best-level/{symbol}/{side}")
async def get_best_level(symbol: str, side: str, limit: int = 200):
    """Поиск лучшего уровня для входа (long/short)"""
    try:
        if side not in ["long", "short"]:
            return APIResponse(
                success=False,
                error="Side must be 'long' or 'short'"
            )
        
        data = await find_levels_for_side(symbol, side, taapi_service, limit)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "side": side,
                "limit": limit,
                "best_level": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.post("/levels/search")
async def search_levels(request: LevelSearchRequest):
    """Поиск уровней на основе контекста (long/short)"""
    try:
        # Определяем тип уровней на основе контекста
        if request.context == "long":
            # Для лонга ищем уровни поддержки
            data = await taapi_service.get_support_resistance(request.symbol, request.origin_tf, 100)
            return APIResponse(
                success=True,
                data={
                    "context": request.context,
                    "symbol": request.symbol,
                    "timeframe": request.origin_tf,
                    "support_levels": data.get("support_levels", []),
                    "current_price": data.get("current_price"),
                    "vwap": data.get("vwap"),
                    "pivot_points": data.get("pivot_points"),
                    "swing_points": data.get("swing_points"),
                    "atr": data.get("atr"),
                    "ema200": data.get("ema200")
                }
            )
        else:
            # Для шорта ищем уровни сопротивления
            data = await taapi_service.get_support_resistance(request.symbol, request.origin_tf, 100)
            return APIResponse(
                success=True,
                data={
                    "context": request.context,
                    "symbol": request.symbol,
                    "timeframe": request.origin_tf,
                    "resistance_levels": data.get("resistance_levels", []),
                    "current_price": data.get("current_price"),
                    "vwap": data.get("vwap"),
                    "pivot_points": data.get("pivot_points"),
                    "swing_points": data.get("swing_points"),
                    "atr": data.get("atr"),
                    "ema200": data.get("ema200")
                }
            )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.post("/ball-flip")
async def record_ball_flip(ball_flip: BallFlip):
    """Запись изменения цвета шариков"""
    try:
        # Здесь можно добавить логику для обработки изменения шариков
        # Например, сохранение в базу данных или отправка уведомлений
        
        return APIResponse(
            success=True,
            data={
                "message": "Ball flip recorded",
                "ball_flip": ball_flip.dict()
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

# Новые bulk эндпоинты
@app.get("/bulk/klines")
async def get_klines_bulk(symbols: Optional[str] = None, intervals: Optional[str] = None):
    """Получение свечных данных для нескольких символов и таймфреймов"""
    try:
        symbol_list = symbols.split(",") if symbols else None
        interval_list = intervals.split(",") if intervals else None
        
        data = await taapi_service.get_klines_bulk(symbol_list, interval_list)
        return APIResponse(
            success=True,
            data={
                "symbols": symbol_list or SYMBOLS,
                "intervals": interval_list or TF_LIST,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/bulk/indicators/{symbol}/{interval}")
async def get_indicators_bulk(symbol: str, interval: str, indicators: str):
    """Получение нескольких индикаторов для одного символа"""
    try:
        indicator_list = indicators.split(",")
        data = await taapi_service.get_indicators_bulk(symbol, interval, indicator_list)
        return APIResponse(
            success=True,
            data={
                "symbol": symbol,
                "interval": interval,
                "indicators": indicator_list,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/bulk/levels")
async def get_support_resistance_bulk(symbols: Optional[str] = None, intervals: Optional[str] = None):
    """Получение уровней поддержки и сопротивления для нескольких символов"""
    try:
        symbol_list = symbols.split(",") if symbols else None
        interval_list = intervals.split(",") if intervals else None
        
        data = await taapi_service.get_support_resistance_bulk(symbol_list, interval_list)
        return APIResponse(
            success=True,
            data={
                "symbols": symbol_list or SYMBOLS,
                "intervals": interval_list or TF_LIST,
                "data": data
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.post("/cache/clear")
async def clear_cache():
    """Очистка кэша"""
    try:
        taapi_service.clear_cache()
        return APIResponse(
            success=True,
            data={"message": "Cache cleared successfully"}
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/cache/stats")
async def get_cache_stats():
    """Получение статистики кэша"""
    try:
        stats = taapi_service.get_cache_stats()
        return APIResponse(
            success=True,
            data=stats
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.delete("/cache/{key}")
async def delete_cache_key(key: str):
    """Удаление конкретного ключа из кэша"""
    try:
        success = taapi_service.delete_cache_key(key)
        if success:
            return APIResponse(
                success=True,
                data={"message": f"Cache key '{key}' deleted successfully"}
            )
        else:
            return APIResponse(
                success=False,
                error=f"Cache key '{key}' not found"
            )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.put("/cache/ttl")
async def update_cache_ttl(new_ttl: int):
    """Обновление TTL кэша"""
    try:
        if new_ttl <= 0:
            return APIResponse(
                success=False,
                error="TTL must be greater than 0"
            )
        
        taapi_service.update_cache_ttl(new_ttl)
        return APIResponse(
            success=True,
            data={
                "message": f"Cache TTL updated to {new_ttl} seconds",
                "new_ttl": new_ttl
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

@app.get("/cache/keys")
async def get_cache_keys():
    """Получение всех ключей кэша"""
    try:
        stats = taapi_service.get_cache_stats()
        return APIResponse(
            success=True,
            data={
                "keys": stats.get("keys", []),
                "count": len(stats.get("keys", []))
            }
        )
    except Exception as e:
        return APIResponse(
            success=False,
            error=str(e)
        )

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    ) 