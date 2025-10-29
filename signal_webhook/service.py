from typing import Optional
from datetime import datetime, timezone
from .parser import parse_setup_message, parse_setup_time, parse_entry_and_amount
from .payload import build_payload
from .sender import send_payload
from .config import APPEND_USDT, DEBUG, QTY_ORDERS_DEFAULT

import os


async def try_process_screener_message(text: str) -> Optional[bool]:
    """Пробует распознать сетап Short/Long и отправить вебхук. Возвращает True/False при попытке, None если не подошло."""
    if DEBUG:
        print("[signal_webhook] incoming text size:", len(text) if text else 0)
    parsed = parse_setup_message(text)
    if not parsed:
        if DEBUG:
            print("[signal_webhook] not a Short setup or missing fields")
        return None
    # Фильтр свежести отключён по запросу: обрабатываем сетап независимо от возраста
    ticker, side, sl, tp, last_order_price, first_order_price = parsed
    # Приоритет входа и суммы из сообщения (если есть)
    try:
        entry_from_msg, amount_from_msg = parse_entry_and_amount(text)
        if entry_from_msg is not None:
            first_order_price = entry_from_msg
    except Exception:
        amount_from_msg = None
    symbol = f"{ticker}USDT" if (APPEND_USDT and not ticker.endswith("USDT")) else ticker

    # Фиксированный риск 50$ на полный набор ордеров
    avg_entry = None
    try:
        if first_order_price is not None and last_order_price is not None:
            avg_entry = (float(first_order_price) + float(last_order_price)) / 2.0
    except Exception:
        avg_entry = None
    amount_sum = None
    try:
        if avg_entry is not None and sl is not None:
            avg_entry = float(avg_entry)
            sl_f = float(sl)
            if side == "buy":
                d = avg_entry - sl_f
            else:
                d = sl_f - avg_entry
            if d > 0:
                amount_sum = 50.0 * avg_entry / d
    except Exception:
        amount_sum = None

    payload = build_payload(
        symbol=symbol,
        side=side,
        sl_price=sl,
        tp_price=tp,
        last_order_price=last_order_price,
        first_order_price=first_order_price,
        qty_orders=QTY_ORDERS_DEFAULT,
    )

    # Вставим рассчитанный amount_sum в buy/sell ветки если возможно
    try:
        if isinstance(payload.get("open"), dict):
            # Явный приоритет суммы из сообщения, затем рассчитанная, затем дефолт (оставлено в payload.py)
            if amount_from_msg is not None:
                payload["open"]["amountType"] = "sum"
                payload["open"]["amount"] = f"{float(amount_from_msg):.2f}"
            elif amount_sum is not None:
                payload["open"]["amountType"] = "sum"
                payload["open"]["amount"] = f"{amount_sum:.2f}"
    except Exception:
        pass

    # Санити: нормализуем только шорт (price1 <= price2). Для лонга — без нормализации.
    try:
        p1 = payload["open"]["scaled"]["price1"]["value"]
        p2 = payload["open"]["scaled"]["price2"]["value"]
        if p1 is not None and p2 is not None and side == "sell" and p1 > p2:
            payload["open"]["scaled"]["price1"]["value"], payload["open"]["scaled"]["price2"]["value"] = p2, p1
    except Exception:
        pass
    # Явно маршрутизируем Long-сетапы в LONG webhook URL
    try:
        if isinstance(payload, dict) and side == "buy":
            payload["_route"] = "long"
    except Exception:
        pass
    if DEBUG:
        print("[signal_webhook] payload:", payload)
    ok = await send_payload(payload)
    if DEBUG:
        print("[signal_webhook] webhook send result:", ok)
    return ok


