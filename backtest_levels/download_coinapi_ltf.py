import os
import csv
from datetime import datetime, timezone
from typing import List, Dict

import httpx

from backtest_levels.config import COINAPI_DIR


COINAPI_KEY = os.getenv("COINAPI_KEY", "a5c1910f-574d-4948-b419-e50f9977b5d8")
BASE_URL = "https://rest.coinapi.io/v1/ohlcv"

# Диапазоны для загрузки
RANGES = [
    ("2025-04-01T00:00:00Z", "2025-05-01T00:00:00Z"),
    ("2025-05-01T00:00:00Z", "2025-06-01T00:00:00Z"),
]

PERIODS = [
    ("5MIN", "5m"),
    ("15MIN", "15m"),
]


def list_symbol_dirs(root: str) -> List[str]:
    out: List[str] = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isdir(full) and name.startswith("BINANCE_SPOT_") and name.endswith("_USDT"):
            out.append(name)
    return sorted(out)


def csv_path(dir_path: str, symbol_dir: str, period_suffix: str, range_idx: int) -> str:
    ym = ["2025-04", "2025-05"][range_idx]
    base = f"{symbol_dir}_{period_suffix}_{ym}.csv"
    return os.path.join(dir_path, base)


async def fetch_ohlcv(symbol_id: str, period_id: str, time_start: str, time_end: str) -> List[Dict]:
    url = f"{BASE_URL}/{symbol_id}/history"
    headers = {"X-CoinAPI-Key": COINAPI_KEY}
    params = {
        "period_id": period_id,
        "time_start": time_start,
        "time_end": time_end,
        "include_empty_items": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()


def write_csv(path: str, rows: List[Dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([
                r.get("time_period_start"),
                r.get("price_open"),
                r.get("price_high"),
                r.get("price_low"),
                r.get("price_close"),
                r.get("volume_traded"),
            ])


async def main():
    import asyncio

    dirs = list_symbol_dirs(COINAPI_DIR)
    if not dirs:
        print("No symbol directories found in", COINAPI_DIR)
        return

    for symbol_dir in dirs:
        dir_path = os.path.join(COINAPI_DIR, symbol_dir)
        symbol_id = symbol_dir  # уже соответствует формату CoinAPI
        for idx, (time_start, time_end) in enumerate(RANGES):
            for period_id, suffix in PERIODS:
                out_csv = csv_path(dir_path, symbol_dir, suffix, idx)
                if os.path.exists(out_csv) and os.path.getsize(out_csv) > 0:
                    print("Skip exists:", out_csv)
                    continue
                print("Downloading", symbol_id, period_id, time_start, "->", time_end)
                try:
                    rows = await fetch_ohlcv(symbol_id, period_id, time_start, time_end)
                    write_csv(out_csv, rows)
                    print("Saved", out_csv, len(rows))
                    await asyncio.sleep(0.2)
                except Exception as e:
                    print("Error:", symbol_id, period_id, e)
                    await asyncio.sleep(1.0)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())




