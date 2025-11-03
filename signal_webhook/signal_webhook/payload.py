from typing import Dict, Any, Optional
from .config import HOOK_NAME, WEBHOOK_SECRET, SLX_ENABLED, SLX_TRAILING_PROFIT, SLX_TRAILING_LAG, SLX_TRAILING_STEP, LONG_HOOK_NAME, LONG_WEBHOOK_SECRET


def build_payload(symbol: str, side: str, sl_price: float, tp_price: float, last_order_price: Optional[float] = None, first_order_price: Optional[float] = None, qty_orders: int = 5) -> Dict[str, Any]:
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
        # price1 = текущая цена (оставляем пустым), price2 = SUPPORT high
        p2 = float(last_order_price) if last_order_price is not None else None
        return {
            "name": LONG_HOOK_NAME,
            "secret": LONG_WEBHOOK_SECRET,
            "symbol": symbol,
            "side": side,
            "open": {
                "enabled": True,
                "amountType": "sum",
                "amount": "1000",
                "mode": "scaled",
                "orderType": "limit",
                "virtual": True,
                "virtualMode": "lp",
                "priceMode": "price",
                "scaled": {
                    "qty": int(qty_orders),
                    "realQty": "1",
                    "price": {"mode": "density", "value": "1"},
                    "price1": {"mode": "value", "value": ""},
                    "price2": {"mode": "value", "value": p2}
                }
            },
            "positionSide": "long",
            "sl": {"enabled": True, "price": sl_num, "orderType": "stop-market"},
            "tp": {"enabled": True, "orders": [{"price": tp_num, "piece": "10"}]}
        }

    # SELL — price1 = текущая цена (пусто), price2 = RESISTANCE low
    tp_block = {
        "enabled": True,
        "orderType": "limit",
        "qty": 1,
        "orders": {"0": {"price": tp_num, "piece": 100}},
        "price": tp_num,
        "adjust": True,
        "virtualMode": "lp",
        "update": False,
    }

    slx_block = {"enabled": bool(SLX_ENABLED)}
    if SLX_ENABLED:
        slx_block.update({
            "mode": "tp",
            "always": False,
            "trailingProfit": SLX_TRAILING_PROFIT,
            "trailingLag": SLX_TRAILING_LAG,
            "trailingStep": SLX_TRAILING_STEP,
            "trailingSum": "0",
            "breakevenSum": "0",
            "trailingMode": "lp",
            "tpNum": 1,
            "orderType": "stop-market",
            "adjust": False,
            "virtualMode": "lp",
            "caOnProfit": "leave",
            "checkProfit": True,
            "update": False,
        })

    open_block = {
        "mode": "scaled",
        "orderType": "limit",
        "virtual": True,
        "virtualMode": "lp",
        "enabled": True,
        "priceMode": "price",
        "scaled": {
            "qty": int(qty_orders),
            "realQty": "1",
            "factor": 1,
            "candleMode": "c",
            "callback": "0.1",
            "adjust": "",
            "price1": {"mode": "value", "value": ""},
            "price2": {"mode": "value", "value": float(last_order_price) if last_order_price is not None else None},
            "price": {"mode": "density", "value": "1"},
        },
    }

    hook_name = HOOK_NAME
    hook_secret = WEBHOOK_SECRET

    return {
        "name": hook_name,
        "secret": hook_secret,
        "symbol": symbol,
        "side": side,
        "close": {"enabled": True},
        "slx": slx_block,
        "sl": {"enabled": True, "price": sl_num, "orderType": "stop-market", "adjust": False, "virtualMode": "lp", "update": False},
        "tp": tp_block,
        "open": open_block,
    }
