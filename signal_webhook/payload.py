from typing import Dict, Any, Optional
from .config import (
    HOOK_NAME,
    WEBHOOK_SECRET,
    SLX_ENABLED,
    SHORT_SLX_TRAILING_PROFIT,
    SHORT_SLX_TRAILING_LAG,
    SHORT_SLX_TRAILING_BREAKEVEN,
    LONG_HOOK_NAME,
    LONG_WEBHOOK_SECRET,
    LONG_SLX_TRAILING_PROFIT,
    LONG_SLX_TRAILING_LAG,
    SHORT_AMOUNT_SUM_DEFAULT,
    LONG_AMOUNT_SUM_DEFAULT,
)


def build_payload(
    symbol: str,
    side: str,
    sl_price: float,
    tp_price: float,
    last_order_price: Optional[float] = None,
    first_order_price: Optional[float] = None,
    qty_orders: int = 5,
    volumes: Optional[list] = None,
    slx_enabled_override: Optional[bool] = None,
    slx_overrides: Optional[Dict[str, Any]] = None,
    be_enabled_override: Optional[bool] = None,
    be_overrides: Optional[Dict[str, Any]] = None,
    open_order_type: str = "limit",
    real_qty_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Формирует JSON в формате из скриншота. Меняем только ticker, sl.price, tp.price.
    side жёстко 'sell' для текущего вебхука (Short), но прокидываем параметр для наглядности.
    """
    if side not in ("sell", "buy"):
        side = "sell"
    # Числа
    try:
        sl_num = float(sl_price)
    except Exception:
        sl_num = None
    try:
        tp_num = float(tp_price)
    except Exception:
        tp_num = None

    if side == "buy":
        # Формируем payload в точном соответствии с актуальным шаблоном (LONG)
        p1 = float(first_order_price) if first_order_price is not None else None
        p2 = float(last_order_price) if last_order_price is not None else None
        slx_enabled = SLX_ENABLED if slx_enabled_override is None else bool(slx_enabled_override)
        trailing_profit = LONG_SLX_TRAILING_PROFIT
        trailing_lag = LONG_SLX_TRAILING_LAG
        if slx_overrides:
            trailing_profit = str(slx_overrides.get("trailingProfit", trailing_profit))
            trailing_lag = str(slx_overrides.get("trailingLag", trailing_lag))
        return {
            "name": LONG_HOOK_NAME,
            "secret": LONG_WEBHOOK_SECRET,
            "symbol": symbol,
            "side": side,
            "open": {
                "enabled": True,
                "amountType": "sum",
                "amount": str(LONG_AMOUNT_SUM_DEFAULT or "20"),
                "scaled": {
                    "price1": {"mode": "value", "value": p1},
                    "price2": {"mode": "value", "value": p2},
                    "qty": str(int(qty_orders)),
                },
            },
            "positionSide": "long",
            "sl": {
                "enabled": True,
                "price": sl_num,
            },
            "tp": {
                "enabled": True,
                "orders": [
                    {"price": tp_num, "piece": "10"}
                ],
            },
            "close": {"enabled": True, "action": "close"},
            "slx": {
                "enabled": bool(slx_enabled),
                "mode": "trailing",
                "tpNum": 1,
                "trailingBreakeven": "",
                "trailingBreakevenProfit": "",
                "trailingProfit": trailing_profit,
                "trailingLag": trailing_lag,
            },
        }

    # SELL: формируем payload в точном соответствии с актуальным шаблоном (SHORT)
    p1 = float(first_order_price) if first_order_price is not None else None
    p2 = float(last_order_price) if last_order_price is not None else None
    slx_enabled = SLX_ENABLED if slx_enabled_override is None else bool(slx_enabled_override)
    trailing_profit = SHORT_SLX_TRAILING_PROFIT
    trailing_lag = SHORT_SLX_TRAILING_LAG
    trailing_be = SHORT_SLX_TRAILING_BREAKEVEN
    if slx_overrides:
        trailing_profit = str(slx_overrides.get("trailingProfit", trailing_profit))
        trailing_lag = str(slx_overrides.get("trailingLag", trailing_lag))
    return {
        "name": HOOK_NAME,
        "secret": WEBHOOK_SECRET,
        "symbol": symbol,
        "side": side,
        "close": {"enabled": True, "action": "close"},
        "sl": {
            "price": sl_num,
            "enabled": True,
            "orderType": "stop-market",
        },
        "open": {
            "scaled": {
                "price2": {"mode": "value", "value": p2},
                "price1": {"mode": "value", "value": p1},
                "qty": str(int(qty_orders)),
            },
            "enabled": True,
            "amountType": "sum",
            "amount": str(SHORT_AMOUNT_SUM_DEFAULT or "20"),
        },
        "tp": {
            "orders": [
                {"price": tp_num, "piece": "10"}
            ],
            "enabled": True,
        },
        "slx": {
            "enabled": bool(slx_enabled),
            "mode": "trailing",
            "trailingBreakeven": str(trailing_be),
            "trailingBreakevenProfit": "",
            "trailingProfit": str(trailing_profit),
            "trailingLag": str(trailing_lag),
        },
    }


def build_close_payload(symbol: str, position_side: str) -> Dict[str, Any]:
    """Формирует минимальный payload для закрытия позиции.
    position_side: 'long' → закрыть long (маршрутизируем в long-хук), 'short' → закрыть short (в обычный хук).
    """
    is_long = str(position_side).lower() == 'long'
    if is_long:
        hook_name = LONG_HOOK_NAME
        hook_secret = LONG_WEBHOOK_SECRET
        # По требованиям терминала для закрытия используем side='sell'
        # и принудительно маршрутизируем в LONG webhook
        side = 'sell'
        route = 'long'
    else:
        hook_name = HOOK_NAME
        hook_secret = WEBHOOK_SECRET
        # Для закрытия SHORT используем side='buy' (чтобы закрыть шорт)
        side = 'buy'
        route = 'short'
    return {
        "name": hook_name,
        "secret": hook_secret,
        "symbol": symbol,
        "side": side,
        # Явный маршрут для отправителя (не влияет на терминал)
        "_route": route,
        # Соответствие шаблону из терминала
        "close": {"enabled": True, "action": "close"},
        "open": {"enabled": False},
        "sl": {"enabled": False},
        "tp": {"enabled": False},
    }


