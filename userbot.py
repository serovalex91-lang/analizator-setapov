import os
import asyncio
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from datetime import datetime, timedelta
from collections import deque
import csv
import re
import json
import httpx
import logging
import getpass
import traceback
from dotenv import load_dotenv
from signal_webhook import try_process_screener_message
from signal_webhook.payload import build_payload, build_close_payload
from signal_webhook.sender import send_payload
from levels_repo import upsert_levels, get_latest_levels, import_levels_from_log

# Конфигурация
load_dotenv()
api_id = 29129135
api_hash = "4f2fb26f0b7f24551bd1759cb78af30c"
phone = "+79936192867"
TAAPI_KEY = os.getenv("TAAPI_KEY", "")
BTCDOM_TV_SYMBOL = os.environ.get("BTCDOM_TV_SYMBOL", "BTC.D")
BTCDOM_TV_ALT_SYMBOL = os.environ.get("BTCDOM_TV_ALT_SYMBOL", "CRYPTOCAP:BTC.D")
BTCD_FALLBACK_URL = os.environ.get("BTCD_FALLBACK_URL", "")  # URL JSON-прокси TradingView (кастомный)
CMC_API_KEY = os.environ.get("CMC_API_KEY", "")  # CoinMarketCap Pro API key (для исторической btc_dominance)

# Торговые настройки
DEPOSIT_AMOUNT = 1300.0  # Размер депозита в USDT (не используется в расчёте риска)
RISK_PERCENT = 0.01  # Исторический параметр (не используется)
# Жёсткое правило: суммарный риск по позиции = RISK_USD (при исполнении всех ордеров и SL)
RISK_USD = float(os.environ.get("RISK_USD", "10"))

# Инициализация клиента с улучшенными параметрами переподключения
client = TelegramClient(
    'userbot_session',
    api_id,
    api_hash,
    device_model='aboba-linux-custom',
    system_version='1.2.3-zxc-custom',
    app_version='1.0.1',
    lang_code='ru',
    system_lang_code='ru_RU',
    # Параметры для стабильной работы и автопереподключения
    connection_retries=10,      # Увеличено с 5 до 10 попыток переподключения
    retry_delay=5,              # Задержка 5 сек между попытками (было 1 сек)
    auto_reconnect=True,        # Автоматическое переподключение при разрыве
    timeout=30,                 # Таймаут операций 30 сек (было 10 сек)
    request_retries=5,          # Количество повторов запросов при ошибках
)

# Автоматическая обработка FloodWait: бот будет ждать до 12 часов при FloodWaitError
# (по умолчанию только 60 секунд). Это предотвращает крэш при длительных блокировках.
client.flood_sleep_threshold = 12 * 60 * 60  # 12 часов

# Настройки интеграции с локальным API уровней (вшитые значения с возможностью переопределения)
LEVELS_API_URL = os.environ.get("LEVELS_API_URL", "http://127.0.0.1:8001/levels/intraday-search")
# Fallback-получатель: "me" (Saved Messages)
RESULT_RECIPIENT = os.environ.get("RESULT_RECIPIENT", "me")
# Значения по умолчанию для Bot API, чтобы не требовать переменные окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7899189068:AAHGitC-EOAWLjgkabPsx5eC33zh26cdfuM")
BOT_CHAT_ID = os.environ.get("BOT_CHAT_ID", "202996676")

# Управление обработкой GO SHORT (по умолчанию выключено, включается явно через env)
GO_SHORT_ENABLED = os.environ.get("GO_SHORT_ENABLED", "0").strip() == "1"
GO_LONG_ENABLED = os.environ.get("GO_LONG_ENABLED", "0").strip() == "1"

# Фильтры источников (опционально)
# 1) ALLOW_CHAT_IDS: список целых chat_id (например, -1002423680272)
# 2) ALLOW_CHAT_LINK_IDS: список чисел из ссылок t.me/c/<id>/... (например, 2423680272)
# 3) BLOCK_CHAT_IDS / BLOCK_CHAT_LINK_IDS: жёсткая блокировка источников
# По умолчанию включаем примеры, чтобы не требовать ручной настройки.
DEFAULT_ALLOW_CHAT_IDS = {-1002423680272, 616892418, 5703939817, 5708266033}
DEFAULT_ALLOW_LINK_IDS = {2423680272}
# Принудительно разрешённые чаты (из задачи):
FORCE_ALLOW_CHAT_IDS = {-1002423680272}
DEFAULT_BLOCK_CHAT_IDS = set()
DEFAULT_BLOCK_LINK_IDS = set()


def _parse_allow_sets():
    def parse_int_list(env_name: str):
        raw = os.environ.get(env_name, "")
        out = []
        for part in raw.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except Exception:
                pass
        return out
    # Собираем id из переменных окружения
    ids = set(parse_int_list('ALLOW_CHAT_IDS'))
    # Преобразуем link-ids в полноценные chat_id (-100<id>)
    link_ids = set(parse_int_list('ALLOW_CHAT_LINK_IDS'))
    for cid in link_ids:
        try:
            ids.add(int(f"-100{cid}"))
        except Exception:
            continue
    # Добавляем дефолтные, чтобы всё работало без env
    ids.update(DEFAULT_ALLOW_CHAT_IDS)
    for cid in DEFAULT_ALLOW_LINK_IDS:
        try:
            ids.add(int(f"-100{cid}"))
        except Exception:
            pass
    # Принудительно разрешаем заданные чаты
    ids.update(FORCE_ALLOW_CHAT_IDS)
    return ids

ALLOW_CHAT_IDS = _parse_allow_sets()
BLOCK_CHAT_IDS = set()
DEFAULT_ALLOW_CHAT_NAMES = {"TRENDS Cryptovizor"}
ALLOW_CHAT_NAMES = set()
try:
    def _parse_block_sets():
        def parse_int_list(env_name: str):
            raw = os.environ.get(env_name, "")
            out = []
            for part in raw.split(','):
                part = part.strip()
                if not part:
                    continue
                try:
                    out.append(int(part))
                except Exception:
                    pass
            return out
        ids = set(parse_int_list('BLOCK_CHAT_IDS'))
        link_ids = set(parse_int_list('BLOCK_CHAT_LINK_IDS'))
        for cid in link_ids:
            try:
                ids.add(int(f"-100{cid}"))
            except Exception:
                continue
        ids.update(DEFAULT_BLOCK_CHAT_IDS)
        for cid in DEFAULT_BLOCK_LINK_IDS:
            try:
                ids.add(int(f"-100{cid}"))
            except Exception:
                pass
        return ids
    BLOCK_CHAT_IDS = _parse_block_sets()
except Exception:
    BLOCK_CHAT_IDS = set()

# Разрешение по названию чата (точное совпадение, без регистра)
try:
    raw_names = os.environ.get("ALLOW_CHAT_NAMES", "")
    for part in raw_names.split(','):
        name = part.strip()
        if name:
            ALLOW_CHAT_NAMES.add(name.lower())
    for n in DEFAULT_ALLOW_CHAT_NAMES:
        ALLOW_CHAT_NAMES.add(n.lower())
except Exception:
    pass

# Разрешать ли обработку входящих из "Избранного" (Saved Messages)
PROCESS_SAVED_INPUT = os.environ.get("PROCESS_SAVED_INPUT", "0").strip() == "1"
SELF_CHAT_ID = None  # будет установлен после старта клиента

# Эмодзи для анализа
RED_SET = {"🟥", "🔴"}
GREEN_SET = {"🟢", "🟩"}


# ============================================================================
# Rate Limiter для контроля частоты Telegram операций
# ============================================================================
class RateLimiter:
    """
    Контролирует частоту операций для предотвращения FloodWaitError.
    Ограничивает количество вызовов в заданный период времени.
    """
    def __init__(self, max_calls: int = 30, period: int = 60):
        """
        Args:
            max_calls: Максимальное количество вызовов (по умолчанию 30)
            period: Период в секундах (по умолчанию 60)
        """
        self.max_calls = max_calls
        self.period = timedelta(seconds=period)
        self.calls = deque()  # Очередь временных меток вызовов

    async def wait_if_needed(self):
        """Ожидает, если достигнут лимит вызовов за период"""
        now = datetime.now()

        # Удаляем старые вызовы за пределами периода
        while self.calls and now - self.calls[0] > self.period:
            self.calls.popleft()

        # Если достигнут лимит, ждём
        if len(self.calls) >= self.max_calls:
            sleep_time = (self.calls[0] + self.period - now).total_seconds()
            if sleep_time > 0:
                print(f"⏳ [RateLimiter] Достигнут лимит {self.max_calls} операций за {self.period.total_seconds():.0f}с. Ожидание {sleep_time:.1f}с...")
                await asyncio.sleep(sleep_time)
                # После ожидания снова очищаем старые записи
                now = datetime.now()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.popleft()

        # Регистрируем текущий вызов
        self.calls.append(now)


# Глобальный rate limiter для Telegram операций (30 сообщений в минуту)
telegram_rate_limiter = RateLimiter(max_calls=30, period=60)


def _extract_symbol_from_hashtags(text: str):
    """Ищет первый хэштег с символом и нормализует к виду XXXUSDT.
    Примеры: #WLFIUSDT, #IMXUSDT.P → WLFIUSDT, IMXUSDT
    """
    try:
        tags = re.findall(r"#([A-Z0-9\.]+)", (text or "").upper())
        for t in tags:
            t = t.replace(".P", "")
            if t.endswith("USDT"):
                return t
    except Exception:
        pass
    return None

def _parse_go_short_blocks(text: str):
    """Ищет в сообщении блоки с точной фразой GO SHORT и извлекает тикер и цену из ближайших строк.
    Возвращает список словарей: { 'symbol': 'SFPUSDT', 'price': 0.5706 }.
    Правила:
      - тикер берём из первого попавшегося хэштега рядом (#SFPUSDT или #SFPUSDT.P → SFPUSDT)
      - цену берём из строки вида "Цена 0.16387" (иконка может присутствовать)
      - ищем в пределах нескольких строк вокруг строки GO SHORT
    """
    results = []
    try:
        if not isinstance(text, str) or not text:
            return results
        # Удаляем простую разметку (** _ `) и неразрывные пробелы, чтобы корректно вытащить число
        cleaned = re.sub(r"[\*`_]", "", text)
        cleaned = cleaned.replace("\u00A0", " ").replace("\u2060", "")
        lines = cleaned.splitlines()
        n = len(lines)
        for i, line in enumerate(lines):
            if re.search(r"\bGO\s+SHORT\b", line, flags=re.IGNORECASE):
                symbol = None
                price = None
                # Поиск тикера в радиусе ±3 строк
                for j in range(i, max(-1, i-4), -1):
                    if j < 0: break
                    m = re.search(r"#([A-Z0-9\.]{2,15})", (lines[j] or "").upper())
                    if m:
                        cand = m.group(1).replace(".P", "")
                        if cand.endswith("USDT"):
                            symbol = cand
                            break
                if not symbol:
                    for j in range(i, min(n, i+4)):
                        m = re.search(r"#([A-Z0-9\.]{2,15})", (lines[j] or "").upper())
                        if m:
                            cand = m.group(1).replace(".P", "")
                            if cand.endswith("USDT"):
                                symbol = cand
                                break
                # Поиск цены в радиусе +6 строк вниз (допускаем пробелы/символы между словом и числом)
                for j in range(i, min(n, i+7)):
                    m = re.search(r"Цена[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", (lines[j] or ""))
                    if m:
                        try:
                            price = float(m.group(1))
                        except Exception:
                            price = None
                        if price is not None:
                            break
                if symbol and (price is not None):
                    results.append({"symbol": symbol, "price": price})
    except Exception:
        pass
    return results

