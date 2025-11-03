from typing import List, Dict, Any
import pandas as pd
from .zones import find_level_zones, LevelZone
from .categorize import classify_zone
from .strength_vs_attack import analyze_attack_defense
from .config import Settings


def pick_best_zone(df: pd.DataFrame, zones: List[Dict[str, Any]]) -> Dict[str, Any]:
	"""Выбор лучшей зоны: приоритет gold>silver>bronze, затем по минимальной дистанции до цены."""
	if zones is None or len(zones) == 0:
		return {}
	last_price = float(df['close'].iloc[-1])
	def key(z: Dict[str, Any]):
		# quality priority will be assigned after classify in caller; here just distance
		return abs(float(z['price']) - last_price)
	return min(zones, key=key)


def find_level_zones_with_quality(df: pd.DataFrame, side: str, settings: Settings = Settings()) -> List[Dict[str, Any]]:
	zones = find_level_zones(df, side, settings)
	res: List[Dict[str, Any]] = []
	for z in zones:
		q = classify_zone(df, z.price, z.atr, z.side)
		res.append({
			'price': z.price,
			'range_low': z.range_low,
			'range_high': z.range_high,
			'side': z.side,
			'atr': z.atr,
			'positional_ok': z.strength_positional,
			'quality': q['quality'],
			'reason': q['reason'],
		})
	return res


def analyze_attack_defense_for_zone(df: pd.DataFrame, zone: Dict[str, Any]) -> Dict[str, Any]:
	return analyze_attack_defense(df, zone['price'], zone['atr'], is_resistance=(zone['side']=='short'))


