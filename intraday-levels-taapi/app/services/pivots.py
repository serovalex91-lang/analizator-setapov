def classic_pivots(prev_high, prev_low, prev_close):
    """Расчет классических пивотных точек"""
    pp = (prev_high + prev_low + prev_close)/3.0
    r1 = 2*pp - prev_low
    s1 = 2*pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    return dict(PP=pp, R1=r1, S1=s1, R2=r2, S2=s2) 