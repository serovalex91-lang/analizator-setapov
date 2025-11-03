import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd
import matplotlib.pyplot as plt

from backtest_levels.plot_levels import (
	parse_balls_jsonl,
	read_unified_csv,
	read_coinapi_csv,
	taapi_get_candles,
	taapi_get_indicator,
	to_ohlcv_list,
)
from app.services.taapi_bulk import session_vwap, classic_pivots, compute_tolerance
from app.services.utils import infer_tick_from_price
from app.services.levels import find_best_level
from backtest_levels.engine_config import LEVEL_ENGINE
from backtest_levels.levels_svet import find_level_zones_with_quality, pick_best_zone, Settings as SvetSettings
from backtest_levels.levels_pivots import find_pivot_level, PivotSettings


@dataclass
class TradeResult:
	opened_ts: datetime
	filled_entries: List[Tuple[datetime, float]]
	exit_ts: datetime
	result: str  # 'win' | 'loss'
	pnl_usd: float
	entry_level: float
	stop_price: float
	tp_price: float


def _norm_direct(arr: List[Dict[str, Any]]) -> pd.DataFrame:
	rows: List[Dict[str, Any]] = []
	for r in (arr or []):
		try:
			ts = r.get("timestamp") or r.get("t") or r.get("time")
			if isinstance(ts, (int, float)):
				t = datetime.fromtimestamp(ts, tz=timezone.utc)
			elif isinstance(ts, str):
				t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
			else:
				continue
			rows.append({
				"t": t,
				"open": float(r["open"]),
				"high": float(r["high"]),
				"low": float(r["low"]),
				"close": float(r["close"]),
				"volume": float(r.get("volume", 0.0)),
			})
		except Exception:
			continue
	if not rows:
		return pd.DataFrame(columns=["t","open","high","low","close","volume"])
	df = pd.DataFrame(rows)
	df.sort_values("t", inplace=True)
	return df


