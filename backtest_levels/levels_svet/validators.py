import pandas as pd


def validate_with_volume_profile(df: pd.DataFrame, level_price: float, atr_value: float) -> bool:
	"""Простая HVN-валидация: средний объём вокруг уровня выше среднего по окну.
	Не точный профайл, но близко к исходному подходу.
	"""
	if atr_value <= 0 or df.empty:
		return False
	zone = df[(df['low'] <= level_price) & (df['high'] >= level_price)]
	if zone.empty:
		return False
	avg_near = zone['volume'].mean()
	avg_all = df['volume'].tail(200).mean()
	return avg_near > avg_all


def check_rsi_divergence(df: pd.DataFrame, period: int = 14, is_resistance: bool = True) -> bool:
	"""Псевдо-проверка дивергенции: сравнение последних локальных максимумов/минимумов цены и RSI.
	Упрощённая версия для переносимости.
	"""
	close = df['close']
	delta = close.diff()
	gain = (delta.clip(lower=0)).ewm(alpha=1/period, adjust=False).mean()
	loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
	rs = gain / (loss.replace(0, pd.NA))
	rsi = 100 - (100 / (1 + rs))
	# Очень простой критерий: для сопротивления RSI снижается при росте цены;
	# для поддержки RSI растёт при падении цены.
	px = close.tail(5).values
	rv = rsi.tail(5).values
	if len(px) < 5 or pd.isna(rv).any():
		return False
	if is_resistance:
		return (px[-1] > px[-3]) and (rv[-1] < rv[-3])
	else:
		return (px[-1] < px[-3]) and (rv[-1] > rv[-3])




