# new bot entrypoint (scaffold)
import asyncio
import re
from typing import List, Tuple, Optional

# Reuse emoji rules from existing bot
RED_SET = {'🟥','🔴'}
GREEN_SET = {'🟩','🟢'}

def parse_trends_line(line: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    m = re.search(r"\$([A-Za-z0-9]{2,15})", line)
    if not m:
        return None, None, None
    ticker = m.group(1).upper()
    squares = [ch for ch in line if ch in RED_SET or ch in GREEN_SET]
    if len(squares) < 5:
        return ticker, None, None
    tf_m = re.search(r"frame\s*:\s*(\d+)[mMhH]", line, flags=re.IGNORECASE)
    origin_tf = None
    if tf_m:
        val = tf_m.group(1)
        if val in {"30","60","120"}:
            origin_tf = f"{val}m"
    return ticker, squares[:5], (origin_tf or "30m")

def is_long_combo(squares: List[str]) -> bool:
    if len(squares) != 5:
        return False
    allowed = [
        ['🟥','🟢','🟢','🟢','🟢'],
        ['🔴','🟢','🟢','🟢','🟩'],
        ['🔴','🟥','🟢','🟢','🟢'],
        ['🔴','🔴','🟥','🟢','🟢'],
        ['🟥','🔴','🔴','🟢','🟢'],
        ['🔴','🟥','🔴','🟢','🟢'],
    ]
    return list(squares) in allowed

def is_short_combo(squares: List[str]) -> bool:
    if len(squares) != 5:
        return False
    allowed = [
        ['🟩','🔴','🔴','🔴','🔴'],
        ['🟢','🔴','🔴','🔴','🟥'],
        ['🟢','🟩','🔴','🔴','🔴'],
        ['🟢','🟢','🟩','🔴','🔴'],
        ['🟩','🟢','🟢','🔴','🔴'],
        ['🟢','🟩','🟢','🔴','🔴'],
    ]
    return list(squares) in allowed

async def main():
    print('newbot starting... (emoji parser ready)')
    # quick self-test
    samples = [
        "$BNB 🟥🔴🔴🟢🟢 FRAME:30M",
        "$BTC 🔴🟥🔴🟢🟢 frame:60m",
        "$AVAX 🟩🟢🟢🔴🔴 FRAME:120M",
        "$SOL 🟢🟩🟢🔴🔴 frame:30M",
    ]
    for s in samples:
        t, sq, tf = parse_trends_line(s)
        print('> ', s)
        print('  ->', t, sq, tf, 'long?', bool(sq and is_long_combo(sq)), 'short?', bool(sq and is_short_combo(sq)))

if __name__ == '__main__':
    asyncio.run(main())
