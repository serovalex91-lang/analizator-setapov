from typing import Optional

def infer_tick_from_price(price: float) -> float:
    """Определяет приблизительный размер тика на основе цены"""
    if price >= 1000: return 0.1
    if price >= 100:  return 0.01
    if price >= 10:   return 0.001
    if price >= 1:    return 0.0001
    return 0.00001

def nearest_round(price: float) -> float:
    """Находит ближайшее круглое число для цены"""
    ROUND_STEPS = [1000, 500, 250, 100, 50, 25, 10, 5, 1, 0.5, 0.1]
    
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

def rr_to_opposite(level_price: float, opposite_levels: list, entry_dir: str) -> Optional[float]:
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