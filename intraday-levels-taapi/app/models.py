from pydantic import BaseModel
from typing import Literal, Dict, Optional, List
from datetime import datetime

class LevelSearchRequest(BaseModel):
    symbol: str                    # 'ETHUSDT'
    context: Literal["long","short"]
    origin_tf: Literal["30m","60m"] = "30m"

class BallFlip(BaseModel):
    symbol: str
    timeframe: Literal["30m","60m"]
    old_color: Literal["green","red"]
    new_color: Literal["green","red"]
    balls: Dict[str, Literal["green","red"]]
    ts: str

class KlineData(BaseModel):
    open_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: str

class RSIResponse(BaseModel):
    value: float
    timestamp: str

class MACDResponse(BaseModel):
    macd: float
    signal: float
    histogram: float
    timestamp: str

class BollingerBandsResponse(BaseModel):
    upper: float
    middle: float
    lower: float
    timestamp: str

class SupportResistanceLevel(BaseModel):
    price: float
    index: int
    strength: float

class SupportResistanceResponse(BaseModel):
    symbol: str
    interval: str
    support_levels: List[SupportResistanceLevel]
    resistance_levels: List[SupportResistanceLevel]
    current_price: Optional[float]
    timestamp: str

class CacheStats(BaseModel):
    cache_size: int
    cache_ttl: int
    cached_keys: List[str]

class APIResponse(BaseModel):
    success: bool
    data: Optional[Dict] = None
    error: Optional[str] = None
    timestamp: str = datetime.now().isoformat() 