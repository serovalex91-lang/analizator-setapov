import os
import csv
import re
import json
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

import pandas as pd

# Paths
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MESSAGES_CSV = os.path.join(ROOT, 'messages.csv')
def discover_resultpairs_dir() -> Optional[str]:
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'проект шарики со светой ', 'resultpairs'),  # with trailing space
        os.path.join(home, 'проект шарики со светой', 'resultpairs'),
        os.path.join(home, 'проект шарики из телеграмма', 'resultpairs'),
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    # brute-force search for resultpairs in home
    for root, dirs, files in os.walk(home):
        if os.path.basename(root) == 'resultpairs':
            return root
    return None

RESULTPAIRS_DIR = discover_resultpairs_dir()
UNIFIED_ROOT = os.path.join(os.path.expanduser('~'), 'Desktop', 'unifiedcandels')

# Pattern dictionaries
LONG_PATTERNS_SQUARE = [
    "🟥🟢🟢🟢🟢",  # L1square
    "🔴🟥🟢🟢🟢",  # L2square
    "🔴🔴🟥🟢🟢",  # L3square
]
SHORT_PATTERNS_SQUARE = [
    "🟩🔴🔴🔴🔴",  # S1square
    "🟢🟩🔴🔴🔴",  # S2square
    "🟢🟢🟩🔴🔴",  # S3square
]

LONG_PATTERNS_BALL = [
    "🔴🟢🟢🟢🟢",  # L1b
    "🔴🔴🟢🟢🟢",  # L2b
    "🔴🔴🔴🟢🟢",  # L3b
]
SHORT_PATTERNS_BALL = [
    "🟢🔴🔴🔴🔴",  # S1b
    "🟢🟢🔴🔴🔴",  # S2b
    "🟢🟢🟢🔴🔴",  # S3b
]

PATTERN_ID_MAP: Dict[str, Tuple[str, str]] = {}
for idx, pat in enumerate(LONG_PATTERNS_SQUARE, start=1):
    PATTERN_ID_MAP[pat] = (f"L{idx}square", "long")
for idx, pat in enumerate(SHORT_PATTERNS_SQUARE, start=1):
    PATTERN_ID_MAP[pat] = (f"S{idx}square", "short")
for idx, pat in enumerate(LONG_PATTERNS_BALL, start=1):
    PATTERN_ID_MAP[pat] = (f"L{idx}b", "long")
for idx, pat in enumerate(SHORT_PATTERNS_BALL, start=1):
    PATTERN_ID_MAP[pat] = (f"S{idx}b", "short")

EMOJI_SET = {"🟥", "🔴", "🟢", "🟩"}

NORMALIZE_MAP = {
    # common circle variants to unify
    "🟢": "🟢",
    "🟩": "🟩",
    "🔴": "🔴",
    "🟥": "🟥",
}

def normalize_emojis(text: str) -> str:
    return ''.join(NORMALIZE_MAP.get(ch, ch) for ch in text)

def decompose_emojis(seq: str):
    """Return list of 5 dicts with shape/color plus helper strings."""
    lst = []
    color_seq = []
    shape_seq = []
    for ch in seq:
        if ch in ("🟥","🟩"):
            shape = "square"
        else:
            shape = "circle"
        color = "red" if ch in ("🟥","🔴") else "green"
        lst.append({"shape": shape, "color": color})
        color_seq.append('R' if color == 'red' else 'G')
        shape_seq.append('S' if shape == 'square' else 'B')
    return lst, ''.join(color_seq), ''.join(shape_seq)

