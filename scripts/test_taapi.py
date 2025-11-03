import os
import sys
import json
from pathlib import Path

# Prefer requests if available; otherwise use urllib
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore


def load_env() -> None:
    if load_dotenv is None:
        return
    repo = Path(__file__).resolve().parents[1]
    # load project .env then service .env
    load_dotenv(repo / ".env")
    load_dotenv(repo / "intraday-levels-taapi" / ".env")


def get_taapi_key() -> str | None:
    return os.getenv("TAAPI_KEY")


def http_get_json(url: str) -> dict:
    if requests is not None:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # pragma: no cover
            return {"error": str(e), "url": url}
    # urllib fallback
    try:
        import urllib.request
        import urllib.error

        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except Exception as e:  # pragma: no cover
        return {"error": str(e), "url": url}


def http_post_json(url: str, body: dict) -> dict:
    if requests is not None:
        try:
            r = requests.post(url, json=body, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # pragma: no cover
            return {"error": str(e), "url": url}
    # urllib fallback
    try:
        import urllib.request
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except Exception as e:  # pragma: no cover
        return {"error": str(e), "url": url}


def probe(symbols: list[str]) -> dict:
    load_env()
    key = get_taapi_key()
    if not key:
        return {"ok": False, "error": "TAAPI_KEY is not set", "hint": "Add TAAPI_KEY to intraday-levels-taapi/.env"}

    # BTC 4h candle (5 results)
    btc = http_get_json(
        "https://api.taapi.io/candle"
        f"?secret={key}&exchange=binance&symbol=BTC/USDT&interval=4h&results=5&addResultTimestamp=true"
    )

    # ETH ATR via bulk (4h)
    bulk_body = {
        "secret": key,
        "construct": {
            "exchange": "binance",
            "symbol": "ETH/USDT",
            "interval": "4h",
            "indicators": [{"indicator": "atr", "period": 14}],
        },
    }
    eth_bulk = http_post_json("https://api.taapi.io/bulk", bulk_body)

    # SOL 12h RSI
    sol = http_get_json(
        "https://api.taapi.io/rsi"
        f"?secret={key}&exchange=binance&symbol=SOL/USDT&interval=12h&period=14"
    )

    return {
        "ok": True,
        "endpoints": {
            "btc_candle_4h": btc,
            "eth_bulk_atr_4h": eth_bulk,
            "sol_rsi_12h": sol,
        },
    }


if __name__ == "__main__":
    # Optionally allow a different BTC interval size or symbol list later
    result = probe(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    print(json.dumps(result, ensure_ascii=False))


