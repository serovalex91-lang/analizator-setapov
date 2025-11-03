import os
import csv
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional

import httpx
import pandas as pd

from backtest_levels.config import COINAPI_DIR

COINAPI_KEY = os.getenv("COINAPI_KEY", "a5c1910f-574d-4948-b419-e50f9977b5d8")
BASE_URL = "https://rest.coinapi.io/v1"
TAAPI_BASE_URL = "https://api.taapi.io"

# TF map: our tf -> CoinAPI period_id
TF_TO_PERIOD = {
    "5m": "5MIN",
    "15m": "15MIN",
    "30m": "30MIN",
    "60m": "1HRS",
    "120m": "2HRS",
    "240m": "4HRS",
    "720m": "12HRS",
}

# Диапазон синхронизации (UTC)
SYNC_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
SYNC_END = datetime(2025, 6, 1, tzinfo=timezone.utc)  # exclusive


def list_symbol_dirs(root: str) -> List[str]:
    return sorted(
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and d.startswith("BINANCE_SPOT_") and d.endswith("_USDT")
    )


def parse_time(s: str) -> datetime:
    # CoinAPI CSV time is ISO like 2025-04-01T00:00:00.0000000Z; handle generically
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return pd.to_datetime(s, utc=True).to_pydatetime()


def minmax_from_existing(symbol_dir_path: str) -> Optional[Tuple[datetime, datetime]]:
    times: List[datetime] = []
    for root, _, files in os.walk(symbol_dir_path):
        for fn in files:
            if not fn.endswith('.csv'):
                continue
            full = os.path.join(root, fn)
            try:
                df = pd.read_csv(full)
                col = 'time' if 'time' in df.columns else ('t' if 't' in df.columns else None)
                if not col:
                    continue
                ts = pd.to_datetime(df[col], utc=True, errors='coerce').dropna()
                if ts.empty:
                    continue
                times.append(ts.min().to_pydatetime())
                times.append(ts.max().to_pydatetime())
            except Exception:
                continue
    if not times:
        return None
    return (min(times), max(times))


def month_range(start: datetime, end: datetime) -> List[str]:
    start = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    end = datetime(end.year, end.month, 1, tzinfo=timezone.utc)
    out = []
    cur = start
    while cur <= end:
        out.append(f"{cur.year}-{cur.month:02d}")
        # advance 1 month
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
    return out


async def coinapi_symbols_map() -> Dict[str, str]:
    """Return mapping like 'BINANCE_SPOT_AVAX_USDT' -> same id (validate existence).
    Also include alternative ids if found.
    """
    url = f"{BASE_URL}/symbols"
    headers = {"X-CoinAPI-Key": COINAPI_KEY}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        syms = r.json()
    out: Dict[str, str] = {}
    for s in syms:
        sid = s.get('symbol_id')
        if not isinstance(sid, str):
            continue
        if sid.startswith('BINANCE_SPOT_') and sid.endswith('_USDT'):
            out[sid] = sid
    return out


async def fetch_ohlcv(symbol_id: str, period_id: str, time_start: str, time_end: str) -> List[Dict]:
    url = f"{BASE_URL}/ohlcv/{symbol_id}/history"
    headers = {"X-CoinAPI-Key": COINAPI_KEY}
    params = {
        "period_id": period_id,
        "time_start": time_start,
        "time_end": time_end,
        "include_empty_items": "false",
    }
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()


def _get_taapi_key() -> Optional[str]:
    key = os.getenv("TAAPI_KEY")
    if key:
        return key
    try:
        from app.config import TAAPI_KEY  # type: ignore
        return TAAPI_KEY
    except Exception:
        return None


