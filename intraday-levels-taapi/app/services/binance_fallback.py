"""
Binance API fallback service for when Taapi.io is unavailable.
Provides basic candles and simple indicators using Binance public API.
"""

import httpx
import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)

class BinanceFallbackService:
    """Fallback service using Binance public API when Taapi.io fails."""
    
    BASE_URL = "https://api.binance.com/api/v3"
    
    # Map our timeframes to Binance intervals
    TIMEFRAME_MAP = {
        "5m": "5m",
        "15m": "15m", 
        "30m": "30m",
        "1h": "1h",
        "4h": "4h"
    }
    
    async def get_candles(self, symbol: str, timeframe: str, limit: int = 100) -> List[Dict]:
        """Get candles from Binance API."""
        if timeframe not in self.TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
            
        interval = self.TIMEFRAME_MAP[timeframe]
        url = f"{self.BASE_URL}/klines"
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, 1000)  # Binance limit
        }
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                # Convert Binance format to our format
                candles = []
                for kline in data:
                    candles.append({
                        "t": int(kline[0]),  # timestamp
                        "o": float(kline[1]),  # open
                        "h": float(kline[2]),  # high
                        "l": float(kline[3]),  # low
                        "c": float(kline[4]),  # close
                        "v": float(kline[5])   # volume
                    })
                
                return candles
                
        except Exception as e:
            logger.error(f"Binance API error for {symbol} {timeframe}: {e}")
            raise
    
    def calculate_simple_indicators(self, candles: List[Dict]) -> Dict[str, float]:
        """Calculate simple indicators from candles."""
        if len(candles) < 50:
            return {
                "atr": None,
                "ema20": None,
                "ema50": None, 
                "ema200": None,
                "adx": None
            }
        
        df = pd.DataFrame(candles)
        df = df.sort_values('t')
        
        # Calculate ATR (simplified)
        df['high_low'] = df['h'] - df['l']
        df['high_close'] = abs(df['h'] - df['c'].shift(1))
        df['low_close'] = abs(df['l'] - df['c'].shift(1))
        df['tr'] = df[['high_low', 'high_close', 'low_close']].max(axis=1)
        atr = df['tr'].rolling(window=14).mean().iloc[-1]
        
        # Calculate EMAs
        ema20 = df['c'].ewm(span=20).mean().iloc[-1]
        ema50 = df['c'].ewm(span=50).mean().iloc[-1]
        ema200 = df['c'].ewm(span=200).mean().iloc[-1]
        
        # Simple ADX approximation (using price momentum)
        df['dm_plus'] = np.where(
            (df['h'] - df['h'].shift(1)) > (df['l'].shift(1) - df['l']),
            df['h'] - df['h'].shift(1),
            0
        )
        df['dm_minus'] = np.where(
            (df['l'].shift(1) - df['l']) > (df['h'] - df['h'].shift(1)),
            df['l'].shift(1) - df['l'],
            0
        )
        df['di_plus'] = 100 * (df['dm_plus'].rolling(14).mean() / df['tr'].rolling(14).mean())
        df['di_minus'] = 100 * (df['dm_minus'].rolling(14).mean() / df['tr'].rolling(14).mean())
        df['dx'] = 100 * abs(df['di_plus'] - df['di_minus']) / (df['di_plus'] + df['di_minus'])
        adx = df['dx'].rolling(14).mean().iloc[-1]
        
        return {
            "atr": float(atr) if not pd.isna(atr) else None,
            "ema20": float(ema20) if not pd.isna(ema20) else None,
            "ema50": float(ema50) if not pd.isna(ema50) else None,
            "ema200": float(ema200) if not pd.isna(ema200) else None,
            "adx": float(adx) if not pd.isna(adx) else None
        }
    
    async def get_simple_levels(self, symbol: str, candles_4h: List[Dict]) -> Dict[str, Any]:
        """Get simple support/resistance levels from 4h candles."""
        if len(candles_4h) < 20:
            return {"support_levels": [], "resistance_levels": []}
        
        df = pd.DataFrame(candles_4h)
        df = df.sort_values('t')
        
        # Simple pivot points
        recent_data = df.tail(20)
        
        # Find recent highs and lows
        highs = recent_data[recent_data['h'] == recent_data['h'].rolling(5, center=True).max()]['h']
        lows = recent_data[recent_data['l'] == recent_data['l'].rolling(5, center=True).min()]['l']
        
        # Get current price
        current_price = float(df['c'].iloc[-1])
        
        # Create levels
        resistance_levels = []
        support_levels = []
        
        # Add recent highs as resistance (above current price)
        for high in highs:
            if high > current_price * 1.01:  # 1% above current price
                resistance_levels.append({
                    "price": float(high),
                    "strength": "medium",
                    "distance_percent": ((high - current_price) / current_price) * 100
                })
        
        # Add recent lows as support (below current price)
        for low in lows:
            if low < current_price * 0.99:  # 1% below current price
                support_levels.append({
                    "price": float(low),
                    "strength": "medium", 
                    "distance_percent": ((low - current_price) / current_price) * 100
                })
        
        # Sort by distance
        resistance_levels.sort(key=lambda x: x["distance_percent"])
        support_levels.sort(key=lambda x: abs(x["distance_percent"]))
        
        return {
            "support_levels": support_levels[:3],  # Top 3 closest
            "resistance_levels": resistance_levels[:3],  # Top 3 closest
            "current_price": current_price
        }

# Global instance
binance_fallback = BinanceFallbackService()
