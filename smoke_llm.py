import asyncio, json
from cursor_pipeline import call_llm

payload = {
    "setup_parsed": {
        "symbol": "TESTUSDT",
        "direction": "short",
        "trend_balls": ["down"] * 5,
        "tf_balls": ["30m", "60m", "120m", "240m", "720m"],
        "current_price": 1.0,
        "tp": 0.9,
        "sl": 1.1,
        "key_levels": None,
        "levels_invalid": False,
    },
    "taapi": {
        "adx": {"4h": 25, "12h": 27},
        "dmi": {"4h": {"di_plus": 10, "di_minus": 20}, "12h": {"di_plus": 11, "di_minus": 19}},
        "macd": {"4h": {"macd": -1, "signal": -1, "hist": -0.1}, "12h": {"macd": -0.5, "signal": -0.4, "hist": -0.1}},
        "atr": {"4h": 0.01, "12h": 0.02},
        "mfi": {"4h": 40},
        "bb_width": {"4h": 0.05},
        "obv": {"4h_trend": "down"},
        "filters": {"ema200_12h": 1.2, "rsi_12h": 40, "price_above_ma200_12h": False, "rsi_12h_gt_50": False},
    },
    "market_context": {
        "last_price": 1,
        "funding_rate": 0.0,
        "open_interest_change_24h_pct": 0.0,
        "btc_context": {"adx_4h": 20, "macd_hist_4h": -0.1},
    },
    "meta": {"source": "smoke", "timestamp_utc": None},
}

async def main():
    p = dict(payload)
    p["_echo_guard"] = "DO_NOT_RETURN"
    res = await call_llm(p)
    print("RES:", json.dumps(res, ensure_ascii=False, indent=2) if res else "None")

if __name__ == "__main__":
    asyncio.run(main())

