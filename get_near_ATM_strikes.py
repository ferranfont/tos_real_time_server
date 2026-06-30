"""Return the N strikes nearest to ATM for a given expiration / DTE.

You give it the option DTE (or an explicit expiration date) and it returns the
closest strikes around the money, classified as ITM / ATM / OTM.

The underlying spot is estimated directly from the option-chain snapshot using
put-call parity (call_mid - put_mid + strike), so the script works offline from
the saved CSV. You can override it with --spot, or pull a live quote with --live.

Examples
--------
    python get_near_ATM_strikes.py 1                 # DTE = 1, 25 nearest strikes
    python get_near_ATM_strikes.py 1 --levels 12
    python get_near_ATM_strikes.py --expiration 2026-06-26
    python get_near_ATM_strikes.py 1 --spot 1185.5
    python get_near_ATM_strikes.py 1 --live          # spot from yfinance
"""

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from config import DEFAULT_LEVELS, TICKERS
from symbol_map import yahoo_ticker_symbol

DATA_DIR = Path(__file__).resolve().parent / "data"
CHAIN_DIR = DATA_DIR / "live" / "option_chain_and_expirations"
OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
DEFAULT_TICKER = TICKERS[0]


def dte_from_expiration(expiration: str) -> int | None:
    try:
        exp = date.fromisoformat(str(expiration))
    except ValueError:
        return None
    return max(0, (exp - date.today()).days)


def latest_chain_csv(ticker: str) -> Path:
    # Chains live in data/live/option_chain_and_expirations/ (fallback to legacy data/).
    files = sorted(CHAIN_DIR.glob(f"{ticker}_option_chain_*.csv"))
    if not files:
        files = sorted(DATA_DIR.glob(f"{ticker}_option_chain_*.csv"))
    if not files:
        raise FileNotFoundError(f"No option-chain CSV found for {ticker} in {CHAIN_DIR}")
    return files[-1]


def mid_price(row: pd.Series) -> float:
    """Best available mid: (bid+ask)/2 if both are positive, else lastPrice."""
    bid, ask = row.get("bid"), row.get("ask")
    if pd.notna(bid) and pd.notna(ask) and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return row.get("lastPrice", float("nan"))


def estimate_spot_from_parity(chain: pd.DataFrame) -> float:
    """Spot estimate via put-call parity: spot ~= call_mid - put_mid + strike.

    Averaged (median) across every strike that has a usable call and put quote,
    which is robust to a few stale/illiquid legs.
    """
    calls = chain[chain["option_type"] == "CALL"].copy()
    puts = chain[chain["option_type"] == "PUT"].copy()
    calls["mid"] = calls.apply(mid_price, axis=1)
    puts["mid"] = puts.apply(mid_price, axis=1)

    merged = calls.merge(puts, on="strike", suffixes=("_c", "_p"))
    merged = merged[(merged["mid_c"] > 0) & (merged["mid_p"] > 0)]
    if merged.empty:
        return float("nan")
    merged["spot_est"] = merged["mid_c"] - merged["mid_p"] + merged["strike"]
    return float(merged["spot_est"].median())


def estimate_spot_from_itm_flag(chain: pd.DataFrame) -> float:
    """Fallback: midpoint of the call ITM/OTM boundary."""
    calls = chain[chain["option_type"] == "CALL"].sort_values("strike")
    itm = calls[calls["inTheMoney"] == True]  # noqa: E712
    otm = calls[calls["inTheMoney"] == False]  # noqa: E712
    if not itm.empty and not otm.empty:
        return float((itm["strike"].max() + otm["strike"].min()) / 2.0)
    raise RuntimeError("Could not infer spot from inTheMoney flag.")


def live_spot(ticker: str) -> float:
    import yfinance as yf

    info = yf.Ticker(yahoo_ticker_symbol(ticker)).fast_info
    price = info.get("last_price") or info.get("lastPrice")
    if not price:
        raise RuntimeError(f"yfinance returned no last price for {ticker}")
    return float(price)


def nearest_strike_distance(strikes, value: float) -> float:
    return min(abs(float(k) - value) for k in strikes)


