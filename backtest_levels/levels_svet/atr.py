import pandas as pd


def atr_sma(df: pd.DataFrame, period: int = 14) -> pd.Series:
	"""ATR по простой скользящей средней (как в исходном проекте).
	Ожидает колонки: high, low, close.
	"""
	series_hl = (df["high"] - df["low"]).abs()
	prev_close = df["close"].shift(1)
	series_hc = (df["high"] - prev_close).abs()
	series_lc = (df["low"] - prev_close).abs()
	tr = pd.concat([series_hl, series_hc, series_lc], axis=1).max(axis=1)
	return tr.rolling(period).mean()


def atr_rma(df: pd.DataFrame, period: int = 14) -> pd.Series:
	"""ATR через RMA (Wilder)."""
	series_hl = (df["high"] - df["low"]).abs()
	prev_close = df["close"].shift(1)
	series_hc = (df["high"] - prev_close).abs()
	series_lc = (df["low"] - prev_close).abs()
	tr = pd.concat([series_hl, series_hc, series_lc], axis=1).max(axis=1)
	return tr.ewm(alpha=1/period, adjust=False).mean()




