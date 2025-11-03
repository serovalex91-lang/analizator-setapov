import os
import json
import math
import time
import csv
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import httpx
import pandas as pd
import matplotlib.pyplot as plt

from app.services.levels import find_best_level
from backtest_levels.engine_config import LEVEL_ENGINE, PIVOT_TF
from backtest_levels.levels_svet import Settings as SvetSettings
from backtest_levels.levels_svet import find_level_zones_with_quality, pick_best_zone
from backtest_levels.levels_pivots import find_pivot_level, PivotSettings
from app.services.taapi_bulk import session_vwap, classic_pivots
from app.services.utils import infer_tick_from_price, nearest_round

from backtest_levels.config import (
    MESSAGES_DIR,
    COINAPI_DIR,
    OUTPUT_DIR,
    MONTHS,
    TAAPI_BASE_URL,
    BACK_CANDLES,
    FWD_CANDLES,
    LEVEL_LOOKBACK_DAYS,
)

# В бэктесте игнорируем форму (квадрат/круг), считаем только цвет
GREEN_SET = {"🟢", "🟩"}
RED_SET = {"🔴", "🟥"}

# Разрешённые последовательности по цветам (R/G) для LONG/SHORT
ALLOWED_LONG_RG = { "RGGGG", "GRGGG", "GGRGG" }
ALLOWED_SHORT_RG = { "Grrrr".upper().replace('R','R').replace('G','G') }  # placeholder to keep syntax
# Переопределим явно, чтобы не путаться
ALLOWED_SHORT_RG = { "GRRRR", "GGRRR", "GGGRR" }


def ensure_output_dir(path: str):
    os.makedirs(path, exist_ok=True)