def normalize_spot_to_chain(spot: float, chain: pd.DataFrame) -> tuple[float, bool]:
    """Normalize RTD/Yahoo scale drift by choosing the spot scale closest to listed strikes."""
    strikes = sorted(float(k) for k in chain["strike"].dropna().unique() if float(k) > 0)
    if not strikes:
        return float(spot), False

    base = abs(float(spot))
    signed = -1.0 if float(spot) < 0 else 1.0
    candidates = {base}
    for factor in (10, 100, 1000, 10000):
        candidates.add(base / factor)
        candidates.add(base * factor)

    min_s, max_s = strikes[0], strikes[-1]
    if min_s <= base <= max_s:
        return signed * base, False

    soft_lo, soft_hi = min_s * 0.5, max_s * 1.5
    ranked = []
    for candidate in candidates:
        if candidate <= 0:
            continue
        distance = nearest_strike_distance(strikes, candidate)
        out_of_range = 0 if soft_lo <= candidate <= soft_hi else 1
        ranked.append((out_of_range, distance, abs(candidate - base), candidate))
    if not ranked:
        return float(spot), False

    normalized = signed * min(ranked)[3]
    return normalized, abs(normalized - float(spot)) > 1e-9


def classify(strike: float, spot: float, atm_strike: float) -> tuple[str, str]:
    """Return (call_moneyness, put_moneyness) for a strike relative to spot."""
    if strike == atm_strike:
        return "ATM", "ATM"
    if strike < spot:
        return "ITM", "OTM"
    return "OTM", "ITM"


def tos_symbol(yahoo_symbol: str) -> str:
    """Convert a Yahoo/OCC symbol (MU260626C01215000) to TOS (.MU260626C1215)."""
    if not isinstance(yahoo_symbol, str) or len(yahoo_symbol) < 16:
        return ""
    body, strike_code = yahoo_symbol[:-8], yahoo_symbol[-8:]
    strike = int(strike_code) / 1000.0
    compact = str(int(strike)) if strike.is_integer() else f"{strike:g}".replace(".", "")
    return f".{body}{compact}"


def near_atm_strikes(chain: pd.DataFrame, spot: float, levels: int):
    sym = {
        (row["strike"], row["option_type"]): row["contractSymbol"]
        for _, row in chain.iterrows()
    }
    # Premium (mid of bid/ask, else lastPrice) per leg, for payoff / breakeven math.
    mids = {
        (row["strike"], row["option_type"]): mid_price(row)
        for _, row in chain.iterrows()
    }
    ivs = {
        (row["strike"], row["option_type"]): row.get("impliedVolatility")
        for _, row in chain.iterrows()
    }

    def mid_of(k, opt):
        v = mids.get((k, opt))
        return round(float(v), 4) if v is not None and pd.notna(v) else None

    def iv_of(k, opt):
        v = ivs.get((k, opt))
        return round(float(v), 4) if v is not None and pd.notna(v) else None

    all_strikes = sorted(chain["strike"].unique())
    # ATM = strike closest to spot, then take `levels` strikes on EACH side of it.
    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    atm_strike = float(all_strikes[atm_idx])
    lo = max(0, atm_idx - levels)
    hi = min(len(all_strikes), atm_idx + levels + 1)
    selected = all_strikes[lo:hi]

    near = pd.DataFrame({"strike": selected}).reset_index(drop=True)
    moneyness = near["strike"].apply(lambda k: classify(k, spot, atm_strike))
    near["call"] = [m[0] for m in moneyness]
    near["put"] = [m[1] for m in moneyness]
    near["offset"] = near["strike"] - spot
    near["call_symbol"] = near["strike"].apply(lambda k: sym.get((k, "CALL"), ""))
    near["put_symbol"] = near["strike"].apply(lambda k: sym.get((k, "PUT"), ""))
    near["call_tos"] = near["call_symbol"].apply(tos_symbol)
    near["put_tos"] = near["put_symbol"].apply(tos_symbol)
    near["call_mid"] = near["strike"].apply(lambda k: mid_of(k, "CALL"))
    near["put_mid"] = near["strike"].apply(lambda k: mid_of(k, "PUT"))
    near["call_iv"] = near["strike"].apply(lambda k: iv_of(k, "CALL"))
    near["put_iv"] = near["strike"].apply(lambda k: iv_of(k, "PUT"))
    return near, atm_strike


