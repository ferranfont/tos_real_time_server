"""Project-level runtime defaults."""

# Tickers to record. For now only MU; later one Excel RTD tab per ticker.
TICKERS = ["MU"]

# Strikes on EACH side of the ATM: 12 below + ATM + 12 above = 25 total.
DEFAULT_LEVELS = 12

# Seconds between RTD polls of the Excel sheet. NOT true tick-by-tick:
# the collector samples the latest RTD value every N seconds.
SERVER_REQUEST_COOLDOWN_FREQUENCY = 60

# Record only during the US cash session (NYSE calendar). Set False to record
# any time (handy for after-hours testing).
RECORD_ONLY_MARKET_HOURS = True

# At the market open, auto-anchor the ladder (refresh chain + ATM + strikes)
# without pressing START. Set False to anchor only manually.
AUTO_ANCHOR_AT_OPEN = True
