import os
import asyncio
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from datetime import datetime, timedelta
import csv
import re
import json
import httpx
import traceback
from dotenv import load_dotenv
from signal_webhook import try_process_screener_message
from signal_webhook.payload import build_payload, build_close_payload
from signal_webhook.sender import send_payload
from levels_repo import upsert_levels, get_latest_levels, import_levels_from_log

# === ДЕДУПЛИКАЦИЯ ===
PROCESSED_MESSAGES = {}
DEDUP_WINDOW_SEC = 5

# Конфигурация
load_dotenv()
api_id = 29129135
api_hash = "4f2fb26f0b7f24551bd1759cb78af30c"
phone = "+79936192867"
TAAPI_KEY = os.getenv("TAAPI_KEY", "")
BTCDOM_TV_SYMBOL = os.environ.get("BTCDOM_TV_SYMBOL", "BTC.D")
BTCDOM_TV_ALT_SYMBOL = os.environ.get("BTCDOM_TV_ALT_SYMBOL", "CRYPTOCAP:BTC.D")
BTCD_FALLBACK_URL = os.environ.get("BTCD_FALLBACK_URL", "")
CMC_API_KEY = os.environ.get("CMC_API_KEY", "")

# Торговые настройки
DEPOSIT_AMOUNT = 1300.0
RISK_PERCENT = 0.01
RISK_USD = float(os.environ.get("RISK_USD", "10"))

# Инициализация клиента
client = TelegramClient(
    'userbot_session',
    api_id,
    api_hash,
    device_model='aboba-linux-custom',
    system_version='1.2.3-zxc-custom',
    app_version='1.0.1',
    lang_code='ru',
    system_lang_code='ru_RU',
    connection_retries=10,
    retry_delay=5,
    auto_reconnect=True,
    timeout=30,
    request_retries=5,
)
client.flood_sleep_threshold = 12 * 60 * 60  # 12 часов

# Настройки интеграции
LEVELS_API_URL = os.environ.get("LEVELS_API_URL", "http://127.0.0.1:8001/levels/intraday-search")
RESULT_RECIPIENT = os.environ.get("RESULT_RECIPIENT", "me")  # не используется
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7899189068:AAHGitC-EOAWLjgkabPsx5eC33zh26cdfuM")
BOT_CHAT_ID = os.environ.get("BOT_CHAT_ID", "202996676")

GO_SHORT_ENABLED = os.environ.get("GO_SHORT_ENABLED", "0").strip() == "1"
GO_LONG_ENABLED = os.environ.get("GO_LONG_ENABLED", "0").strip() == "1"

# Фильтры чатов
DEFAULT_ALLOW_CHAT_IDS = {-1002423680272, 616892418, 5703939817, 5708266033}
DEFAULT_ALLOW_LINK_IDS = {2423680272}
FORCE_ALLOW_CHAT_IDS = {-1002423680272}
DEFAULT_BLOCK_CHAT_IDS = set()
DEFAULT_BLOCK_LINK_IDS = set()

def _parse_allow_sets():
    def parse_int_list(env_name: str):
        raw = os.environ.get(env_name, "")
        out = []
        for part in raw.split(','):
            part = part.strip()
            if not part: continue
            try: out.append(int(part))
            except: pass
        return out
    ids = set(parse_int_list('ALLOW_CHAT_IDS'))
    link_ids = set(parse_int_list('ALLOW_CHAT_LINK_IDS'))
    for cid in link_ids:
        try: ids.add(int(f"-100{cid}"))
        except: continue
    ids.update(DEFAULT_ALLOW_CHAT_IDS)
    for cid in DEFAULT_ALLOW_LINK_IDS:
        try: ids.add(int(f"-100{cid}"))
        except: pass
    ids.update(FORCE_ALLOW_CHAT_IDS)
    return ids

ALLOW_CHAT_IDS = _parse_allow_sets()
BLOCK_CHAT_IDS = set()

try:
    def _parse_block_sets():
        def parse_int_list(env_name: str):
            raw = os.environ.get(env_name, "")
            out = []
            for part in raw.split(','):
                part = part.strip()
                if not part: continue
                try: out.append(int(part))
                except: pass
            return out
        ids = set(parse_int_list('BLOCK_CHAT_IDS'))
        link_ids = set(parse_int_list('BLOCK_CHAT_LINK_IDS'))
        for cid in link_ids:
            try: ids.add(int(f"-100{cid}"))
            except: continue
        ids.update(DEFAULT_BLOCK_CHAT_IDS)
        for cid in DEFAULT_BLOCK_LINK_IDS:
            try: ids.add(int(f"-100{cid}"))
            except: pass
        return ids
    BLOCK_CHAT_IDS = _parse_block_sets()
except:
    BLOCK_CHAT_IDS = set()

ALLOW_CHAT_NAMES = set()
try:
    raw_names = os.environ.get("ALLOW_CHAT_NAMES", "")
    for part in raw_names.split(','):
        name = part.strip()
        if name: ALLOW_CHAT_NAMES.add(name.lower())
    for n in {"TRENDS Cryptovizor"}:
        ALLOW_CHAT_NAMES.add(n.lower())
