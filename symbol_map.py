"""Symbol aliases for external data providers."""

YAHOO_TICKER_ALIASES = {
    "SPX": "^SPX",
    "NDX": "^NDX",
}


def yahoo_ticker_symbol(ticker: str) -> str:
    """Return the ticker symbol expected by Yahoo Finance/yfinance."""
    clean = str(ticker or "").upper().strip()
    return YAHOO_TICKER_ALIASES.get(clean, clean)

OPTION_UNDERLYING_ALIASES = {
    "SPXW": "SPX",
    "NDXP": "NDX",
}


def underlying_symbol_from_option_root(root: str) -> str:
    """Return configured underlying ticker for an option root."""
    clean = str(root or "").upper().strip()
    return OPTION_UNDERLYING_ALIASES.get(clean, clean)
