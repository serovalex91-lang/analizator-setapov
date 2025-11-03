from dataclasses import dataclass
from typing import Literal, Dict
import pandas as pd
from .validators import validate_with_volume_profile, check_rsi_divergence


Quality = Literal['gold','silver','bronze']


def classify_zone(df: pd.DataFrame, level_price: float, atr_value: float, side: str) -> Dict[str, str]:
	"""Классификация зоны в gold/silver/bronze по исходной логике.
	- gold: volume + RSI
	- silver: одна из валидаций
	- bronze: только позиция
	"""
	is_res = (side == 'short')
	vol_ok = validate_with_volume_profile(df, level_price, atr_value)
	rsi_ok = check_rsi_divergence(df, is_resistance=is_res)
	if vol_ok and rsi_ok:
		return {'quality': 'gold', 'reason': 'volume+rsi'}
	if vol_ok or rsi_ok:
		return {'quality': 'silver', 'reason': 'volume' if vol_ok else 'rsi'}
	return {'quality': 'bronze', 'reason': 'position'}