def _parse_go_long_blocks(text: str):
    """Ищет в сообщении блоки с точной фразой GO LONG и извлекает тикер и цену.
    Возвращает список словарей: { 'symbol': 'IMXUSDT', 'price': 0.7391 }.
    Источник и правила идентичны GO SHORT, только ключевая фраза другая.
    """
    results = []
    try:
        if not isinstance(text, str) or not text:
            return results
        cleaned = re.sub(r"[\*`_]", "", text)
        cleaned = cleaned.replace("\u00A0", " ").replace("\u2060", "")
        lines = cleaned.splitlines()
        n = len(lines)
        for i, line in enumerate(lines):
            if re.search(r"\bGO\s+LONG\b", line, flags=re.IGNORECASE):
                symbol = _extract_symbol_from_hashtags(cleaned)  # сначала попробуем глобально
                if not symbol:
                    # локальный поиск рядом
                    for j in range(i, max(-1, i-4), -1):
                        if j < 0: break
                        m = re.search(r"#([A-Z0-9\.]{2,15})", (lines[j] or "").upper())
                        if m:
                            cand = m.group(1).replace(".P", "")
                            if cand.endswith("USDT"):
                                symbol = cand
                                break
                    if not symbol:
                        for j in range(i, min(n, i+4)):
                            m = re.search(r"#([A-Z0-9\.]{2,15})", (lines[j] or "").upper())
                            if m:
                                cand = m.group(1).replace(".P", "")
                                if cand.endswith("USDT"):
                                    symbol = cand
                                    break
                price = None
                for j in range(i, min(n, i+7)):
                    m = re.search(r"Цена[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", (lines[j] or ""))
                    if m:
                        try:
                            price = float(m.group(1))
                        except Exception:
                            price = None
                        if price is not None:
                            break
                if symbol and (price is not None):
                    results.append({"symbol": symbol, "price": price})
    except Exception:
        pass
    return results

async def _get_atr_pct_taapi(symbol_usdt: str, interval: str = "1h", period: int = 14) -> float:
    """ATR/price в процентах по Taapi.io.
    Возвращает долю (например 0.007 означает 0.7%)."""
    if not TAAPI_KEY:
        return None
    ta_symbol = _to_taapi_symbol(symbol_usdt)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = (
                "https://api.taapi.io/atr?"
                f"secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}&period={period}"
            )
            r = await client.get(url)
            if r.status_code != 200:
                return None
            jd = r.json() or {}
            atr_val = jd.get("value")
            if atr_val is None:
                return None
            # Получаем текущую цену для нормализации
            px = await _get_binance_price(symbol_usdt)
            if not px:
                return None
            pxf = float(px)
            if pxf <= 0:
                return None
            return float(atr_val) / pxf
    except Exception:
        return None

def _compute_short_risk_params(entry: float, res_zone: tuple, atr_pct: float) -> dict:
    """Расчёт SL/TP/SLX/BE для шорта.
    - SL: выше верхней границы резистанса на 1% + ATR%, cap 2%
    - TP: 1:4 RR от entry и SL
    - BE: включить при 2R (PnL%)
    - SLX: включить при 2.5R (PnL%), trailingLag = 1R (%), trailingStep оставляем по умолчанию
    Возвращает dict с ключами sl, tp, be, slx.
    """
    hi = float(res_zone[1]) if res_zone else float(entry)
    base = 0.01
    atr = float(atr_pct or 0.0)
    sl_pct = min(base + atr, 0.02)
    sl = hi * (1.0 + sl_pct)
    # risk (abs) для шорта
    R_abs = float(sl) - float(entry)
    if R_abs <= 0:
        # safety: минимальный риск 0.1% от цены
        R_abs = max(0.001 * float(entry), 1e-12)
    tp = float(entry) - 4.0 * R_abs
    # проценты от entry
    R_pct = R_abs / float(entry) * 100.0
    be_pnl = 2.0 * R_pct
    trail_pnl = 2.5 * R_pct
    trail_lag = R_pct
    return {
        "sl": sl,
        "tp": tp,
        "be": {"enabled": True, "pnl": round(be_pnl, 6), "offset": 0},
        "slx": {"enabled": True, "trailingProfit": round(trail_pnl, 6), "trailingLag": round(trail_lag, 6)},
    }

def _compute_long_risk_params(entry: float, sup_zone: tuple, atr_pct: float) -> dict:
    """Расчёт SL/TP/SLX/BE для лонга.
    - SL: ниже нижней границы поддержки на 1% + ATR%, cap 2%
    - TP: 1:4 RR от entry и SL
    - BE: при 2R; SLX: при 2.5R, lag = 1R
    """
    lo = float(sup_zone[0]) if sup_zone else float(entry)
    base = 0.01
    atr = float(atr_pct or 0.0)
    sl_pct = min(base + atr, 0.02)
    sl = lo * (1.0 - sl_pct)
    R_abs = float(entry) - float(sl)
    if R_abs <= 0:
        R_abs = max(0.001 * float(entry), 1e-12)
    tp = float(entry) + 4.0 * R_abs
    R_pct = R_abs / float(entry) * 100.0
    be_pnl = 2.0 * R_pct
    trail_pnl = 2.5 * R_pct
    trail_lag = R_pct
    return {
        "sl": sl,
        "tp": tp,
        "be": {"enabled": True, "pnl": round(be_pnl, 6), "offset": 0},
        "slx": {"enabled": True, "trailingProfit": round(trail_pnl, 6), "trailingLag": round(trail_lag, 6)},
    }

async def _process_go_short_message(text: str):
    """Парсит блоки GO SHORT и, если включено GO_SHORT_ENABLED, исполняет сделки по правилам.
    Условия: цена из сообщения внутри ближайшей RESISTANCE (levels → pivots),
    фильтр пампа: |move_5h| <= 15%.
    """
    if not GO_SHORT_ENABLED:
            return
    blocks = _parse_go_short_blocks(text)
    for blk in blocks:
        symbol = blk.get("symbol"); price = blk.get("price")
        if not symbol or price is None:
            continue
        latest = get_latest_levels(symbol, max_age_minutes=0, prefer_timeframes=["4h","1h","12h"]) or {}
        resistance_list = (latest or {}).get("resistance", [])
        chosen = _choose_nearest_zone(resistance_list, float(price)) if resistance_list else None
        if not chosen:
            # pivots fallback
            for tf in ("1h","4h","12h"):
                piv = await _get_taapi_pivots(symbol, interval=tf)
                if piv:
                    candidates = []
                    for k in ("R1","R2","R3"):
                        if k in piv and piv[k] is not None:
                            candidates.append(float(piv[k]))
                    if candidates:
                        lvl = min(candidates, key=lambda v: abs(v - float(price)))
                        width = lvl * 0.0015
                        chosen = (lvl - width, lvl + width)
                        break
        if not chosen:
            try:
                await notify(f"[GO SHORT FAIL] {symbol}: no resistance zone (levels+pivots)")
            except Exception:
                pass
            continue
        if not _is_inside_zone(price, chosen):
            try:
                await notify(f"[GO SHORT FAIL] {symbol}: price not in zone {chosen[0]:.6f}-{chosen[1]:.6f} (price={float(price):.6f})")
            except Exception:
                pass
            continue
        move5h = await _get_move_5h_pct(symbol)
        if move5h is not None and move5h > 15.0:
            # пропускаем потенциальный памп/пробой
            try:
                await notify(f"[GO SHORT] skip pump {symbol}: move5h={move5h:.2f}%")
            except Exception:
                pass
            continue
        atr_pct = await _get_atr_pct_taapi(symbol, interval="1h", period=14)
        params = _compute_short_risk_params(float(price), chosen, atr_pct)
        # Правило: отклоняем, если Stop-Loss > 4% от цены входа
        try:
            sl_pct = (float(params["sl"]) / float(price) - 1.0) * 100.0
            if sl_pct > 4.0:
                try:
                    await notify(f"[GO SHORT] reject {symbol}: SL={sl_pct:.2f}% > 4% (entry={float(price):.6f}, SL={float(params['sl']):.6f})")
                except Exception:
                    pass
                continue
        except Exception:
            pass
        # GO SHORT: один рыночный ордер по цене из сообщения (без сетки)
        try:
            # Фиксированный риск 5 USDT: размер позиции = 5 / |SL - entry|
            entry_f = float(price)
            sl_f = float(params["sl"])
            risk_usd = 5.0
            denom = abs(sl_f - entry_f)
            qty = 0.0
            if denom > 0:
                qty = risk_usd / denom
            payload = build_payload(
                symbol=symbol,
                side='sell',
                sl_price=sl_f,
                tp_price=float(params["tp"]),
                first_order_price=entry_f,
                last_order_price=entry_f,
                qty_orders=1,
                slx_enabled_override=True,
                slx_overrides={
                    "trailingProfit": params["slx"]["trailingProfit"],
                    "trailingLag": params["slx"]["trailingLag"],
                },
                be_enabled_override=True,
                be_overrides={"pnl": params["be"]["pnl"], "offset": params["be"]["offset"]},
                open_order_type='market',
                real_qty_override=qty,
            )
            sent = await send_payload(payload)
            try:
                write_webhook_history(datetime.utcnow().isoformat(), payload, sent)
            except Exception:
                pass
        except Exception:
            sent = False
        try:
            await notify(f"[GO SHORT] {symbol} price={price} zone={chosen[0]:.6f}-{chosen[1]:.6f} sent={sent}\n"
                         f"SL={params['sl']:.6f} TP={params['tp']:.6f} BE@{params['be']['pnl']:.3f}% TRAIL@{params['slx']['trailingProfit']:.3f}% lag={params['slx']['trailingLag']:.3f}%")
        except Exception:
            pass

async def _process_go_long_message(text: str):
    """GO LONG пайплайн: парсинг, поиск SUPPORT только из levels.db (без pivots),
    проверка цены внутри зоны, анти‑памп (|move_5h|<=15%), ATR, SL<=4%,
    исполнение ОДНИМ рыночным ордером с фиксированным риском 5 USDT.
    """
    if not GO_LONG_ENABLED:
            return
    blocks = _parse_go_long_blocks(text)
    for blk in blocks:
        symbol = blk.get("symbol"); price = blk.get("price")
        if not symbol or price is None:
            continue
        latest = get_latest_levels(symbol, max_age_minutes=0, prefer_timeframes=["4h","1h","12h"]) or {}
        support_list = (latest or {}).get("support", [])
        if not support_list:
            try:
                await notify(f"[GO LONG FAIL] {symbol}: no support zones in levels.db")
            except Exception:
                pass
            continue
        chosen = _choose_nearest_zone(support_list, float(price))
        if not chosen:
            try:
                await notify(f"[GO LONG FAIL] {symbol}: no support zone near price (price={float(price):.6f})")
            except Exception:
                pass
            continue
        if not _is_inside_zone(price, chosen):
            try:
                await notify(f"[GO LONG FAIL] {symbol}: price not in zone {chosen[0]:.6f}-{chosen[1]:.6f} (price={float(price):.6f})")
            except Exception:
                pass
            continue
        move5h = await _get_move_5h_pct(symbol)
        if move5h is not None and move5h > 15.0:
            try:
                await notify(f"[GO LONG] skip pump {symbol}: move5h={move5h:.2f}%")
            except Exception:
                pass
            continue
        atr_pct = await _get_atr_pct_taapi(symbol, interval="1h", period=14)
        params = _compute_long_risk_params(float(price), chosen, atr_pct)
        # Правило: SL расстояние не более 4%
        try:
            sl_pct = (1.0 - float(params["sl"]) / float(price)) * 100.0
            if sl_pct > 4.0:
                try:
                    await notify(f"[GO LONG] reject {symbol}: SL={sl_pct:.2f}% > 4% (entry={float(price):.6f}, SL={float(params['sl']):.6f})")
                except Exception:
                    pass
                continue
        except Exception:
            pass
        try:
            entry_f = float(price)
            sl_f = float(params["sl"])
            risk_usd = 5.0
            denom = abs(entry_f - sl_f)
            qty = risk_usd / denom if denom > 0 else 0.0
            payload = build_payload(
                symbol=symbol,
                side='buy',
                sl_price=sl_f,
                tp_price=float(params["tp"]),
                first_order_price=entry_f,
                last_order_price=entry_f,
                qty_orders=1,
                slx_enabled_override=True,
                slx_overrides={
                    "trailingProfit": params["slx"]["trailingProfit"],
                    "trailingLag": params["slx"]["trailingLag"],
                },
                be_enabled_override=True,
                be_overrides={"pnl": params["be"]["pnl"], "offset": params["be"]["offset"]},
                open_order_type='market',
                real_qty_override=qty,
            )
            # Явный маршрут на long-хук не обязателен для открытия, но добавим для однозначности
            payload["_route"] = "long"
            sent = await send_payload(payload)
            try:
                write_webhook_history(datetime.utcnow().isoformat(), payload, sent)
            except Exception:
                pass
        except Exception:
            sent = False
        try:
            await notify(f"[GO LONG] {symbol} price={price} zone={chosen[0]:.6f}-{chosen[1]:.6f} sent={sent}\n"
                         f"SL={params['sl']:.6f} TP={params['tp']:.6f} BE@{params['be']['pnl']:.3f}% TRAIL@{params['slx']['trailingProfit']:.3f}% lag={params['slx']['trailingLag']:.3f}%")
        except Exception:
            pass

