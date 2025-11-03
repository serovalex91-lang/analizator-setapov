from typing import Dict, Any, Optional, List, Tuple
from .taapi_bulk import swing_points, cluster_levels, compute_tolerance, score_level
from .utils import nearest_round, rr_to_opposite

def find_best_level(c5, c15, c30, c1h, c4h, session_info,
                    tick_size, indicators, side, origin_tf="30m"):
    """
    Находит лучший уровень для входа на основе множественных факторов
    
    Args:
        c5, c15, c30, c1h, c4h: Свечные данные для разных таймфреймов
        session_info: Информация о сессии (пивоты, VWAP, PDH/PDL)
        tick_size: Размер тика
        indicators: Индикаторы (ATR, EMA200 и др.)
        side: Направление ('long' или 'short')
        origin_tf: Исходный таймфрейм
    
    Returns:
        Dict с информацией о лучшем уровне или None
    """
    price_now = float(c5[-1]['close'])
    # Robust to None/invalid ATR
    if indicators:
        try:
            atr = float(indicators.get('atr') or 0.0)
        except Exception:
            atr = 0.0
    else:
        atr = 0.0

    # Находим свинг-точки на разных таймфреймах (используются для touches/конфлюэнса)
    highs5, lows5 = swing_points(c5)
    highs15, lows15 = swing_points(c15)
    highs30, lows30 = swing_points(c30)
    highs1h, lows1h = swing_points(c1h)

    # Пул свингов для оценки touches
    if side == 'long':
        swing_pool = [p for _, p in (lows5 + lows15 + lows30 + lows1h)]
    else:
        swing_pool = [p for _, p in (highs5 + highs15 + highs30 + highs1h)]

    # Рассчитываем толерантность
    tol = compute_tolerance(price_now, atr, tick_size)

    # Получаем информацию о сессии (пивоты на прошлый день и пр.)
    piv = session_info.get("pivots_daily", {})
    pdh = session_info.get("PDH")
    pdl = session_info.get("PDL")
    vwap_val = session_info.get("vwap_session")

    # Проверяем приближение к старшим таймфреймам
    def htf_swing_ok(level):
        """Проверяет, есть ли свинг-точка на старшем таймфрейме рядом с уровнем"""
        highs1h = [float(x['high']) for x in c1h[-200:]]
        lows1h = [float(x['low']) for x in c1h[-200:]]
        if any(abs(level - hh) <= tol for hh in highs1h): 
            return True
        if any(abs(level - ll) <= tol for ll in lows1h): 
            return True
        return False

    ema200 = indicators.get('ema200') if indicators else None
    candidates = []

    # Кандидаты уровней: используем ПИВОТЫ TAAPI/классические
    # Для LONG — поддержка (S1..S3), для SHORT — сопротивление (R1..R3)
    pivot_keys_long = ["S1", "S2", "S3", "S4"]
    pivot_keys_short = ["R1", "R2", "R3", "R4"]
    chosen_keys = pivot_keys_long if side == 'long' else pivot_keys_short

    pivot_levels: list[tuple[str, float]] = []
    for k in chosen_keys:
        v = piv.get(k)
        try:
            if v is not None:
                pivot_levels.append((k, float(v)))
        except Exception:
            continue

    # Фоллбэк: если нет пивотов — вернемся к старой логике на базе свингов/кластеров
    if not pivot_levels:
        # Старый механизм: кластеризуем свинги и выбираем лучший по скору
        raw = swing_pool
        if not raw:
            return None
        cl = cluster_levels(raw, tol)
        for lvl in cl:
            conf = set()
            if any(abs(lvl - float(v)) <= tol for v in piv.values() if v is not None):
                conf.add("pivot")
            if pdl and abs(lvl - float(pdl)) <= tol:
                conf.add("pivot")
            if pdh and abs(lvl - float(pdh)) <= tol:
                conf.add("pivot")
            if ema200 and abs(lvl - float(ema200)) <= tol:
                conf.add("ema200_near")
            if vwap_val and abs(lvl - float(vwap_val)) <= tol:
                conf.add("vwap")
            if abs(lvl - nearest_round(lvl)) <= tol:
                conf.add("round")
            if htf_swing_ok(lvl):
                conf.add("htf_swing")
            touches = sum(1 for p in raw if abs(p - lvl) <= tol)
            if touches >= 2:
                conf.add("touches")
            conf.add("trend_ok")
            opposite = [p for p in cl if (p > lvl if side == 'long' else p < lvl)]
            rr = rr_to_opposite(lvl, opposite, "long" if side == 'long' else "short")
            score = score_level(lvl, conf, rr, smashed_recent=False)
            candidates.append((score, lvl, conf, rr))
    else:
        # НОВЫЙ МЕХАНИЗМ: берем только уровни пивотов и ранжируем как "фундаментальные"
        tier_bonus_map = {"S1": 0.03, "S2": 0.07, "S3": 0.12, "S4": 0.12,
                          "R1": 0.03, "R2": 0.07, "R3": 0.12, "R4": 0.12}
        for key, lvl in pivot_levels:
            conf = set(["pivot"])  # пивот — базовый фактор
            if pdl and abs(lvl - float(pdl)) <= tol:
                conf.add("pivot")
            if pdh and abs(lvl - float(pdh)) <= tol:
                conf.add("pivot")
            if ema200 and abs(lvl - float(ema200)) <= tol:
                conf.add("ema200_near")
            if vwap_val and abs(lvl - float(vwap_val)) <= tol:
                conf.add("vwap")
            if abs(lvl - nearest_round(lvl)) <= tol:
                conf.add("round")
            if htf_swing_ok(lvl):
                conf.add("htf_swing")
            touches = sum(1 for p in swing_pool if abs(p - lvl) <= tol)
            if touches >= 2:
                conf.add("touches")
            conf.add("trend_ok")

            # Для RR ориентируемся на другие pivot-уровни с противоположной стороны
            other_lvls = [pl for _, pl in pivot_levels if pl != lvl]
            opposite = [pl for pl in other_lvls if (pl > lvl if side == 'long' else pl < lvl)]
            rr = rr_to_opposite(lvl, opposite, "long" if side == 'long' else "short")

            base_score = score_level(lvl, conf, rr, smashed_recent=False)
            bonus = tier_bonus_map.get(key, 0.0)
            score = min(base_score + bonus, 1.0)
            candidates.append((score, lvl, conf, rr, key))

    # Сортируем кандидатов по оценке; при равенстве — по "более фундаментальному" уровню (S3/R3 предпочтительнее)
    def sort_key(item):
        # item может быть (score, lvl, conf, rr) или (score, lvl, conf, rr, key)
        score = item[0]
        pivot_rank = 0
        if len(item) >= 5:
            key = item[4]
            order = {"S4": 4, "S3": 3, "S2": 2, "S1": 1, "R4": 4, "R3": 3, "R2": 2, "R1": 1}
            pivot_rank = order.get(key, 0)
        return (score, pivot_rank)
    candidates.sort(reverse=True, key=sort_key)
    
    # Возвращаем лучший уровень только если оценка достаточно высокая
    if not candidates or candidates[0][0] < 0.60:
        return None

    best = candidates[0]
    # Используем более точное округление на основе размера тика
    rounded_price = round(best[1]/tick_size)*tick_size
    return {
        "price": rounded_price,
        "score": round(best[0], 4),
        "confluence": sorted(list(best[2])),
        "rr": best[3],
        "tolerance": tol,
        "tick_size": tick_size
    } 