async def eval_symbol(symbol: str, months: List[str]) -> Dict[str, Any]:
	# 1) Считываем сигналы
	signals = parse_balls_jsonl(symbol, months=months)
	if not signals:
		return {"total": 0, "wins": 0, "losses": 0, "skipped": 0, "win_rate": None, "equity": 1000.0}
	
	# 2) HTF данные (полные, для окон)
	c30_all = read_unified_csv(symbol, "30m");  c30_all = c30_all if not c30_all.empty else read_coinapi_csv(symbol, "30m")
	c60_all = read_unified_csv(symbol, "60m");  c60_all = c60_all if not c60_all.empty else read_coinapi_csv(symbol, "60m")
	c120_all = read_unified_csv(symbol, "120m"); c120_all = c120_all if not c120_all.empty else read_coinapi_csv(symbol, "120m")
	
	equity = 1000.0
	wins = 0
	losses = 0
	skipped = 0
	results: List[TradeResult] = []
	
	for s in signals:
		dt: datetime = s["dt_utc"]
		origin_tf: str = s["origin_tf"]
		side: str = s["context"]  # 'long'|'short'
		
		if c30_all.empty or c60_all.empty or c120_all.empty:
			skipped += 1
			continue
		
		# 3) Обрезаем до момента сигнала (только для алгоритма уровня)
		c30 = c30_all[c30_all["t"] <= pd.Timestamp(dt)]
		c60 = c60_all[c60_all["t"] <= pd.Timestamp(dt)]
		c120 = c120_all[c120_all["t"] <= pd.Timestamp(dt)]
		if len(c30) < 50 or len(c60) < 50 or len(c120) < 50:
			skipped += 1
			continue
		
		# 4) LTF свечи (5m/15m) для входов и отслеживания
		ts_from = int((dt - timedelta(days=10)).timestamp()); ts_to = int(dt.timestamp())
		c5_raw = await taapi_get_candles(symbol, "5m", ts_from, ts_to)
		c15_raw = await taapi_get_candles(symbol, "15m", ts_from, ts_to)
		c5 = _norm_direct(c5_raw)
		c15 = _norm_direct(c15_raw)
		if c5.empty:
			c5 = c15.copy() if not c15.empty else c30.copy()
		if c15.empty:
			c15 = c30.copy()
		
		# 5) Индикаторы (для алгоритма уровней берём 30m ATR/EMA, как в основном API)
		atr30 = await taapi_get_indicator("atr", symbol, "30m", {"period": 14}, ts_from, ts_to)
		ema200 = await taapi_get_indicator("ema", symbol, "30m", {"period": 200}, ts_from, ts_to)
		inds = {"atr": atr30, "ema200": ema200}
		
		# 6) session_info
		def to_list(df: pd.DataFrame) -> List[Dict[str, Any]]:
			return [{
				"t": x.t.to_pydatetime().replace(tzinfo=timezone.utc).isoformat(),
				"open": float(x.open),
				"high": float(x.high),
				"low": float(x.low),
				"close": float(x.close),
				"volume": float(x.volume),
			} for _, x in df.iterrows()]
		c30_list = to_list(c30)
		day = dt.date(); prev_day = day - timedelta(days=1)
		c30_today = [r for r in c30_list if datetime.fromisoformat(r["t"]).date() == day]
		vwap_val = session_vwap(c30_today) if c30_today else None
		c30_prev = [r for r in c30_list if datetime.fromisoformat(r["t"]).date() == prev_day]
		if c30_prev:
			ph = max(float(r["high"]) for r in c30_prev)
			pl = min(float(r["low"]) for r in c30_prev)
			pc = float(c30_prev[-1]["close"])
			piv = classic_pivots(ph, pl, pc)
		else:
			piv = {}
		session = {"vwap_session": vwap_val, "pivots_daily": piv, "PDH": piv.get("R1"), "PDL": piv.get("S1")}
		
		# 7) Поиск уровня (движок по конфигу)
		last_price = float(c5.iloc[-1]["close"]) if len(c5) else float(c30.iloc[-1]["close"])
		tick_size = infer_tick_from_price(last_price)
		level = None
		if LEVEL_ENGINE == "svet":
			zones = find_level_zones_with_quality(c30, side, SvetSettings())
			if zones:
				best = pick_best_zone(c30, zones)
				if best:
					level = {"price": float(best['price'])}
		elif LEVEL_ENGINE == "pivots":
			# выбираем базовый ТФ для выявления последнего pivot-high/low
			base_algo_df = {"30m": c30, "60m": c60, "120m": c120}.get(origin_tf, c30)
			piv = find_pivot_level(base_algo_df, side, PivotSettings(), tick_size)
			if piv:
				level = {"price": float(piv["price"])}
		else:
			level = find_best_level(to_list(c5), to_list(c15), to_list(c30), to_list(c60), to_list(c120), session, tick_size, inds, side, origin_tf=origin_tf)
		if not level:
			skipped += 1
			continue
		entry_level = float(level["price"])
		
		# 8) ATR120m tolerance (локально по 120m, RMA14)
		def _atr_rma(df: pd.DataFrame, period: int = 14) -> pd.Series:
			prev_close = df["close"].shift(1)
			tr = pd.concat([
				(df["high"] - df["low"]).abs(),
				(df["high"] - prev_close).abs(),
				(df["low"] - prev_close).abs(),
			], axis=1).max(axis=1)
			return tr.ewm(alpha=1/period, adjust=False).mean()
		# берём окно до dt и считаем ATR
		c120_for_atr = c120.copy()
		c120_for_atr["atr"] = _atr_rma(c120_for_atr, 14)
		atr120 = float(c120_for_atr.iloc[-1]["atr"]) if not c120_for_atr.empty else None
		if atr120 is None or pd.isna(atr120):
			skipped += 1
			continue
		tol = float(compute_tolerance(entry_level, float(atr120), tick_size))
		range_low = entry_level - tol
		range_high = entry_level + tol
		
		# 9) Сетка из 4 ордеров внутри уровня
		if side == "long":
			prices = [range_low + i*(range_high-range_low)/3.0 for i in range(4)]
		else:
			prices = [range_high - i*(range_high-range_low)/3.0 for i in range(4)]
		
		# 10) Окно ожидания входов: 6 часов после сигнала
		ltf = c5 if not c5.empty else (c15 if not c15.empty else c30)
		f_start = pd.Timestamp(dt)
		f_end = f_start + pd.Timedelta(hours=6)
		fwin = ltf[(ltf["t"] >= f_start) & (ltf["t"] <= f_end)].copy()
		filled: List[Tuple[datetime, float]] = []
		for _, r in fwin.iterrows():
			lo, hi = float(r["low"]) if "low" in r else float(r["close"]), float(r["high"]) if "high" in r else float(r["close"])
			for p in list(prices):
				if side == "long":
					if lo <= p <= hi:
						filled.append((r["t"], p)); prices.remove(p)
				else:
					if lo <= p <= hi:
						filled.append((r["t"], p)); prices.remove(p)
			if not prices:
				break
		if not filled:
			skipped += 1
			continue
		filled.sort(key=lambda x: x[0])
		first_fill_ts = filled[0][0]
		
		# 11) SL/TP единые для сделки от entry_level
		sl = entry_level - 1.3*tol if side == "long" else entry_level + 1.3*tol
		tp = entry_level + 3.9*tol if side == "long" else entry_level - 3.9*tol
		
		# 12) Трекинг исхода: первое касание SL или TP после first_fill_ts
		post = ltf[ltf["t"] >= first_fill_ts].copy()
		result = None
		exit_ts: Optional[datetime] = None
		for _, r in post.iterrows():
			lo, hi = float(r["low"]) if "low" in r else float(r["close"]), float(r["high"]) if "high" in r else float(r["close"])
			if side == "long":
				hit_tp = hi >= tp
				hit_sl = lo <= sl
			else:
				hit_tp = lo <= tp
				hit_sl = hi >= sl
			if hit_tp and hit_sl:
				result = "loss"  # conservative
				exit_ts = r["t"]
				break
			if hit_tp:
				result = "win"; exit_ts = r["t"]; break
			if hit_sl:
				result = "loss"; exit_ts = r["t"]; break
		if result is None or exit_ts is None:
			skipped += 1
			continue
		
		# 13) Риск 10$ фикс, делим поровну по исполненным ордерам
		k = len(filled)
		risk_total = 10.0
		risk_share = risk_total / k
		pnl = 0.0
		for _, entry_px in filled:
			if side == "long":
				qty = risk_share / max(entry_px - sl, 1e-12)
				exit_px = tp if result == "win" else sl
				pnl += qty * (exit_px - entry_px)
			else:
				qty = risk_share / max(sl - entry_px, 1e-12)
				exit_px = tp if result == "win" else sl
				pnl += qty * (entry_px - exit_px)
		
		# 14) Итоги
		if result == "win":
			wins += 1
		else:
			losses += 1
		equity += pnl
		results.append(TradeResult(opened_ts=first_fill_ts, filled_entries=filled, exit_ts=exit_ts, result=result, pnl_usd=pnl, entry_level=entry_level, stop_price=sl, tp_price=tp))
	
	return {"total": len(signals), "wins": wins, "losses": losses, "skipped": skipped, "win_rate": (wins/(wins+losses) if (wins+losses) else None), "equity": equity, "trades": results}


