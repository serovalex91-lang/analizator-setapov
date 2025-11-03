import os

# Абсолютные пути к данным
MESSAGES_DIR = "/Users/alekseyserov/проект шарики со светой /resultpairs"
COINAPI_DIR = "/Users/alekseyserov/Desktop/coinapi_full_data_2025"

# Выходная директория
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# Период бэктеста
MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]

# Источник индикаторов/свечей (Taapi)
TAAPI_BASE_URL = "https://api.taapi.io"

# Тайминги окна визуализации
# Для графика берём ровно N свечей до сигнала и N свечей после
BACK_CANDLES = 100
FWD_CANDLES = 100

# Максимальная глубина истории для расчёта уровня (дней до сигнала)
LEVEL_LOOKBACK_DAYS = 30