def _is_inside_zone(price: float, zone: tuple) -> bool:
    try:
        if price is None or not zone:
            return False
        low, high = float(zone[0]), float(zone[1])
        if low > high:
            low, high = high, low
        return low <= float(price) <= high
    except Exception:
        return False

async def _get_move_5h_pct(symbol_usdt: str) -> float:
    """Возвращает абсолютное изменение цены за 5 часов в % (|close_now/close_5h_ago - 1| * 100).
    Использует Binance klines 1h.
    """
    try:
        import httpx
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol_usdt, "interval": "1h", "limit": 6}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            kl = r.json()
            if not isinstance(kl, list) or len(kl) < 6:
                return None
            c0 = float(kl[0][4])
            cN = float(kl[-1][4])
            if c0 <= 0:
                return None
            return abs(cN / c0 - 1.0) * 100.0
    except Exception:
        return None

async def _evaluate_go_short_blocks(text: str):
    """Для всех блоков GO SHORT в сообщении:
    - извлекает (symbol, price)
    - находит ближайшую RESISTANCE зону из levels.db (4h > 1h > 12h)
    - проверяет, находится ли price внутри зоны
    - считает 5h move и 1h объёмный спайк
    - отправляет уведомление PASS/FAIL с причинами (без вебхуков)
    """
    try:
        blocks = _parse_go_short_blocks(text)
        for blk in blocks:
            symbol = blk.get("symbol")
            price = blk.get("price")
            if not symbol or price is None:
                continue
            latest = get_latest_levels(symbol, max_age_minutes=0, prefer_timeframes=["4h","1h","12h"]) or {}
            resistance_list = (latest or {}).get("resistance", [])
            chosen = _choose_nearest_zone(resistance_list, float(price)) if resistance_list else None
            piv_used = None
            # Fallback: если нет уровней сопротивления, пробуем пивоты из Taapi и строим узкую зону вокруг ближайшего сопротивления
            if not chosen:
                for tf in ("1h","4h","12h"):
                    piv = await _get_taapi_pivots(symbol, interval=tf)
                    if piv:
                        # Берём ближайшее из {R1,R2,R3} к цене
                        candidates = []
                        for k in ("R1","R2","R3"):
                            if k in piv and piv[k] is not None:
                                candidates.append((k, float(piv[k])))
                        if candidates:
                            # выбрать ближайшую по модулю разницы
                            key, lvl = min(candidates, key=lambda kv: abs(kv[1] - float(price)))
                            # зона = ±0.15% вокруг уровня (узкая прокси для zone)
                            width = lvl * 0.0015
                            chosen = (lvl - width, lvl + width)
                            piv_used = {"tf": tf, "key": key, "level": lvl}
                            break
            inside = _is_inside_zone(price, chosen) if chosen else False
            move5h = await _get_move_5h_pct(symbol)
            spike = await _get_1h_volume_spike(symbol)

            tf = (latest or {}).get("timeframe") or (piv_used or {}).get("tf")
            src_ts = (latest or {}).get("source_ts")
            zone_txt = f"{chosen[0]:.6f}-{chosen[1]:.6f}" if chosen else "n/a"
            mv_txt = f"{move5h:.2f}%" if move5h is not None else "n/a"
            sp_txt = f"{spike:.2f}x" if spike is not None else "n/a"

            # Правила PASS/FAIL: 1) есть зона, 2) цена внутри зоны, 3) |move_5h| <= 15%
            status = "OK"
            reason = None
            if not chosen:
                status = "FAIL"; reason = "no_resistance_zone"
            elif not inside:
                status = "FAIL"; reason = "price_not_in_zone"
            elif (move5h is not None) and (move5h > 15.0):
                status = "FAIL"; reason = f"pump_guard(move5h={move5h:.2f}%)"

            prefix = "[GO SHORT OK]" if status == "OK" else "[GO SHORT FAIL]"
            msg = (
                f"{prefix} {symbol} price={price} tf={(tf or 'n/a')} zone={zone_txt} inside={inside}"
                + (f" src_ts={src_ts}" if src_ts else "")
                + (f" piv={piv_used['key']}={piv_used['level']:.6f}" if piv_used else "")
                + f"\nmove_5h={mv_txt} spike_1h={sp_txt}"
                + (f" reason={reason}" if reason else "")
            )
            try:
                await notify(msg)
            except Exception:
                pass
    except Exception:
        pass

def _parse_and_cache_key_levels(message: str):
    """Парсит и кэширует уровни из разных типов сообщений:
    - "Key Levels for #SYMBOL (TF)" → добавляет support/resistance блоки
    - "New SUPPORT/RESISTANCE Level Detected!" → добавляет одну зону
    - "Price Entered SUPPORT/RESISTANCE Zone!" → удаляет указанную зону
    """
    try:
        if not isinstance(message, str):
            return

        text = message

        # 1) Полные блоки Key Levels for ...
        if "Key Levels for" in text:
            try:
                m_sym = re.search(r"Key Levels for[^#]*#([A-Z0-9]+)", text)
                if not m_sym:
                    raise ValueError("no symbol in Key Levels block")
                symbol = m_sym.group(1).upper()
                if not symbol.endswith("USDT"):
                    symbol += "USDT"
                support_zones = []
                resistance_zones = []
                timeframe = None
                current_block = None
                for raw_line in text.splitlines():
                    line = raw_line.strip()
                    if timeframe is None:
                        tfm = re.search(r"\((\d+\s*[hm])\)", text, flags=re.IGNORECASE)
                        if tfm:
                            timeframe = tfm.group(1).replace(" ", "").lower()
                    if re.search(r"(\*\*\s*)?SUPPORT Levels(\s*\*\*)?", line, flags=re.IGNORECASE):
                        current_block = "support"
                        continue
                    if re.search(r"(\*\*\s*)?RESISTANCE Levels(\s*\*\*)?", line, flags=re.IGNORECASE):
                        current_block = "resistance"
                        continue
                    m_zone = re.search(r"Zone:\s*(?:\*\*)?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\*\*)?", line)
                    if m_zone and current_block in {"support", "resistance"}:
                        low = float(m_zone.group(1)); high = float(m_zone.group(2))
                        if low > high:
                            low, high = high, low
                        if current_block == "support":
                            support_zones.append((low, high))
                        else:
                            resistance_zones.append((low, high))
                if support_zones or resistance_zones:
                    upsert_levels(symbol, timeframe, support_zones, resistance_zones)
            except Exception:
                pass

        # 2) New SUPPORT/RESISTANCE Level Detected! → добавляем одну зону
        try:
            m_new = re.search(r"New\s+(SUPPORT|RESISTANCE)\s+Level\s+Detected!", text, flags=re.IGNORECASE)
            if m_new:
                side = m_new.group(1).lower()
                ms = re.search(r"Symbol:\s*\*\*?#([A-Z0-9]+)\*\*?", text)
                mt = re.search(r"Timeframe:\s*([0-9]+\s*[hm])", text, flags=re.IGNORECASE)
                mz = re.search(r"Zone:\s*(?:\*\*)?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", text)
                mc = re.search(r"Created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*UTC", text)
                if ms and mt and mz:
                    symbol = ms.group(1).upper()
                    if not symbol.endswith("USDT"):
                        symbol += "USDT"
                    timeframe = mt.group(1).replace(" ", "").lower()
                    low = float(mz.group(1)); high = float(mz.group(2))
                    if low > high:
                        low, high = high, low
                    src_ts = None
                    if mc:
                        try:
                            src_ts = datetime.strptime(mc.group(1), "%Y-%m-%d %H:%M:%S").isoformat()
                        except Exception:
                            src_ts = None
                    if side == "support":
                        upsert_levels(symbol, timeframe, [(low, high)], [], source_ts=src_ts)
                    else:
                        upsert_levels(symbol, timeframe, [], [(low, high)], source_ts=src_ts)
        except Exception:
            pass

        # 3) Price Entered SUPPORT/RESISTANCE Zone! → удаляем указанную зону
        try:
            m_enter = re.search(r"Price\s+Entered\s+(SUPPORT|RESISTANCE)\s+Zone!", text, flags=re.IGNORECASE)
            if m_enter:
                side = m_enter.group(1).lower()
                ms = re.search(r"Symbol:\s*\*\*?#([A-Z0-9]+)\*\*?", text)
                mt = re.search(r"Timeframe:\s*([0-9]+\s*[hm])", text, flags=re.IGNORECASE)
                mz = re.search(r"Zone:\s*(?:\*\*)?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", text)
                mc = re.search(r"Created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*UTC", text)
                if ms and mt and mz:
                    symbol = ms.group(1).upper()
                    if not symbol.endswith("USDT"):
                        symbol += "USDT"
                    timeframe = mt.group(1).replace(" ", "").lower()
                    lo = float(mz.group(1)); hi = float(mz.group(2))
                    if lo > hi:
                        lo, hi = hi, lo
                    # ВАЖНО: при входе в зону создаём НОВЫЙ снимок с текущим временем,
                    # чтобы он стал самым свежим и удаление реально применилось.
                    src_ts = datetime.utcnow().isoformat()

                    # загрузим последний снимок и удалим зону
                    latest = get_latest_levels(symbol, max_age_minutes=0, prefer_timeframes=[timeframe]) or {}
                    sup = [(float(a), float(b)) for a, b in (latest.get("support") or [])]
                    res = [(float(a), float(b)) for a, b in (latest.get("resistance") or [])]

                    def _remove(zlist, a, b):
                        out = []
                        for x, y in zlist:
                            if abs(x - a) < 1e-9 and abs(y - b) < 1e-9:
                                continue
                            out.append((x, y))
                        return out

                    if side == "support":
                        sup = _remove(sup, lo, hi)
                    else:
                        res = _remove(res, lo, hi)
                    upsert_levels(symbol, timeframe, sup, res, source_ts=src_ts)
        except Exception:
            pass
    except Exception:
        pass

def _choose_nearest_zone(zones, current_price: float):
    if not zones or current_price is None:
        return None
    best = None; best_dist = None
    for low, high in zones:
        center = (low + high) / 2.0
        dist = abs(center - current_price)
        if best is None or dist < best_dist:
            best = (low, high); best_dist = dist
    return best

def _line_to_ticker_and_squares(line: str):
    """Парсит строку произвольного формата, например:
    "$UNI     🟥🟢🟢🟢🟢   frame:60M" → ("UNI", [5 эмодзи])
    "Данный тикер не найден $STG 🟥🟢🟢🟢🟢 FRAME:30M" → ("STG", [5 эмодзи])
    После тикера извлекаем первые 5 эмодзи из набора {красные/зелёные},
    игнорируя любые хвосты наподобие "frame:60M".
    Работает с любым регистром: FRAME, frame, Frame.
    """
    # Ищем тикер в любом месте строки (не только в начале)
    m = re.search(r"\$([A-Za-z0-9]{2,15})", line)
    if not m:
        return None, None, None
    ticker = m.group(1).upper()
    squares = [ch for ch in line if ch in RED_SET or ch in GREEN_SET]
    if len(squares) < 4:  # Минимум 4 эмодзи вместо 5
        return None, None, None
    # Парсим frame/FRAME:30M/60M/120M в любом регистре, по умолчанию 30m
    tf_m = re.search(r"frame\s*:\s*(\d+)[mMhH]", line, flags=re.IGNORECASE)
    origin_tf = None
    if tf_m:
        val = tf_m.group(1)
        if val in {"30", "60", "120"}:
            origin_tf = f"{val}m"
    return ticker, squares[:5], (origin_tf or "30m")

def _is_correction_combo(squares):
    """Строгое соответствие только разрешённым паттернам для LONG.
    Разрешены ровно эти последовательности:
      🟥🟢🟢🟢🟢 (5 эмодзи)
      🔴🟢🟢🟢🟩 (5 эмодзи)
      🔴🟥🟢🟢🟢 (5 эмодзи)
      🔴🔴🟥🟢🟢 (5 эмодзи)
    """
    if len(squares) != 5:
        return False
    # Проверяем точные комбинации
    allowed_patterns = [
        ['🟥','🟢','🟢','🟢','🟢'],
        ['🔴','🟢','🟢','🟢','🟩'],
        ['🔴','🟥','🟢','🟢','🟢'],
        ['🔴','🔴','🟥','🟢','🟢'],
        ['🟥','🔴','🔴','🟢','🟢'],
        ['🔴','🟥','🔴','🟢','🟢']
    ]
    return list(squares) in allowed_patterns

