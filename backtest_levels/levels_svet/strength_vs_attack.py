from typing import Dict
import pandas as pd


def analyze_attack_defense(df: pd.DataFrame, level_price: float, atr_value: float, is_resistance: bool) -> Dict[str, object]:
	"""Перенос логики сравнения атаки и защиты (упрощённо, как в проекте)."""
	level_zone_range = atr_value / 4
	level_zone_low = level_price - level_zone_range
	level_zone_high = level_price + level_zone_range

	historical_touches = df[(df['low'] <= level_zone_high) & (df['high'] >= level_zone_low)]
	if len(historical_touches) > 0:
		defense_volume = historical_touches['volume'].mean()
		max_defense_volume = historical_touches['volume'].max()
		touch_count = len(historical_touches)
	else:
		defense_volume = df['volume'].mean()
		max_defense_volume = defense_volume
		touch_count = 0

	attacking_candles = df.tail(5)
	if is_resistance:
		attack_candles = attacking_candles[attacking_candles['close'] > attacking_candles['open']]
	else:
		attack_candles = attacking_candles[attacking_candles['close'] < attacking_candles['open']]
	if len(attack_candles) > 0:
		attack_volume = attack_candles['volume'].mean()
		max_attack_volume = attack_candles['volume'].max()
		attack_candle_count = len(attack_candles)
	else:
		attack_volume = attacking_candles['volume'].mean()
		max_attack_volume = attacking_candles['volume'].max()
		attack_candle_count = 0

	volume_ratio = attack_volume / defense_volume if defense_volume else 1.0
	max_volume_ratio = max_attack_volume / max_defense_volume if max_defense_volume else 1.0

	# Композитный скор (упрощённо повторяет формулы из исходника)
	base_score = volume_ratio
	if max_volume_ratio > 1.5:
		base_score *= 1.2
	# тренд объёма в последних свечах
	recent = attacking_candles['volume'].values
	if len(recent) >= 3 and recent[-3] != 0:
		volume_trend = (recent[-1] - recent[-3]) / recent[-3]
		if volume_trend > 0.3:
			base_score *= 1.1
	else:
		volume_trend = 0.0

	# Пороговые зоны прогноза
	if base_score < 0.6:
		prediction = 'HELD'
	elif base_score < 0.9:
		prediction = 'BALANCE'
	elif base_score < 1.4:
		prediction = 'RISK_BREAK'
	else:
		prediction = 'BREAK_LIKELY'

	return {
		'defense_volume': defense_volume,
		'attack_volume': attack_volume,
		'volume_ratio': volume_ratio,
		'max_volume_ratio': max_volume_ratio,
		'touch_count': touch_count,
		'attack_candle_count': attack_candle_count,
		'volume_trend': volume_trend,
		'prediction': prediction,
	}