def _ensure_dir(path: str):
	os.makedirs(path, exist_ok=True)


def _plot_trade(symbol: str, origin_tf: str, dt_signal: datetime, base_df: pd.DataFrame, trade: TradeResult, out_path: str):
	# окно +- 100 свечей
	idx = base_df["t"].searchsorted(pd.Timestamp(dt_signal))
	left = max(0, idx - 100)
	right = min(len(base_df), idx + 100)
	plot_df = base_df.iloc[left:right].copy()
	if plot_df.empty:
		return
	fig, ax = plt.subplots(figsize=(12, 6))
	if len(plot_df) >= 2:
		dtw = (plot_df["t"].iloc[1] - plot_df["t"].iloc[0]).total_seconds()
	else:
		dtw = 1800
	width_days = (dtw / 86400.0) * 0.7
	up = plot_df["close"] >= plot_df["open"]
	down = ~up
	ax.vlines(plot_df["t"], plot_df["low"], plot_df["high"], color="#777", lw=0.6, zorder=1)
	ax.bar(plot_df["t"][up], (plot_df["close"][up] - plot_df["open"][up]), bottom=plot_df["open"][up], width=width_days, color="#2ecc71", alpha=0.9, zorder=2, align="center")
	ax.bar(plot_df["t"][down], (plot_df["close"][down] - plot_df["open"][down]), bottom=plot_df["open"][down], width=width_days, color="#e74c3c", alpha=0.9, zorder=2, align="center")
	ax.axvline(pd.Timestamp(dt_signal), color="gray", ls="--", alpha=0.6)
	ax.set_xlim(plot_df["t"].iloc[0], plot_df["t"].iloc[-1])
	# зона уровня
	lp = float(trade.entry_level)
	# восстановим tolerance из SL/TP расстояний
	if trade.result in ("win", "loss"):
		# SL distance = 1.3 * tol
		tol = abs(trade.entry_level - trade.stop_price) / 1.3
		ax.axhspan(lp - tol, lp + tol, color="#b5651d", alpha=0.18)
	# входы и выход
	for t_entry, px in trade.filled_entries:
		ax.axvline(pd.Timestamp(t_entry), color="#3498db", lw=1.0, alpha=0.6)
		ax.text(pd.Timestamp(t_entry), px, "entry", color="#3498db", fontsize=8, va="bottom")
	ax.axvline(pd.Timestamp(trade.exit_ts), color="#000", lw=1.2, alpha=0.7)
	ax.text(pd.Timestamp(trade.exit_ts), lp, f"PNL {trade.pnl_usd:+.2f}", fontsize=9, color=("#2e8b57" if trade.pnl_usd>=0 else "#b22222"))
	ax.set_title(f"{symbol} | {origin_tf} | result={trade.result} | pnl={trade.pnl_usd:+.2f}")
	fig.tight_layout(); fig.savefig(out_path); plt.close(fig)