def parse_line(line: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Backward-compatible single-pair parser (kept for reference)."""
    m = re.search(r"\$?([A-Z0-9]{2,15})", line)
    if not m:
        return None, None, None
    ticker = m.group(1).upper()
    seq = "".join([ch for ch in line if ch in EMOJI_SET])[:5]
    if len(seq) != 5:
        return None, None, None
    tf = None
    tm = re.search(r"frame\s*:\s*(\d+)[mMhH]", line, flags=re.IGNORECASE)
    if tm:
        val = tm.group(1)
        if val in {"30","60","120"}:
            tf = f"{val}m"
    if tf is None:
        tf = "30m"
    return ticker, seq, tf


def parse_pairs(line: str):
    """Return list of (ticker, emoji_sequence5, frame_tf) parsed from one line.
    Handles multiple "$TICKER + 5 emojis" pairs in a single message line.
    """
    line = normalize_emojis(line)
    # timeframe hint (applies to all pairs in line)
    tf = None
    tm = re.search(r"frame\s*:\s*(\d+)[mMhH]", line, flags=re.IGNORECASE)
    if tm:
        val = tm.group(1)
        if val in {"30","60","120"}:
            tf = f"{val}m"
    if tf is None:
        tf = "30m"
    # find all occurrences of "$TICKER <5 emojis>"
    # Allow any whitespace between ticker and emojis
    # Note: we will use a finditer below with optional .P suffix
    pairs = []
    for m in re.finditer(r"\$?([A-Z0-9]{2,15})(?:\.P)?\s*((?:[🟥🔴🟢🟩]\s*){5})", line):
        ticker = m.group(1).upper()
        seq = ''.join(ch for ch in m.group(2) if ch in EMOJI_SET)
        pairs.append((ticker, seq, tf))
    # If regex found nothing, try fallback: first ticker + first 5 emojis in whole line
    if not pairs:
        t, s, f = parse_line(line)
        if t and s:
            pairs.append((t, s, f))
    return pairs

def observed_price_from_unified(symbol: str, ts_iso: str) -> Optional[float]:
    """Pick close from 5m candles at or before ts.
    symbol like AVAXUSDT, ts_iso ISO8601.
    """
    path = os.path.join(UNIFIED_ROOT, symbol, '5m.csv')
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df['time'] = pd.to_datetime(df['time'], utc=True, errors='coerce')
        t = pd.to_datetime(ts_iso, utc=True)
        df = df[df['time'] <= t]
        if df.empty:
            return None
        return float(df.iloc[-1]['close'])
    except Exception:
        return None

def iter_messages_from_resultpairs(symbol: str):
    """Yield (signal_ts_iso, line_text) from Telegram export JSON for the symbol.
    Supports files named {BASE}.json or {SYMBOL}.json, where BASE is without USDT.
    """
    base = symbol.replace('USDT','')
    candidates = [
        os.path.join(RESULTPAIRS_DIR, f"{base}.json"),
        os.path.join(RESULTPAIRS_DIR, f"{symbol}.json"),
    ]
    src_path = None
    for p in candidates:
        if os.path.exists(p):
            src_path = p
            break
    if not src_path:
        return
    try:
        data = json.load(open(src_path, 'r', encoding='utf-8'))
    except Exception:
        return
    for msg in data:
        # date like 2025-04-01T09:31:00+05:00
        dt_raw = msg.get('date') or msg.get('date_unixtime')
        # build full text from parts
        parts = msg.get('text', [])
        full_text = ''
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, str):
                    full_text += part
                elif isinstance(part, dict):
                    full_text += part.get('text','')
        elif isinstance(parts, str):
            full_text = parts
        if not full_text:
            continue
        try:
            ts = datetime.fromisoformat(str(dt_raw))
            ts_utc = ts.astimezone(timezone.utc).isoformat()
        except Exception:
            ts_utc = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        for ln in full_text.splitlines():
            yield ts_utc, ln

def discover_master_result() -> Optional[str]:
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'проект шарики со светой ', 'result.json'),
        os.path.join(home, 'проект шарики со светой', 'result.json'),
        os.path.join(home, 'проект шарики из телеграмма', 'result.json'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

def iter_messages_from_master_result(symbol: str):
    path = discover_master_result()
    if not path:
        return
    try:
        data = json.load(open(path, 'r', encoding='utf-8'))
    except Exception:
        return
    messages = data.get('messages') or []
    for msg in messages:
        parts = msg.get('text', [])
        if isinstance(parts, list):
            text = ''.join([p if isinstance(p, str) else p.get('text', '') for p in parts])
        else:
            text = str(parts)
        # Only yield lines that mention the symbol/ticker
        base = symbol.replace('USDT', '')
        if f'${base}' not in text and base not in text:
            continue
        dt_raw = msg.get('date') or msg.get('date_unixtime')
        try:
            ts = datetime.fromisoformat(str(dt_raw))
            ts_utc = ts.astimezone(timezone.utc).isoformat()
        except Exception:
            ts_utc = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        for ln in text.splitlines():
            yield ts_utc, ln

def prepare(symbol: str):
    symbol = symbol.upper()
    out_dir = os.path.join(UNIFIED_ROOT, symbol, 'signals')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'balls.jsonl')

    count = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        # Collect from all available sources
        it = []
        it.extend(list(iter_messages_from_resultpairs(symbol) or []))
        it.extend(list(iter_messages_from_master_result(symbol) or []))
        if os.path.exists(MESSAGES_CSV):
            with open(MESSAGES_CSV, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get('timestamp_utc') or row.get('timestamp') or row.get('time')
                    for ln in str(row.get('message_text','')).splitlines():
                        it.append((ts, ln))
        # De-duplicate exact (ts, line) tuples
        seen = set()
        uniq = []
        for k in it:
            if k not in seen:
                seen.add(k)
                uniq.append(k)
        it = uniq

        for idx, (ts, line) in enumerate(it):
                pairs = parse_pairs(line)
                for ticker, seq, frame_tf in pairs:
                    if not ticker or not seq:
                        continue
                    # filter to requested symbol
                    if ticker + 'USDT' != symbol and ticker != symbol:
                        continue
                    # helper encodings
                    # compute encodings strictly from the 5-emoji sequence for this pair
                    emojis_list, color_code, shape_code = decompose_emojis(seq)
                    pattern_id = PATTERN_ID_MAP.get(seq, (None, None))[0]
                    context = PATTERN_ID_MAP.get(seq, (None, None))[1]
                    # timestamps
                    try:
                        signal_ts = datetime.fromisoformat(str(ts).replace('Z','+00:00'))
                    except Exception:
                        signal_ts = datetime.utcnow().replace(tzinfo=timezone.utc)
                    signal_ts_iso = signal_ts.astimezone(timezone.utc).isoformat()
                    obs_price = observed_price_from_unified(symbol, signal_ts_iso)
                    event = {
                        "schema_version": 1,
                        "symbol": symbol,
                        "tf": frame_tf,
                        "source": "trends_cryptovizor",
                        "signal_ts": signal_ts_iso,
                        "received_ts": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                        "observed_price": obs_price,
                        "emojis": emojis_list,
                        "raw_emojis": seq,
                        "color_seq": color_code,
                        "shape_seq": shape_code,
                        "frame_hint": frame_tf,
                        "context": context,
                        "pattern_id": pattern_id,
                        "message_id": f"resultpairs_{symbol}_{idx}",
                        "meta": {
                            "raw_text_hash": str(hash(line)),
                        }
                    }
                    out.write(json.dumps(event, ensure_ascii=False) + "\n")
                    count += 1
    print(f"Wrote {count} events to {out_path}")

if __name__ == '__main__':
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else 'AVAXUSDT'
    prepare(sym)


