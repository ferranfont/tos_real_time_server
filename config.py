"""Project-level runtime defaults."""

# Tickers to record. The first ticker is the dashboard default.
TICKERS = ["NDX", "SPX", "MU", "SNDK", "WDC", "STX"]    # , "INTC"

# Strikes on EACH side of the ATM for the STRAT near-ATM display: 12 below + ATM + 12 above.
DEFAULT_LEVELS = 12

# Strikes on EACH side of the ATM that the RECORDER captures (call+put per strike),
# nearest expiration, for every ticker. 30 -> 61 strikes -> 122 legs per ticker.
# Wide enough that a day's move stays inside the range, without flooding TOS RTD.
RECORD_LEVELS = 30

# Seconds between RTD polls of the Excel sheet. NOT true tick-by-tick:
# the collector samples the latest RTD value every N seconds.
SERVER_REQUEST_COOLDOWN_FREQUENCY = 5

# Record only during the US cash session (NYSE calendar). Set False to record
# any time (handy for after-hours testing).
RECORD_ONLY_MARKET_HOURS = False

# At the market open, auto-anchor the ladder (refresh chain + ATM + strikes)
# without pressing START. Set False to anchor only manually.
AUTO_ANCHOR_AT_OPEN = True


# Plot only RTH (regular trading hours) data in the Excel chart. Set False to plot all.
USE_RTH_ONLY = True

# Time zone used to interpret RTH_START/RTH_END. Valid values: "BCN", "NY", "CHICAGO".
# The server converts this window to Barcelona local time before sending it to the UI.
RTH_TIMEZONE = "BCN"  # BCN, NY, CHICAGO
RTH_START = "15:30"
RTH_END = "22:50"

# Backwards-compatible Barcelona local-time RTH window.
RTH_START_BCN = RTH_START
RTH_END_BCN = RTH_END