async def run_and_plot(symbol: str, months: List[str], out_dir: str) -> Dict[str, Any]:
	res = await eval_symbol(symbol, months)
	# визуализируем каждую закрытую сделку
	base_all = read_unified_csv(symbol, "30m")
	if base_all.empty:
		base_all = read_coinapi_csv(symbol, "30m")
	_ensure_dir(out_dir)
	for tr in res.get("trades", []):
		# Для простоты рисуем на 30m
		_base = base_all
		fname = os.path.join(out_dir, f"{tr.opened_ts.strftime('%Y%m%d_%H%M')}.png")
		_plot_trade(symbol, "30m", tr.opened_ts, _base, tr, fname)
	return res


if __name__ == "__main__":
	import asyncio
	import argparse
	parser = argparse.ArgumentParser()
	parser.add_argument("--symbol", required=True)
	parser.add_argument("--months", default="2025-01,2025-02,2025-03,2025-04,2025-05")
	args = parser.parse_args()
	months = [m.strip() for m in args.months.split(',') if m.strip()]
	out = asyncio.run(run_and_plot(args.symbol, months, os.path.join(os.path.dirname(__file__), "output", args.symbol, "winrate")))
	print({k: v for k, v in out.items() if k in ("total","wins","losses","skipped","win_rate","equity")})