except:
    pass

PROCESS_SAVED_INPUT = os.environ.get("PROCESS_SAVED_INPUT", "0").strip() == "1"
SELF_CHAT_ID = None

# Эмодзи
RED_SET = {"🟥", "🔴"}
GREEN_SET = {"🟢", "🟩"}

# ============================================================================
# ФУНКЦИИ ОТПРАВКИ ЧЕРЕЗ BOT API
# ============================================================================

async def _send_via_bot(text: str) -> bool:
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
        print(f"Ошибка Bot API sendMessage: {e}")
        return False

async def _send_file_via_bot(caption: str, file_bytes: bytes, filename: str = "chart.png") -> bool:
    if not BOT_TOKEN or not BOT_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        from io import BytesIO
        files = {"photo": (filename, BytesIO(file_bytes), "image/png")}
        data = {"chat_id": BOT_CHAT_ID, "caption": caption[:1024]}
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, data=data, files=files)
            r.raise_for_status()
            jd = r.json()
            return bool(jd.get("ok"))
    except Exception as e:
        print(f"Ошибка отправки файла через Bot API: {e}")
        return False

async def notify(text: str):
    """Отправляет ТОЛЬКО через Bot API. Без fallback на userbot."""
    if await _send_via_bot(text):
        return
    print(f"[Bot API failed, skipped] {text[:100]}")

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (БЕЗ ИЗМЕНЕНИЙ)
# ============================================================================

def _extract_symbol_from_hashtags(text: str):
    try:
        tags = re.findall(r"#([A-Z0-9\.]+)", (text or "").upper())
        for t in tags:
            t = t.replace(".P", "")
            if t.endswith("USDT"):
                return t
    except:
        pass
    return None

def _parse_go_short_blocks(text: str):
    results = []
    try:
        if not isinstance(text, str) or not text:
            return results
        cleaned = re.sub(r"[\*`_]", "", text)
        cleaned = cleaned.replace("\u00A0", " ").replace("\u2060", "")
        lines = cleaned.splitlines()
        n = len(lines)
        for i, line in enumerate(lines):
            if re.search(r"\bGO\s+SHORT\b", line, flags=re.IGNORECASE):
                symbol = None
                price = None
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
                for j in range(i, min(n, i+7)):
                    m = re.search(r"Цена[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", (lines[j] or ""))
                    if m:
                        try:
                            price = float(m.group(1))
                        except:
                            price = None
                        if price is not None:
                            break
                if symbol and (price is not None):
                    results.append({"symbol": symbol, "price": price})
    except:
        pass
    return results

def _parse_go_long_blocks(text: str):
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
                symbol = _extract_symbol_from_hashtags(cleaned)
                if not symbol:
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
                        except:
                            price = None
                        if price is not None:
                            break
                if symbol and (price is not None):
                    results.append({"symbol": symbol, "price": price})
    except:
        pass
    return results

async def _get_atr_pct_taapi(symbol_usdt: str, interval: str = "1h", period: int = 14) -> float:
    if not TAAPI_KEY:
        return None
    ta_symbol = _to_taapi_symbol(symbol_usdt)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = f"https://api.taapi.io/atr?secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}&period={period}"
            r = await client.get(url)
            if r.status_code != 200:
                return None
            jd = r.json() or {}
            atr_val = jd.get("value")
            if atr_val is None:
                return None
            px = await _get_binance_price(symbol_usdt)
            if not px:
                return None
            pxf = float(px)
            if pxf <= 0:
                return None
            return float(atr_val) / pxf
    except:
        return None

def _compute_short_risk_params(entry: float, res_zone: tuple, atr_pct: float) -> dict:
    hi = float(res_zone[1]) if res_zone else float(entry)
    base = 0.01
    atr = float(atr_pct or 0.0)
    sl_pct = min(base + atr, 0.02)
    sl = hi * (1.0 + sl_pct)
    R_abs = float(sl) - float(entry)
    if R_abs <= 0:
        R_abs = max(0.001 * float(entry), 1e-12)
    tp = float(entry) - 4.0 * R_abs
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
            print(f"[GO SHORT FAIL] {symbol}: no resistance zone (levels+pivots)")
            continue
        if not _is_inside_zone(price, chosen):
            print(f"[GO SHORT FAIL] {symbol}: price not in zone {chosen[0]:.6f}-{chosen[1]:.6f} (price={float(price):.6f})")
            continue
        move5h = await _get_move_5h_pct(symbol)
        if move5h is not None and move5h > 15.0:
            print(f"[GO SHORT] skip pump {symbol}: move5h={move5h:.2f}%")
            continue
        atr_pct = await _get_atr_pct_taapi(symbol, interval="1h", period=14)
        params = _compute_short_risk_params(float(price), chosen, atr_pct)
        try:
            sl_pct = (float(params["sl"]) / float(price) - 1.0) * 100.0
            if sl_pct > 4.0:
                print(f"[GO SHORT] reject {symbol}: SL={sl_pct:.2f}% > 4% (entry={float(price):.6f}, SL={float(params['sl']):.6f})")
                continue
        except:
            pass
        try:
            entry_f = float(price)
            sl_f = float(params["sl"])
            risk_usd = 5.0
            denom = abs(sl_f - entry_f)
            qty = risk_usd / denom if denom > 0 else 0.0
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
            except:
                pass
        except:
            sent = False
        await notify(f"[GO SHORT] {symbol} price={price} zone={chosen[0]:.6f}-{chosen[1]:.6f} sent={sent}\n"
                     f"SL={params['sl']:.6f} TP={params['tp']:.6f} BE@{params['be']['pnl']:.3f}% TRAIL@{params['slx']['trailingProfit']:.3f}% lag={params['slx']['trailingLag']:.3f}%")