def build_payload(ticker=DEFAULT_TICKER, expiration=None, dte=None,
                  levels=DEFAULT_LEVELS, spot=None, csv=None, live=False):
    """Compute the near-ATM payload for one expiration. Reused by the CLI and the server.

    Returns (payload_dict, near_df, csv_path, spot_src).
    Raises ValueError if the requested expiration/DTE is not in the chain.
    """
    csv_path = Path(csv) if csv else latest_chain_csv(ticker)
    df = pd.read_csv(csv_path)

    if expiration:
        chain = df[df["expiration"] == expiration]
        selector = f"expiration {expiration}"
    elif dte is not None:
        chain = df[df["dte"] == dte]
        selector = f"DTE {dte}"
    else:
        nearest_dte = int(df["dte"].min())
        chain = df[df["dte"] == nearest_dte]
        selector = f"DTE {nearest_dte} (nearest)"

    if chain.empty:
        avail = ", ".join(str(x) for x in sorted(df["dte"].unique()))
        raise ValueError(f"No rows for {selector} in {csv_path.name}. Available DTEs: {avail}")

    exp = chain["expiration"].iloc[0]
    dte_v = dte_from_expiration(exp)
    if dte_v is None:
        dte_v = int(chain["dte"].iloc[0])

    if spot is not None:
        spot_src = "manual (--spot)"
    elif live:
        spot, spot_src = live_spot(ticker), "yfinance live"
    else:
        spot = estimate_spot_from_parity(chain)
        spot_src = "put-call parity (snapshot)"
        if pd.isna(spot):
            spot = estimate_spot_from_itm_flag(chain)
            spot_src = "ITM-flag boundary (snapshot)"

    spot, normalized = normalize_spot_to_chain(float(spot), chain)
    if normalized:
        spot_src = f"{spot_src}, normalized to option-chain scale"

    near, atm_strike = near_atm_strikes(chain, spot, levels)
    spot = float(spot)

    def num(v):
        # Keep JSON valid: NaN/None -> null.
        return None if v is None or pd.isna(v) else float(v)

    payload = {
        "symbol": ticker,
        "expiration": exp,
        "dte": dte_v,
        "spot": round(spot, 2),
        "spot_source": spot_src,
        "atm": atm_strike,
        "levels": levels,
        "strikes": [
            {
                "strike": float(r["strike"]),
                "offset": round(float(r["strike"]) - spot, 2),
                "call": r["call_tos"] or None,
                "put": r["put_tos"] or None,
                "call_money": r["call"],
                "put_money": r["put"],
                "call_mid": num(r["call_mid"]),
                "put_mid": num(r["put_mid"]),
                "call_iv": num(r["call_iv"]),
                "put_iv": num(r["put_iv"]),
            }
            for _, r in near.iterrows()
        ],
    }
    return payload, near, csv_path, spot_src


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Return the N strikes nearest to ATM for a given DTE / expiration."
    )
    parser.add_argument("dte", nargs="?", type=int, help="Days to expiration (as shown in the chain).")
    parser.add_argument("--expiration", help="Expiration date YYYY-MM-DD (overrides dte).")
    parser.add_argument("--ticker", default=DEFAULT_TICKER, help=f"Underlying ticker. Default: {DEFAULT_TICKER}")
    parser.add_argument("--levels", type=int, default=DEFAULT_LEVELS,
                        help=f"Strikes on EACH side of the ATM. Default: {DEFAULT_LEVELS} ({DEFAULT_LEVELS} below + ATM + {DEFAULT_LEVELS} above)")
    parser.add_argument("--csv", help="Path to an option-chain CSV (default: latest in data/).")
    parser.add_argument("--spot", type=float, help="Override the underlying spot price.")
    parser.add_argument("--live", action="store_true", help="Fetch spot live from yfinance.")
    args = parser.parse_args()

    try:
        payload, _, csv_path, spot_src = build_payload(
            ticker=args.ticker, expiration=args.expiration, dte=args.dte,
            levels=args.levels, spot=args.spot, csv=args.csv, live=args.live,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    spot = payload["spot"]
    atm_strike = payload["atm"]

    print(f"\n{payload['symbol']}  expiration {payload['expiration']}  (DTE {payload['dte']})")
    print(f"Source CSV : {csv_path.name}")
    print(f"Spot       : {spot:.2f}   [{spot_src}]")
    print(f"ATM strike : {atm_strike:g}")
    print(f"Showing {len(payload['strikes'])} strikes ({args.levels} each side of ATM)\n")

    print(f"{'STRIKE':>8} {'OFFSET':>8} {'CALL':>4} {'PUT':>4}  {'CALL (TOS)':<16} {'PUT (TOS)':<16}")
    print("-" * 66)
    for s in payload["strikes"]:
        marker = " <-- ATM" if s["strike"] == atm_strike else ""
        print(
            f"{s['strike']:>8g} {s['offset']:>+8.2f} {s['call_money']:>4} {s['put_money']:>4}  "
            f"{(s['call'] or ''):<16} {(s['put'] or ''):<16}{marker}"
        )

    strikes = payload["strikes"]
    print("\nStrikes : " + ", ".join(f"{s['strike']:g}" for s in strikes))
    print("Calls   : " + ", ".join(s["call"] or "" for s in strikes))
    print("Puts    : " + ", ".join(s["put"] or "" for s in strikes))

    # Export JSON so the live dashboard (STRAT view) has a default to read.
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUTS_DIR / "near_atm_strikes.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nExported: {json_path}")


if __name__ == "__main__":
    main()