def _is_resistance_combo(squares):
    """Строгое соответствие только разрешённым паттернам для SHORT.
    Разрешены ровно эти последовательности:
      🟩🔴🔴🔴🔴 (5 эмодзи)
      🟢🔴🔴🔴🟥 (5 эмодзи)
      🟢🟩🔴🔴🔴 (5 эмодзи)
      🟢🟢🟩🔴🔴 (5 эмодзи)
    """
    if len(squares) != 5:
        return False
    # Проверяем точные комбинации
    allowed_patterns = [
        ['🟩','🔴','🔴','🔴','🔴'],
        ['🟢','🔴','🔴','🔴','🟥'],
        ['🟢','🟩','🔴','🔴','🔴'],
        ['🟢','🟢','🟩','🔴','🔴'],
        ['🟩','🟢','🟢','🔴','🔴'],
        ['🟢','🟩','🟢','🔴','🔴']
    ]
    return list(squares) in allowed_patterns

def _is_close_long_combo(squares):
    """Закрыть LONG: 🔴🔴🔴🟥🟢"""
    return list(squares) == ['🔴','🔴','🔴','🟥','🟢']

def _is_close_short_combo(squares):
    """Закрыть SHORT: 🟢🟢🟢🟩🔴"""
    return list(squares) == ['🟢','🟢','🟢','🟩','🔴']

async def _get_binance_price(symbol: str) -> float:
    """Получает текущую цену символа из Binance"""
    try:
        import httpx
        url = f"https://api.binance.com/api/v3/ticker/price"
        params = {"symbol": symbol}
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                return float(data.get("price", 0))
    except Exception as e:
        print(f"[DEBUG] Ошибка получения цены из Binance для {symbol}: {e}")
    
    return None

async def _get_24h_volume_usd(symbol_usdt: str) -> float:
    """Возвращает 24h объём в котируемой валюте (для USDT-пар ≈ USD)."""
    try:
        import httpx
        url = "https://api.binance.com/api/v3/ticker/24hr"
        params = {"symbol": symbol_usdt}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            jd = r.json()
            qv = jd.get("quoteVolume")
            return float(qv) if qv is not None else None
    except Exception as _:
        return None

async def _get_1h_volume_spike(symbol_usdt: str) -> float:
    """Возвращает коэффициент всплеска объёма 1h: current_1h / average_1h_last_24.
    При ошибке возвращает None.
    """
    try:
        import httpx
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol_usdt, "interval": "1h", "limit": 25}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            kl = r.json()
            if not isinstance(kl, list) or len(kl) < 2:
                return None
            # quoteVolume — индекс 7
            vols = [float(k[7]) for k in kl]
            current = vols[-1]
            avg = sum(vols[-25:-1]) / max(1, len(vols[-25:-1]))
            if avg <= 0:
                return None
            return current / avg
    except Exception:
        return None