def compute_candidates(c5, c15, c30, c1h, c4h, session_info,
                       tick_size, indicators, side, include_1h_swings: bool = False,
                       score_threshold: float = 0.60):
    """Возвращает список кандидатов-уровней со всеми факторами и флагами прохождения.
    Каждый элемент: {price, touches, confluence:[...], rr, score, passed:bool, reason:str}
    """
    price_now = float(c5[-1]['close'])
    try:
        atr = float(indicators.get('atr') or 0.0)
    except Exception:
        atr = 0.0

    # База свингов
    highs5, lows5 = swing_points(c5)
    highs15, lows15 = swing_points(c15)
    highs30, lows30 = swing_points(c30)
    highs1h, lows1h = swing_points(c1h)

    if side == 'long':
        raw = [p for _, p in lows5 + lows15 + lows30]
        if include_1h_swings:
            raw += [p for _, p in lows1h]
    else:
        raw = [p for _, p in highs5 + highs15 + highs30]
        if include_1h_swings:
            raw += [p for _, p in highs1h]

    if not raw:
        return []

    tol = compute_tolerance(price_now, atr, tick_size)
    clusters = cluster_levels(raw, tol)

    piv = session_info.get("pivots_daily", {})
    pdh = session_info.get("PDH")
    pdl = session_info.get("PDL")
    vwap_val = session_info.get("vwap_session")
    ema200 = indicators.get('ema200') if indicators else None

    def htf_swing_ok(level):
        highs1 = [float(x['high']) for x in c1h[-200:]]
        lows1 = [float(x['low']) for x in c1h[-200:]]
        if any(abs(level - hh) <= tol for hh in highs1):
            return True
        if any(abs(level - ll) <= tol for ll in lows1):
            return True
        return False

    candidates = []
    for lvl in clusters:
        conf = set()
        if any(abs(lvl - float(v)) <= tol for v in piv.values() if v is not None):
            conf.add("pivot")
        if pdl and abs(lvl - float(pdl)) <= tol:
            conf.add("pivot")
        if pdh and abs(lvl - float(pdh)) <= tol:
            conf.add("pivot")
        if ema200 and abs(lvl - float(ema200)) <= tol:
            conf.add("ema200_near")
        if vwap_val and abs(lvl - float(vwap_val)) <= tol:
            conf.add("vwap")
        if abs(lvl - nearest_round(lvl)) <= tol:
            conf.add("round")
        if htf_swing_ok(lvl):
            conf.add("htf_swing")
        touches = sum(1 for p in raw if abs(p - lvl) <= tol)
        if touches >= 2:
            conf.add("touches")
        conf.add("trend_ok")

        opposite = [p for p in clusters if (p > lvl if side == 'long' else p < lvl)]
        rr = rr_to_opposite(lvl, opposite, "long" if side == 'long' else "short")
        score = score_level(lvl, conf, rr, smashed_recent=False)
        passed = score >= score_threshold
        reason = "" if passed else f"score<{score_threshold}"
        candidates.append({
            "price": lvl,
            "touches": touches,
            "confluence": sorted(list(conf)),
            "rr": rr,
            "score": round(score, 4),
            "passed": passed,
            "reason": reason
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates