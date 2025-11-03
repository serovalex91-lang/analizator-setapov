import asyncio
from backtest_levels.sync_coinapi_tf import coinapi_symbols_map, sync_symbol

# Problem symbols to re-sync with Taapi aliases and alternate request styles
PROBLEM_DIRS = [
	"BINANCE_SPOT_BCH_USDT",
	"BINANCE_SPOT_FTM_USDT",
	"BINANCE_SPOT_NANO_USDT",
	"BINANCE_SPOT_OMG_USDT",
	"BINANCE_SPOT_OP_USDT",
	"BINANCE_SPOT_REN_USDT",
	"BINANCE_SPOT_STRAT_USDT",
	"BINANCE_SPOT_WAVES_USDT",
	"BINANCE_SPOT_XMR_USDT",
]


async def main():
	syms = await coinapi_symbols_map()
	for d in PROBLEM_DIRS:
		print("-- Sync", d)
		await sync_symbol(d, syms)
		print("-- Done", d)


if __name__ == "__main__":
	asyncio.run(main())