async def _process_go_long_message(text: str):
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
            print(f"[GO LONG FAIL] {symbol}: no support zones in levels.db")
            continue
        chosen = _choose_nearest_zone(support_list, float(price))
        if not chosen:
            print(f"[GO LONG FAIL] {symbol}: no support zone near price (price={float(price):.6f})")
            continue
        if not _is_inside_zone(price, chosen):
            print(f"[GO LONG FAIL] {symbol}: price not in zone {chosen[0]:.6f}-{chosen[1]:.6f} (price={float(price):.6f})")
            continue
        move5h = await _get_move_5h_pct(symbol)
        if move5h is not None and move5h > 15.0:
            print(f"[GO LONG] skip pump {symbol}: move5h={move5h:.2f}%")
            continue
        atr_pct = await _get_atr_pct_taapi(symbol, interval="1h", period=14)
        params = _compute_long_risk_params(float(price), chosen, atr_pct)
        try:
            sl_pct = (1.0 - float(params["sl"]) / float(price)) * 100.0
            if sl_pct > 4.0:
                print(f"[GO LONG] reject {symbol}: SL={sl_pct:.2f}% > 4% (entry={float(price):.6f}, SL={float(params['sl']):.6f})")
                continue
        except:
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
            payload["_route"] = "long"
            sent = await send_payload(payload)
            try:
                write_webhook_history(datetime.utcnow().isoformat(), payload, sent)
            except:
                pass
        except:
            sent = False
        await notify(f"[GO LONG] {symbol} price={price} zone={chosen[0]:.6f}-{chosen[1]:.6f} sent={sent}\n"
                     f"SL={params['sl']:.6f} TP={params['tp']:.6f} BE@{params['be']['pnl']:.3f}% TRAIL@{params['slx']['trailingProfit']:.3f}% lag={params['slx']['trailingLag']:.3f}%")

def _is_inside_zone(price: float, zone: tuple) -> bool:
    try:
        if price is None or not zone:
            return False
        low, high = float(zone[0]), float(zone[1])
        if low > high:
            low, high = high, low
        return low <= float(price) <= high
    except:
        return False

async def _get_move_5h_pct(symbol_usdt: str) -> float:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.binance.com/api/v3/klines", params={"symbol": symbol_usdt, "interval": "1h", "limit": 6})
            r.raise_for_status()
            kl = r.json()
            if not isinstance(kl, list) or len(kl) < 6:
                return None
            c0 = float(kl[0][4]); cN = float(kl[-1][4])
            if c0 <= 0:
                return None
            return abs(cN / c0 - 1.0) * 100.0
    except:
        return None

async def _evaluate_go_short_blocks(text: str):
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
            if not chosen:
                for tf in ("1h","4h","12h"):
                    piv = await _get_taapi_pivots(symbol, interval=tf)
                    if piv:
                        candidates = []
                        for k in ("R1","R2","R3"):
                            if k in piv and piv[k] is not None:
                                candidates.append((k, float(piv[k])))
                        if candidates:
                            key, lvl = min(candidates, key=lambda kv: abs(kv[1] - float(price)))
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
            await notify(msg)
    except:
        pass

def _parse_and_cache_key_levels(message: str):
    try:
        if not isinstance(message, str):
            return
        text = message
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
            except:
                pass
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
                        except:
                            src_ts = None
                    if side == "support":
                        upsert_levels(symbol, timeframe, [(low, high)], [], source_ts=src_ts)
                    else:
                        upsert_levels(symbol, timeframe, [], [(low, high)], source_ts=src_ts)
        except:
            pass
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
                    src_ts = datetime.utcnow().isoformat()
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
        except:
            pass
    except:
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
    m = re.search(r"\$([A-Za-z0-9]{2,15})", line)
    if not m:
        return None, None, None
    ticker = m.group(1).upper()
    squares = [ch for ch in line if ch in RED_SET or ch in GREEN_SET]
    if len(squares) < 4:
        return None, None, None
    tf_m = re.search(r"frame\s*:\s*(\d+)[mMhH]", line, flags=re.IGNORECASE)
    origin_tf = None
    if tf_m:
        val = tf_m.group(1)
        if val in {"30", "60", "120"}:
            origin_tf = f"{val}m"
    return ticker, squares[:5], (origin_tf or "30m")

