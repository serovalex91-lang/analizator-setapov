from dataclasses import dataclass
from typing import List, Tuple
import pandas as pd
from .atr import atr_sma
from .config import Settings


@dataclass
class LevelZone:
	price: float
	range_low: float
	range_high: float
	side: str  # 'long' (support) | 'short' (resistance)
	atr: float
	strength_positional: bool  # прошла ли свеча позиционный фильтр


def _swing_lows(df: pd.DataFrame) -> List[float]:
	vals: List[float] = []
	low = df["low"].values
	for i in range(2, len(low) - 2):
		if low[i] < low[i-2] and low[i] < low[i-1] and low[i] < low[i+1] and low[i] < low[i+2]:
			vals.append(float(low[i]))
	return vals


def _swing_highs(df: pd.DataFrame) -> List[float]:
	vals: List[float] = []
	high = df["high"].values
	for i in range(2, len(high) - 2):
		if high[i] > high[i-2] and high[i] > high[i-1] and high[i] > high[i+1] and high[i] > high[i+2]:
			vals.append(float(high[i]))
	return vals


def find_level_zones(df: pd.DataFrame, side: str, settings: Settings = Settings()) -> List[LevelZone]:
	"""Строит зоны уровней по свингам и ATR/2, с позиционным фильтром 70% (configurable)."""
	if len(df) < 30:
		return []
	atr_series = atr_sma(df, 14)
	atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
	if atr_val <= 0:
		return []
	last_low = float(df["low"].iloc[-1]); last_high = float(df["high"].iloc[-1])
	candle_size = last_high - last_low
	zone_half = settings.zone_half_atr * atr_val
	zones: List[LevelZone] = []
	if side == 'long':
		cands = _swing_lows(df)
		for price in cands:
			range_low = price - zone_half
			range_high = price + zone_half
			seventy_from_top = last_high - settings.position_threshold * candle_size
			pos_ok = seventy_from_top > range_high
			zones.append(LevelZone(price=price, range_low=range_low, range_high=range_high, side='long', atr=atr_val, strength_positional=pos_ok))
	else:
		cands = _swing_highs(df)
		for price in cands:
			range_low = price - zone_half
			range_high = price + zone_half
			seventy_of_candle = last_low + settings.position_threshold * candle_size
			pos_ok = seventy_of_candle < range_low
			zones.append(LevelZone(price=price, range_low=range_low, range_high=range_high, side='short', atr=atr_val, strength_positional=pos_ok))
	return zones




