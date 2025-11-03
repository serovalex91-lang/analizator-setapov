from app.services.taapi_bulk import swing_points, cluster_levels, classic_pivots, session_vwap, compute_tolerance, score_level

ROUND_STEPS = [1000, 500, 250, 100, 50, 25, 10, 5, 1, 0.5, 0.1]

def infer_tick_from_price(price: float) -> float:
    """Определяет приблизительный размер тика на основе цены"""
    if price >= 1000: return 0.1
    if price >= 100:  return 0.01
    if price >= 10:   return 0.001
    if price >= 1:    return 0.0001
    return 0.00001

def nearest_round(price):
    """Находит ближайшее круглое число для цены"""
    # Используем размер тика для более точного определения круглых чисел
    tick_size = infer_tick_from_price(price)
    
    # Для высоких цен используем стандартные шаги
    for step in ROUND_STEPS:
        if price/step >= 1:
            return round(price/step)*step
    
    # Для низких цен используем размер тика
    if price < 1:
        return round(price/tick_size)*tick_size
    
    return round(price, 2)

def rr_to_opposite(level_price, opposite_levels, entry_dir):
    """Рассчитывает соотношение риск/прибыль до противоположного уровня"""
    if not opposite_levels: 
        return None
    if entry_dir == "long":
        opp = [p for p in opposite_levels if p > level_price]
        if not opp: 
            return None
        return (min(opp) - level_price) / max(1e-9, level_price*0.01)
    else:
        opp = [p for p in opposite_levels if p < level_price]
        if not opp: 
            return None
        return (level_price - max(opp)) / max(1e-9, level_price*0.01)

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
    atr = float(indicators.get('atr', 0.0)) if indicators else 0.0

    # Находим свинг-точки на разных таймфреймах
    highs5, lows5 = swing_points(c5)
    highs15, lows15 = swing_points(c15)
    highs30, lows30 = swing_points(c30)

    # Выбираем соответствующие уровни в зависимости от стороны
    if side == 'long':
        raw = [p for _, p in lows5 + lows15 + lows30]
    else:
        raw = [p for _, p in highs5 + highs15 + highs30]
    
    if not raw: 
        return None

    # Рассчитываем толерантность и кластеризуем уровни
    tol = compute_tolerance(price_now, atr, tick_size)
    cl = cluster_levels(raw, tol)

    # Получаем информацию о сессии
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

    ema200 = indicators.get('ema200')
    candidates = []
    
    # Анализируем каждый кластеризованный уровень
    for lvl in cl:
        conf = set()
        
        # Проверяем близость к пивотным точкам
        if any(abs(lvl - float(v)) <= tol for v in piv.values() if v is not None): 
            conf.add("pivot")
        if pdl and abs(lvl - float(pdl)) <= tol: 
            conf.add("pivot")
        if pdh and abs(lvl - float(pdh)) <= tol: 
            conf.add("pivot")
        
        # Проверяем близость к EMA200
        if ema200 and abs(lvl - float(ema200)) <= tol: 
            conf.add("ema200_near")
        
        # Проверяем близость к VWAP
        if vwap_val and abs(lvl - float(vwap_val)) <= tol: 
            conf.add("vwap")
        
        # Проверяем круглые числа
        if abs(lvl - nearest_round(lvl)) <= tol: 
            conf.add("round")
        
        # Проверяем свинг-точки на старших таймфреймах
        if htf_swing_ok(lvl): 
            conf.add("htf_swing")
        
        # Подсчитываем количество касаний
        touches = sum(1 for p in raw if abs(p - lvl) <= tol)
        if touches >= 2: 
            conf.add("touches")
        
        # Добавляем соответствие тренду
        conf.add("trend_ok")

        # Рассчитываем соотношение риск/прибыль
        opposite = [p for p in cl if (p > lvl if side == 'long' else p < lvl)]
        rr = rr_to_opposite(lvl, opposite, "long" if side == 'long' else "short")
        
        # Оцениваем уровень
        score = score_level(lvl, conf, rr, smashed_recent=False)
        candidates.append((score, lvl, conf, rr))

    # Сортируем кандидатов по оценке
    candidates.sort(reverse=True, key=lambda x: x[0])
    
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

def analyze_session_info(candles_1h, candles_4h):
    """Анализирует информацию о торговой сессии"""
    session_info = {}
    
    # Рассчитываем VWAP для сессии
    if candles_1h:
        session_info["vwap_session"] = session_vwap(candles_1h)
    
    # Рассчитываем пивотные точки на дневном таймфрейме
    if len(candles_4h) >= 2:
        prev_candle = candles_4h[-2]
        prev_high = float(prev_candle['high'])
        prev_low = float(prev_candle['low'])
        prev_close = float(prev_candle['close'])
        session_info["pivots_daily"] = classic_pivots(prev_high, prev_low, prev_close)
    
    # Находим PDH и PDL (Previous Day High/Low)
    if candles_4h:
        highs = [float(c['high']) for c in candles_4h[-6:]]  # Последние 6 свечей (24 часа)
        lows = [float(c['low']) for c in candles_4h[-6:]]
        session_info["PDH"] = max(highs)
        session_info["PDL"] = min(lows)
    
    return session_info

async def find_levels_for_side(symbol, side, taapi_service, limit=200):
    """
    Находит лучшие уровни для заданной стороны (long/short)
    
    Args:
        symbol: Торговая пара
        side: 'long' или 'short'
        taapi_service: Экземпляр TaapiService
        limit: Количество свечей для анализа
    
    Returns:
        Dict с информацией о лучшем уровне
    """
    try:
        # Получаем свечные данные для разных таймфреймов
        c5 = await taapi_service.get_klines(symbol, "5m", limit)
        c15 = await taapi_service.get_klines(symbol, "15m", limit)
        c30 = await taapi_service.get_klines(symbol, "30m", limit)
        c1h = await taapi_service.get_klines(symbol, "1h", limit)
        c4h = await taapi_service.get_klines(symbol, "4h", limit)
        
        if not all([c5, c15, c30, c1h, c4h]):
            return None
        
        # Получаем индикаторы
        atr_data = await taapi_service.get_atr(symbol, "1h", 14)
        ema200_data = await taapi_service.get_ema200(symbol, "1h")
        
        indicators = {
            'atr': atr_data.get('value') if atr_data else None,
            'ema200': ema200_data.get('value') if ema200_data else None
        }
        
        # Анализируем информацию о сессии
        session_info = analyze_session_info(c1h, c4h)
        
        # Рассчитываем размер тика на основе текущей цены
        current_price = float(c5[-1]['close'])
        tick_size = infer_tick_from_price(current_price)
        
        # Ищем лучший уровень
        best_level = find_best_level(
            c5, c15, c30, c1h, c4h, 
            session_info, tick_size, indicators, side
        )
        
        if best_level:
            best_level.update({
                "symbol": symbol,
                "side": side,
                "current_price": current_price,
                "tick_size": tick_size,
                "session_info": session_info,
                "indicators": indicators
            })
        
        return best_level
        
    except Exception as e:
        print(f"Error finding levels for {symbol} {side}: {e}")
        return None 