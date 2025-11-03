from pydantic import BaseModel
from typing import Dict, List, Literal, Optional, Any
from datetime import datetime

class LevelSearchRequest(BaseModel):
    symbol: str
    context: Literal["long", "short"]
    origin_tf: Literal["30m", "60m", "120m"] = "30m"

class BallFlip(BaseModel):
    symbol: str
    timeframe: Literal["30m", "60m"]
    old_color: Literal["green", "red"]
    new_color: Literal["green", "red"]
    balls: Dict[str, Literal["green", "red"]]
    ts: str

class APIResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: str = datetime.now().isoformat()

class SupportResistanceLevel(BaseModel):
    price: float
    index: int
    strength: int
    confluence: Optional[Dict[str, Any]] = None
    tolerance: Optional[float] = None
    score: Optional[float] = None

class SupportResistanceResponse(BaseModel):
    symbol: str
    interval: str
    support_levels: List[SupportResistanceLevel]
    resistance_levels: List[SupportResistanceLevel]
    current_price: Optional[float] = None
    vwap: Optional[float] = None
    pivot_points: Optional[Dict[str, float]] = None
    swing_points: Optional[Dict[str, Any]] = None
    atr: Optional[float] = None
    ema200: Optional[float] = None
    timestamp: str = datetime.now().isoformat()

class CacheStats(BaseModel):
    size: int
    keys: List[str]
    ttl_seconds: int
    hit_rate: Optional[float] = None

class KlineData(BaseModel):
    t: str
    o: float
    h: float
    l: float
    c: float
    v: float

class IndicatorData(BaseModel):
    value: Optional[float] = None
    timestamp: str = datetime.now().isoformat()

class RSIResponse(BaseModel):
    symbol: str
    interval: str
    period: int
    data: IndicatorData

class MACDResponse(BaseModel):
    symbol: str
    interval: str
    fast_period: int
    slow_period: int
    signal_period: int
    data: Dict[str, Optional[float]]

class BollingerBandsResponse(BaseModel):
    symbol: str
    interval: str
    period: int
    std_dev: float
    data: Dict[str, Optional[float]]

class VWAPResponse(BaseModel):
    symbol: str
    interval: str
    data: Optional[float]

class PivotPointsResponse(BaseModel):
    symbol: str
    interval: str
    data: Dict[str, float]

class SwingPointsResponse(BaseModel):
    symbol: str
    interval: str
    data: Dict[str, Any]

class ATRResponse(BaseModel):
    symbol: str
    interval: str
    period: int
    data: IndicatorData

class EMA200Response(BaseModel):
    symbol: str
    interval: str
    data: IndicatorData

class BestLevelResponse(BaseModel):
    symbol: str
    side: str
    limit: int
    best_level: Optional[Dict[str, Any]] = None

class IntradaySearchResponse(BaseModel):
    decision: str
    reason: str
    level: Optional[Dict[str, Any]] = None
    orders: Optional[Dict[str, Any]] = None 
    key_levels: Optional[Dict[str, Any]] = None
    last_price: Optional[float] = None
    trade_setup: Optional[Dict[str, Any]] = None


class ChartRequest(BaseModel):
    symbol: str
    origin_tf: Literal["5m", "15m", "30m", "60m", "120m"] = "30m"
    level_price: float
    range_low: float
    range_high: float
    entry: float
    sl: float
    signal_ts: str  # ISO8601


# Cursor (LLM) module schemas
class CursorSetupFlags(BaseModel):
    symbol: str
    correction_tf: List[Literal["30m","60m","120m"]]
    htf_trend_up: List[Literal["120m","240m","720m"]]
    price_above_ma200_12h: bool
    rsi_12h_gt_50: bool

class CursorScoringPrefs(BaseModel):
    threshold_enter: float | None = 7.0
    weights: Dict[str, float] | None = None

class CursorRunRequest(BaseModel):
    setup: CursorSetupFlags
    scoring_prefs: CursorScoringPrefs | None = None

class CursorRunResponse(BaseModel):
    status: Literal["placed","skipped","error"]
    message: str | None = None
    payload_sent: Dict[str, Any] | None = None
    llm_raw: Dict[str, Any] | None = None