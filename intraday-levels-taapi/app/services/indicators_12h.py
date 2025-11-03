"""
Сервис для получения RSI и EMA200 на 12h timeframe
с поддержкой Binance API (основной) и CoinGecko API (резервный)
"""
import asyncio
import httpx
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class Indicators12hService:
    def __init__(self):
        self.binance_base_url = "https://api.binance.com/api/v3"
        self.coingecko_base_url = "https://api.coingecko.com/api/v3"
        self.timeout = 10.0
        
    async def get_rsi_ema200_12h(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Получить RSI и EMA200 на 12h timeframe
        Возвращает (rsi_12h, ema200_12h) или (None, None) при ошибке
        """
        # Попытка 1: Binance API
        try:
            rsi, ema = await self._get_from_binance(symbol)
            if rsi is not None and ema is not None:
                logger.info(f"Got 12h indicators from Binance for {symbol}: RSI={rsi:.2f}, EMA200={ema:.6f}")
                return rsi, ema
        except Exception as e:
            logger.warning(f"Binance API failed for {symbol}: {e}")
        
        # Попытка 2: CoinGecko API
        try:
            rsi, ema = await self._get_from_coingecko(symbol)
            if rsi is not None and ema is not None:
                logger.info(f"Got 12h indicators from CoinGecko for {symbol}: RSI={rsi:.2f}, EMA200={ema:.6f}")
                return rsi, ema
        except Exception as e:
            logger.warning(f"CoinGecko API failed for {symbol}: {e}")
        
        logger.error(f"All sources failed for {symbol} 12h indicators")
        return None, None
    
    async def _get_from_binance(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Получить данные с Binance API"""
        # Конвертируем USDT символ для Binance
        binance_symbol = symbol.replace("USDT", "USDT")
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Получаем 12h свечи (максимум 1000 свечей = ~500 дней)
            url = f"{self.binance_base_url}/klines"
            params = {
                "symbol": binance_symbol,
                "interval": "12h",
                "limit": 1000
            }
            
            response = await client.get(url, params=params)
            response.raise_for_status()
            klines = response.json()
            
            if not klines:
                raise ValueError("No klines data from Binance")
            
            # Конвертируем в DataFrame
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            
            # Конвертируем типы данных
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            df = df.dropna()
            
            if len(df) < 200:  # Нужно минимум 200 свечей для EMA200
                raise ValueError(f"Insufficient data: {len(df)} candles")
            
            # Вычисляем RSI(14) на 12h
            rsi = self._calculate_rsi(df['close'], 14)
            
            # Вычисляем EMA(200) на 12h
            ema200 = self._calculate_ema(df['close'], 200)
            
            return rsi, ema200
    
    async def _get_from_coingecko(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Получить данные с CoinGecko API"""
        # Конвертируем символ для CoinGecko
        coingecko_id = self._symbol_to_coingecko_id(symbol)
        if not coingecko_id:
            raise ValueError(f"Unknown symbol for CoinGecko: {symbol}")
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Получаем исторические данные (максимум 365 дней)
            url = f"{self.coingecko_base_url}/coins/{coingecko_id}/market_chart"
            params = {
                "vs_currency": "usd",
                "days": "365",
                "interval": "12h"  # 12-часовые интервалы
            }
            
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if 'prices' not in data or not data['prices']:
                raise ValueError("No price data from CoinGecko")
            
            # Конвертируем в DataFrame
            prices = data['prices']
            df = pd.DataFrame(prices, columns=['timestamp', 'price'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df = df.set_index('timestamp')
            
            if len(df) < 200:
                raise ValueError(f"Insufficient data: {len(df)} points")
            
            # Вычисляем RSI(14) на 12h
            rsi = self._calculate_rsi(df['price'], 14)
            
            # Вычисляем EMA(200) на 12h
            ema200 = self._calculate_ema(df['price'], 200)
            
            return rsi, ema200
    
    def _symbol_to_coingecko_id(self, symbol: str) -> Optional[str]:
        """Конвертировать символ в CoinGecko ID"""
        symbol_map = {
            'BTCUSDT': 'bitcoin',
            'ETHUSDT': 'ethereum',
            'ADAUSDT': 'cardano',
            'SOLUSDT': 'solana',
            'AVAXUSDT': 'avalanche-2',
            'DOTUSDT': 'polkadot',
            'LINKUSDT': 'chainlink',
            'UNIUSDT': 'uniswap',
            'LTCUSDT': 'litecoin',
            'BCHUSDT': 'bitcoin-cash',
            'XRPUSDT': 'ripple',
            'DOGEUSDT': 'dogecoin',
            'SHIBUSDT': 'shiba-inu',
            'MATICUSDT': 'matic-network',
            'ATOMUSDT': 'cosmos',
            'NEARUSDT': 'near',
            'ALGOUSDT': 'algorand',
            'ICPUSDT': 'internet-computer',
            'BOMEUSDT': 'book-of-meme',
            'WALUSDT': 'walrus',
            'SWARMSUSDT': 'swarm',
        }
        return symbol_map.get(symbol.upper())
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> Optional[float]:
        """Вычислить RSI"""
        try:
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
        except Exception as e:
            logger.error(f"RSI calculation error: {e}")
            return None
    
    def _calculate_ema(self, prices: pd.Series, period: int = 200) -> Optional[float]:
        """Вычислить EMA"""
        try:
            ema = prices.ewm(span=period, adjust=False).mean()
            return float(ema.iloc[-1]) if not pd.isna(ema.iloc[-1]) else None
        except Exception as e:
            logger.error(f"EMA calculation error: {e}")
            return None

# Глобальный экземпляр сервиса
indicators_12h_service = Indicators12hService()
