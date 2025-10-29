import re
from typing import Optional, Tuple
from datetime import datetime
from .config import DEBUG

# Разрешаем "Author: Setup Screener" в любой строке сообщения (не только в начале),
# с игнорированием ведущих служебных символов/эмодзи/кавычек.
AUTHOR_OK_RE = re.compile(r"Author\s*:\s*Setup\s*Screener\b", re.IGNORECASE)
# Жёстко отбрасываем кириллическое начало "Автор: ..."
AUTHOR_BAD_CYRILLIC_RE = re.compile(r"^\s*Автор\s*:\s*", re.IGNORECASE)

TICKER_DOLLAR_RE = re.compile(r"\$([A-Z][A-Z0-9]{1,14})\b")
TICKER_WITH_USDT_RE = re.compile(r"\b([A-Z0-9]{2,15})USDT\b")
TICKER_LETTERS_ONLY_RE = re.compile(r"\b([A-Z]{2,15})\b")
SIDE_SHORT_RE = re.compile(r"\bshort\b", re.IGNORECASE)
SIDE_LONG_RE = re.compile(r"\blong\b", re.IGNORECASE)
TP_RE = re.compile(r"TP\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
SL_RE = re.compile(r"SL\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
# Entry (вход)
ENTRY_RE = re.compile(r"Entry\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
CURRENT_RE = re.compile(r"Current\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
RES_RANGE_RE = re.compile(r"RESISTANCE\s*:?[\s]*([0-9]+(?:\.[0-9]+)?)\s*[-–]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
SUP_RANGE_RE = re.compile(r"SUPPORT\s*:?[\s]*([0-9]+(?:\.[0-9]+)?)\s*[-–]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def parse_setup_message(text: str) -> Optional[Tuple[str, str, float, float, Optional[float], Optional[float]]]:
    """Возвращает (symbol, side, sl_price, tp_price, last_order_price, first_order_price) если распознано и это Short/Long.
    symbol — без суффикса USDT; добавлять при формировании payload.
    """
    if not text:
        return None
    # Нормализация текста: убираем невидимые символы и неразрывные пробелы
    try:
        # Zero-width and BOM
        for ch in ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"):
            text = text.replace(ch, "")
        # NBSP → space
        text = text.replace("\xa0", " ")
    except Exception:
        pass
    # Автор
    has_latin_author = bool(AUTHOR_OK_RE.search(text))
    has_cyrillic_author = bool(AUTHOR_BAD_CYRILLIC_RE.search(text))
    if not has_latin_author:
        # Нет латинского Author — возможно кириллица или другой формат → пропускаем
        if has_cyrillic_author and DEBUG:
            print("[parser] skip: cyrillic author prefix")
        if DEBUG and not has_cyrillic_author:
            print("[parser] skip: invalid author header")
        return None
    # Есть корректный латинский Author — продолжаем независимо от наличия кириллических блоков в сообщении
    if False:  # placeholder to keep indentation consistent
        pass
    if not has_latin_author:
        if DEBUG:
            print("[parser] skip: invalid author header")
        return None
    side = None
    if SIDE_SHORT_RE.search(text):
        side = "sell"
    elif SIDE_LONG_RE.search(text):
        side = "buy"
    else:
        return None
    # Тикер
    m_t = TICKER_DOLLAR_RE.search(text)
    ticker = None
    if m_t:
        ticker = m_t.group(1).upper()
    if not ticker:
        m2 = TICKER_WITH_USDT_RE.search(text)
        if m2:
            ticker = m2.group(1).upper()
    if not ticker:
        m3 = TICKER_LETTERS_ONLY_RE.search(text)
        if m3:
            ticker = m3.group(1).upper()
    # Поля
    m_tp = TP_RE.search(text)
    m_sl = SL_RE.search(text)
    m_entry = ENTRY_RE.search(text)
    m_res = RES_RANGE_RE.search(text)
    m_sup = SUP_RANGE_RE.search(text)
    m_cur = CURRENT_RE.search(text)
    if not (ticker and m_tp and m_sl):
        return None
    try:
        tp = float(m_tp.group(1))
        sl = float(m_sl.group(1))
    except Exception:
        return None
    if DEBUG:
        print("[parser] matched ticker:", ticker, "side:", side)
    # Последний ордер (граница сетки)
    last_order_price = None
    if side == "sell" and m_res:
        try:
            a = float(m_res.group(1)); b = float(m_res.group(2))
            # Для SHORT сетка тянется вверх до верхней границы резистанса
            last_order_price = max(a, b)
        except Exception:
            last_order_price = None
    if side == "buy" and m_sup:
        try:
            a = float(m_sup.group(1)); b = float(m_sup.group(2))
            # Для LONG сетка тянется вниз до нижней границы поддержки
            last_order_price = min(a, b)
        except Exception:
            last_order_price = None
    # Первый ордер — приоритет Current, затем Entry
    first_order_price = None
    if m_cur:
        try:
            first_order_price = float(m_cur.group(1))
        except Exception:
            first_order_price = None
    if first_order_price is None and m_entry:
        try:
            first_order_price = float(m_entry.group(1))
        except Exception:
            first_order_price = None
    return ticker, side, sl, tp, last_order_price, first_order_price


def parse_entry_and_amount(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Возвращает (entry_price, amount_sum) если найдены в тексте, иначе (None, None).
    Ищем строки вида:
      ▶️ Entry: 16.396
      💰 Amount: 1222.32
    """
    entry = None
    amount = None
    try:
        m_e = ENTRY_RE.search(text)
        if m_e:
            entry = float(m_e.group(1))
    except Exception:
        entry = None
    try:
        m_a = re.search(r"Amount\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
        if m_a:
            amount = float(m_a.group(1))
    except Exception:
        amount = None
    return entry, amount


# Извлекает метку времени из сообщения Setup Screener.
# Поддерживаем форматы:
#  - "HH:MM DD.MM.YYYY" (например, 12:00 14.09.2025)
#  - "DD.MM.YYYY HH:MM"
#  - строки вида "▶️  Open" затем на следующей строке "DD.MM.YY HH:MM" (например, 19.09.25 11:29)
def parse_setup_time(text: str) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        # Нормализуем невидимые символы
        for ch in ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"):
            text = text.replace(ch, "")
        text = text.replace("\xa0", " ")
    except Exception:
        pass
    # Вариант 1: HH:MM DD.MM.YYYY
    m1 = re.search(r"(\d{1,2}:\d{2})\s+(\d{2}\.\d{2}\.\d{4})", text)
    if m1:
        try:
            hhmm, dmy = m1.group(1), m1.group(2)
            return datetime.strptime(f"{dmy} {hhmm}", "%d.%m.%Y %H:%M")
        except Exception:
            pass
    # Вариант 2: DD.MM.YYYY HH:MM
    m2 = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})", text)
    if m2:
        try:
            dmy, hhmm = m2.group(1), m2.group(2)
            return datetime.strptime(f"{dmy} {hhmm}", "%d.%m.%Y %H:%M")
        except Exception:
            pass
    # Вариант 3: после строки Open — дата в формате DD.MM.YY HH:MM
    m3 = re.search(r"Open[\s\S]*?(\d{2}\.\d{2}\.\d{2})\s+(\d{1,2}:\d{2})", text, flags=re.IGNORECASE)
    if m3:
        try:
            dmy2, hhmm = m3.group(1), m3.group(2)
            # Преобразуем двухзначный год к 20xx/19xx — примем 00..69 => 2000..2069, иначе 1900..1999
            dt = datetime.strptime(f"{dmy2} {hhmm}", "%d.%m.%y %H:%M")
            return dt
        except Exception:
            pass
    return None

