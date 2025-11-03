from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional, List

import pandas as pd


@dataclass
class PivotSettings:
    # Number of bars to the left/right for pivot detection of the main pivot
    left_right_window: int = 10
    # Window used to search the opposite swing (softer)
    opposite_window: int = 5
    # Number of bars to scan back from the end (useful to restrict heavy scans)
    max_lookback_bars: int = 1500
    # Buffer in tick sizes placed beyond the pivot extreme
    buffer_ticks: int = 2
    # Tolerance as a fraction of most recent local swing range (pivot high - pivot low)
    tolerance_of_range: float = 0.2
    # Minimum tolerance in tick sizes
    min_tolerance_ticks: int = 3
    # Minimum distance from signal bar to selected pivot as percent (e.g., 0.008 = 0.8%)
    min_gap_percent: float = 0.008


def _find_last_pivot_high(df: pd.DataFrame, k: int) -> Optional[int]:
    if df is None or df.empty:
        return None
    highs = df["high"].values
    n = len(highs)
    if n < 2 * k + 1:
        return None
    # iterate from last to first to get the most recent pivot
    for i in range(n - k - 1, k - 1, -1):
        h = highs[i]
        window = highs[i - k:i + k + 1]
        # Allow flat tops (>=) instead of strict greater-than
        if h >= window[:k].max() and h >= window[k + 1:].max():
            return i
    return None


def _find_last_pivot_low(df: pd.DataFrame, k: int) -> Optional[int]:
    if df is None or df.empty:
        return None
    lows = df["low"].values
    n = len(lows)
    if n < 2 * k + 1:
        return None
    for i in range(n - k - 1, k - 1, -1):
        l = lows[i]
        window = lows[i - k:i + k + 1]
        # Allow flat bottoms (<=) instead of strict less-than
        if l <= window[:k].min() and l <= window[k + 1:].min():
            return i
    return None


def find_pivot_level(
    df: pd.DataFrame,
    side: str,
    settings: PivotSettings,
    tick_size: float,
    sig_low: Optional[float] = None,
    sig_high: Optional[float] = None,
    signal_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Compute a level strictly from pivot highs/lows:
      - For short: place level just above the most recent pivot high
      - For long:  place level just below the most recent pivot low

    Tolerance is derived from the most recent local swing range using only pivots.
    """
    if df is None or df.empty:
        return None
    k = int(max(1, settings.left_right_window))
    k_op = int(max(1, getattr(settings, "opposite_window", 5)))
    use_df = df.iloc[-min(len(df), settings.max_lookback_bars):].copy()

    if side == "short":
        # collect pivot highs from most recent backwards
        all_pivot_highs_indices: List[int] = []  # store integer positions within use_df
        current_df = use_df.copy()
        while True:
            idx = _find_last_pivot_high(current_df, k)
            if idx is None:
                break
            pos = len(use_df) - len(current_df) + idx
            all_pivot_highs_indices.append(pos)
            current_df = current_df.iloc[:idx]
            if current_df.empty:
                break

        best_pivot_idx = None
        for pos in all_pivot_highs_indices:
            pivot_px = float(use_df.iloc[pos]["high"])
            # Enforce pivot high is above signal-bar high
            if sig_high is not None and pivot_px <= float(sig_high):
                continue
            # Enforce minimum gap from signal price
            if signal_price is not None and getattr(settings, "min_gap_percent", 0.0):
                gap = pivot_px - float(signal_price)
                min_gap_abs = float(settings.min_gap_percent) * float(signal_price)
                if gap < min_gap_abs:
                    continue
            best_pivot_idx = pos
            break

        if best_pivot_idx is None:
            return None

        pivot_px = float(use_df.iloc[best_pivot_idx]["high"])
        prior_low_idx = _find_last_pivot_low(use_df.iloc[:best_pivot_idx + 1], k_op)
        if prior_low_idx is None:
            tol = max(0.01 * pivot_px, settings.min_tolerance_ticks * tick_size)
            pivots_meta: List[Dict[str, Any]] = [{"t": pd.Timestamp(use_df.iloc[best_pivot_idx]["t"]).to_pydatetime(), "price": pivot_px, "kind": "high"}]
        else:
            prior_low_px = float(use_df.iloc[prior_low_idx]["low"])
            swing_range = abs(pivot_px - prior_low_px)
            tol = max(settings.tolerance_of_range * swing_range, settings.min_tolerance_ticks * tick_size)
            pivots_meta = [
                {"t": pd.Timestamp(use_df.iloc[best_pivot_idx]["t"]).to_pydatetime(), "price": pivot_px, "kind": "high"},
                {"t": pd.Timestamp(use_df.iloc[prior_low_idx]["t"]).to_pydatetime(), "price": prior_low_px, "kind": "low"},
            ]
        level_price = pivot_px + settings.buffer_ticks * tick_size
        return {"price": level_price, "tolerance": float(tol), "source": "pivot_high", "pivot_bars": pivots_meta}

    else:
        # long side: pivot low logic
        all_pivot_lows_indices: List[int] = []
        current_df = use_df.copy()
        while True:
            idx = _find_last_pivot_low(current_df, k)
            if idx is None:
                break
            pos = len(use_df) - len(current_df) + idx
            all_pivot_lows_indices.append(pos)
            current_df = current_df.iloc[:idx]
            if current_df.empty:
                break

        best_pivot_idx = None
        for pos in all_pivot_lows_indices:
            pivot_px = float(use_df.iloc[pos]["low"])
            if sig_low is not None and pivot_px >= float(sig_low):
                continue
            if signal_price is not None and getattr(settings, "min_gap_percent", 0.0):
                gap = float(signal_price) - pivot_px
                min_gap_abs = float(settings.min_gap_percent) * float(signal_price)
                if gap < min_gap_abs:
                    continue
            best_pivot_idx = pos
            break

        if best_pivot_idx is None:
            return None

        pivot_px = float(use_df.iloc[best_pivot_idx]["low"])
        prior_high_idx = _find_last_pivot_high(use_df.iloc[:best_pivot_idx + 1], k_op)
        if prior_high_idx is None:
            tol = max(0.01 * pivot_px, settings.min_tolerance_ticks * tick_size)
            pivots_meta = [{"t": pd.Timestamp(use_df.iloc[best_pivot_idx]["t"]).to_pydatetime(), "price": pivot_px, "kind": "low"}]
        else:
            prior_high_px = float(use_df.iloc[prior_high_idx]["high"])
            swing_range = abs(prior_high_px - pivot_px)
            tol = max(settings.tolerance_of_range * swing_range, settings.min_tolerance_ticks * tick_size)
            pivots_meta = [
                {"t": pd.Timestamp(use_df.iloc[best_pivot_idx]["t"]).to_pydatetime(), "price": pivot_px, "kind": "low"},
                {"t": pd.Timestamp(use_df.iloc[prior_high_idx]["t"]).to_pydatetime(), "price": prior_high_px, "kind": "high"},
            ]
        level_price = pivot_px - settings.buffer_ticks * tick_size
        return {"price": level_price, "tolerance": float(tol), "source": "pivot_low", "pivot_bars": pivots_meta}