def parse_messages_for_symbol(symbol: str, months: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    # Ищем файл по имени символа в папке сообщений
    # Допускаем APE / APEUSDT имена: ищем по startswith
    # Нормализуем сравнение: убираем USDT без учета регистра
    sym_prefix = symbol.upper().replace("USDT", "")
    files = [f for f in os.listdir(MESSAGES_DIR) if f.upper().startswith(sym_prefix) and f.endswith(".json")]
    out: List[Dict[str, Any]] = []
    for fn in files:
        full = os.path.join(MESSAGES_DIR, fn)
        with open(full, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Telegram export может быть массивом сообщений или объектом со списком
        msgs = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            try:
                date_iso = m.get("date")  # UTC+5 по описанию
                if not date_iso:
                    continue
                # Преобразуем в UTC: исходно +5, значит вычитаем 5 часов
                dt_local = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                dt_utc = dt_local - timedelta(hours=5)

                # Собираем плоский текст
                txt = ""
                if isinstance(m.get("text"), list):
                    for t in m["text"]:
                        if isinstance(t, str):
                            txt += t
                        elif isinstance(t, dict):
                            txt += t.get("text", "")
                elif isinstance(m.get("text"), str):
                    txt = m["text"]

                # Достаём тикер и 5 эмодзи (цвет), форму игнорируем
                # Тикер как $WORD или WORD; для бэктеста приводим к USDT
                import re
                m_t = re.search(r"\$?([A-Z0-9]{2,15})", txt)
                if not m_t:
                    continue
                tkr = m_t.group(1).upper()
                # выбираем 5 первых из набора зелёных/красных (любой формы)
                balls = [ch for ch in txt if ch in GREEN_SET or ch in RED_SET]
                if len(balls) < 5:
                    continue
                balls = balls[:5]
                # нормализуем в R/G
                rg = ''.join('G' if ch in GREEN_SET else 'R' for ch in balls)

                # Проверяем разрешённые шаблоны по цветам (форма не важна)
                context = None
                if rg in ALLOWED_LONG_RG:
                    context = "long"
                elif rg in ALLOWED_SHORT_RG:
                    context = "short"
                else:
                    continue

                # frame из текста (если есть), по умолчанию 30m
                m_frame = re.search(r"frame\s*:?\s*(\d+)[mMhH]", txt)
                origin_tf = "30m"
                if m_frame:
                    val = m_frame.group(1)
                    origin_tf = f"{val}m"

                out.append({
                    "dt_utc": dt_utc.replace(tzinfo=timezone.utc),
                    "symbol": tkr + ("USDT" if not tkr.endswith("USDT") else ""),
                    "context": context,
                    "origin_tf": origin_tf,
                })
            except Exception:
                continue
    # фильтр по месяцам
    if months:
        use_months = months
        def month_ok(d: datetime) -> bool:
            ym = f"{d.year}-{d.month:02d}"
            return ym in use_months
        out = [x for x in out if month_ok(x["dt_utc"]) ]
    return out


def parse_balls_jsonl(symbol: str, months: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Читает унифицированные сигналы из unifiedcandels/{SYMBOL}/signals/balls.jsonl
    и возвращает список событий: {dt_utc, symbol, context, origin_tf}.
    Предпочтительно использовать вместо прямого парсинга Telegram JSON.
    """
    base = os.path.expanduser(os.path.join('~/Desktop/unifiedcandels', symbol, 'signals', 'balls.jsonl'))
    if not os.path.exists(base):
        return []
    out: List[Dict[str, Any]] = []
    allowed_long = {"RGGGG", "GRGGG", "GGRGG"}
    allowed_short = {"GRRRR", "GGRRR", "GGGRR"}
    with open(base, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ev_symbol = ev.get('symbol') or symbol
            if ev_symbol.upper() != symbol.upper():
                continue
            st = ev.get('signal_ts')
            if not st:
                continue
            try:
                dt_utc = datetime.fromisoformat(st.replace('Z','+00:00')).astimezone(timezone.utc)
            except Exception:
                continue
            # months filter
            if months:
                ym = f"{dt_utc.year}-{dt_utc.month:02d}"
                if ym not in months:
                    continue
            # derive RG sequence
            rg = None
            if isinstance(ev.get('emojis'), list) and ev['emojis']:
                seq = []
                for e in ev['emojis'][:5]:
                    color = (e.get('color') or '').lower()
                    seq.append('G' if 'green' in color else 'R')
                if len(seq) == 5:
                    rg = ''.join(seq)
            if not rg and isinstance(ev.get('color_seq'), str):
                cs = ev['color_seq'].upper()
                if len(cs) >= 5:
                    rg = cs[:5]
            if rg not in allowed_long | allowed_short:
                continue
            context = 'long' if rg in allowed_long else 'short'
            origin_tf = ev.get('frame_hint') or '30m'
            out.append({
                'dt_utc': dt_utc,
                'symbol': symbol,
                'context': context,
                'origin_tf': origin_tf,
            })
    return out


def read_coinapi_csv(symbol: str, interval: str) -> pd.DataFrame:
    # Ищем CSV по маске в каталоге COINAPI_DIR (ожидаем файлы за нужные месяцы)
    # Предполагаем столбцы: time, open, high, low, close, volume (UTC)
    frames: List[pd.DataFrame] = []
    for root, _, files in os.walk(COINAPI_DIR):
        for fn in files:
            if symbol in fn and interval in fn and fn.endswith('.csv'):
                full = os.path.join(root, fn)
                try:
                    df = pd.read_csv(full)
                    # нормализуем
                    if 'time' in df.columns:
                        try:
                            df['t'] = pd.to_datetime(df['time'], utc=True)
                        except Exception:
                            df['t'] = pd.to_datetime(df['time'])
                    elif 'timestamp' in df.columns:
                        df['t'] = pd.to_datetime(df['timestamp'], utc=True)
                    else:
                        continue
                    for c in ['open','high','low','close','volume']:
                        if c in df.columns:
                            df[c] = pd.to_numeric(df[c], errors='coerce')
                    frames.append(df[['t','open','high','low','close','volume']].dropna())
                except Exception:
                    continue
    if not frames:
        return pd.DataFrame(columns=['t','open','high','low','close','volume'])
    out = pd.concat(frames, ignore_index=True)
    out.sort_values('t', inplace=True)
    out.drop_duplicates(subset=['t'], inplace=True)
    return out


def read_unified_csv(symbol: str, interval: str) -> pd.DataFrame:
    """Читает из ~/Desktop/unifiedcandels/{SYMBOL}/{interval}.csv если есть."""
    base = os.path.expanduser(os.path.join('~/Desktop/unifiedcandels', symbol, f'{interval}.csv'))
    if not os.path.exists(base):
        return pd.DataFrame(columns=['t','open','high','low','close','volume'])
    try:
        df = pd.read_csv(base)
        if 't' in df.columns:
            df['t'] = pd.to_datetime(df['t'], utc=True, errors='coerce')
        elif 'time' in df.columns:
            df['t'] = pd.to_datetime(df['time'], utc=True, errors='coerce')
        else:
            return pd.DataFrame(columns=['t','open','high','low','close','volume'])
        for c in ['open','high','low','close','volume']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df[['t','open','high','low','close','volume']].dropna()
        df.sort_values('t', inplace=True)
        df.drop_duplicates(subset=['t'], inplace=True)
        return df
    except Exception:
        return pd.DataFrame(columns=['t','open','high','low','close','volume'])


def _get_taapi_key() -> Optional[str]:
    key = os.getenv("TAAPI_KEY")
    if key:
        return key
    try:
        from app.config import TAAPI_KEY  # type: ignore
        return TAAPI_KEY
    except Exception:
        return None


async def taapi_get_candles(symbol: str, interval: str, ts_from: int, ts_to: int) -> List[Dict[str, Any]]:
    url = f"{TAAPI_BASE_URL}/candle"
    params = {
        "secret": _get_taapi_key(),
        "exchange": "binance",
        "symbol": f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol,
        "interval": interval,
        "fromTimestamp": ts_from,
        "toTimestamp": ts_to,
        "addResultTimestamp": "true",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        jd = r.json()
        return jd.get('data', [])


async def taapi_get_indicator(ind: str, symbol: str, interval: str, params_extra: Dict[str, Any], ts_from: int, ts_to: int) -> Optional[float]:
    url = f"{TAAPI_BASE_URL}/{ind}"
    params = {
        "secret": _get_taapi_key(),
        "exchange": "binance",
        "symbol": f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol,
        "interval": interval,
        "fromTimestamp": ts_from,
        "toTimestamp": ts_to,
    }
    params.update(params_extra)
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        jd = r.json()
        data = jd.get('data') or jd
        # Возьмём последнюю точку
        try:
            if isinstance(data, list) and data:
                last = data[-1]
                # поля value/valueATR/valueEMA/valueADX
                for k in ('value','valueATR','valueEMA','valueADX'):
                    if k in last:
                        return float(last[k])
        except Exception:
            return None
    return None


def to_ohlcv_list(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {
            't': row.t.to_pydatetime().replace(tzinfo=timezone.utc).isoformat(),
            'open': float(row.open),
            'high': float(row.high),
            'low': float(row.low),
            'close': float(row.close),
            'volume': float(row.volume),
        }
        for _, row in df.iterrows()
    ]


async def process_symbol(symbol: str, months: Optional[List[str]] = None):
    # Предпочтительно используем унифицированные сигналы, если есть
    signals = parse_balls_jsonl(symbol, months=months)
    if not signals:
        signals = parse_messages_for_symbol(symbol, months=months)
    if not signals:
        print(f"No signals for {symbol}")
        return
    out_dir = os.path.join(OUTPUT_DIR, symbol)
    ensure_output_dir(out_dir)

    for sig in signals:
        dt = sig['dt_utc']
        origin_tf = sig['origin_tf']
        context = sig['context']

        # Чтение старших свечей: сперва из unifiedcandels, потом CoinAPI как фолбэк
        c30_all = read_unified_csv(symbol, '30m')
        if c30_all.empty:
            c30_all = read_coinapi_csv(symbol, '30m')
        c60_all = read_unified_csv(symbol, '60m')
        if c60_all.empty:
            c60_all = read_coinapi_csv(symbol, '60m')
        c120_all = read_unified_csv(symbol, '120m')
        if c120_all.empty:
            c120_all = read_coinapi_csv(symbol, '120m')
        # Для алгоритма используем данные только до сигнала,
        # для графика — окно вокруг сигнала, поэтому оставим _all нетронутыми
        cutoff = pd.Timestamp(dt) - pd.Timedelta(days=LEVEL_LOOKBACK_DAYS)
        c30 = c30_all[(c30_all['t'] >= cutoff) & (c30_all['t'] <= pd.Timestamp(dt))]
        c60 = c60_all[(c60_all['t'] >= cutoff) & (c60_all['t'] <= pd.Timestamp(dt))]
        c120 = c120_all[(c120_all['t'] >= cutoff) & (c120_all['t'] <= pd.Timestamp(dt))]

        # Недостающие LTF с Taapi
        ts_from = int((dt - timedelta(days=10)).timestamp())
        ts_to = int(dt.timestamp())
        c5_raw = await taapi_get_candles(symbol, '5m', ts_from, ts_to)
        c15_raw = await taapi_get_candles(symbol, '15m', ts_from, ts_to)
        def norm_direct(arr):
            rows = []
            for r in (arr or []):
                try:
                    ts = r.get('timestamp') or r.get('t') or r.get('time')
                    if isinstance(ts, (int, float)):
                        t = datetime.fromtimestamp(ts, tz=timezone.utc)
                    elif isinstance(ts, str):
                        # ISO8601 string
                        t = datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(timezone.utc)
                    else:
                        continue
                    rows.append({
                        't': t,
                        'open': float(r['open']),
                        'high': float(r['high']),
                        'low': float(r['low']),
                        'close': float(r['close']),
                        'volume': float(r.get('volume', 0.0)),
                    })
                except Exception:
                    continue
            if not rows:
                return pd.DataFrame(columns=['t','open','high','low','close','volume'])
            df = pd.DataFrame(rows)
            if 't' in df.columns:
                df.sort_values('t', inplace=True)
            return df
        c5 = norm_direct(c5_raw)
        c15 = norm_direct(c15_raw)

        # Fallback-логика для неполных/пустых 5m: используем 15m, затем 30m
        if c5 is None or c5.empty:
            if c15 is not None and not c15.empty:
                c5 = c15.copy()
            elif not c30.empty:
                c5 = c30.copy()
            else:
                c5 = pd.DataFrame(columns=['t','open','high','low','close','volume'])

        # Fallback-логика для пустых 15m: используем 30m
        if c15 is None or c15.empty:
            if not c30.empty:
                c15 = c30.copy()
            else:
                c15 = pd.DataFrame(columns=['t','open','high','low','close','volume'])

        # Индикаторы 30m из Taapi
        atr = await taapi_get_indicator('atr', symbol, '30m', {'period': 14}, ts_from, ts_to)
        ema200 = await taapi_get_indicator('ema', symbol, '30m', {'period': 200}, ts_from, ts_to)
        adx = await taapi_get_indicator('adx', symbol, '30m', {'period': 14}, ts_from, ts_to)

        indicators_30m = {'atr': atr, 'ema200': ema200, 'adx': adx}

        # Подготовка входов в find_best_level
        def as_list(df):
            return to_ohlcv_list(df)
        pack = {
            'c5': as_list(c5),
            'c15': as_list(c15),
            'c30': as_list(c30),
            'c1h': as_list(c60),
            'c2h': as_list(c120),
        }

        # Простейшая сессия/pivots/vwap из 30m (прошлый день)
        c30_list = pack['c30']
        if len(c30_list) < 50:
            print(f"Not enough 30m data for {symbol} at {dt}")
            continue
        # сессионный VWAP за текущую сессию (UTC день)
        day = dt.date()
        c30_today = [r for r in c30_list if datetime.fromisoformat(r['t']).date() == day]
        vwap_val = session_vwap(c30_today) if c30_today else None
        # Дневные пивоты по предыдущему дню
        prev_day = day - timedelta(days=1)
        c30_prev = [r for r in c30_list if datetime.fromisoformat(r['t']).date() == prev_day]
        if c30_prev:
            ph = max(float(r['high']) for r in c30_prev)
            pl = min(float(r['low']) for r in c30_prev)
            pc = float(c30_prev[-1]['close'])
            piv = classic_pivots(ph, pl, pc)
        else:
            piv = {}
        session_info = {
            'vwap_session': vwap_val,
            'pivots_daily': piv,
            'PDH': piv.get('R1'),
            'PDL': piv.get('S1'),
        }

        last_price = float(c5.iloc[-1]['close']) if len(c5) else float(c30.iloc[-1]['close'])
        tick_size = infer_tick_from_price(last_price)

        # Определяем сторону и выбираем движок
        side = 'long' if context == 'long' else 'short'
        level = None
        if LEVEL_ENGINE == 'svet':
            # используем 30m как базовый фрейм для зон (как в исходном проекте)
            df30 = c30.copy()
            zones = find_level_zones_with_quality(df30, side, SvetSettings())
            if zones:
                best = pick_best_zone(df30, zones)
                if best:
                    price = float(best['price'])
                    tol = float(best['atr']) * 0.5  # ширина зоны ±ATR/2
                    level = {
                        'price': price,
                        'score': 0.0,
                        'confluence': [best.get('quality','')],
                        'rr': None,
                        'tolerance': tol,
                        'tick_size': tick_size,
                    }
        elif LEVEL_ENGINE == 'pivots':
            # Для пивотов используем таймфрейм из конфигурации PIVOT_TF
            if PIVOT_TF.upper() == '1H':
                base_algo_df = c60
            elif PIVOT_TF.upper() == '4H':
                base_algo_df = c120
            else:
                base_algo_df = c60
            # ограничим выбор пивота экстремумами сигнального бара
            sig_idx = base_algo_df['t'].searchsorted(pd.Timestamp(dt))
            sig_idx = min(max(0, sig_idx-1), len(base_algo_df)-1)
            sig_bar_low = float(base_algo_df['low'].iloc[sig_idx]) if 'low' in base_algo_df.columns else float(base_algo_df['close'].iloc[sig_idx])
            sig_bar_high = float(base_algo_df['high'].iloc[sig_idx]) if 'high' in base_algo_df.columns else float(base_algo_df['close'].iloc[sig_idx])
            piv = find_pivot_level(base_algo_df, side, PivotSettings(), tick_size, sig_low=sig_bar_low, sig_high=sig_bar_high, signal_price=last_price)
            if piv:
                level = {
                    'price': float(piv['price']),
                    'score': 0.0,
                    'confluence': ['pivot'],
                    'rr': None,
                    'tolerance': float(piv['tolerance']),
                    'tick_size': tick_size,
                }
                pivot_bars = piv.get('pivot_bars') or []
            else:
                pivot_bars = []
        else:
            level = find_best_level(
                pack['c5'], pack['c15'], pack['c30'], pack['c1h'], pack['c2h'],
                session_info, tick_size, indicators_30m, side, origin_tf=origin_tf
            )

        # Рисуем свечи и RR
        base_df = {'30m': c30_all, '60m': c60_all, '120m': c120_all}.get(origin_tf, c30_all)
        # Вырезаем ровно 100 свечей назад и 100 вперёд от ближайшей свечи к dt
        if base_df.empty:
            continue
        idx = base_df['t'].searchsorted(pd.Timestamp(dt))
        left = max(0, idx - BACK_CANDLES)
        right = min(len(base_df), idx + FWD_CANDLES)
        plot_df = base_df.iloc[left:right].copy()
        if plot_df.empty:
            continue

        # Dark theme for pivots
        if LEVEL_ENGINE == 'pivots':
            plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12,6))
        # Свечи: ширина колонки ~70% расстояния между центрами (в днях для Matplotlib)
        if len(plot_df) >= 2:
            dtw_seconds = (plot_df['t'].iloc[1] - plot_df['t'].iloc[0]).total_seconds()
        else:
            dtw_seconds = 60 * 30
        width_days = (dtw_seconds / 86400.0) * 0.7
        up = plot_df['close'] >= plot_df['open']
        down = ~up
        ax.vlines(plot_df['t'], plot_df['low'], plot_df['high'], color='#777', linewidth=0.6, zorder=1)
        ax.bar(plot_df['t'][up], (plot_df['close'][up]-plot_df['open'][up]), bottom=plot_df['open'][up], width=width_days, color='#2ecc71', alpha=0.9, zorder=2, align='center')
        ax.bar(plot_df['t'][down], (plot_df['close'][down]-plot_df['open'][down]), bottom=plot_df['open'][down], width=width_days, color='#e74c3c', alpha=0.9, zorder=2, align='center')
        ax.axvline(pd.Timestamp(dt), color='gray', ls='--', alpha=0.6)
        # Ограничим ось X строго выбранным окном
        ax.set_xlim(plot_df['t'].iloc[0], plot_df['t'].iloc[-1])

        title = f"{symbol} | {context.upper()} | {origin_tf} | {dt.isoformat()}"
        pnl_text = ''
        if level:
            lp = float(level['price'])
            tol = float(level['tolerance'])
            score = float(level.get('score', 0.0))
            # Уровень/зона и подпись как на примере
            ax.axhspan(lp - tol, lp + tol, color='#b5651d', alpha=0.18)
            # Возраст уровня: время с последнего касания диапазона до сигнала
            left_df = plot_df[plot_df['t'] <= pd.Timestamp(dt)]
            last_touch = None
            if not left_df.empty:
                rng_low, rng_high = lp - tol, lp + tol
                touch_rows = left_df[(left_df['low'] <= rng_high) & (left_df['high'] >= rng_low)]
                if not touch_rows.empty:
                    last_touch = touch_rows['t'].iloc[-1]
            age_str = ''
            if last_touch is not None:
                age_hours = (pd.Timestamp(dt) - last_touch).total_seconds() / 3600.0
                if age_hours >= 24:
                    age_str = f" (Age: {int(age_hours//24)}d)"
                else:
                    age_str = f" (Age: {int(age_hours)}h)"
            ax.text(plot_df['t'].iloc[0], lp + tol*0.9,
                    f"{'RESISTANCE' if side=='short' else 'SUPPORT'}: {lp - tol:.3f} - {lp + tol:.3f}{age_str}",
                    color='#fff', fontsize=9, bbox=dict(facecolor='#5a4635', alpha=0.6, pad=4))

            # Entry/SL/TP из правил RR 1:3 (как в API); entry = level
            entry = lp
            sl = entry - 1.3*tol if side=='long' else entry + 1.3*tol
            rr = 3.0
            tp = entry + rr*max(entry - sl, 1e-9) if side=='long' else entry - rr*max(sl - entry, 1e-9)

            # Прямоугольник RR (зелёный/красный)
            t0 = pd.Timestamp(dt)
            t1 = plot_df['t'].iloc[-1]
            ax.fill_between([t0, t1], entry, tp, color='#2ecc71', alpha=0.18, label='TP area')
            ax.fill_between([t0, t1], sl, entry, color='#e74c3c', alpha=0.18, label='SL area')

            # Подписи
            ax.text(t0, tp, f"TP", color='#2ecc71', fontsize=8, va='bottom')
            ax.text(t0, sl, f"SL", color='#e74c3c', fontsize=8, va='top')
            ax.plot([t0], [entry], marker='o', color='#2e8b57')
            # Метка прихода сигнала (точка на цене закрытия ближайшей свечи)
            # Найдём ближайшую свечу к dt
            idx_sig = base_df['t'].searchsorted(pd.Timestamp(dt))
            idx_sig = min(max(0, idx_sig), len(base_df)-1)
            sig_price = float(base_df['close'].iloc[idx_sig])
            ax.plot([t0], [sig_price], marker='o', color='#3498db')
            ax.text(t0, sig_price, ' signal', color='#3498db', fontsize=8, va='bottom')
            # Pivot markers (if pivots engine)
            for pbar in (pivot_bars if LEVEL_ENGINE=='pivots' else []):
                pt = pd.Timestamp(pbar['t'])
                price = float(pbar['price'])
                col = 'yellow' if pbar.get('kind')=='high' else 'deepskyblue'
                ax.scatter([pt], [price], c=col, s=30, zorder=5)

            # Инфоблок по сделке
            rr_val = abs((tp - entry) / (entry - sl)) if (entry - sl) != 0 else 0.0
            info = f"Entry {entry:.4f}  SL {sl:.4f}  TP {tp:.4f}  RR {rr_val:.2f}  score {score:.2f}"
            ax.text(plot_df['t'].iloc[0], plot_df['high'].max(), info,
                    color='#ffffff', fontsize=8, bbox=dict(facecolor='#2f3640', alpha=0.5, pad=4))
            title += f" | level={lp:.6f} RR~{rr_val:.2f}"

            # PnL на форвардном участке (если есть close до t1)
            fwd = plot_df[(plot_df['t'] > t0) & (plot_df['t'] <= t1)]
            if not fwd.empty:
                last = float(fwd.iloc[-1]['close'])
                pnl = (last-entry) if side=='long' else (entry-last)
                pnl_text = f" PnL: {pnl:.4f}"
        ax.set_title(title + pnl_text)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(loc='upper left', fontsize=8)
        ax.grid(True, alpha=0.2)

        ym = f"{dt.year}-{dt.month:02d}"
        ensure_output_dir(os.path.join(OUTPUT_DIR, symbol, ym))
        out_path = os.path.join(OUTPUT_DIR, symbol, ym, f"{dt.strftime('%Y%m%d_%H%M')}.png")
        fig.tight_layout()
        fig.savefig(out_path)
        plt.close(fig)
        print(f"Saved {out_path}")


if __name__ == "__main__":
    import asyncio
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="e.g. AVAXUSDT")
    parser.add_argument("--months", default=",".join(MONTHS), help="comma-separated list, e.g. 2025-04,2025-05")
    args = parser.parse_args()
    months = [m.strip() for m in args.months.split(',') if m.strip()]
    asyncio.run(process_symbol(args.symbol, months=months))


