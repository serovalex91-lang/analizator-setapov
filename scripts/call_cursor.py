import json
import sys
import os

import requests


def main() -> int:
    url = os.getenv("CURSOR_URL", "http://127.0.0.1:8003/cursor/run")
    payload = {
        "setup": {
            "symbol": "AVAXUSDT",
            "correction_tf": ["30m", "60m"],
            "htf_trend_up": ["120m", "240m", "720m"],
            "price_above_ma200_12h": True,
            "rsi_12h_gt_50": True,
        },
        # Optionally relax threshold to increase chance of 'placed'
        # "scoring_prefs": {"threshold_enter": 6.5},
    }
    try:
        resp = requests.post(url, json=payload, timeout=90)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            sys.stdout.write(resp.text)
            return 0
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        sys.stderr.write(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