def _is_correction_combo(squares):
    if len(squares) != 5:
        return False
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
    if len(squares) != 5:
        return False
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
    return list(squares) == ['🔴','🔴','🔴','🟥','🟢']

def _is_close_short_combo(squares):
    return list(squares) == ['🟢','🟢','🟢','🟩','🔴']

async def _get_binance_price(symbol: str) -> float:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol})
            if response.status_code == 200:
                data = response.json()
                return float(data.get("price", 0))
    except:
        pass
    return None

async def _get_24h_volume_usd(symbol_usdt: str) -> float:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": symbol_usdt})
            r.raise_for_status()
            jd = r.json()
            qv = jd.get("quoteVolume")
            return float(qv) if qv is not None else None
    except:
        return None

async def _get_1h_volume_spike(symbol_usdt: str) -> float:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.binance.com/api/v3/klines", params={"symbol": symbol_usdt, "interval": "1h", "limit": 25})
            r.raise_for_status()
            kl = r.json()
            if not isinstance(kl, list) or len(kl) < 2:
                return None
            vols = [float(k[7]) for k in kl]
            current = vols[-1]
            avg = sum(vols[-25:-1]) / max(1, len(vols[-25:-1]))
            if avg <= 0:
                return None
            return current / avg
    except:
        return None

def _calc_hours_since_iso(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_ts)
        delta = datetime.utcnow() - dt
        return delta.total_seconds() / 3600.0
    except:
        return 1e9

def _to_taapi_symbol(symbol_usdt: str) -> str:
    if symbol_usdt.endswith("USDT"):
        base = symbol_usdt[:-4]
        return f"{base}/USDT"
    return symbol_usdt

async def _get_rsi_1h_taapi(symbol_usdt: str) -> float:
    if not TAAPI_KEY:
        return None
    try:
        ta_symbol = _to_taapi_symbol(symbol_usdt)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"https://api.taapi.io/rsi?secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval=1h")
            r.raise_for_status()
            jd = r.json()
            return float(jd.get("value")) if jd and jd.get("value") is not None else None
    except:
        return None

async def _get_btcdom_rsi_1h_taapi() -> float:
    if not TAAPI_KEY:
        return None
    try:
        candidates = [
            ("tradingview", BTCDOM_TV_SYMBOL),
            ("tradingview", BTCDOM_TV_ALT_SYMBOL),
            ("binance", "BTCDOM/USDT"),
        ]
        async with httpx.AsyncClient(timeout=15.0) as client:
            for exch, sym in candidates:
                try:
                    r = await client.get(f"https://api.taapi.io/rsi?secret={TAAPI_KEY}&exchange={exch}&symbol={sym}&interval=1h")
                    if r.status_code == 200:
                        jd = r.json()
                        if jd and jd.get("value") is not None:
                            return float(jd.get("value"))
                except:
                    continue
    except:
        pass
    return None

async def _get_taapi_pivots(symbol_usdt: str, interval: str = "1h") -> dict:
    if not TAAPI_KEY:
        return {}
    ta_symbol = _to_taapi_symbol(symbol_usdt)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # PPSR
            try:
                r = await client.get(f"https://api.taapi.io/ppsr?secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}")
                if r.status_code == 200:
                    jd = r.json() or {}
                    out = {}
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
            except:
                pass
            # pivotPoints
            try:
                r = await client.get(f"https://api.taapi.io/pivotPoints?secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval={interval}")
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
            except:
                pass
    except:
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
    except:
        return None

async def _get_btcdom_rsi_1h_fallback() -> float:
    if not BTCD_FALLBACK_URL:
        return None
    try:
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
    except:
        return None

