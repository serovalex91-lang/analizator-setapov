# Выбор движка вычисления уровней для бэктеста/графиков.
# Доступные значения:
#   "default" — текущая логика find_best_level из intraday-levels-taapi
#   "svet"    — новая библиотека backtest_levels.levels_svet

import os as _os

# Allow override via env LEVEL_ENGINE, default to pivots for this workflow
LEVEL_ENGINE = (_os.getenv("LEVEL_ENGINE") or "pivots").strip().lower()

# Base timeframe for pivots engine (1H, 4H, 12H)
PIVOT_TF = (_os.getenv("PIVOT_TF") or "1H").strip().upper()