async def taapi_fetch(symbol_usdt: str, interval: str, start: datetime, end: datetime) -> List[Dict]:
    """Fetch candles via Taapi Direct /candle for a [start, end) month window.
    Tries multiple symbol variants and request styles to maximize success.
    """
    key = _get_taapi_key()
    if not key:
        return []

    url = f"{TAAPI_BASE_URL}/candle"
    tf_map = {
        '5m': '5m',
        '15m': '15m',
        '30m': '30m',
        '60m': '1h',
        '120m': '2h',
        '240m': '4h',
        '720m': '12h',
    }

    base = symbol_usdt.replace('USDT', '')
    # Popular renames/delist mappings for Binance symbols
    aliases_map: Dict[str, List[str]] = {
        'NANO': ['XNO'],
        'STRAT': ['STRAX'],
        # Keep originals first; some below may still fail if delisted
        'BCH': ['BCH'],
        'FTM': ['FTM'],
        'OMG': ['OMG'],
        'REN': ['REN'],
        'WAVES': ['WAVES'],
        'XMR': ['XMR'],
        'OP': ['OP'],
    }
    candidates: List[str] = [base]
    if base in aliases_map:
        for alt in aliases_map[base]:
            if alt not in candidates:
                candidates.append(alt)

    # Also try uppercased just in case
    if base.upper() not in candidates:
        candidates.append(base.upper())

    # Two request styles: (1) from/to timestamps, (2) results count
    # Estimate results for the month window by timeframe
    seconds = (end - start).total_seconds()
    tf_seconds = {
        '5m': 300,
        '15m': 900,
        '30m': 1800,
        '60m': 3600,
        '120m': 7200,
        '240m': 14400,
        '720m': 43200,
    }[interval]
    est_results = max(100, int(seconds // tf_seconds) + 5)

    async with httpx.AsyncClient(timeout=40.0) as client:
        last_error: Optional[Exception] = None
        for exch in ['binance', 'binanceusdm']:
            for sym_base in candidates:
                slash_symbol = f"{sym_base}/USDT"
                # Style 1: from/to timestamps
                params_ts = {
                    'secret': key,
                    'exchange': exch,
                    'symbol': slash_symbol,
                    'interval': tf_map.get(interval, interval),
                    'fromTimestamp': int(start.timestamp()),
                    'toTimestamp': int(end.timestamp()),
                    'addResultTimestamp': 'true'
                }
                try:
                    r = await client.get(url, params=params_ts)
                    r.raise_for_status()
                    data = r.json().get('data', [])
                    if data:
                        out = []
                        for d in data:
                            out.append({
                                'time_period_start': datetime.fromtimestamp(d.get('timestamp'), tz=timezone.utc).isoformat().replace('+00:00','Z'),
                                'price_open': d.get('open'),
                                'price_high': d.get('high'),
                                'price_low': d.get('low'),
                                'price_close': d.get('close'),
                                'volume_traded': d.get('volume'),
                            })
                        return out
                except Exception as e:
                    last_error = e

                # Style 2: results count
                params_res = {
                    'secret': key,
                    'exchange': exch,
                    'symbol': slash_symbol,
                    'interval': tf_map.get(interval, interval),
                    'results': est_results,
                    'addResultTimestamp': 'true'
                }
                try:
                    r = await client.get(url, params=params_res)
                    r.raise_for_status()
                    data = r.json().get('data', [])
                    if data:
                        # Filter to our window [start, end)
                        out = []
                        for d in data:
                            ts = int(d.get('timestamp'))
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                            if start <= dt < end:
                                out.append({
                                    'time_period_start': dt.isoformat().replace('+00:00','Z'),
                                    'price_open': d.get('open'),
                                    'price_high': d.get('high'),
                                    'price_low': d.get('low'),
                                    'price_close': d.get('close'),
                                    'volume_traded': d.get('volume'),
                                })
                        if out:
                            return out
                except Exception as e:
                    last_error = e

        if last_error:
            raise last_error
        return []


def write_month_csv(base_dir: str, tf: str, ym: str, rows: List[Dict]):
    out_dir = os.path.join(base_dir, tf, ym)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{tf}_{ym}.csv")
    # Normalize and sort
    df = pd.DataFrame([
        {
            'time': r.get('time_period_start') or r.get('time'),
            'open': r.get('price_open') or r.get('open'),
            'high': r.get('price_high') or r.get('high'),
            'low': r.get('price_low') or r.get('low'),
            'close': r.get('price_close') or r.get('close'),
            'volume': r.get('volume_traded') or r.get('volume'),
        } for r in rows
    ])
    if not df.empty:
        df['time'] = pd.to_datetime(df['time'], utc=True, errors='coerce')
        df = df.dropna().sort_values('time')
    df.to_csv(out_path, index=False)
    print('Saved', out_path, len(df))


def reorganize_existing_monthly(symbol_dir_path: str):
    """Repack any CSV files present into TF/YYYY-MM folders as single monthly CSV files."""
    # Detect files by suffix *_{tf}_{ym}.csv
    for root, _, files in os.walk(symbol_dir_path):
        for fn in files:
            if not fn.endswith('.csv'):
                continue
            full = os.path.join(root, fn)
            parts = fn.split('_')
            if len(parts) >= 3 and parts[-2] in ('5m','15m','30m','60m','120m'):
                tf = parts[-2]
                ym = parts[-1].replace('.csv','')
                df = pd.read_csv(full)
                # move into tf/ym/tf_ym.csv
                out_dir = os.path.join(symbol_dir_path, tf, ym)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{tf}_{ym}.csv")
                # append/merge
                if os.path.exists(out_path):
                    df_old = pd.read_csv(out_path)
                    df = pd.concat([df_old, df], ignore_index=True)
                if 'time' in df.columns:
                    df['time'] = pd.to_datetime(df['time'], utc=True, errors='coerce')
                    df = df.dropna().drop_duplicates(subset=['time']).sort_values('time')
                df.to_csv(out_path, index=False)


async def sync_symbol(symbol_dir: str, symbols_map: Dict[str, str]):
    base_dir = os.path.join(COINAPI_DIR, symbol_dir)
    reorganize_existing_monthly(base_dir)

    # Фиксированный диапазон
    start, end = SYNC_START, SYNC_END - timedelta(seconds=1)
    months = month_range(start, end)

    symbol_id = symbols_map.get(symbol_dir)
    use_taapi_only = False
    if not symbol_id:
        print('Symbol not found in CoinAPI map:', symbol_dir, '→ using Taapi only')
        use_taapi_only = True

    for tf, period_id in TF_TO_PERIOD.items():
        for ym in months:
            ym_start = datetime(int(ym.split('-')[0]), int(ym.split('-')[1]), 1, tzinfo=timezone.utc)
            if ym_start.month == 12:
                ym_end = datetime(ym_start.year+1,1,1,tzinfo=timezone.utc)
            else:
                ym_end = datetime(ym_start.year, ym_start.month+1,1,tzinfo=timezone.utc)
            # Skip future beyond global end range
            if ym_end <= start or ym_start >= end + timedelta(days=1):
                continue
            out_dir = os.path.join(base_dir, tf, ym)
            out_path = os.path.join(out_dir, f"{tf}_{ym}.csv")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                continue
            if not use_taapi_only:
                try:
                    rows = await fetch_ohlcv(
                        symbol_id,
                        period_id,
                        ym_start.isoformat().replace('+00:00','Z'),
                        ym_end.isoformat().replace('+00:00','Z'),
                    )
                    if not rows:
                        raise httpx.HTTPStatusError("empty", request=None, response=httpx.Response(204))
                    write_month_csv(base_dir, tf, ym, rows)
                    continue
                except Exception:
                    pass
            # Taapi path (either fallback or taapi-only)
            try:
                rows = await taapi_fetch(symbol_dir.replace('BINANCE_SPOT_','').replace('_',''), tf, ym_start, ym_end)
                if rows:
                    write_month_csv(base_dir, tf, ym, rows)
                else:
                    print('Fallback empty', symbol_dir, tf, ym)
            except Exception as e2:
                print('Error', symbol_dir, tf, ym, e2)


async def main():
    import asyncio
    syms = await coinapi_symbols_map()
    dirs = list_symbol_dirs(COINAPI_DIR)
    for d in dirs:
        await sync_symbol(d, syms)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())


