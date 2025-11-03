import os
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any


DB_PATH = os.path.join(os.path.dirname(__file__), "levels.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS key_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT,
            support_json TEXT,
            resistance_json TEXT,
            source_ts TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_key_levels_symbol_ts
        ON key_levels(symbol, source_ts);
        """
    )
    return conn


def upsert_levels(
    symbol: str,
    timeframe: Optional[str],
    support_zones: List[Tuple[float, float]],
    resistance_zones: List[Tuple[float, float]],
    source_ts: Optional[str] = None,
) -> None:
    conn = _get_conn()
    try:
        ts = source_ts or datetime.utcnow().isoformat()
        payload = (
            symbol.upper(),
            (timeframe or "").lower(),
            json.dumps(support_zones, ensure_ascii=False),
            json.dumps(resistance_zones, ensure_ascii=False),
            ts,
            datetime.utcnow().isoformat(),
        )
        conn.execute(
            """
            INSERT INTO key_levels(symbol, timeframe, support_json, resistance_json, source_ts, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            payload,
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_levels(
    symbol: str,
    max_age_minutes: int = 1440,
    prefer_timeframes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    try:
        since_dt = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        since_iso = since_dt.isoformat()
        sym = symbol.upper()
        cur = conn.cursor()
        def _lookthrough_tf(target_tf: str) -> Optional[Dict[str, Any]]:
            """Scan rows for given timeframe newest→oldest and take first non-empty support and first non-empty resistance.
            Returns dict if any side found, otherwise None.
            """
            # Если max_age_minutes > 0, трактуем это как минимальный возраст (т.е. брать только записи старше или равные since_iso)
            if max_age_minutes and max_age_minutes > 0:
                cur.execute(
                    """
                    SELECT timeframe, support_json, resistance_json, source_ts
                    FROM key_levels
                    WHERE symbol = ? AND timeframe = ? AND source_ts <= ?
                    ORDER BY source_ts DESC, id DESC
                    """,
                    (sym, target_tf.lower(), since_iso),
                )
            else:
                cur.execute(
                    """
                    SELECT timeframe, support_json, resistance_json, source_ts
                    FROM key_levels
                    WHERE symbol = ? AND timeframe = ?
                    ORDER BY source_ts DESC, id DESC
                    """,
                    (sym, target_tf.lower()),
                )
            rows = cur.fetchall()
            if not rows:
                return None
            found_support: Optional[List[Any]] = None
            found_resistance: Optional[List[Any]] = None
            ts_support: Optional[str] = None
            ts_resistance: Optional[str] = None
            for tf_val, sup_j, res_j, ts_val in rows:
                if found_support is None:
                    sup = json.loads(sup_j or "[]")
                    if isinstance(sup, list) and len(sup) > 0:
                        found_support = sup
                        ts_support = ts_val
                if found_resistance is None:
                    res = json.loads(res_j or "[]")
                    if isinstance(res, list) and len(res) > 0:
                        found_resistance = res
                        ts_resistance = ts_val
                if found_support is not None and found_resistance is not None:
                    break
            if found_support is None and found_resistance is None:
                return None
            # Choose representative source_ts: the newest among the sides we actually found
            candidates = [ts for ts in [ts_support, ts_resistance] if ts]
            rep_ts = max(candidates) if candidates else rows[0][3]
            return {
                "timeframe": target_tf.lower(),
                "support": found_support or [],
                "resistance": found_resistance or [],
                "source_ts": rep_ts,
            }

        # Restrict to allowed TFs only
        allowed_tfs = ["4h", "1h", "12h"]
        tf_order = [tf for tf in (prefer_timeframes or allowed_tfs) if tf.lower() in allowed_tfs]
        if not tf_order:
            tf_order = allowed_tfs
        for tf in tf_order:
            looked = _lookthrough_tf(tf)
            if looked is not None:
                return looked
        # No allowed TFs yielded data
        return None
    finally:
        conn.close()


# --- Utilities to backfill the repository from logs ---
def import_levels_from_log(log_path: str) -> int:
    """Parses messages.log and imports Key Levels blocks into the SQLite repo.
    Returns number of imported records. Only imports blocks that provide a
    concrete creation timestamp (Created: ... UTC) to keep freshness accurate.
    """
    try:
        if not os.path.exists(log_path):
            return 0
        with open(log_path, 'r', encoding='utf-8') as f:
            data = f.read()
    except Exception:
        return 0

    imported = 0

    # Find all occurrences of "Key Levels for ..." and process block until next occurrence
    import re
    pattern = re.compile(r"Key\s+Levels\s+for[^#]*#([A-Z0-9]+)", re.IGNORECASE)
    starts = [m.start() for m in pattern.finditer(data)]
    starts.append(len(data))  # sentinel end

    for i in range(len(starts) - 1):
        block_start = starts[i]
        block_end = starts[i + 1]
        block = data[block_start:block_end]

        try:
            m_sym = pattern.search(block)
            if not m_sym:
                continue
            symbol = m_sym.group(1).upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"

            # timeframe e.g. (1h) (4h) (12h)
            m_tf = re.search(r"\((\d+\s*[hm])\)", block, flags=re.IGNORECASE)
            timeframe = m_tf.group(1).replace(" ", "").lower() if m_tf else None

            # Created: 2025-09-13 10:00:00 UTC
            m_created = re.search(r"Created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*UTC", block)
            if not m_created:
                # Skip import if we cannot determine actual creation time
                continue
            try:
                created_dt = datetime.strptime(m_created.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            source_ts = created_dt.isoformat()

            # Parse zones line-by-line to respect sections
            support: List[Tuple[float, float]] = []
            resistance: List[Tuple[float, float]] = []
            current_block = None
            for raw_line in block.splitlines():
                line = raw_line.strip()
                if re.search(r"(\*\*\s*)?SUPPORT\s+Levels(\s*\*\*)?", line, flags=re.IGNORECASE):
                    current_block = "support"; continue
                if re.search(r"(\*\*\s*)?RESISTANCE\s+Levels(\s*\*\*)?", line, flags=re.IGNORECASE):
                    current_block = "resistance"; continue
                m_zone = re.search(r"Zone:\s*(?:\*\*)?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\*\*)?", line)
                if m_zone and current_block in {"support", "resistance"}:
                    low = float(m_zone.group(1)); high = float(m_zone.group(2))
                    if low > high:
                        low, high = high, low
                    if current_block == "support":
                        support.append((low, high))
                    else:
                        resistance.append((low, high))

            if support or resistance:
                upsert_levels(symbol, timeframe, support, resistance, source_ts=source_ts)
                imported += 1
        except Exception:
            # Ignore errors for individual blocks
            continue

    # --- Additionally parse alert formats: "New ... Level Detected!" ---
    try:
        new_pat = re.compile(r"New\s+(SUPPORT|RESISTANCE)\s+Level\s+Detected!", re.IGNORECASE)
        for m in new_pat.finditer(data):
            block_start = m.start()
            block_end = data.find("\n\n", block_start)
            if block_end == -1:
                block_end = min(block_start + 2000, len(data))
            block = data[block_start:block_end]
            try:
                side = m.group(1).lower()
                ms = re.search(r"Symbol:\s*\*\*?#([A-Z0-9]+)\*\*?", block)
                mt = re.search(r"Timeframe:\s*([0-9]+\s*[hm])", block, flags=re.IGNORECASE)
                mz = re.search(r"Zone:\s*(?:\*\*)?\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", block)
                mc = re.search(r"Created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*UTC", block)
                if not (ms and mt and mz and mc):
                    continue
                symbol = ms.group(1).upper()
                if not symbol.endswith("USDT"):
                    symbol += "USDT"
                timeframe = mt.group(1).replace(" ", "").lower()
                low = float(mz.group(1)); high = float(mz.group(2))
                if low > high:
                    low, high = high, low
                source_ts = datetime.strptime(mc.group(1), "%Y-%m-%d %H:%M:%S").isoformat()
                if side == "support":
                    upsert_levels(symbol, timeframe, [(low, high)], [], source_ts=source_ts)
                else:
                    upsert_levels(symbol, timeframe, [], [(low, high)], source_ts=source_ts)
                imported += 1
            except Exception:
                continue
    except Exception:
        pass

    # --- Parse crossed updates: "SUPPORT/RESISTANCE Level Crossed!" ---
    try:
        cross_pat = re.compile(r"(SUPPORT|RESISTANCE)\s+Level\s+Crossed!", re.IGNORECASE)
        for m in cross_pat.finditer(data):
            block_start = m.start()
            block_end = data.find("\n\n", block_start)
            if block_end == -1:
                block_end = min(block_start + 2500, len(data))
            block = data[block_start:block_end]
            try:
                crossed_side = m.group(1).lower()
                ms = re.search(r"Symbol:\s*#([A-Z0-9]+)", block)
                mt = re.search(r"Timeframe:\s*([0-9]+\s*[hm])", block, flags=re.IGNORECASE)
                mx = re.search(r"Crossed\s+Zone:\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", block)
                mc = re.search(r"Created:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*UTC", block)
                if not (ms and mt and mx and mc):
                    continue
                symbol = ms.group(1).upper()
                if not symbol.endswith("USDT"):
                    symbol += "USDT"
                timeframe = mt.group(1).replace(" ", "").lower()
                low = float(mx.group(1)); high = float(mx.group(2))
                if low > high:
                    low, high = high, low
                source_ts = datetime.strptime(mc.group(1), "%Y-%m-%d %H:%M:%S").isoformat()

                # Parse Active Levels section if present
                active_support: List[Tuple[float, float]] = []
                active_resistance: List[Tuple[float, float]] = []
                for line in block.splitlines():
                    line_s = line.strip()
                    ma = re.search(r"^•\s*RESISTANCE:\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", line_s)
                    if ma:
                        lo = float(ma.group(1)); hi = float(ma.group(2))
                        if lo > hi:
                            lo, hi = hi, lo
                        active_resistance.append((lo, hi))
                        continue
                    mb = re.search(r"^•\s*SUPPORT:\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)", line_s)
                    if mb:
                        lo = float(mb.group(1)); hi = float(mb.group(2))
                        if lo > hi:
                            lo, hi = hi, lo
                        active_support.append((lo, hi))
                        continue

                # Load latest and produce a new snapshot arrays
                conn = _get_conn()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT support_json, resistance_json
                        FROM key_levels
                        WHERE symbol=? AND timeframe=?
                        ORDER BY source_ts DESC, id DESC
                        LIMIT 1
                        """,
                        (symbol, timeframe),
                    )
                    row = cur.fetchone()
                    support = json.loads(row[0] or "[]") if row else []
                    resistance = json.loads(row[1] or "[]") if row else []

                    # Remove crossed zone from appropriate list
                    def _remove_zone(zones: List[List[float]], lo: float, hi: float) -> List[Tuple[float, float]]:
                        out = []
                        for a, b in zones:
                            if abs(a - lo) < 1e-9 and abs(b - hi) < 1e-9:
                                continue
                            out.append((float(a), float(b)))
                        return out

                    if crossed_side == "support":
                        support = _remove_zone(support, low, high)
                    else:
                        resistance = _remove_zone(resistance, low, high)

                    # If Active Levels provided, replace with them
                    if active_support or active_resistance:
                        if active_support:
                            support = active_support
                        if active_resistance:
                            resistance = active_resistance

                    # Insert new snapshot
                    upsert_levels(symbol, timeframe, support, resistance, source_ts=source_ts)
                    imported += 1
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass

    return imported

