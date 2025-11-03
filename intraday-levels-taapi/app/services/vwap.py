def session_vwap(ohlcv_rows):
    """Расчет VWAP (Volume Weighted Average Price)"""
    num, den = 0.0, 0.0
    for r in ohlcv_rows:
        p = (float(r['high']) + float(r['low']) + float(r['close'])) / 3.0
        v = float(r['volume'])
        num += p * v
        den += v
    return num/den if den > 0 else None 