async def _get_btcdom_rsi_1h_cmc() -> float:
    if not CMC_API_KEY:
        return None
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {"interval": "1h", "count": 200, "convert": "USD"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get("https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/historical", headers=headers, params=params)
            if r.status_code != 200:
                return None
            jd = r.json()
            data = jd.get("data") or []
            if not isinstance(data, list) or not data:
                return None
            closes = []
            for row in data:
                dom = row.get("btc_dominance")
                if dom is not None:
                    closes.append(float(dom))
            if len(closes) < 15:
                return None
            return _compute_rsi_from_closes(closes, period=14)
    except:
        return None

async def _check_extra_filters(symbol_usdt: str, context: str):
    vol = await _get_24h_volume_usd(symbol_usdt)
    rsi1h = await _get_rsi_1h_taapi(symbol_usdt)
    if rsi1h is None:
        print(f"[ERROR] RSI 1h (Taapi) n/a for {symbol_usdt}")
        return False, vol, None
    if context == "long" and rsi1h > 45:
        return False, vol, rsi1h
    if context == "short" and rsi1h < 55:
        return False, vol, rsi1h
    return True, vol, rsi1h

def _calculate_order_volumes(first_price: float, last_price: float, sl_price: float, side: str) -> list:
    try:
        total_risk_usdt = RISK_USD
        risk_per_order = total_risk_usdt / 5
        if side == "buy":
            prices = [first_price - (first_price - last_price) * i / 4 for i in range(5)]
        else:
            prices = [first_price + (last_price - first_price) * i / 4 for i in range(5)]
        volumes = []
        for i, price in enumerate(prices):
            if side == "buy":
                price_diff = price - sl_price
            else:
                price_diff = sl_price - price
            if price_diff > 0:
                volume = risk_per_order / price_diff
                volumes.append(volume)
            else:
                volumes.append(0)
        return volumes
    except:
        return [0] * 5

async def _get_rsi_ema_12h(symbol_usdt: str):
    try:
        async with httpx.AsyncClient() as client:
            rsi = None
            if TAAPI_KEY:
                try:
                    ta_symbol = _to_taapi_symbol(symbol_usdt)
                    r = await client.get(f"https://api.taapi.io/rsi?secret={TAAPI_KEY}&exchange=binance&symbol={ta_symbol}&interval=12h")
                    if r.status_code == 200:
                        jd = r.json()
                        if isinstance(jd, dict) and jd.get("value") is not None:
                            rsi = float(jd.get("value"))
                except:
                    rsi = None
            if rsi is None:
                print(f"[ERROR] RSI 12h (Taapi) n/a for {symbol_usdt}")
            ema_url = f"https://api.binance.com/api/v3/klines?symbol={symbol_usdt}&interval=12h&limit=200"
            ema_response = await client.get(ema_url)
            if ema_response.status_code == 200:
                klines = ema_response.json()
                if len(klines) >= 200:
                    closes = [float(k[4]) for k in klines[-200:]]
                    multiplier = 2 / (200 + 1)
                    ema = closes[0]
                    for close in closes[1:]:
                        ema = (close * multiplier) + (ema * (1 - multiplier))
                else:
                    ema = None
            else:
                ema = None
        return rsi, ema
    except:
        return None, None

async def _check_12h_filters(symbol_usdt: str, context: str):
    try:
        rsi, ema = await _get_rsi_ema_12h(symbol_usdt)
        if rsi is None or ema is None:
            return False, rsi, ema, None
        current_price = None
        try:
            async with httpx.AsyncClient() as client:
                ticker_response = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol_usdt}")
                if ticker_response.status_code == 200:
                    current_price = float(ticker_response.json()['price'])
        except:
            pass
        if current_price is None:
            return False, rsi, ema, None
        if context == "long":
            rsi_ok = rsi >= 50
            ema_ok = current_price >= ema
            return (rsi_ok and ema_ok), rsi, ema, current_price
        else:
            rsi_ok = rsi <= 50
            ema_ok = current_price <= ema
            return (rsi_ok and ema_ok), rsi, ema, current_price
    except:
        return False, None, None, None

