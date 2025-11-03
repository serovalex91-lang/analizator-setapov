import os
import json
import asyncio
import math
import httpx
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone
from app.config import TAAPI_KEY, HTTP_TIMEOUT_SECONDS, MAX_RETRIES, TAAPI_BASE_URL

def to_ta_symbol(sym: str) -> str:
    """Конвертирует символ в формат Taapi.io"""
    return sym.replace("USDT", "").replace("BTC", "")

def construct_candles(symbol: str, interval: str, limit: int) -> Dict[str, Any]:
    """Создает конструкт для запроса свечных данных"""
    return {
        "exchange": "binance",
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

def construct_indicator(indicator: str, symbol: str, interval: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Создает конструкт для запроса индикатора"""
    return {
        "exchange": "binance",
        "symbol": symbol,
        "interval": interval,
        "indicator": indicator,
        "parameters": parameters
    }

async def taapi_bulk(constructs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Выполняет bulk запрос к Taapi.io API с retry логикой"""
    url = f"{TAAPI_BASE_URL}/bulk"
    
    payload = {
        "secret": TAAPI_KEY,
        "constructs": constructs
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                
                data = response.json()
                
                # Проверяем на rate limit
                if "error" in data and "rate limit" in data["error"].lower():
                    if attempt < MAX_RETRIES - 1:
                        wait_time = 2 ** attempt  # Экспоненциальная задержка
                        print(f"Rate limit hit, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                
                return data
                
        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** attempt
                print(f"Timeout, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            else:
                raise Exception(f"Timeout after {MAX_RETRIES} attempts")
                
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** attempt
                print(f"Error: {e}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            else:
                raise Exception(f"Failed after {MAX_RETRIES} attempts: {e}")
    
    raise Exception("Max retries exceeded")

def parse_bulk_candles(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Парсит свечные данные из bulk ответа"""
    if not data or "result" not in data:
        return []
    
    result = data["result"]
    if not isinstance(result, list):
        return []
    
    candles = []
    for candle in result:
        if isinstance(candle, dict) and all(k in candle for k in ["t", "o", "h", "l", "c", "v"]):
            candles.append({
                "t": candle["t"],
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
                "v": float(candle["v"])
            })
    
    return candles

def parse_indicator_value(data: Dict[str, Any]) -> Optional[float]:
    """Парсит значение индикатора из bulk ответа"""
    if not data or "result" not in data:
        return None
    
    result = data["result"]
    if isinstance(result, (int, float)):
        return float(result)
    elif isinstance(result, list) and len(result) > 0:
        last_value = result[-1]
        if isinstance(last_value, (int, float)):
            return float(last_value)
        elif isinstance(last_value, dict) and "value" in last_value:
            return float(last_value["value"])
    
    return None

def parse_indicator_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Парсит данные индикатора из bulk ответа"""
    if not data or "result" not in data:
        return {}
    
    result = data["result"]
    if isinstance(result, dict):
        return result
    elif isinstance(result, list) and len(result) > 0:
        return result[-1] if isinstance(result[-1], dict) else {}
    
    return {} 