import os


WEBHOOK_URL = os.environ.get("SIGNAL_WEBHOOK_URL", "https://hook.finandy.com/zyzulAJkwB1Gag7trlUK").strip()
WEBHOOK_SECRET = os.environ.get("SIGNAL_WEBHOOK_SECRET", "aa7wohs1xkt").strip()

# Имя вебхука (SHORT)
HOOK_NAME = os.environ.get("SIGNAL_HOOK_NAME", "шорт трейдерсетап").strip()

# Для безопасности: сухой прогон без отправки (0/1)
DRY_RUN = os.environ.get("SIGNAL_DRY_RUN", "0").strip() == "1"

# Подробные логи шагов парсинга/отправки
DEBUG = os.environ.get("SIGNAL_DEBUG", "1").strip() == "1"

# Управление SLX из env
SLX_ENABLED = os.environ.get("SIGNAL_SLX_ENABLED", "1").strip() == "1"

# Параметры SLX для SHORT (по актуальным данным пользователя)
SHORT_SLX_TRAILING_PROFIT = os.environ.get("SIGNAL_SHORT_SLX_TRAILING_PROFIT", "6").strip()
SHORT_SLX_TRAILING_LAG = os.environ.get("SIGNAL_SHORT_SLX_TRAILING_LAG", "1").strip()
SHORT_SLX_TRAILING_BREAKEVEN = os.environ.get("SIGNAL_SHORT_SLX_TRAILING_BREAKEVEN", "4").strip()

# Параметры SLX для LONG (по актуальным данным пользователя)
LONG_SLX_TRAILING_PROFIT = os.environ.get("SIGNAL_LONG_SLX_TRAILING_PROFIT", "0.6").strip()
LONG_SLX_TRAILING_LAG = os.environ.get("SIGNAL_LONG_SLX_TRAILING_LAG", "0.3").strip()

# Автодобавление USDT к тикеру, если отсутствует
APPEND_USDT = os.environ.get("SIGNAL_APPEND_USDT", "1").strip() == "1"


# Настройки для LONG вебхука (новые)
LONG_WEBHOOK_URL = os.environ.get("SIGNAL_LONG_WEBHOOK_URL", "https://hook.finandy.com/8LPIa2NBMCdt7ObvrlUK").strip()
LONG_WEBHOOK_SECRET = os.environ.get("SIGNAL_LONG_WEBHOOK_SECRET", "ucpxgbtqwi").strip()
# Имя вебхука (LONG)
LONG_HOOK_NAME = os.environ.get("SIGNAL_LONG_HOOK_NAME", "лонг трейдерсетап").strip()

# Значения по умолчанию для суммы ордера (если не рассчитали динамически)
SHORT_AMOUNT_SUM_DEFAULT = os.environ.get("SIGNAL_SHORT_AMOUNT_SUM_DEFAULT", "20").strip()
LONG_AMOUNT_SUM_DEFAULT = os.environ.get("SIGNAL_LONG_AMOUNT_SUM_DEFAULT", "20").strip()

# Количество ордеров в сетке по умолчанию
QTY_ORDERS_DEFAULT = int(os.environ.get("SIGNAL_QTY_ORDERS_DEFAULT", "20").strip() or 20)

def set_dry_run(val: bool):
    global DRY_RUN
    DRY_RUN = bool(val)


