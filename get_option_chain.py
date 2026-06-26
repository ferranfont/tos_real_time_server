import argparse
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


DEFAULT_TICKER = "MU"
OUTPUT_DIR = Path(__file__).resolve().parent / "data"
YAHOO_OPTION_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
TOS_OPTION_RE = re.compile(r"^\.?([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")


def expiration_dte(expiration: str) -> int:
    expiration_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    return (expiration_date - date.today()).days


def compact_strike_for_tos(strike: float) -> str:
    if strike.is_integer():
        return str(int(strike))

    strike_text = f"{strike:.3f}".rstrip("0").rstrip(".")
    if strike_text.endswith(".5"):
        return strike_text.replace(".", "")
    return strike_text



def parse_yahoo_option_symbol(symbol: str) -> dict:
    clean_symbol = symbol.upper().strip()
    match = YAHOO_OPTION_RE.match(clean_symbol)
    if not match:
        raise ValueError(f"Not a Yahoo/OCC option symbol: {symbol}")

    ticker, yy, mm, dd, option_type, strike_raw = match.groups()
    expiration = f"20{yy}-{mm}-{dd}"
    strike = int(strike_raw) / 1000

    return {
        "source": "YAHOO",
        "ticker": ticker,
        "expiration": expiration,
        "dte": expiration_dte(expiration),
        "option_type": "CALL" if option_type == "C" else "PUT",
        "cp": option_type,
        "strike": strike,
        "yahoo_symbol": clean_symbol,
        "tos_symbol": f".{ticker}{yy}{mm}{dd}{option_type}{compact_strike_for_tos(strike)}",
        "note": "Yahoo strike is encoded as the last 8 digits divided by 1000.",
    }


def parse_tos_option_symbol(symbol: str) -> dict:
    clean_symbol = symbol.upper().strip()
    match = TOS_OPTION_RE.match(clean_symbol)
    if not match:
        raise ValueError(f"Not a TOS option symbol: {symbol}")

    ticker, yy, mm, dd, option_type, strike_text = match.groups()
    expiration = f"20{yy}-{mm}-{dd}"
    note = ""

    if "." in strike_text:
        strike = float(strike_text)
    else:
        strike_int = int(strike_text)
        if strike_int >= 1000:
            strike = strike_int / 10
            note = "TOS strike had no decimal point; interpreted 1195 as 119.5."
        else:
            strike = float(strike_int)
            note = "TOS strike had no decimal point; interpreted it as a whole-dollar strike."

    strike_code = f"{int(round(strike * 1000)):08d}"

    return {
        "source": "TOS",
        "ticker": ticker,
        "expiration": expiration,
        "dte": expiration_dte(expiration),
        "option_type": "CALL" if option_type == "C" else "PUT",
        "cp": option_type,
        "strike": strike,
        "yahoo_symbol": f"{ticker}{yy}{mm}{dd}{option_type}{strike_code}",
        "tos_symbol": f".{ticker}{yy}{mm}{dd}{option_type}{compact_strike_for_tos(strike)}",
        "note": note,
    }


def parse_option_symbol(symbol: str) -> dict:
    try:
        return parse_yahoo_option_symbol(symbol)
    except ValueError:
        return parse_tos_option_symbol(symbol)


def print_parsed_symbol(parsed: dict) -> None:
    print("\nParsed option symbol:\n")
    print(f"Source:       {parsed['source']}")
    print(f"Ticker:       {parsed['ticker']}")
    print(f"Expiration:   {parsed['expiration']}")
    print(f"DTE:          {parsed['dte']}")
    print(f"Type:         {parsed['option_type']}")
    print(f"Strike:       {parsed['strike']:g}")
    print(f"Yahoo symbol: {parsed['yahoo_symbol']}")
    print(f"TOS symbol:   {parsed['tos_symbol']}")
    if parsed["note"]:
        print(f"Note:         {parsed['note']}")


def fetch_option_chain(ticker_symbol: str) -> tuple[list[dict], pd.DataFrame]:
    ticker_symbol = ticker_symbol.upper().strip()
    ticker = yf.Ticker(ticker_symbol)
    expirations = list(ticker.options)

    if not expirations:
        raise RuntimeError(f"No option expirations found for {ticker_symbol}.")

    rows = []
    expiration_summary = []

    for expiration in sorted(expirations, key=expiration_dte):
        dte = expiration_dte(expiration)
        chain = ticker.option_chain(expiration)

        calls = chain.calls.copy()
        puts = chain.puts.copy()

        calls.insert(0, "option_type", "CALL")
        puts.insert(0, "option_type", "PUT")

        combined = pd.concat([calls, puts], ignore_index=True)
        combined.insert(0, "ticker", ticker_symbol)
        combined.insert(1, "expiration", expiration)
        combined.insert(2, "dte", dte)

        rows.append(combined)
        expiration_summary.append(
            {
                "expiration": expiration,
                "dte": dte,
                "calls": len(calls),
                "puts": len(puts),
                "total_contracts": len(combined),
            }
        )

    return expiration_summary, pd.concat(rows, ignore_index=True)


def print_summary(ticker_symbol: str, expiration_summary: list[dict]) -> None:
    print(f"\nOption chain expirations for {ticker_symbol.upper()} ordered by nearest DTE:\n")
    print(f"{'Expiration':<12} {'DTE':>5} {'Calls':>7} {'Puts':>7} {'Total':>7}")
    print("-" * 43)

    for item in expiration_summary:
        print(
            f"{item['expiration']:<12} "
            f"{item['dte']:>5} "
            f"{item['calls']:>7} "
            f"{item['puts']:>7} "
            f"{item['total_contracts']:>7}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch an option chain by expiration/DTE from Yahoo Finance.")
    parser.add_argument("ticker", nargs="?", default=DEFAULT_TICKER, help="Underlying ticker. Default: MU")
    parser.add_argument("--no-save", action="store_true", help="Print the DTE summary without saving CSV files.")
    parser.add_argument("--parse-symbol", help="Parse a Yahoo or TOS option symbol and print both formats.")
    args = parser.parse_args()

    if args.parse_symbol:
        print_parsed_symbol(parse_option_symbol(args.parse_symbol))
        return

    ticker_symbol = args.ticker.upper().strip()
    expiration_summary, chain = fetch_option_chain(ticker_symbol)
    print_summary(ticker_symbol, expiration_summary)

    if args.no_save:
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chain_file = OUTPUT_DIR / f"{ticker_symbol}_option_chain_{timestamp}.csv"
    summary_file = OUTPUT_DIR / f"{ticker_symbol}_option_expirations_{timestamp}.csv"

    chain.to_csv(chain_file, index=False)
    pd.DataFrame(expiration_summary).to_csv(summary_file, index=False)

    print(f"\nSaved full chain: {chain_file}")
    print(f"Saved expiration summary: {summary_file}")


if __name__ == "__main__":
    main()