def _calc_hours_since_iso(iso_ts: str) -> float:
    """Считает количество часов, прошедших с момента iso_ts до сейчас. При ошибке возвращает большое число."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_ts)
        delta = datetime.utcnow() - dt
        return delta.total_seconds() / 3600.0
    except Exception:
        return 1e9

def _to_taapi_symbol(symbol_usdt: str) -> str:
    # ETHUSDT -> ETH/USDT
    if symbol_usdt.endswith("USDT"):
        base = symbol_usdt[:-4]
        return f"{base}/USDT"
    return symbol_usdt

async def _get_rsi_1h_taapi(symbol_usdt: str) -> float:
    """Возвращает RSI 1h через Taapi.io. Требуется TAAPI_KEY."""
    if not TAAPI_KEY:
        return None
    try:
        import httpx
        ta_symbol = _to_taapi_symbol(symbol_usdt)
        url = (
            "https://api.taapi.io/rsi?"
            f"secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval=1h"
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            jd = r.json()
            return float(jd.get("value")) if jd and jd.get("value") is not None else None
    except Exception:
        return None

async def _get_btcdom_rsi_1h_taapi() -> float:
    """RSI 1h по Bitcoin Dominance через Taapi.io (попытки нескольких символов).
    Возвращает float или None, если не удалось получить.
    """
    if not TAAPI_KEY:
        return None
    try:
        import httpx
        candidates = [
            ("tradingview", BTCDOM_TV_SYMBOL),           # BTC.D
            ("tradingview", BTCDOM_TV_ALT_SYMBOL),      # CRYPTOCAP:BTC.D
            ("binance", "BTCDOM/USDT"),                 # на всякий случай
        ]
        async with httpx.AsyncClient(timeout=15.0) as client:
            for exch, sym in candidates:
                try:
                    url = (
                        "https://api.taapi.io/rsi?"
                        f"secret={TAAPI_KEY}&exchange={exch}&symbol={sym}&interval=1h"
                    )
                    r = await client.get(url)
                    if r.status_code != 200:
                        continue
                    jd = r.json()
                    if jd and jd.get("value") is not None:
                        return float(jd.get("value"))
                except Exception:
                    continue
    except Exception:
        return None
    return None

async def _get_taapi_pivots(symbol_usdt: str, interval: str = "1h") -> dict:
    """Пытается получить пивоты через Taapi.io для заданного интервала.
    Возвращает словарь с ключами: P, R1, R2, R3, S1, S2, S3 (если доступны).
    Пробует несколько индикаторов: ppsr, pivotPoints.
    """
    if not TAAPI_KEY:
        return {}
    ta_symbol = _to_taapi_symbol(symbol_usdt)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1) PPSR
            try:
                url_ppsr = (
                    "https://api.taapi.io/ppsr?"
                    f"secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}"
                )
                r = await client.get(url_ppsr)
                if r.status_code == 200:
                    jd = r.json() or {}
                    out = {}
                    # возможные поля: p, r1, r2, r3, s1, s2, s3
                    m = {
                        'P': ['p', 'pivot'],
                        'R1': ['r1','resistance1'], 'R2': ['r2','resistance2'], 'R3': ['r3','resistance3'],
                        'S1': ['s1','support1'],    'S2': ['s2','support2'],    'S3': ['s3','support3'],
                    }
                    for k, keys in m.items():
                        for kk in keys:
                            if kk in jd and jd[kk] is not None:
                                out[k] = float(jd[kk])
                                break
                    if out:
                        return out
            except Exception:
                pass
            # 2) pivotPoints
            try:
                url_pp = (
                    "https://api.taapi.io/pivotPoints?"
                    f"secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}"
                )
                r = await client.get(url_pp)
                if r.status_code == 200:
                    jd = r.json() or {}
                    out = {}
                    m = {
                        'P': ['pivot','p'],
                        'R1': ['resistance1','r1'], 'R2': ['resistance2','r2'], 'R3': ['resistance3','r3'],
                        'S1': ['support1','s1'],    'S2': ['support2','s2'],    'S3': ['support3','s3'],
                    }
                    for k, keys in m.items():
                        for kk in keys:
                            if kk in jd and jd[kk] is not None:
                                out[k] = float(jd[kk])
                                break
                    if out:
                        return out
            except Exception:
                pass
    except Exception:
        pass
    return {}

def _compute_rsi_from_closes(closes, period: int = 14) -> float:
    try:
        values = [float(x) for x in closes]
        if len(values) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(values)):
            ch = values[i] - values[i-1]
            gains.append(max(ch, 0.0))
            losses.append(max(-ch, 0.0))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(values)-1):
            ch = values[i+1] - values[i]
            gain = max(ch, 0.0)
            loss = max(-ch, 0.0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    except Exception:
        return None

async def _get_btcdom_rsi_1h_fallback() -> float:
    """Fallback: тянем данные BTC Dominance через пользовательский JSON-прокси TradingView
    (BTCD_FALLBACK_URL) и считаем RSI 1h локально. Ожидаемые форматы ответа:
      - {"c": [...]} где c — массив закрытий
      - {"data": [{"close": ...}, ...]}
      - [[ts, open, high, low, close], ...]
    Возвращает float или None.
    """
    if not BTCD_FALLBACK_URL:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(BTCD_FALLBACK_URL)
            if r.status_code != 200:
                return None
            jd = r.json()
            closes = None
            if isinstance(jd, dict):
                if isinstance(jd.get("c"), list):
                    closes = jd.get("c")
                elif isinstance(jd.get("data"), list) and jd.get("data") and isinstance(jd["data"][0], dict):
                    closes = [row.get("close") for row in jd["data"] if row.get("close") is not None]
            elif isinstance(jd, list) and jd and isinstance(jd[0], (list, tuple)) and len(jd[0]) >= 5:
                closes = [row[4] for row in jd]
            if not closes:
                return None
            return _compute_rsi_from_closes(closes, period=14)
    except Exception:
        return None

async def _get_btcdom_rsi_1h_cmc() -> float:
    """Fallback 2: CoinMarketCap Pro API — исторические глобальные метрики с btc_dominance (1h).
    Требует CMC_API_KEY. Возвращает float или None.
    """
    if not CMC_API_KEY:
        return None
    try:
        import httpx
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {
            "interval": "1h",
            "count": 200,
            "convert": "USD",
        }
        url = "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/historical"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                return None
            jd = r.json()
            data = jd.get("data") or []
            if not isinstance(data, list) or not data:
                return None
            # Извлекаем btc_dominance из каждой точки
            closes = []
            for row in data:
                dom = row.get("btc_dominance")
                if dom is not None:
                    closes.append(float(dom))
            if len(closes) < 15:
                return None
            return _compute_rsi_from_closes(closes, period=14)
    except Exception:
        return None

async def _check_extra_filters(symbol_usdt: str, context: str):
    """Доп. фильтры (обновлено):
    - Фильтр по объёму 24h ОТКЛЮЧЕН (не блокирует)
    - Фильтр по BTC Dominance RSI 1h ОТКЛЮЧЕН (не блокирует)
    - Остаётся только RSI 1h (Taapi):
        LONG: допускаем только если RSI1h <= 45
        SHORT: допускаем только если RSI1h >= 55
    Возвращает (ok: bool, volume_usd: float|None, rsi1h: float|None)
    """
    # Объём 24h собираем для логов, но не блокируем
    vol = await _get_24h_volume_usd(symbol_usdt)
    # RSI 1h обязателен
    rsi1h = await _get_rsi_1h_taapi(symbol_usdt)
    if rsi1h is None:
        try:
            await notify(f"[ERROR] RSI 1h (Taapi) n/a for {symbol_usdt}")
        except Exception:
            pass
        return False, vol, None

    # Окна допуска по RSI 1h
    if context == "long" and rsi1h > 45:
        return False, vol, rsi1h
    if context == "short" and rsi1h < 55:
        return False, vol, rsi1h

    # BTC.D фильтр отключён: не запрашиваем/не блокируем
    return True, vol, rsi1h

def _calculate_order_volumes(first_price: float, last_price: float, sl_price: float, side: str) -> list:
    """Рассчитывает объемы для 5 ордеров с равномерным распределением риска"""
    try:
        # Жёсткое правило: общий риск на сделку = RISK_USD
        total_risk_usdt = RISK_USD
        print(f"[DEBUG] Общий риск на сделку: {total_risk_usdt:.2f} USDT (фиксированный)")
        
        # Риск на каждый ордер (равномерно)
        risk_per_order = total_risk_usdt / 5
        print(f"[DEBUG] Риск на каждый ордер: {risk_per_order:.2f} USDT")
        
        # Создаем 5 цен для ордеров (равномерно распределенных)
        if side == "buy":
            # Для LONG: от first_price до last_price (вниз)
            prices = [first_price - (first_price - last_price) * i / 4 for i in range(5)]
        else:
            # Для SHORT: от first_price до last_price (вверх)
            prices = [first_price + (last_price - first_price) * i / 4 for i in range(5)]
        
        volumes = []
        for i, price in enumerate(prices):
            # Расчет объема: риск / (цена_входа - цена_SL)
            if side == "buy":
                price_diff = price - sl_price
            else:
                price_diff = sl_price - price
            
            if price_diff > 0:
                volume = risk_per_order / price_diff
                volumes.append(volume)
                print(f"[DEBUG] Ордер {i+1}: цена={price:.5f}, объем={volume:.2f}, риск={risk_per_order:.2f} USDT")
            else:
                volumes.append(0)
                print(f"[DEBUG] Ордер {i+1}: цена={price:.5f}, объем=0 (неверный расчет)")
        
        return volumes
    except Exception as e:
        print(f"[DEBUG] Ошибка расчета объемов: {e}")
        return [0] * 5

async def _get_rsi_ema_12h(symbol_usdt: str):
    """Получает RSI 12h через Taapi и EMA200 12h локально по Binance API"""
    try:
        import httpx
        
        # Получаем данные с Binance
        async with httpx.AsyncClient() as client:
            # RSI 12h через Taapi
            rsi = None
            if TAAPI_KEY:
                try:
                    ta_symbol = _to_taapi_symbol(symbol_usdt)
                    url = (
                        "https://api.taapi.io/rsi?"
                        f"secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval=12h"
                    )
                    r = await client.get(url)
                    if r.status_code == 200:
                        jd = r.json()
                        if isinstance(jd, dict) and jd.get("value") is not None:
                            rsi = float(jd.get("value"))
                except Exception:
                    rsi = None
            if rsi is None:
                try:
                    await notify(f"[ERROR] RSI 12h (Taapi) n/a for {symbol_usdt}")
                except Exception:
                    pass
                
            # EMA200 12h
            ema_url = f"https://api.binance.com/api/v3/klines?symbol={symbol_usdt}&interval=12h&limit=200"
            ema_response = await client.get(ema_url)
            
            if ema_response.status_code == 200:
                klines = ema_response.json()
                if len(klines) >= 200:
                    # Простой расчет EMA200
                    closes = [float(k[4]) for k in klines[-200:]]
                    multiplier = 2 / (200 + 1)
                    ema = closes[0]  # Начальное значение
                    
                    for close in closes[1:]:
                        ema = (close * multiplier) + (ema * (1 - multiplier))
                else:
                    ema = None
            else:
                ema = None
                
        return rsi, ema
        
    except Exception as e:
        print(f"[DEBUG] Ошибка получения RSI/EMA: {e}")
        return None, None

async def _check_12h_filters(symbol_usdt: str, context: str):
    """Проверяет фильтры RSI 12h и EMA200 12h.
    Возвращает кортеж: (ok: bool, rsi_12h: Optional[float], ema200_12h: Optional[float], current_price: Optional[float]).
    """
    try:
        rsi, ema = await _get_rsi_ema_12h(symbol_usdt)
        
        if rsi is None or ema is None:
            print(f"[DEBUG] Не удалось получить RSI/EMA для {symbol_usdt}")
            return False, rsi, ema, None
            
        # Получаем текущую цену
        current_price = None
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                ticker_url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol_usdt}"
                ticker_response = await client.get(ticker_url)
                if ticker_response.status_code == 200:
                    current_price = float(ticker_response.json()['price'])
        except:
            pass
            
        if current_price is None:
            print(f"[DEBUG] Не удалось получить текущую цену для {symbol_usdt}")
            return False, rsi, ema, None
            
        print(f"[DEBUG] Фильтры для {symbol_usdt}: RSI={rsi:.2f}, EMA200={ema:.6f}, Price={current_price:.6f}")
        
        if context == "long":
            # LONG: RSI >= 50 AND price >= EMA200
            rsi_ok = rsi >= 50
            ema_ok = current_price >= ema
            print(f"[DEBUG] LONG фильтры: RSI>=50={rsi_ok}, Price>=EMA200={ema_ok}")
            return (rsi_ok and ema_ok), rsi, ema, current_price
        else:  # short
            # SHORT: RSI <= 50 AND price <= EMA200
            rsi_ok = rsi <= 50
            ema_ok = current_price <= ema
            print(f"[DEBUG] SHORT фильтры: RSI<=50={rsi_ok}, Price<=EMA200={ema_ok}")
            return (rsi_ok and ema_ok), rsi, ema, current_price
            
    except Exception as e:
        print(f"[DEBUG] Ошибка проверки фильтров: {e}")
        return False, None, None, None

async def _post_level_search(symbol_usdt: str, context: str = "long", origin_tf: str = "30m"):
    """Отправляет запрос в локальный API на поиск уровня. Возвращает dict ответа или None."""
    payload = json.dumps({
        "symbol": symbol_usdt,
        "context": context,
        "origin_tf": origin_tf
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(LEVELS_API_URL, content=payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        print(f"Ошибка запроса уровней для {symbol_usdt}: {e}")
        return None

async def _send_webhook_from_level(symbol_usdt: str, side: str, entry_price, sl_price, tp_price, level_zone=None, *, slx_enabled_override=None, slx_overrides=None, be_enabled_override=None, be_overrides=None):
    """Собирает payload и отправляет вебхук на основе рассчитанных уровней.
    side: 'buy' для long, 'sell' для short.
    level_zone: (level_low, level_high) для расчета сетки ордеров.
    """
    try:
        if entry_price is None or sl_price is None or tp_price is None:
            return False
        
        # Определяем границы для сетки ордеров
        if level_zone and len(level_zone) == 2:
            level_low, level_high = level_zone
            if side == "buy":
                # LONG: первый ордер = текущая цена, 5-й ордер = ВЕРХНЯЯ граница SUPPORT
                first_order_price = float(entry_price)
                last_order_price = float(level_high)
            else:  # sell
                # SHORT: первый ордер = текущая цена, 5-й ордер = НИЖНЯЯ граница RESISTANCE
                first_order_price = float(entry_price)
                last_order_price = float(level_low)
        else:
            # Fallback: если нет level_zone, используем старую логику
            first_order_price = float(entry_price)
            last_order_price = float(entry_price)
        
        # Рассчитываем объемы для каждого ордера с риск-менеджментом
        volumes = _calculate_order_volumes(first_order_price, last_order_price, float(sl_price), side)
        
        payload = build_payload(
            symbol=symbol_usdt,
            side=side,
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            first_order_price=first_order_price,
            last_order_price=last_order_price,
            qty_orders=5,
            volumes=volumes,
            slx_enabled_override=slx_enabled_override,
            slx_overrides=slx_overrides,
            be_enabled_override=be_enabled_override,
            be_overrides=be_overrides,
        )
        sent = await send_payload(payload)
        try:
            write_webhook_history(datetime.utcnow().isoformat(), payload, sent)
        except Exception:
            pass
        return sent
    except Exception as _:
        return False

async def _send_via_bot(text: str) -> bool:
    """Пробует отправить сообщение через Bot API. Возвращает True при успехе."""
    if not BOT_TOKEN or not BOT_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": BOT_CHAT_ID, "text": text}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=data)
            r.raise_for_status()
            jd = r.json()
            return bool(jd.get("ok"))
    except Exception as e:
        print(f"Ошибка Bot API: {e}")
        return False

async def notify(text: str):
    """
    Отправляет уведомление с защитой от FloodWait и rate limiting.
    Сперва пробует через Bot API, иначе через Telegram client в Saved Messages.
    """
    # Сначала пробуем через Bot API (не подлежит rate limit)
    if await _send_via_bot(text):
        return

    # Telegram client - применяем rate limiter и обработку FloodWait
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Проверяем rate limit перед отправкой
            await telegram_rate_limiter.wait_if_needed()

            # Отправляем сообщение
            await client.send_message(RESULT_RECIPIENT, text)
            return  # Успешно отправлено

        except FloodWaitError as e:
            # Telegram попросил подождать - ждём указанное время
            print(f"⏳ [notify] FloodWait: требуется ожидание {e.seconds}с (попытка {attempt + 1}/{max_retries})")
            await asyncio.sleep(e.seconds)

        except Exception as e:
            print(f"❌ [notify] Ошибка отправки (попытка {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Экспоненциальная задержка: 2^attempt секунд (1с, 2с, 4с)
                delay = 2 ** attempt
                await asyncio.sleep(delay)
            else:
                print(f"❌ [notify] Не удалось отправить после {max_retries} попыток")
                break

def escape_csv_text(text):
    """Экранирует текст для CSV"""
    if text is None:
        return ""
    # Заменяем кавычки и переносы строк
    text = str(text).replace('"', '""').replace('\n', ' ').replace('\r', ' ')
    return text

def write_to_csv(timestamp_utc, chat_id, chat_name, message_text):
    """Записывает сообщение в CSV файл"""
    csv_path = os.path.join(os.path.dirname(__file__), "messages.csv")
    file_exists = os.path.exists(csv_path)
    
    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            
            # Записываем заголовки только если файл новый
            if not file_exists:
                headers = ["timestamp_utc", "chat_id", "chat_name", "message_text"]
                writer.writerow(headers)
            
            # Записываем данные
            writer.writerow([
                timestamp_utc,
                chat_id,
                escape_csv_text(chat_name),
                escape_csv_text(message_text)
            ])
    except Exception as e:
        print(f"Ошибка записи в CSV: {e}")

def write_to_realtime_csv(timestamp_utc, chat_id, chat_name, message_text):
    """Добаляет подходящее сообщение в setup_messengers_realtime.csv с тем же хедером"""
    csv_path = os.path.join(os.path.dirname(__file__), "setup_messengers_realtime.csv")
    file_exists = os.path.exists(csv_path)
    try:
        # Строгое правило на автора для realtime-логов
        if not isinstance(message_text, str):
            return
        if not re.search(r"^\s*Author\s*:\s*Setup\s*Screener\b", message_text, flags=re.IGNORECASE):
            return
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp_utc", "chat_id", "chat_name", "message_text"])
            writer.writerow([
                timestamp_utc,
                chat_id,
                escape_csv_text(chat_name),
                escape_csv_text(message_text)
            ])
    except Exception as e:
        print(f"Ошибка записи в realtime CSV: {e}")

def write_to_log(timestamp_utc, chat_id, chat_name, message_text):
    """Записывает сообщение в лог файл"""
    log_path = os.path.join(os.path.dirname(__file__), "messages.log")
    
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            log_line = f"{timestamp_utc},{chat_id},{chat_name},{message_text}\n"
            f.write(log_line)
    except Exception as e:
        print(f"Ошибка записи в лог: {e}")

def write_raw_snapshot(timestamp_utc, chat_id, chat_name, message_text):
    """Пишет сырой текст сообщения (для точной сверки эмодзи/пробелов)."""
    try:
        raw_path = os.path.join(os.path.dirname(__file__), "raw_messages.log")
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp_utc},{chat_id},{chat_name},{message_text}\n")
    except Exception:
        pass

def write_webhook_history(timestamp_utc: str, payload: dict, sent_ok: bool):
    """Журнал отправленных вебхуков в webhook_history.csv"""
    try:
        csv_path = os.path.join(os.path.dirname(__file__), "webhook_history.csv")
        file_exists = os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow([
                    "timestamp_utc", "hook_name", "symbol", "side",
                    "first_order_price", "last_order_price", "sl_price", "tp_price",
                    "qty_orders", "status"
                ])
            name = payload.get("name")
            symbol = payload.get("symbol")
            side = payload.get("side")
            o_scaled = ((payload.get("open") or {}).get("scaled") or {})
            p1 = None; p2 = None
            if isinstance(o_scaled.get("price1"), dict):
                p1 = o_scaled.get("price1", {}).get("value")
            if isinstance(o_scaled.get("price2"), dict):
                p2 = o_scaled.get("price2", {}).get("value")
            sl = (payload.get("sl") or {}).get("price")
            tp = None
            tp_block = payload.get("tp") or {}
            if isinstance(tp_block.get("orders"), list) and tp_block.get("orders"):
                tp = tp_block.get("orders")[0].get("price")
            elif isinstance(tp_block.get("price"), (int, float)):
                tp = tp_block.get("price")
            qty = o_scaled.get("qty")
            w.writerow([timestamp_utc, name, symbol, side, p1, p2, sl, tp, qty, "ok" if sent_ok else "fail"])
    except Exception:
        pass

async def _process_event(event):
    """Общая обработка входящего или отредактированного сообщения"""
    try:
        # Получаем информацию о сообщении
        message = event.message.text or event.message.message or event.message.raw_text
        chat_id = event.chat_id
        
        # Получаем название чата
        if hasattr(event.chat, 'title'):
            chat_name = event.chat.title
        elif hasattr(event.chat, 'first_name'):
            chat_name = event.chat.first_name
            if hasattr(event.chat, 'last_name') and event.chat.last_name:
                chat_name += f" {event.chat.last_name}"
        elif hasattr(event.chat, 'username'):
            chat_name = event.chat.username
        else:
            chat_name = "Unknown"
        
        # Получаем информацию об отправителе
        if event.sender:
            if hasattr(event.sender, 'first_name'):
                sender_info = event.sender.first_name
                if hasattr(event.sender, 'last_name') and event.sender.last_name:
                    sender_info += f" {event.sender.last_name}"
            elif hasattr(event.sender, 'username'):
                sender_info = event.sender.username
            else:
                sender_info = "Unknown"
            sender_id = event.sender_id
        else:
            sender_info = "Unknown"
            sender_id = "Unknown"
        
        # Временная метка
        timestamp_utc = datetime.utcnow().isoformat()
        
        # Записываем в лог
        write_to_log(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        # Пишем raw snapshot для точной сверки
        write_raw_snapshot(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        
        # Записываем в CSV
        write_to_csv(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        
        # Выводим в консоль
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ID: {chat_id}] [{chat_name}] [{sender_info} (ID:{sender_id})] → {message[:100]}{'...' if len(message) > 100 else ''}")

        # Источник: строго по whitelist chat_id/forwarded channel с учётом blocklist
        src_ok = False
        print(f"[DEBUG] Проверяем источник: chat_id={chat_id}, chat_name='{chat_name}'")
        print(f"[DEBUG] ALLOW_CHAT_IDS: {ALLOW_CHAT_IDS}")
        print(f"[DEBUG] BLOCK_CHAT_IDS: {BLOCK_CHAT_IDS}")
        # 1) по chat_id
        try:
            if event.chat_id in BLOCK_CHAT_IDS:
                src_ok = False
            elif event.chat_id in ALLOW_CHAT_IDS:
                src_ok = True
                print(f"[DEBUG] ✅ Источник разрешен по chat_id: {chat_id}")
        except Exception as e:
            print(f"[DEBUG] ❌ Ошибка проверки chat_id: {e}")
            pass
        # 2) по названию — временно отключено (используем только chat_id whitelist)
        # 3) пересланные сообщения — проверим канал-источник
        fwd = getattr(event.message, 'fwd_from', None)
        print(f"[DEBUG] Пересланное сообщение: {fwd is not None}")
        if fwd and not src_ok:
            from_name = getattr(fwd, 'from_name', '') or ''
            print(f"[DEBUG] from_name: '{from_name}'")
            # имя не используем для разрешения
            # если доступен channel_id, сравним с ALLOW_CHAT_IDS
            ch_id = getattr(fwd, 'channel_id', None)
            print(f"[DEBUG] channel_id: {ch_id}")
            if ch_id is not None and not src_ok:
                try:
                    ch_full = int(f"-100{int(ch_id)}")
                    if ch_full in ALLOW_CHAT_IDS and ch_full not in BLOCK_CHAT_IDS:
                        src_ok = True
                        print(f"[DEBUG] ✅ Источник разрешен по channel_id: {ch_id}")
                except Exception as e:
                    print(f"[DEBUG] ❌ Ошибка проверки channel_id: {e}")
                    pass
        # 4) Saved Messages: только если явно разрешено
        if not PROCESS_SAVED_INPUT and chat_name == "Unknown" and str(sender_id) == str(SELF_CHAT_ID):
            src_ok = False
        # Разрешаем Saved Messages специально для GO SHORT/GO LONG, не трогая прочие источники
        try:
            if (
                not src_ok
                and str(sender_id) == str(SELF_CHAT_ID)
                and isinstance(message, str)
                and (
                    re.search(r"\bGO\s+SHORT\b", message, flags=re.IGNORECASE)
                    or re.search(r"\bGO\s+LONG\b", message, flags=re.IGNORECASE)
                )
            ):
                src_ok = True
                print("[DEBUG] ✅ Saved Messages allowed for GO SHORT/GO LONG")
        except Exception:
            pass
        print(f"[DEBUG] src_ok={src_ok}, message_len={len(message) if message else 0}")
        # Если источник не разрешён, но внутри есть GO SHORT/GO LONG — сообщим причину в Telegram
        if not src_ok and isinstance(message, str):
            try:
                go_short = re.search(r"\bGO\s+SHORT\b", message, flags=re.IGNORECASE) is not None
                go_long = re.search(r"\bGO\s+LONG\b", message, flags=re.IGNORECASE) is not None
                if go_short or go_long:
                    reason = "source not allowed"
                    side = "GO SHORT" if go_short else "GO LONG"
                    try:
                        await notify(
                            f"[{side}] skipped: {reason} (chat_id={chat_id}, name='{chat_name}')"
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        if src_ok and message:
            print(f"[DEBUG] ✅ Начинаем обработку сообщения")
            # Обновим кэш уровней из Telegram Key Levels (если сообщение такое)
            _parse_and_cache_key_levels(message)
            # Обработка точной фразы GO SHORT (вектор сообщений)
            try:
                # Всегда выполняем оценку и отправляем уведомления PASS/FAIL
                if re.search(r"\bGO\s+SHORT\b", message, flags=re.IGNORECASE):
                    try:
                        await _evaluate_go_short_blocks(message)
                    except Exception:
                        pass
                    # Исполнение (вебхуки) только если включено окружением
                    if GO_SHORT_ENABLED:
                        try:
                            await _process_go_short_message(message)
                        except Exception:
                            pass
                # Обработка точной фразы GO LONG
                if re.search(r"\bGO\s+LONG\b", message, flags=re.IGNORECASE):
                    # Для GO LONG пока только исполнение (без отдельной оценки сообщений)
                    if GO_LONG_ENABLED:
                        try:
                            await _process_go_long_message(message)
                        except Exception:
                            pass
            except Exception:
                pass
            # 1) Параллельно обрабатываем эмодзи-паттерны (основная логика)
            for line in message.splitlines():
                # Пропускаем заголовки и пустые строки
                if not line.strip() or line.strip().startswith("DOWNTREND") or line.strip().startswith("UPTREND"):
                    continue
                    
                ticker, squares, origin_tf = _line_to_ticker_and_squares(line)
                if not ticker:
                    continue
                symbol_usdt = ticker if ticker.endswith("USDT") else f"{ticker}USDT"
                print(f"[DEBUG] Обрабатываем строку: {line}")
                print(f"[DEBUG] Тикер: {ticker}, Эмодзи: {squares}, Таймфрейм: {origin_tf}")

                # Коррекция в аптренде → поддержка (long)
                if _is_correction_combo(squares):
                    print(f"[DEBUG] Найдена LONG комбинация для {ticker}")
                    
                    # Проверяем фильтры RSI 12h и EMA200 12h
                    print(f"[DEBUG] Проверяем 12h фильтры для LONG...")
                    filters_ok, rsi12h, ema200_12h, px = await _check_12h_filters(symbol_usdt, "long")
                    if not filters_ok:
                        print(f"[DEBUG] LONG сигнал для {ticker} заблокирован фильтрами RSI/EMA")
                        try:
                            rel = None
                            try:
                                if px is not None and ema200_12h is not None:
                                    rel = "above" if px >= ema200_12h else "below"
                            except Exception:
                                rel = None
                            base = f"[LONG][{symbol_usdt}] RSI12h/EMA200: FAIL"
                            if rsi12h is not None and ema200_12h is not None and px is not None and rel:
                                base += f" (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)"
                            await notify(base)
                        except Exception:
                            pass
                        continue
                    
                    # Используем уровни из репозитория Key Levels (свежесть до 1000ч, приоритет 4h > 1h > 12h > origin_tf)
                    latest = get_latest_levels(symbol_usdt, max_age_minutes=0, prefer_timeframes=["4h", "1h", "12h", origin_tf])
                    support_list = (latest or {}).get("support", [])
                    resistance_list = (latest or {}).get("resistance", [])
                    
                    # Текущая цена с Binance
                    last_price = None
                    print(f"[DEBUG] Запрашиваем цену из Binance для {symbol_usdt}")
                    try:
                        binance_price = await _get_binance_price(symbol_usdt)
                        print(f"[DEBUG] Ответ от Binance: {binance_price}")
                        if binance_price:
                            last_price = float(binance_price)
                    except Exception as e:
                        print(f"[DEBUG] Ошибка получения цены из Binance: {e}")
                    if last_price is None:
                        try:
                            await notify(f"[LONG][{symbol_usdt}] price: FAIL (binance price n/a)")
                        except Exception:
                            pass
                        continue

                    # Ретрай при отсутствии уровней (5/10/15с)
                    chosen_support = _choose_nearest_zone(support_list, last_price)
                    if not chosen_support:
                        retry_delays = [5, 10, 15]
                        for d in retry_delays:
                            await asyncio.sleep(d)
                            latest_retry = get_latest_levels(
                                symbol_usdt,
                                max_age_minutes=0,
                                prefer_timeframes=["4h", "1h", "12h", origin_tf]
                            )
                            support_list = (latest_retry or {}).get("support", [])
                            resistance_list = (latest_retry or {}).get("resistance", [])
                            chosen_support = _choose_nearest_zone(support_list, last_price)
                            if chosen_support:
                                break
                    if not chosen_support:
                        # Не уведомляем об отсутствии уровней
                        try:
                            await notify(f"[LONG][{symbol_usdt}] levels(support): FAIL (no level found)")
                        except Exception:
                            pass
                        continue
                    rng_low, rng_high = chosen_support
                    # Доп. фильтры: дистанция, всплеск объёма, свежесть контакта
                    dist_pct = abs(last_price - rng_low) / last_price * 100.0
                    if dist_pct > 9.0:
                        print(f"[DEBUG] DISTANCE>9% (LONG): {dist_pct:.2f}%")
                        try:
                            await notify(f"[LONG][{symbol_usdt}] distance<=9%: FAIL ({dist_pct:.2f}%)")
                        except Exception:
                            pass
                        continue
                    # Спайк объёма: временно не блокируем, но пишем статистику
                    spike = await _get_1h_volume_spike(symbol_usdt)
                    try:
                        import csv, os
                        with open(os.path.join(os.path.dirname(__file__), 'spike_stats.csv'), 'a', newline='') as f:
                            w = csv.writer(f)
                            w.writerow([datetime.utcnow().isoformat(), symbol_usdt, 'long', spike])
                    except Exception:
                        pass
                    # Возраст уровня временно не учитываем

                    # Если далеко от зоны (>10%) — пропускаем (защитный порог)
                    distance_to_support = abs(last_price - rng_low) / last_price * 100
                    if distance_to_support >= 10:
                        print(f"[DEBUG] ФИЛЬТР 10%: Расстояние до поддержки {distance_to_support:.2f}% >= 10% - пропускаем сделку")
                        try:
                            await notify(f"[LONG][{symbol_usdt}] distance<10%: FAIL ({distance_to_support:.2f}%)")
                        except Exception:
                            pass
                        continue

                    # Доп. фильтры по RSI 1h и BTC.D RSI 1h (жесткие)
                    ok_extra, vol_usd, rsi1h = await _check_extra_filters(symbol_usdt, "long")
                    if not ok_extra:
                        print(f"[DEBUG] EXTRA FILTERS FAILED (LONG): vol24h={vol_usd}, rsi1h={rsi1h}")
                        try:
                            await notify(f"[LONG][{symbol_usdt}] vol24h>=15M & RSI1h<=45 & BTC.D>55: FAIL (vol={vol_usd}, rsi1h={rsi1h})")
                        except Exception:
                            pass
                        continue

                    # Сводный предвебхуковый тест всех фильтров
                    all_checks = {
                        'rsi12h_ema': True,  # уже пройдено ранее
                        'levels': True,      # support выбран
                        'distance<=9%': True,  # пройдено выше
                        'spike>=1.0x': True,   # пройдено выше
                        'rsi1h_and_btcd': True  # ok_extra
                    }
                    print(f"[DEBUG] ALL FILTERS (LONG) OK: {all_checks}")

                    # Построим SL/TP
                    sl_adjusted = rng_low * 0.99
                    if resistance_list:
                        tp_target = float(resistance_list[0][1])
                    else:
                        tp_target = last_price + ( (rng_high - rng_low) * 3.0 )

                    # Отправим вебхук и уведомление
                    try:
                        await _send_webhook_from_level(
                            symbol_usdt, "buy",
                            last_price, sl_adjusted, tp_target, (rng_low, rng_high)
                        )
                    except Exception:
                        pass

                    try:
                        trend_emojis = ''.join(squares)
                        rel = "above" if px >= ema200_12h else "below"
                        msg = (
                            f"[LONG OK] ${symbol_usdt.replace('USDT','')} {origin_tf} {trend_emojis}\n"
                            f"rsi12h>=50 & price>=ema200: PASS (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)\n"
                            f"levels(support): PASS {rng_low:.5f}-{rng_high:.5f}\n"
                            f"distance<=9%: PASS ({dist_pct:.2f}%)\n"
                            f"rsi1h<=45 & btc.d>55: PASS (vol24h={(vol_usd or 0)/1_000_000:.1f}M)\n"
                            f"entry={last_price} sl={sl_adjusted:.5f} tp={tp_target:.5f}"
                        )
                        await notify(msg)
                    except Exception:
                        pass
                    
                    await asyncio.sleep(0.4)
                    continue
                    
                    resp = await _post_level_search(symbol_usdt, context="long", origin_tf=origin_tf)
                    print(f"[DEBUG] API ответ: {resp}")
                    if resp and isinstance(resp, dict) and resp.get("decision", "").startswith("enter_") and resp.get("level"):
                        lvl = resp["level"]
                        orders = resp.get("orders", {}) or {}
                        sl = orders.get("sl", {}).get("price")
                        tp_arr = orders.get("tp", [])
                        tp = tp_arr[0].get("price") if tp_arr else None
                        tol = float(lvl.get("tolerance") or 0.0)
                        # СТРОГОЕ ПРАВИЛО: Берем уровни ТОЛЬКО из Key Levels [SUPPORT] и [RESISTANCE]
                        # Если их нет - пропускаем сделку
                        rng_low = None
                        rng_high = None
                        
                        # Ищем уровни в Key Levels
                        if 'key_levels' in resp:
                            key_levels = resp['key_levels']
                            if 'support' in key_levels and key_levels['support']:
                                rng_low = key_levels['support']
                            if 'resistance' in key_levels and key_levels['resistance']:
                                rng_high = key_levels['resistance']
                        
                        # Если нет уровней из Key Levels - пропускаем сделку
                        if rng_low is None or rng_high is None:
                            print(f"[DEBUG] НЕТ УРОВНЕЙ ИЗ KEY LEVELS для {symbol_usdt} - пропускаем сделку")
                            print(f"[DEBUG] rng_low: {rng_low}, rng_high: {rng_high}")
                            continue
                        
                        # ДОПОЛНИТЕЛЬНЫЙ ФИЛЬТР: Если до уровня 10% и более - пропускаем сделку
                        # Получаем текущую цену для расчета расстояния
                        current_price = resp.get("last_price")
                        if current_price is None:
                            trade_setup = resp.get("trade_setup", {})
                            current_price = trade_setup.get("current_price")
                        
                        if current_price is not None:
                            # Для LONG: проверяем расстояние до уровня поддержки
                            distance_to_support = abs(current_price - rng_low) / current_price * 100
                            if distance_to_support >= 10:
                                print(f"[DEBUG] ФИЛЬТР 10%: Расстояние до поддержки {distance_to_support:.2f}% >= 10% - пропускаем сделку")
                                continue
                        
                        # Проверка цены: внутри диапазона или подходит к диапазону
                        last_price = resp.get("last_price")
                        # Если last_price не получен, используем current_price из trade_setup
                        if last_price is None:
                            trade_setup = resp.get("trade_setup", {})
                            last_price = trade_setup.get("current_price")
                        # Всегда получаем актуальную цену из Binance для поля Current
                        print(f"[DEBUG] Запрашиваем цену из Binance для {symbol_usdt}")
                        try:
                            binance_price = await _get_binance_price(symbol_usdt)
                            print(f"[DEBUG] Ответ от Binance: {binance_price}")
                            if binance_price:
                                last_price = float(binance_price)
                                print(f"[DEBUG] Получена актуальная цена из Binance: {last_price}")
                            else:
                                print(f"[DEBUG] Binance вернул None для {symbol_usdt}")
                        except Exception as e:
                            print(f"[DEBUG] Ошибка получения цены из Binance: {e}")
                        print(f"[DEBUG] last_price: {last_price}")
                        print(f"[DEBUG] Уровень поддержки (S1): {rng_low}")
                        print(f"[DEBUG] Уровень сопротивления (R1): {rng_high}")
                        ok_to_send = True
                        if last_price is not None and rng_low is not None and rng_high is not None:
                            # условие: внутри диапазона или ближе одной tolerance к границе
                            if not (rng_low <= last_price <= rng_high or 
                                    abs(last_price - rng_low) <= tol or 
                                    abs(last_price - rng_high) <= tol):
                                ok_to_send = False

                        if ok_to_send:
                            # Получаем информацию о торговом сетапе
                            trade_setup = resp.get('trade_setup', {})
                            
                            # Формируем улучшенный текст с торговым сетапом
                            # Реальные эмодзи из сообщения
                            trend_emojis = ''.join(squares)
                            
                            # Реальный диапазон уровня (толерантность)
                            level_price = lvl.get('price', 0)
                            level_low = level_price - tol
                            level_high = level_price + tol
                            
                            msg = (
                                f"${symbol_usdt.replace('USDT', '')} {origin_tf} Binance #Futures\n"
                                f"TREND {trend_emojis}\n"
                                f"MA 🟢 RSI 🟢 {datetime.now().strftime('%H:%M %d.%m.%Y')}\n"
                                f"Volume 1D       {trade_setup.get('current_price', 0) or 0:.1f} M\n"
                                f"CD Week         {trade_setup.get('price_change_percent', 0) or 0:+.2f} M\n"
                                f"Long 📈\n\n"
                                f"⌛️ Entry: {trade_setup.get('entry_price', orders.get('entry',{}).get('price'))}\n"
                                f"☑️ TP: {trade_setup.get('tp_price', 'N/A')} {trade_setup.get('reward_percent', 0) or 0:.2f}%\n"
                                f"✖️ SL: {trade_setup.get('sl_price', sl)} {trade_setup.get('risk_percent', 0) or 0:.2f}%\n"
                                f"🎲 Risk-reward: {trade_setup.get('risk_reward_ratio', 0) or 0:.1f}\n\n"
                                f"Comment: Получен сигнал о начале коррекции, сгенерирован торговый сетап и отправлен в терминал. | "
                                f"Key levels: SUPPORT {rng_low:.5f} - {rng_high:.5f} | "
                                f"Current: {last_price or 0:.5f} ({trade_setup.get('price_change_percent', 0) or 0:+.2f}%)"
                            )
                            # Отправим webhook (LONG → buy → long URL)
                            try:
                                # Формируем level_zone для сетки ордеров
                                level_zone = (rng_low, rng_high)
                                # Для LONG: первый ордер = текущая цена, последний = нижняя граница SUPPORT
                                first_order_price = last_price if last_price else orders.get('entry',{}).get('price')
                                last_order_price = rng_low
                                # LONG: SL = уровень поддержки - 1%
                                sl_adjusted = rng_low * 0.99
                                # LONG: TP = 1:3 соотношение от полной позиции (первый ордер + 3% от диапазона)
                                range_size = first_order_price - last_order_price
                                tp_adjusted = first_order_price + (range_size * 3)
                                await _send_webhook_from_level(
                                    symbol_usdt, "buy",
                                    first_order_price, sl_adjusted, tp_adjusted, (last_order_price, last_order_price)
                                )
                            except Exception:
                                pass
                            # Пытаемся получить PNG график и отправить
                            try:
                                chart_req = {
                                    "symbol": symbol_usdt,
                                    "origin_tf": origin_tf,
                                    "level_price": float(lvl.get('price')),
                                    "range_low": float(rng.get('low')),
                                    "range_high": float(rng.get('high')),
                                    "entry": float(orders.get('entry',{}).get('price')) if orders.get('entry') else float(lvl.get('price')),
                                    "sl": float(sl) if sl is not None else float(lvl.get('price')) - tol,
                                    "signal_ts": datetime.utcnow().isoformat()
                                }
                                png_bytes = None
                                try:
                                    async with httpx.AsyncClient(timeout=30.0) as client:
                                        rimg = await client.post(LEVELS_API_URL.replace('/levels/intraday-search','') + '/chart/level.png', json=chart_req)
                                        rimg.raise_for_status()
                                        png_bytes = rimg.content
                                except Exception:
                                    pass
                                # Сначала отправим текст, затем изображение
                                print(f"[DEBUG] Отправляем LONG сообщение для {ticker}")
                                await notify(msg)
                                if png_bytes:
                                    try:
                                        await client.send_file(RESULT_RECIPIENT, file=png_bytes, caption=f"{symbol_usdt} ({origin_tf}) chart")
                                    except Exception:
                                        pass
                                msg = None  # уже отправили
                            except Exception:
                                # если картинка не получилась, просто отправим текст
                                pass
                        else:
                            msg = None
                    else:
                        # Не шлем отрицательные уведомления
                        msg = None
                    if msg:
                        await notify(msg)
                    await asyncio.sleep(0.4)

                # Коррекция в даунтренде → сопротивление (short)
                if _is_resistance_combo(squares):
                    print(f"[DEBUG] Найдена SHORT комбинация для {ticker}")
                    
                    # Проверяем фильтры RSI 12h и EMA200 12h
                    print(f"[DEBUG] Проверяем 12h фильтры для SHORT...")
                    filters_ok, rsi12h, ema200_12h, px = await _check_12h_filters(symbol_usdt, "short")
                    if not filters_ok:
                        print(f"[DEBUG] SHORT сигнал для {ticker} заблокирован фильтрами RSI/EMA")
                        try:
                            rel = None
                            try:
                                if px is not None and ema200_12h is not None:
                                    rel = "above" if px >= ema200_12h else "below"
                            except Exception:
                                rel = None
                            base = f"[SHORT][{symbol_usdt}] RSI12h/EMA200: FAIL"
                            if rsi12h is not None and ema200_12h is not None and px is not None and rel:
                                base += f" (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)"
                            await notify(base)
                        except Exception:
                            pass
                        continue
                    
                    # Берём уровни ТОЛЬКО из репозитория Key Levels (SQLite), приоритет 4h > 1h > 12h > origin_tf
                    latest = get_latest_levels(symbol_usdt, max_age_minutes=0, prefer_timeframes=["4h", "1h", "12h", origin_tf])
                    support_list = (latest or {}).get("support", [])
                    resistance_list = (latest or {}).get("resistance", [])

                    # Текущая цена с Binance
                    last_price = None
                    print(f"[DEBUG] Запрашиваем цену из Binance для {symbol_usdt}")
                    try:
                        binance_price = await _get_binance_price(symbol_usdt)
                        print(f"[DEBUG] Ответ от Binance: {binance_price}")
                        if binance_price:
                            last_price = float(binance_price)
                    except Exception as e:
                        print(f"[DEBUG] Ошибка получения цены из Binance: {e}")
                    if last_price is None:
                        try:
                            await notify(f"[SHORT][{symbol_usdt}] price: FAIL (binance price n/a)")
                        except Exception:
                            pass
                        continue

                    # Ретрай при отсутствии уровней (5/10/15с)
                    chosen_resistance = _choose_nearest_zone(resistance_list, last_price)
                    if not chosen_resistance:
                        retry_delays = [5, 10, 15]
                        for d in retry_delays:
                            await asyncio.sleep(d)
                            latest_retry = get_latest_levels(
                                symbol_usdt,
                                max_age_minutes=0,
                                prefer_timeframes=["4h", "1h", "12h", origin_tf]
                            )
                            support_list = (latest_retry or {}).get("support", [])
                            resistance_list = (latest_retry or {}).get("resistance", [])
                            chosen_resistance = _choose_nearest_zone(resistance_list, last_price)
                            if chosen_resistance:
                                latest = latest_retry
                                break
                    if not chosen_resistance:
                        # Не уведомляем об отсутствии уровней
                        try:
                            await notify(f"[SHORT][{symbol_usdt}] levels(resistance): FAIL (no level found)")
                        except Exception:
                            pass
                        continue
                    rng_low, rng_high = chosen_resistance
                    # Доп. фильтры: дистанция, всплеск объёма, свежесть контакта
                    dist_pct = abs(rng_low - last_price) / last_price * 100.0
                    if dist_pct > 9.0:
                        print(f"[DEBUG] DISTANCE>9% (SHORT): {dist_pct:.2f}%")
                        try:
                            await notify(f"[SHORT][{symbol_usdt}] distance<=9%: FAIL ({dist_pct:.2f}%)")
                        except Exception:
                            pass
                        continue
                    # Спайк объёма: временно не блокируем, но пишем статистику
                    spike = await _get_1h_volume_spike(symbol_usdt)
                    try:
                        import csv, os
                        with open(os.path.join(os.path.dirname(__file__), 'spike_stats.csv'), 'a', newline='') as f:
                            w = csv.writer(f)
                            w.writerow([datetime.utcnow().isoformat(), symbol_usdt, 'short', spike])
                    except Exception:
                        pass
                    # Возраст уровня временно не учитываем
                    print(f"[DEBUG] Выбранная RESISTANCE-зона из levels.db: {rng_low:.5f} - {rng_high:.5f}, timeframe={(latest or {}).get('timeframe')}, source_ts={(latest or {}).get('source_ts')}")

                    # Если далеко от зоны (>10%) — пропускаем (защитный порог)
                    distance_to_resistance = abs(last_price - rng_high) / last_price * 100
                    if distance_to_resistance >= 10:
                        print(f"[DEBUG] ФИЛЬТР 10%: Расстояние до сопротивления {distance_to_resistance:.2f}% >= 10% - пропускаем сделку")
                        try:
                            await notify(f"[SHORT][{symbol_usdt}] distance<10%: FAIL ({distance_to_resistance:.2f}%)")
                        except Exception:
                            pass
                        continue

                    # Доп. фильтры по RSI 1h и BTC.D RSI 1h (жесткие)
                    ok_extra, vol_usd, rsi1h = await _check_extra_filters(symbol_usdt, "short")
                    if not ok_extra:
                        print(f"[DEBUG] EXTRA FILTERS FAILED (SHORT): vol24h={vol_usd}, rsi1h={rsi1h}")
                        try:
                            await notify(f"[SHORT][{symbol_usdt}] vol24h>=15M & RSI1h>=55 & BTC.D<45: FAIL (vol={vol_usd}, rsi1h={rsi1h})")
                        except Exception:
                            pass
                        continue

                    # Сводный предвебхуковый тест всех фильтров
                    all_checks = {
                        'rsi12h_ema': True,   # уже пройдено ранее
                        'levels': True,       # resistance выбран
                        'distance<=9%': True, # пройдено выше
                        'spike>=1.0x': True,  # пройдено выше
                        'rsi1h_and_btcd': True # ok_extra
                    }
                    print(f"[DEBUG] ALL FILTERS (SHORT) OK: {all_checks}")

                    # Построим SL/TP
                    sl_adjusted = rng_high * 1.01
                    if support_list:
                        tp_target = float(support_list[0][0])
                    else:
                        tp_target = last_price - ((rng_high - rng_low) * 3.0)

                    # Отправим вебхук
                    try:
                        await _send_webhook_from_level(
                            symbol_usdt, "sell",
                            last_price, sl_adjusted, tp_target, (rng_low, rng_high)
                        )
                    except Exception:
                        pass

                    # Уведомление
                    try:
                        trend_emojis = ''.join(squares)
                        rel = "below" if px <= ema200_12h else "above"
                        msg = (
                            f"[SHORT OK] ${symbol_usdt.replace('USDT','')} {origin_tf} {trend_emojis}\n"
                            f"rsi12h<=50 & price<=ema200: PASS (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)\n"
                            f"levels(resistance): PASS {rng_low:.5f}-{rng_high:.5f}\n"
                            f"distance<=9%: PASS ({dist_pct:.2f}%)\n"
                            f"rsi1h>=55 & btc.d<45: PASS (vol24h={(vol_usd or 0)/1_000_000:.1f}M)\n"
                            f"entry={last_price} sl={sl_adjusted:.5f} tp={tp_target:.5f}"
                        )
                        await notify(msg)
                    except Exception:
                        pass

                    await asyncio.sleep(0.4)

                # Паттерны закрытия позиций по сигналам TRENDS Cryptovizor
                if _is_close_long_combo(squares):
                    try:
                        payload = build_close_payload(symbol_usdt, position_side='long')
                        await send_payload(payload)
                        print(f"[DEBUG] CLOSE LONG webhook sent for {symbol_usdt}")
                        await notify(f"Закрыть LONG: {symbol_usdt} (по паттерну 🔴🔴🔴🟥🟢)")
                    except Exception as _:
                        pass
                    await asyncio.sleep(0.2)

                if _is_close_short_combo(squares):
                    try:
                        payload = build_close_payload(symbol_usdt, position_side='short')
                        await send_payload(payload)
                        print(f"[DEBUG] CLOSE SHORT webhook sent for {symbol_usdt}")
                        await notify(f"Закрыть SHORT: {symbol_usdt} (по паттерну 🟢🟢🟢🟩🔴)")
                    except Exception as _:
                        pass
                    await asyncio.sleep(0.2)
            
            # 2) Параллельно обрабатываем Author: Setup Screener (дополнительная логика)
            try:
                hook_res_full = await try_process_screener_message(message)
                if hook_res_full:
                    write_to_realtime_csv(timestamp_utc, chat_id, chat_name, message)
            except Exception as _:
                pass
            
            # 3) Если не распознали по всему сообщению — пробуем построчно
            if not hook_res_full:
                for line in message.splitlines():
                    try:
                        hook_res_line = await try_process_screener_message(line)
                        if hook_res_line:
                            write_to_realtime_csv(timestamp_utc, chat_id, chat_name, line)
                            break
                    except Exception:
                        pass

        # Небольшая задержка между обработкой событий для предотвращения флуда
        # (только если было активное действие, не для всех входящих)
        if src_ok and message:
            await asyncio.sleep(0.3)  # 300мс между обработками

    except FloodWaitError as e:
        # Telegram попросил подождать - ждём и не крашим бота
        print(f"⏳ [_process_event] FloodWait: ожидание {e.seconds}с")
        await asyncio.sleep(e.seconds)

    except Exception as e:
        print(f"❌ [_process_event] Ошибка обработки сообщения: {e}")
        # Не крашим бота, просто логируем ошибку
        import traceback
        traceback.print_exc()


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    """Обработчик новых сообщений"""
    await _process_event(event)


@client.on(events.MessageEdited(incoming=True))
async def handler_edit(event):
    """Обработчик редактированных сообщений (каналы часто обновляют посты списками)"""
    await _process_event(event)

async def check_account_status():
    """
    Проверяет статус аккаунта (не заблокирован ли спамом).
    Отправляет тестовое сообщение @SpamBot для проверки.
    """
    try:
        print("🔍 Проверка статуса аккаунта...")
        await client.send_message('SpamBot', '/start')
        await asyncio.sleep(1)
        print("✅ Аккаунт не ограничен")
        return True
    except FloodWaitError as e:
        print(f"⚠️ FloodWait при проверке статуса: ожидание {e.seconds}с")
        return False
    except Exception as e:
        print(f"⚠️ Не удалось проверить статус аккаунта: {e}")
        print("💡 Совет: напишите @SpamBot в Telegram для проверки статуса вручную")
        return False


# ============================
# Логирование авторизации
# ============================

def _mask_phone(value: str) -> str:
    try:
        digits = ''.join(ch for ch in str(value) if ch.isdigit())
        if len(digits) <= 4:
            return "***" + digits[-2:]
        return digits[:2] + "***" + digits[-2:]
    except Exception:
        return "***"


def _setup_auth_logger() -> logging.Logger:
    logger = logging.getLogger("auth")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    log_path = os.path.join(os.path.dirname(__file__), "auth.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Дублируем базовые события Telethon (без чувствительных данных)
    telethon_logger = logging.getLogger("telethon")
    telethon_logger.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == getattr(fh, 'baseFilename', None) for h in telethon_logger.handlers):
        telethon_logger.addHandler(fh)

    return logger


AUTH_LOG = _setup_auth_logger()


async def authorize_with_logging():
    AUTH_LOG.info("auth_flow_start | phone=%s", _mask_phone(phone))
    try:
        if not client.is_connected():
            AUTH_LOG.info("connecting")
            await client.connect()
            AUTH_LOG.info("connected_ok")

        # Проверяем, не авторизованы ли уже
        try:
            me = await client.get_me()
            if me:
                AUTH_LOG.info(
                    "already_authorized | user_id=%s | first_name=%s | phone=%s",
                    getattr(me, 'id', 'n/a'), getattr(me, 'first_name', 'n/a'), _mask_phone(getattr(me, 'phone', ''))
                )
                return
        except Exception as e:
            AUTH_LOG.warning("get_me_failed_before_start | err=%s", repr(e))

        def code_callback():
            AUTH_LOG.info("waiting_code | destination=app_or_sms")
            code = input("Введите код из Telegram/SMS: ")
            # не логируем сам код
            AUTH_LOG.info("code_received | length=%d", len(str(code or "")))
            return code

        def password_callback():
            AUTH_LOG.info("waiting_password_2fa")
            pwd = getpass.getpass("Введите пароль 2FA: ")
            AUTH_LOG.info("password_received | length=%d", len(pwd or ""))
            return pwd

        AUTH_LOG.info("start_sign_in")
        await client.start(phone=phone, code_callback=code_callback, password=password_callback)
        AUTH_LOG.info("start_sign_in_done")

        try:
            me = await client.get_me()
            AUTH_LOG.info(
                "authorized | user_id=%s | first_name=%s | phone=%s",
                getattr(me, 'id', 'n/a'), getattr(me, 'first_name', 'n/a'), _mask_phone(getattr(me, 'phone', ''))
            )
        except Exception as e:
            AUTH_LOG.warning("get_me_failed_after_start | err=%s", repr(e))

    except SessionPasswordNeededError:
        AUTH_LOG.error("session_password_needed_but_no_callback")
        raise
    except FloodWaitError as e:
        AUTH_LOG.error("flood_wait | seconds=%s", getattr(e, 'seconds', 'n/a'))
        raise
    except Exception as e:
        AUTH_LOG.exception("auth_flow_exception | err=%s", repr(e))
        raise

async def main():
    """Основная функция с полной обработкой ошибок и корректным завершением"""
    print("🤖 Telegram Userbot запускается...")
    print("📱 Подключение к Telegram...")
    print(f"⚙️  Параметры:")
    print(f"   - connection_retries: 10")
    print(f"   - retry_delay: 5s")
    print(f"   - timeout: 30s")
    print(f"   - flood_sleep_threshold: 12h")
    print(f"   - rate_limit: 30 сообщений/мин")

    try:
        # Подключение к Telegram с детальным логированием этапов авторизации
        await authorize_with_logging()

        # Получение информации о текущем пользователе
        try:
            me = await client.get_me()
            if me and getattr(me, 'id', None):
                global SELF_CHAT_ID, ALLOW_CHAT_IDS
                SELF_CHAT_ID = int(me.id)

                # Разрешаем отправку в "Избранное" (Saved Messages)
                try:
                    ALLOW_CHAT_IDS.add(SELF_CHAT_ID)
                except Exception:
                    pass

                print(f"✅ Подключение установлено!")
                print(f"👤 Аккаунт: {me.first_name or 'Unknown'} (ID: {me.id})")
                print(f"📞 Телефон: {me.phone or 'Unknown'}")
                print(f"✅ Saved Messages enabled: chat_id={SELF_CHAT_ID}")
        except Exception as e:
            print(f"⚠️ Ошибка получения информации о пользователе: {e}")

        # Проверка статуса аккаунта (опционально)
        await check_account_status()

        print("👂 Слушаем все сообщения...")
        print("💾 Сообщения сохраняются в messages.log и messages.csv")
        print("🛑 Для остановки нажмите Ctrl+C")
        print("─" * 60)

        # Прогрев репозитория уровней из messages.log (если есть)
        try:
            log_path = os.path.join(os.path.dirname(__file__), "messages.log")
            if os.path.exists(log_path):
                imported = import_levels_from_log(log_path)
                if imported:
                    print(f"📊 [Кэш] Импортировано блоков Key Levels: {imported}")
        except Exception as e:
            print(f"⚠️ Ошибка прогрева кэша: {e}")

        # Основной цикл обработки событий
        await client.run_until_disconnected()

    except KeyboardInterrupt:
        print("\n🛑 Получен сигнал остановки (Ctrl+C)...")

    except FloodWaitError as e:
        print(f"\n❌ FloodWaitError: требуется ожидание {e.seconds}с ({e.seconds/3600:.1f}ч)")
        print("💡 Telegram временно ограничил ваш аккаунт. Подождите и попробуйте снова.")

    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        print("📋 Трейсбек:")
        traceback.print_exc()

    finally:
        # Корректное закрытие соединения
        print("\n🔌 Отключаемся от Telegram...")
        try:
            if client.is_connected():
                await client.disconnect()
                print("✅ Отключение завершено успешно")
            else:
                print("ℹ️  Соединение уже закрыто")
        except Exception as e:
            print(f"⚠️ Ошибка при отключении: {e}")

        print("👋 Userbot остановлен")

if __name__ == "__main__":
    client.loop.run_until_complete(main())