async def _post_level_search(symbol_usdt: str, context: str = "long", origin_tf: str = "30m"):
    payload = json.dumps({"symbol": symbol_usdt, "context": context, "origin_tf": origin_tf}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(LEVELS_API_URL, content=payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except:
        return None

async def _send_webhook_from_level(symbol_usdt: str, side: str, entry_price, sl_price, tp_price, level_zone=None, *, slx_enabled_override=None, slx_overrides=None, be_enabled_override=None, be_overrides=None):
    try:
        if entry_price is None or sl_price is None or tp_price is None:
            return False
        if level_zone and len(level_zone) == 2:
            level_low, level_high = level_zone
            if side == "buy":
                first_order_price = float(entry_price)
                last_order_price = float(level_high)
            else:
                first_order_price = float(entry_price)
                last_order_price = float(level_low)
        else:
            first_order_price = float(entry_price)
            last_order_price = float(entry_price)
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
        except:
            pass
        return sent
    except:
        return False

def escape_csv_text(text):
    if text is None:
        return ""
    text = str(text).replace('"', '""').replace('\n', ' ').replace('\r', ' ')
    return text

def write_to_csv(timestamp_utc, chat_id, chat_name, message_text):
    csv_path = os.path.join(os.path.dirname(__file__), "messages.csv")
    file_exists = os.path.exists(csv_path)
    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                headers = ["timestamp_utc", "chat_id", "chat_name", "message_text"]
                writer.writerow(headers)
            writer.writerow([
                timestamp_utc,
                chat_id,
                escape_csv_text(chat_name),
                escape_csv_text(message_text)
            ])
    except:
        pass

def write_to_realtime_csv(timestamp_utc, chat_id, chat_name, message_text):
    csv_path = os.path.join(os.path.dirname(__file__), "setup_messengers_realtime.csv")
    file_exists = os.path.exists(csv_path)
    try:
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
    except:
        pass

def write_to_log(timestamp_utc, chat_id, chat_name, message_text):
    log_path = os.path.join(os.path.dirname(__file__), "messages.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            log_line = f"{timestamp_utc},{chat_id},{chat_name},{message_text}\n"
            f.write(log_line)
    except:
        pass

def write_raw_snapshot(timestamp_utc, chat_id, chat_name, message_text):
    try:
        raw_path = os.path.join(os.path.dirname(__file__), "raw_messages.log")
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp_utc},{chat_id},{chat_name},{message_text}\n")
    except:
        pass

def write_webhook_history(timestamp_utc: str, payload: dict, sent_ok: bool):
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
    except:
        pass

# ============================================================================
# ОБРАБОТКА СОБЫТИЙ
# ============================================================================

async def _process_event(event):
    try:
        message = event.message.text or event.message.message or event.message.raw_text
        if not message:
            return

        msg_id = event.message.id
        text_hash = hash(message)
        now = datetime.utcnow()

        # Дедупликация
        if msg_id in PROCESSED_MESSAGES:
            prev_hash, prev_time = PROCESSED_MESSAGES[msg_id]
            if text_hash == prev_hash or (now - prev_time).total_seconds() < DEDUP_WINDOW_SEC:
                return
        PROCESSED_MESSAGES[msg_id] = (text_hash, now)
        if len(PROCESSED_MESSAGES) > 2000:
            PROCESSED_MESSAGES.clear()

        chat_id = event.chat_id
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

        timestamp_utc = datetime.utcnow().isoformat()
        write_to_log(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        write_raw_snapshot(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        write_to_csv(timestamp_utc, chat_id, f"{chat_name} | {sender_info} (ID:{sender_id})", message)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ID: {chat_id}] [{chat_name}] [{sender_info} (ID:{sender_id})] → {message[:100]}{'...' if len(message) > 100 else ''}")

        src_ok = False
        if event.chat_id in BLOCK_CHAT_IDS:
            src_ok = False
        elif event.chat_id in ALLOW_CHAT_IDS:
            src_ok = True
        else:
            fwd = getattr(event.message, 'fwd_from', None)
            if fwd:
                ch_id = getattr(fwd, 'channel_id', None)
                if ch_id is not None:
                    try:
                        ch_full = int(f"-100{int(ch_id)}")
                        if ch_full in ALLOW_CHAT_IDS and ch_full not in BLOCK_CHAT_IDS:
                            src_ok = True
                    except:
                        pass
            if not PROCESS_SAVED_INPUT and str(sender_id) == str(SELF_CHAT_ID):
                src_ok = False
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

        if not src_ok and isinstance(message, str):
            go_short = re.search(r"\bGO\s+SHORT\b", message, flags=re.IGNORECASE) is not None
            go_long = re.search(r"\bGO\s+LONG\b", message, flags=re.IGNORECASE) is not None
            if go_short or go_long:
                reason = "source not allowed"
                side = "GO SHORT" if go_short else "GO LONG"
                print(f"[{side}] skipped: {reason} (chat_id={chat_id}, name='{chat_name}')")

        if src_ok and message:
            _parse_and_cache_key_levels(message)

            if re.search(r"\bGO\s+SHORT\b", message, flags=re.IGNORECASE):
                try:
                    await _evaluate_go_short_blocks(message)
                except:
                    pass
                if GO_SHORT_ENABLED:
                    try:
                        await _process_go_short_message(message)
                    except:
                        pass
            if re.search(r"\bGO\s+LONG\b", message, flags=re.IGNORECASE):
                if GO_LONG_ENABLED:
                    try:
                        await _process_go_long_message(message)
                    except:
                        pass

            for line in message.splitlines():
                if not line.strip() or line.strip().startswith(("DOWNTREND", "UPTREND")):
                    continue
                ticker, squares, origin_tf = _line_to_ticker_and_squares(line)
                if not ticker:
                    continue
                symbol_usdt = ticker if ticker.endswith("USDT") else f"{ticker}USDT"

                if _is_correction_combo(squares):
                    filters_ok, rsi12h, ema200_12h, px = await _check_12h_filters(symbol_usdt, "long")
                    if not filters_ok:
                        rel = "above" if px >= ema200_12h else "below"
                        base = f"[LONG][{symbol_usdt}] RSI12h/EMA200: FAIL"
                        if rsi12h is not None and ema200_12h is not None and px is not None and rel:
                            base += f" (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)"
                        print(base)
                        continue

                    latest = get_latest_levels(symbol_usdt, max_age_minutes=0, prefer_timeframes=["4h", "1h", "12h", origin_tf])
                    support_list = (latest or {}).get("support", [])
                    resistance_list = (latest or {}).get("resistance", [])

                    last_price = None
                    try:
                        binance_price = await _get_binance_price(symbol_usdt)
                        if binance_price:
                            last_price = float(binance_price)
                    except:
                        pass
                    if last_price is None:
                        print(f"[LONG][{symbol_usdt}] price: FAIL (binance price n/a)")
                        continue

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
                        print(f"[LONG][{symbol_usdt}] levels(support): FAIL (no level found)")
                        continue

                    rng_low, rng_high = chosen_support
                    dist_pct = abs(last_price - rng_low) / last_price * 100.0
                    if dist_pct > 9.0:
                        print(f"[LONG][{symbol_usdt}] distance<=9%: FAIL ({dist_pct:.2f}%)")
                        continue

                    spike = await _get_1h_volume_spike(symbol_usdt)
                    try:
                        with open(os.path.join(os.path.dirname(__file__), 'spike_stats.csv'), 'a', newline='') as f:
                            w = csv.writer(f)
                            w.writerow([datetime.utcnow().isoformat(), symbol_usdt, 'long', spike])
                    except:
                        pass

                    distance_to_support = abs(last_price - rng_low) / last_price * 100
                    if distance_to_support >= 10:
                        print(f"[LONG][{symbol_usdt}] distance<10%: FAIL ({distance_to_support:.2f}%)")
                        continue

                    ok_extra, vol_usd, rsi1h = await _check_extra_filters(symbol_usdt, "long")
                    if not ok_extra:
                        print(f"[LONG][{symbol_usdt}] vol24h>=15M & RSI1h<=45 & BTC.D>55: FAIL (vol={vol_usd}, rsi1h={rsi1h})")
                        continue

                    sl_adjusted = rng_low * 0.99
                    if resistance_list:
                        tp_target = float(resistance_list[0][1])
                    else:
                        tp_target = last_price + ((rng_high - rng_low) * 3.0)

                    try:
                        await _send_webhook_from_level(
                            symbol_usdt, "buy",
                            last_price, sl_adjusted, tp_target, (rng_low, rng_high)
                        )
                    except:
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
                    except:
                        pass

                    # Отправка графика
                    try:
                        chart_req = {
                            "symbol": symbol_usdt,
                            "origin_tf": origin_tf,
                            "level_price": (rng_low + rng_high) / 2,
                            "range_low": rng_low,
                            "range_high": rng_high,
                            "entry": last_price,
                            "sl": sl_adjusted,
                            "signal_ts": datetime.utcnow().isoformat()
                        }
                        png_bytes = None
                        try:
                            async with httpx.AsyncClient(timeout=30.0) as client:
                                rimg = await client.post(LEVELS_API_URL.replace('/levels/intraday-search','') + '/chart/level.png', json=chart_req)
                                rimg.raise_for_status()
                                png_bytes = rimg.content
                        except:
                            pass
                        if png_bytes:
                            caption = f"{symbol_usdt} ({origin_tf}) chart"
                            if not await _send_file_via_bot(caption, png_bytes):
                                print(f"[Bot API file send failed] {symbol_usdt}")
                    except:
                        pass

                if _is_resistance_combo(squares):
                    filters_ok, rsi12h, ema200_12h, px = await _check_12h_filters(symbol_usdt, "short")
                    if not filters_ok:
                        rel = "below" if px <= ema200_12h else "above"
                        base = f"[SHORT][{symbol_usdt}] RSI12h/EMA200: FAIL"
                        if rsi12h is not None and ema200_12h is not None and px is not None and rel:
                            base += f" (rsi12h={rsi12h:.2f}, ema200={ema200_12h:.2f}, price={px:.4f}, price {rel} ema)"
                        print(base)
                        continue

                    latest = get_latest_levels(symbol_usdt, max_age_minutes=0, prefer_timeframes=["4h", "1h", "12h", origin_tf])
                    support_list = (latest or {}).get("support", [])
                    resistance_list = (latest or {}).get("resistance", [])

                    last_price = None
                    try:
                        binance_price = await _get_binance_price(symbol_usdt)
                        if binance_price:
                            last_price = float(binance_price)
                    except:
                        pass
                    if last_price is None:
                        print(f"[SHORT][{symbol_usdt}] price: FAIL (binance price n/a)")
                        continue

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
                        print(f"[SHORT][{symbol_usdt}] levels(resistance): FAIL (no level found)")
                        continue

                    rng_low, rng_high = chosen_resistance
                    dist_pct = abs(rng_low - last_price) / last_price * 100.0
                    if dist_pct > 9.0:
                        print(f"[SHORT][{symbol_usdt}] distance<=9%: FAIL ({dist_pct:.2f}%)")
                        continue

                    spike = await _get_1h_volume_spike(symbol_usdt)
                    try:
                        with open(os.path.join(os.path.dirname(__file__), 'spike_stats.csv'), 'a', newline='') as f:
                            w = csv.writer(f)
                            w.writerow([datetime.utcnow().isoformat(), symbol_usdt, 'short', spike])
                    except:
                        pass

                    distance_to_resistance = abs(last_price - rng_high) / last_price * 100
                    if distance_to_resistance >= 10:
                        print(f"[SHORT][{symbol_usdt}] distance<10%: FAIL ({distance_to_resistance:.2f}%)")
                        continue

                    ok_extra, vol_usd, rsi1h = await _check_extra_filters(symbol_usdt, "short")
                    if not ok_extra:
                        print(f"[SHORT][{symbol_usdt}] vol24h>=15M & RSI1h>=55 & BTC.D<45: FAIL (vol={vol_usd}, rsi1h={rsi1h})")
                        continue

                    sl_adjusted = rng_high * 1.01
                    if support_list:
                        tp_target = float(support_list[0][0])
                    else:
                        tp_target = last_price - ((rng_high - rng_low) * 3.0)

                    try:
                        await _send_webhook_from_level(
                            symbol_usdt, "sell",
                            last_price, sl_adjusted, tp_target, (rng_low, rng_high)
                        )
                    except:
                        pass

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
                    except:
                        pass

                    # Отправка графика
                    try:
                        chart_req = {
                            "symbol": symbol_usdt,
                            "origin_tf": origin_tf,
                            "level_price": (rng_low + rng_high) / 2,
                            "range_low": rng_low,
                            "range_high": rng_high,
                            "entry": last_price,
                            "sl": sl_adjusted,
                            "signal_ts": datetime.utcnow().isoformat()
                        }
                        png_bytes = None
                        try:
                            async with httpx.AsyncClient(timeout=30.0) as client:
                                rimg = await client.post(LEVELS_API_URL.replace('/levels/intraday-search','') + '/chart/level.png', json=chart_req)
                                rimg.raise_for_status()
                                png_bytes = rimg.content
                        except:
                            pass
                        if png_bytes:
                            caption = f"{symbol_usdt} ({origin_tf}) chart"
                            if not await _send_file_via_bot(caption, png_bytes):
                                print(f"[Bot API file send failed] {symbol_usdt}")
                    except:
                        pass

                if _is_close_long_combo(squares):
                    try:
                        payload = build_close_payload(symbol_usdt, position_side='long')
                        await send_payload(payload)
                        await notify(f"Закрыть LONG: {symbol_usdt} (по паттерну 🔴🔴🔴🟥🟢)")
                    except:
                        pass

                if _is_close_short_combo(squares):
                    try:
                        payload = build_close_payload(symbol_usdt, position_side='short')
                        await send_payload(payload)
                        await notify(f"Закрыть SHORT: {symbol_usdt} (по паттерну 🟢🟢🟢🟩🔴)")
                    except:
                        pass

            try:
                hook_res_full = await try_process_screener_message(message)
                if hook_res_full:
                    write_to_realtime_csv(timestamp_utc, chat_id, chat_name, message)
            except:
                pass

    except FloodWaitError as e:
        print(f"⏳ [_process_event] FloodWait: ожидание {e.seconds}с")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"❌ [_process_event] Ошибка обработки сообщения: {e}")
        traceback.print_exc()

# Обработчики ТОЛЬКО для разрешённых чатов
@client.on(events.NewMessage(incoming=True, chats=list(ALLOW_CHAT_IDS)))
async def handler(event):
    await _process_event(event)

@client.on(events.MessageEdited(incoming=True, chats=list(ALLOW_CHAT_IDS)))
async def handler_edit(event):
    await _process_event(event)

async def check_account_status():
    try:
        print("🔍 Проверка статуса аккаунта...")
        await client.send_message('SpamBot', '/start')
        await asyncio.sleep(1)
        print("✅ Аккаунт не ограничен")
        return True
    except FloodWaitError as e:
        print(f"⚠️ FloodWait при проверке: ожидание {e.seconds}с")
        return False
    except:
        print("⚠️ Не удалось проверить статус")
        return False

async def main():
    print("🤖 Telegram Userbot запускается...")
    try:
        await client.start(phone)
        me = await client.get_me()
        if me and getattr(me, 'id', None):
            global SELF_CHAT_ID
            SELF_CHAT_ID = int(me.id)
            print(f"✅ Подключение установлено! Аккаунт: {me.first_name or 'Unknown'} (ID: {me.id})")
        await check_account_status()
        print("👂 Слушаем сообщения ТОЛЬКО из разрешённых чатов...")
        print("─" * 60)
        try:
            log_path = os.path.join(os.path.dirname(__file__), "messages.log")
            if os.path.exists(log_path):
                imported = import_levels_from_log(log_path)
                if imported:
                    print(f"📊 [Кэш] Импортировано блоков Key Levels: {imported}")
        except:
            pass
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\n🛑 Получен сигнал остановки (Ctrl+C)...")
    except FloodWaitError as e:
        print(f"\n❌ FloodWaitError: требуется ожидание {e.seconds}с ({e.seconds/3600:.1f}ч)")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        traceback.print_exc()
    finally:
        print("\n🔌 Отключаемся от Telegram...")
        try:
            if client.is_connected():
                await client.disconnect()
                print("✅ Отключение завершено успешно")
        except:
            print("⚠️ Ошибка при отключении")
        print("👋 Userbot остановлен")

if __name__ == "__main__":
    client.loop.run_until_complete(main())