import csv
import io
import json
import re
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import ladder
import session_store as store
import importlib
from config import DEFAULT_LEVELS, RECORD_LEVELS, TICKERS, USE_RTH_ONLY
from get_near_ATM_strikes import build_payload
from get_option_chain import fetch_and_save_expiration, fetch_and_save_nearest
from symbol_map import underlying_symbol_from_option_root, yahoo_ticker_symbol
from utils.black_scholes import bs_price, bs_greeks, DEFAULT_RATE
from utils.api_health_check import run_checks

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LIVE_DATA_DIR = DATA_DIR / "live"
GAMMA_DIR = DATA_DIR / "gamma"
CSV_FILE_NAME = "registro_opcion_minuto_a_minuto.csv"
LEGACY_TOS_CSV = DATA_DIR / CSV_FILE_NAME
ACTIVE_SYMBOLS_FILE = PROJECT_ROOT / "RTD_live_excel" / "active_symbols.json"
HOST = "127.0.0.1"
PORT = 8898

def safe_symbol_token(symbol):
    token = "".join(ch if ch.isalnum() else "_" for ch in str(symbol or "").upper()).strip("_")
    return token or "UNKNOWN"


def option_underlying_symbol(symbol):
    match = re.match(r"^\.?([A-Z]+)\d{6}[CP]", str(symbol or "").strip().upper())
    return underlying_symbol_from_option_root(match.group(1)) if match else None


def live_csv_file(ticker="MU", day=None):
    day = day or date.today()
    if isinstance(day, datetime):
        day = day.date()
    if isinstance(day, str):
        date_text = day
    else:
        date_text = day.isoformat()
    return LIVE_DATA_DIR / f"{safe_symbol_token(ticker)}_{date_text}_{CSV_FILE_NAME}"


def latest_live_csv(ticker="MU"):
    today_file = live_csv_file(ticker)
    if today_file.exists():
        return today_file
    pattern = f"{safe_symbol_token(ticker)}_*_{CSV_FILE_NAME}"
    files = sorted(LIVE_DATA_DIR.glob(pattern), key=lambda p: (p.name, p.stat().st_mtime)) if LIVE_DATA_DIR.exists() else []
    if files:
        return files[-1]
    if LEGACY_TOS_CSV.exists():
        return LEGACY_TOS_CSV
    return today_file

LEGACY_UNDERLYING_CSV_HEADER = [
    "timestamp", "underlying_symbol",
    "UNDERLYING_LAST", "UNDERLYING_BID", "UNDERLYING_ASK", "UNDERLYING_MARK", "UNDERLYING_VOLUME",
]


def _parse_clock(value):
    h, m = str(value).split(":", 1)
    return int(h), int(m)


RTH_TZ_ALIASES = {
    "BCN": "Europe/Madrid",
    "BARCELONA": "Europe/Madrid",
    "MADRID": "Europe/Madrid",
    "NY": "America/New_York",
    "NEW_YORK": "America/New_York",
    "NEW YORK": "America/New_York",
    "CHICAGO": "America/Chicago",
    "CHI": "America/Chicago",
}


def _rth_source(runtime_config=None):
    runtime_config = runtime_config or __import__("config")
    tz_key = str(getattr(runtime_config, "RTH_TIMEZONE", "BCN") or "BCN").upper().strip()
    start = getattr(runtime_config, "RTH_START", None)
    end = getattr(runtime_config, "RTH_END", None)
    if start is None:
        start = getattr(runtime_config, "RTH_START_BCN", "15:30")
    if end is None:
        end = getattr(runtime_config, "RTH_END_BCN", "22:50")
    return tz_key, str(start), str(end)


def rth_window_bcn(day=None, runtime_config=None):
    """Return configured RTH window converted to Barcelona local HH:MM strings."""
    target_day = date.fromisoformat(str(day)) if day else date.today()
    tz_key, start_text, end_text = _rth_source(runtime_config)
    source_tz_name = RTH_TZ_ALIASES.get(tz_key, tz_key)
    source_tz = ZoneInfo(source_tz_name)
    bcn_tz = ZoneInfo("Europe/Madrid")
    sh, sm = _parse_clock(start_text)
    eh, em = _parse_clock(end_text)
    start_src = datetime(target_day.year, target_day.month, target_day.day, sh, sm, tzinfo=source_tz)
    end_src = datetime(target_day.year, target_day.month, target_day.day, eh, em, tzinfo=source_tz)
    start_bcn = start_src.astimezone(bcn_tz)
    end_bcn = end_src.astimezone(bcn_tz)
    return start_bcn.strftime("%H:%M"), end_bcn.strftime("%H:%M"), tz_key, start_text, end_text


def _parse_local_ts(ts):
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def use_row_for_plot(ts, day=None):
    """Dashboard plot filter: today only, optionally Barcelona RTH only."""
    dt = _parse_local_ts(ts)
    if not dt:
        return False
    target_day = date.fromisoformat(str(day)) if day else date.today()
    if dt.date() != target_day:
        return False
    if not USE_RTH_ONLY:
        return True
    start_bcn, end_bcn, *_ = rth_window_bcn(target_day)
    sh, sm = _parse_clock(start_bcn)
    eh, em = _parse_clock(end_bcn)
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= dt <= end


def session_underlying_csv_bytes(ticker="MU", day=None):
    """Build the legacy CSV shape expected by the dashboard from a session folder."""
    day = day or store.latest_session_day(ticker)
    if not day:
        return None, None
    path = store.underlying_path(ticker, day)
    if not path.exists():
        if store.read_index(ticker, day):
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=LEGACY_UNDERLYING_CSV_HEADER, extrasaction="ignore")
            writer.writeheader()
            return out.getvalue().encode("utf-8"), store.session_dir(ticker, day)
        return None, None
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LEGACY_UNDERLYING_CSV_HEADER, extrasaction="ignore")
    writer.writeheader()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, skipinitialspace=True):
            if not use_row_for_plot(row.get("timestamp", ""), day):
                continue
            writer.writerow({
                "timestamp": row.get("timestamp", ""),
                "underlying_symbol": safe_symbol_token(ticker),
                "UNDERLYING_LAST": row.get("UNDERLYING_LAST", ""),
                "UNDERLYING_BID": row.get("UNDERLYING_BID", ""),
                "UNDERLYING_ASK": row.get("UNDERLYING_ASK", ""),
                "UNDERLYING_MARK": row.get("UNDERLYING_MARK", ""),
                "UNDERLYING_VOLUME": row.get("UNDERLYING_VOLUME", ""),
            })
    return out.getvalue().encode("utf-8"), path


def latest_session_underlying_bid(ticker="MU"):
    day = store.latest_session_day(ticker)
    if not day:
        return None
    idx = store.read_index(ticker, day) or {}
    normalized = bool(idx.get("normalized"))
    path = store.underlying_path(ticker, day)
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        for row in reversed(list(csv.DictReader(f, skipinitialspace=True))):
            for key in ("UNDERLYING_BID", "UNDERLYING_LAST", "UNDERLYING_MARK"):
                raw = row.get(key)
                price = _num(raw) if normalized else parse_live_price(raw, key)
                if price is not None and price > 0:
                    return price
    return None



def previous_session_underlying_close(ticker="MU", day=None):
    """Last valid underlying bid from the available session before `day`."""
    try:
        target_day = date.fromisoformat(str(day)) if day else date.today()
    except (TypeError, ValueError):
        target_day = date.today()
    target_text = target_day.isoformat()
    prev_days = [d for d in store.list_session_days(ticker) if d < target_text]
    for prev_day in reversed(prev_days):
        idx = store.read_index(ticker, prev_day) or {}
        normalized = bool(idx.get("normalized"))
        path = store.underlying_path(ticker, prev_day)
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, skipinitialspace=True))
        rth_rows = [r for r in rows if use_row_for_plot(r.get("timestamp", ""), prev_day)]
        for row in reversed(rth_rows or rows):
            for key in ("UNDERLYING_BID", "UNDERLYING_LAST", "UNDERLYING_MARK"):
                raw = row.get(key)
                price = _num(raw) if normalized else parse_live_price(raw, key)
                if price is not None and price > 0:
                    return {
                        "ticker": safe_symbol_token(ticker),
                        "date": prev_day,
                        "close": round(price, 4),
                        "timestamp": row.get("timestamp", ""),
                        "field": key,
                        "source": path.name,
                    }
    return None

def latest_session_option_quote(symbol):
    if not symbol:
        return None
    ticker = option_underlying_symbol(symbol) or "MU"
    day = store.latest_session_day(ticker)
    idx = store.read_index(ticker, day) if day else None
    if not idx:
        return None
    wanted = store.safe_token(symbol)
    contract = next((c for c in idx.get("contracts", []) if store.safe_token(c.get("symbol")) == wanted), None)
    if not contract:
        return None
    path = store.session_dir(ticker, day) / contract.get("file", "")
    if not path.exists():
        return None
    normalized = bool(idx.get("normalized"))
    with open(path, newline="", encoding="utf-8") as f:
        for row in reversed(list(csv.DictReader(f, skipinitialspace=True))):
            bid = _num(row.get("BID")) if normalized else parse_option_price(row.get("BID"), "BID")
            if bid is None or bid <= 0:
                continue
            return {
                "symbol": symbol,
                "timestamp": row.get("timestamp", ""),
                "bid": bid,
                "ask": None,
                "mark": None,
                "last": None,
                "mid": round(bid, 4),
                "source": "tos_session_bid",
                "iv": parse_percent_decimal(row.get("IMPL_VOL")),
                "greeks": {
                    "delta": normalize_live_greek("DELTA", row.get("DELTA")),
                    "gamma": normalize_live_greek("GAMMA", row.get("GAMMA")),
                    "theta": normalize_live_greek("THETA", row.get("THETA")),
                    "vega": normalize_live_greek("VEGA", row.get("VEGA")),
                },
                "raw": {
                    "DELTA": row.get("DELTA"),
                    "GAMMA": row.get("GAMMA"),
                    "THETA": row.get("THETA"),
                    "VEGA": row.get("VEGA"),
                    "IMPL_VOL": row.get("IMPL_VOL"),
                },
            }
    return None


def _num(value):
    """Plain float from a normalized tape cell (already in real units), else None."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _scale_option_price(value, field="BID"):
    """Option price from a tape cell, de-scaling the legacy comma-strip x100."""
    price = parse_live_price(value, field)
    if price is None:
        return None
    if abs(price) >= 1000:
        price /= 100.0
    return price


def session_strikes(ticker, day):
    """Strikes + ATM from a per-contract session folder. None if no folder."""
    idx = store.read_index(ticker, day)
    if not idx:
        return None
    strikes = set()
    for c in idx.get("contracts", []):
        try:
            strikes.add(float(c.get("strike")))
        except (TypeError, ValueError):
            pass
    out = [int(s) if s.is_integer() else s for s in sorted(strikes)]
    normalized = bool(idx.get("normalized"))
    underlying = None
    for _ts, raw in store.read_series(store.underlying_path(ticker, day), "UNDERLYING_BID"):
        ub = _num(raw) if normalized else parse_live_price(raw, "UNDERLYING_BID")
        if ub:
            underlying = ub  # most recent valid underlying bid
    atm = min(out, key=lambda s: abs(s - underlying)) if out and underlying else None
    return {
        "date": store._day_text(day),
        "source": store.session_dir(ticker, day).name,
        "strikes": out,
        "underlying": round(underlying, 2) if underlying else None,
        "atm": atm,
    }


# Per-contract session CSVs are named <ROOT><YYMMDD><C|P><STRIKE>.csv,
# e.g. MU260702C1130.csv, SPXW260630P7500.csv, NDXP260630C7500.csv.
_CONTRACT_RE = re.compile(r"^([A-Za-z]+)(\d{6})([CP])(\d+(?:\.\d+)?)\.csv$")


def _contract_filename_meta(name):
    """Parse a per-contract CSV name into root/expiration/type/strike, or None."""
    m = _CONTRACT_RE.match(name)
    if not m:
        return None
    root, yymmdd, typ, strike = m.groups()
    try:
        return {
            "root": root,
            "expiration": f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
            "type": "CALL" if typ.upper() == "C" else "PUT",
            "strike": float(strike),
        }
    except ValueError:
        return None


def session_expirations(ticker, day):
    """Distinct option expirations (ISO) recorded in a session folder, sorted."""
    sdir = store.session_dir(ticker, day)
    exps = set()
    if sdir.exists():
        for f in sdir.glob("*.csv"):
            meta = _contract_filename_meta(f.name)
            if meta:
                exps.add(meta["expiration"])
    return sorted(exps)


def session_premium(ticker, day, strike, expiration=None):
    """Call/put BID series + underlying from a per-contract session folder. None if no folder.

    `expiration` (ISO) optionally narrows the match when a session holds more than
    one expiration for the same strike.
    """
    idx = store.read_index(ticker, day)
    if not idx:
        return None
    target = float(strike)
    call_file = put_file = None
    for c in idx.get("contracts", []):
        try:
            if float(c.get("strike")) != target:
                continue
        except (TypeError, ValueError):
            continue
        if (c.get("type") or "").upper() == "CALL":
            call_file = c.get("file")
        elif (c.get("type") or "").upper() == "PUT":
            put_file = c.get("file")

    sdir = store.session_dir(ticker, day)

    # Fallback: the index may be empty/stale while the per-contract CSVs exist on
    # disk. Resolve the strike's files by their name, e.g. MU260702C1130.csv.
    if call_file is None and put_file is None and sdir.exists():
        for f in sorted(sdir.glob("*.csv")):
            meta = _contract_filename_meta(f.name)
            if not meta or meta["strike"] != target:
                continue
            if expiration and meta["expiration"] != expiration:
                continue
            if meta["type"] == "CALL":
                call_file = f.name
            else:
                put_file = f.name

    normalized = bool(idx.get("normalized"))

    greek_cols = ("DELTA", "GAMMA", "THETA", "VEGA", "IMPL_VOL")

    def leg_series(fname):
        out_t, out_v = [], []
        out_g = {g.lower(): [] for g in greek_cols}  # delta/gamma/theta/vega aligned with out_t
        path = sdir / fname if fname else None
        if path and path.exists():
            with open(path, newline="", encoding="utf-8") as f:  # one pass: BID + greeks aligned
                for row in csv.DictReader(f):
                    ts = row.get("timestamp", "")
                    if not use_row_for_plot(ts, day):
                        continue
                    raw = row.get("BID", "")
                    price = _num(raw) if normalized else _scale_option_price(raw, "BID")
                    if price is None:
                        continue
                    out_t.append(ts)
                    out_v.append(round(price, 4))
                    for g in greek_cols:
                        gv = _num(row.get(g, ""))
                        out_g[g.lower()].append(round(gv, 6) if gv is not None else None)
        return {"t": out_t, "last": out_v, **out_g}

    under_t, under_b, seen = [], [], set()
    for ts, raw in store.read_series(store.underlying_path(ticker, day), "UNDERLYING_BID"):
        if ts in seen or not use_row_for_plot(ts, day):
            continue
        ub = _num(raw) if normalized else parse_live_price(raw, "UNDERLYING_BID")
        if ub:
            seen.add(ts)
            under_t.append(ts)
            under_b.append(round(ub, 4))

    return {
        "strike": target,
        "source": sdir.name,
        "date": store._day_text(day),
        "call": leg_series(call_file),
        "put": leg_series(put_file),
        "underlying": {"t": under_t, "bid": under_b},
    }


def parse_live_price(value, field=None):
    """Normalize TOS RTD price scale for underlying fields."""
    try:
        price = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None

    field = (field or "").upper()
    if field in {"UNDERLYING_BID", "UNDERLYING_ASK", "BID", "ASK"}:
        while abs(price) >= 10000:
            price = price / 100.0
    elif field in {"UNDERLYING_LAST", "LAST"}:
        price = price / 10000.0 if abs(price) >= 1000000 else (price / 100.0 if abs(price) >= 10000 else price)
    elif field in {"UNDERLYING_MARK", "MARK"}:
        price = price / 1000.0 if abs(price) >= 1000000 else (price / 100.0 if abs(price) >= 10000 else price)
    elif abs(price) >= 10000:
        price = price / 100.0
    return price


def latest_live_underlying_bid(ticker="MU"):
    """Read the latest live UNDERLYING_BID from session storage, falling back to legacy CSV."""
    session_bid = latest_session_underlying_bid(ticker)
    if session_bid is not None:
        return session_bid
    csv_path = latest_live_csv(ticker)
    if not csv_path.exists():
        raise ValueError(f"CSV live no existe: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, skipinitialspace=True))

    for row in reversed(rows):
        symbol = (row.get("underlying_symbol") or "").strip().upper()
        if ticker and symbol and symbol != ticker.upper():
            continue
        for key in ("UNDERLYING_BID", "UNDERLYING_LAST", "UNDERLYING_MARK"):
            price = parse_live_price(row.get(key), key)
            if price is not None and price > 0:
                return price

    raise ValueError(f"No hay UNDERLYING_BID valido en {csv_path}")


def request_spot_or_none(ticker, spot_arg=None):
    """Explicit spot wins; otherwise use live tape if available, else let chain logic infer it."""
    if spot_arg not in (None, ""):
        return float(spot_arg)
    try:
        return latest_live_underlying_bid(ticker)
    except ValueError:
        return None

def parse_option_price(value, field=None):
    """Normalize option RTD prices from TOS.

    BID/ASK/LAST commonly arrive as cents (1150 -> 11.50). MARK often
    arrives with one extra digit (11725 -> 11.725).
    """
    try:
        price = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    field = (field or "").upper()
    if field == "MARK":
        return price / 1000.0 if abs(price) >= 1000 else price
    return price / 100.0 if abs(price) >= 10 else price



def parse_percent_decimal(value):
    if value in (None, ""):
        return None
    raw = str(value).strip().replace(",", "")
    if raw in {"-", "--", "N/A", "#N/A"}:
        return None
    try:
        if raw.endswith("%"):
            return float(raw[:-1]) / 100.0
        number = float(raw)
    except ValueError:
        return None
    return number / 100.0 if number > 10 else number


def parse_float_value(value):
    if value in (None, ""):
        return None
    raw = str(value).strip().replace(",", "")
    if raw in {"-", "--", "N/A", "#N/A"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def normalize_live_greek(field, value):
    number = parse_float_value(value)
    if number is None:
        return None
    field = field.upper()
    if field == "DELTA" and abs(number) > 1:
        return number / 100.0
    if field == "GAMMA" and abs(number) > 1:
        return number / 100.0
    if field == "THETA" and abs(number) > 100:
        return number / 100.0
    if field == "VEGA" and abs(number) > 100:
        return number / 100.0
    return number


def scale_short_live_greeks(quote, fallback, mult):
    live = (quote or {}).get("greeks") or {}
    result = {}
    for key in ("delta", "gamma", "theta", "vega"):
        value = live.get(key)
        if value is None:
            value = (fallback or {}).get(key)
        result[key] = round(-value * mult, 4) if value is not None else None
    rho = (fallback or {}).get("rho")
    result["rho"] = round(-rho * mult, 4) if rho is not None else None
    return result

def latest_live_option_quote(symbol):
    """Return latest normalized live quote for one option symbol."""
    session_quote = latest_session_option_quote(symbol)
    if session_quote:
        return session_quote
    if not symbol:
        return None
    csv_path = latest_live_csv(option_underlying_symbol(symbol) or "MU")
    if not csv_path.exists():
        return None
    wanted = str(symbol).strip().upper()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in reversed(list(csv.DictReader(f, skipinitialspace=True))):
            if str(row.get("symbol") or "").strip().upper() != wanted:
                continue
            # Only BID is stored now; use it as the price for everything.
            bid = parse_option_price(row.get("BID"), "BID")
            if bid is None or bid <= 0:
                return None
            mid = bid
            source = "tos_live_bid"
            return {
                "symbol": symbol,
                "timestamp": row.get("timestamp", ""),
                "bid": bid,
                "ask": None,
                "mark": None,
                "last": None,
                "mid": round(mid, 4),
                "source": source,
                "iv": parse_percent_decimal(row.get("IMPL_VOL")),
                "greeks": {
                    "delta": normalize_live_greek("DELTA", row.get("DELTA")),
                    "gamma": normalize_live_greek("GAMMA", row.get("GAMMA")),
                    "theta": normalize_live_greek("THETA", row.get("THETA")),
                    "vega": normalize_live_greek("VEGA", row.get("VEGA")),
                },
                "raw": {
                    "DELTA": row.get("DELTA"),
                    "GAMMA": row.get("GAMMA"),
                    "THETA": row.get("THETA"),
                    "VEGA": row.get("VEGA"),
                    "IMPL_VOL": row.get("IMPL_VOL"),
                },
            }
    return None


def premium_from_live_or_chain(entry, opt_type):
    live = latest_live_option_quote(entry.get(opt_type.lower()) if entry else None)
    if live and live.get("mid") is not None:
        return live["mid"], live
    if not entry:
        return None, None
    key = "call_mid" if opt_type == "CALL" else "put_mid"
    value = entry.get(key)
    return value, {"source": "yahoo_snapshot", "mid": value}


def fmt_strike(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number).replace(".", "_")


def strategy_display_name(strategy):
    return "Short Strangle" if strategy == "short_strangle" else "Short Straddle"


def strategy_id(symbol, expiration, strategy, put_strike, call_strike):
    if strategy == "short_strangle":
        strikes = f"{fmt_strike(put_strike)}_{fmt_strike(call_strike)}"
    else:
        strikes = fmt_strike(call_strike)
    return f"{symbol}_{expiration}_{strategy.upper()}_{strikes}"


def read_active_book():
    if not ACTIVE_SYMBOLS_FILE.exists():
        return {"strategies": []}
    try:
        data = json.loads(ACTIVE_SYMBOLS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"strategies": []}

    if isinstance(data.get("strategies"), list):
        return data

    options = data.get("options") or []
    if not options:
        return {"strategies": []}

    strategy = (data.get("strategy") or "short_straddle").lower()
    if strategy not in {"short_straddle", "short_strangle"}:
        strategy = "short_straddle"
    expiration = data.get("expiration") or ""
    symbol = data.get("underlying") or "MU"
    put_strike = next((o.get("strike") for o in options if (o.get("role") or o.get("type")) == "PUT"), None)
    call_strike = next((o.get("strike") for o in options if (o.get("role") or o.get("type")) == "CALL"), None)
    if put_strike is None:
        put_strike = options[0].get("strike", "")
    if call_strike is None:
        call_strike = options[0].get("strike", "")

    sid = strategy_id(symbol, expiration, strategy, put_strike, call_strike)
    label_strike = f"{fmt_strike(put_strike)}/{fmt_strike(call_strike)}" if strategy == "short_strangle" else fmt_strike(call_strike)
    label = f"{strategy_display_name(strategy)} - {symbol} {expiration} @ {label_strike}"
    legs = []
    for i, option in enumerate(options, start=1):
        role = option.get("role") or option.get("type") or ""
        legs.append({
            "symbol": option.get("symbol"),
            "role": role,
            "type": role,
            "strike": option.get("strike"),
            "expiration": option.get("expiration") or expiration,
            "side": option.get("side") or "SHORT",
            "qty": option.get("qty", -1),
            "leg_index": i,
        })
    return {
        "underlying": symbol,
        "updated_at": data.get("written_at") or data.get("updated_at"),
        "strategies": [{
            "id": sid,
            "strategy": strategy,
            "label": label,
            "expiration": expiration,
            "dte": data.get("dte"),
            "put_strike": put_strike,
            "call_strike": call_strike,
            "legs": legs,
        }],
    }


def flatten_strategy_options(strategies):
    options = []
    for strat in strategies:
        for leg in strat.get("legs", []):
            option = dict(leg)
            option["strategy_id"] = strat.get("id", "")
            option["strategy"] = strat.get("strategy", "")
            option["strategy_label"] = strat.get("label", "")
            option["dte"] = strat.get("dte", "")
            options.append(option)
    return options



def build_payload_with_chain_refresh(ticker, expiration=None, dte=None, levels=DEFAULT_LEVELS, spot=None):
    """Build near-ATM payload, fetching the needed Yahoo chain if no local CSV exists."""
    try:
        return build_payload(ticker=ticker, expiration=expiration, dte=dte, levels=levels, spot=spot)
    except (FileNotFoundError, ValueError):
        if expiration:
            fetch_and_save_expiration(ticker, expiration)
        elif dte is None:
            fetch_and_save_nearest(ticker)
        else:
            raise
        return build_payload(ticker=ticker, expiration=expiration, dte=dte, levels=levels, spot=spot)


def current_chart_config():
    """Read chart range settings from config.py for the browser."""
    import config as runtime_config
    runtime_config = importlib.reload(runtime_config)
    start_bcn, end_bcn, tz_key, source_start, source_end = rth_window_bcn(runtime_config=runtime_config)
    return {
        "use_rth_only": bool(getattr(runtime_config, "USE_RTH_ONLY", True)),
        "rth_start_bcn": start_bcn,
        "rth_end_bcn": end_bcn,
        "rth_timezone": tz_key,
        "rth_start": source_start,
        "rth_end": source_end,
    }

class TosLiveHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/save-record":
            self.serve_save_record()
            return
        self.send_error(404, "unknown POST")

    def serve_save_record(self):
        """Save a trading-record snapshot (PNG capture + JSON data) under
        outputs/trading_record/<symbol>_<date>_<strikes>_<entry>[ _n].{png,json}."""
        import base64
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"JSON invalido: {exc}"})
            return
        base = re.sub(r"[^A-Za-z0-9._-]", "_", str(payload.get("filename") or "record")).strip("_") or "record"
        folder = PROJECT_ROOT / "outputs" / "trading_record"
        folder.mkdir(parents=True, exist_ok=True)
        name, i = base, 2
        while (folder / f"{name}.json").exists() or (folder / f"{name}.png").exists():
            name, i = f"{base}_{i}", i + 1
        try:
            (folder / f"{name}.json").write_text(json.dumps(payload.get("record") or {}, indent=2), encoding="utf-8")
            png = payload.get("png") or ""
            if png.startswith("data:image/png;base64,"):
                (folder / f"{name}.png").write_bytes(base64.b64decode(png.split(",", 1)[1]))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"No se pudo guardar: {exc}"})
            return
        self._send_json({"ok": True, "name": name, "folder": "outputs/trading_record"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tos-live-csv":
            self.serve_tos_csv(parse_qs(parsed.query))
            return
        if parsed.path == "/api/near-atm":
            self.serve_near_atm(parse_qs(parsed.query))
            return
        if parsed.path == "/api/payoff":
            self.serve_payoff(parse_qs(parsed.query))
            return
        if parsed.path == "/api/send-to-excel":
            self.serve_send(parse_qs(parsed.query))
            return
        if parsed.path == "/api/premium-series":
            self.serve_premium(parse_qs(parsed.query))
            return
        if parsed.path == "/api/live-sessions":
            self.serve_sessions(parse_qs(parsed.query))
            return
        if parsed.path == "/api/live-strikes":
            self.serve_strikes(parse_qs(parsed.query))
            return
        if parsed.path == "/api/live-expirations":
            self.serve_session_expirations(parse_qs(parsed.query))
            return
        if parsed.path == "/api/gamma":
            self.serve_gamma(parse_qs(parsed.query))
            return
        if parsed.path == "/api/gamma-history":
            self.serve_gamma_history(parse_qs(parsed.query))
            return
        if parsed.path == "/api/health-check":
            self.serve_health_check(parse_qs(parsed.query))
            return
        if parsed.path == "/api/start":
            self.serve_start(parse_qs(parsed.query))
            return
        if parsed.path == "/api/expirations":
            self.serve_expirations(parse_qs(parsed.query))
            return
        if parsed.path == "/api/previous-close":
            self.serve_previous_close(parse_qs(parsed.query))
            return
        if parsed.path == "/api/chart-config":
            self._send_json(current_chart_config())
            return
        if parsed.path == "/api/tickers":
            self._send_json({"tickers": list(TICKERS)})
            return
        if parsed.path in ("/", ""):
            self.path = "/outputs/tos_live_underlying.html"
        super().do_GET()

    def serve_near_atm(self, query):
        """Compute near-ATM strikes for the requested expiration/DTE from the latest chain CSV."""
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        try:
            expiration = first("expiration")
            dte = first("dte")
            levels = first("levels")
            ticker = first("ticker") or "MU"
            spot_arg = first("spot")
            spot = request_spot_or_none(ticker, spot_arg)
            payload, *_ = build_payload_with_chain_refresh(
                ticker=ticker,
                expiration=expiration,
                dte=int(dte) if dte not in (None, "") else None,
                levels=int(levels) if levels not in (None, "") else DEFAULT_LEVELS,
                spot=spot,
            )
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any compute error to the client
            self.send_error(500, str(exc))
            return

        self._send_json(payload)
    def serve_payoff(self, query):
        """Short premium strategies: expiration payoff + 'today' Black-Scholes curve."""
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        try:
            expiration = first("expiration")
            dte = first("dte")
            strategy = (first("strategy") or "short_straddle").lower()
            anchor = float(first("strike")) if first("strike") not in (None, "") else None
            contracts = int(first("contracts") or 1)
            rate = float(first("r")) if first("r") not in (None, "") else DEFAULT_RATE
            ticker = first("ticker") or "MU"
            spot_arg = first("spot")
            spot = request_spot_or_none(ticker, spot_arg)
            payload, *_ = build_payload_with_chain_refresh(
                ticker=ticker,
                expiration=expiration,
                dte=int(dte) if dte not in (None, "") else None,
                spot=spot,
            )
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))
            return

        def entry_for(strike):
            return next((s for s in payload["strikes"] if float(s["strike"]) == float(strike)), None)

        if strategy == "short_strangle":
            try:
                put_strike = float(first("put_strike")) if first("put_strike") not in (None, "") else None
                call_strike = float(first("call_strike")) if first("call_strike") not in (None, "") else None
                if put_strike is None or call_strike is None:
                    if anchor is None:
                        raise ValueError("Short Strangle necesita put_strike/call_strike o strike ancla.")
                    strikes = sorted(float(s["strike"]) for s in payload["strikes"])
                    put_strike = max((s for s in strikes if s < anchor), default=None)
                    call_strike = min((s for s in strikes if s > anchor), default=None)
                if put_strike is None or call_strike is None or put_strike >= call_strike:
                    raise ValueError("Short Strangle necesita un put inferior y un call superior.")
            except ValueError as exc:
                self._send_json({"error": str(exc)})
                return
            put_entry = entry_for(put_strike)
            call_entry = entry_for(call_strike)
        else:
            if anchor is None:
                self._send_json({"error": "Falta strike para Short Straddle."})
                return
            strategy = "short_straddle"
            put_strike = call_strike = anchor
            put_entry = call_entry = entry_for(anchor)

        if put_entry is None or call_entry is None:
            self._send_json({"error": "Los strikes seleccionados no estan en la cadena para esa expiracion."})
            return

        call_mid, call_quote = premium_from_live_or_chain(call_entry, "CALL")
        put_mid, put_quote = premium_from_live_or_chain(put_entry, "PUT")
        if call_mid is None or put_mid is None:
            missing = "call" if call_mid is None else "put"
            strike = call_strike if call_mid is None else put_strike
            self._send_json({"error": f"Sin prima live/Yahoo para la {missing} de {strike:g}."})
            return

        call_is_live = (call_quote or {}).get("source", "").startswith("tos_")
        put_is_live = (put_quote or {}).get("source", "").startswith("tos_")
        premium_source = "tos_live" if call_is_live and put_is_live else "yahoo_snapshot"
        dte_v = payload["dte"]
        if dte_v == 0 and premium_source != "tos_live":
            missing_symbols = []
            if not call_is_live:
                missing_symbols.append(call_entry.get("call") or f"CALL {call_strike:g}")
            if not put_is_live:
                missing_symbols.append(put_entry.get("put") or f"PUT {put_strike:g}")
            self._send_json({
                "error": "Esperando TOS live para " + ", ".join(missing_symbols) + ". Pulsa SEND y espera el siguiente tick del collector; no uso Yahoo para 0DTE.",
                "premium_source": premium_source,
                "call_quote": call_quote,
                "put_quote": put_quote,
            })
            return

        mult = 100 * contracts
        credit = call_mid + put_mid
        T = max(dte_v, 0) / 365.0
        today_T = max(dte_v, 1) / 365.0
        greeks_T = today_T
        call_iv = (call_quote or {}).get("iv") or call_entry["call_iv"] or 0.0
        put_iv = (put_quote or {}).get("iv") or put_entry["put_iv"] or 0.0
        iv_source = "tos_live" if (call_quote or {}).get("iv") is not None and (put_quote or {}).get("iv") is not None else "yahoo_snapshot"
        center = (put_strike + call_strike) / 2.0
        be_low = put_strike - credit
        be_high = call_strike + credit

        half = max(credit * 1.3 + (call_strike - put_strike) / 2.0, center * 0.05)
        n = 121
        xs = [center - half + i * (2 * half) / (n - 1) for i in range(n)]
        expiry = [
            (credit - max(S - call_strike, 0.0) - max(put_strike - S, 0.0)) * mult
            for S in xs
        ]
        today = [
            (credit - (
                bs_price(S, call_strike, today_T, rate, call_iv, "C")
                + bs_price(S, put_strike, today_T, rate, put_iv, "P")
            )) * mult
            for S in xs
        ]

        spot_for_greeks = float(payload["spot"])
        call_g = bs_greeks(spot_for_greeks, call_strike, greeks_T, rate, call_iv, "C")
        put_g = bs_greeks(spot_for_greeks, put_strike, greeks_T, rate, put_iv, "P")

        short_call_g = scale_short_live_greeks(call_quote, call_g, mult)
        short_put_g = scale_short_live_greeks(put_quote, put_g, mult)
        net_g = {
            k: round((short_call_g.get(k) or 0.0) + (short_put_g.get(k) or 0.0), 4)
            for k in short_call_g
        }
        greeks_source = "tos_live" if (call_quote or {}).get("greeks") and (put_quote or {}).get("greeks") else "black_scholes"

        self._send_json({
            "strategy": strategy,
            "symbol": payload["symbol"],
            "expiration": payload["expiration"],
            "dte": dte_v,
            "strike": center if strategy == "short_strangle" else call_strike,
            "put_strike": put_strike,
            "call_strike": call_strike,
            "contracts": contracts,
            "call_mid": call_mid,
            "put_mid": put_mid,
            "premium_source": premium_source,
            "call_quote": call_quote,
            "put_quote": put_quote,
            "call_iv": call_iv,
            "put_iv": put_iv,
            "avg_iv": round((call_iv + put_iv) / 2.0, 4),
            "iv_source": iv_source,
            "greeks_source": greeks_source,
            "today_t_years": round(today_T, 5),
            "greeks_t_years": round(greeks_T, 5),
            "greeks": {
                "short_call": short_call_g,
                "short_put": short_put_g,
                "net": net_g,
            },
            "credit": round(credit, 4),
            "net": round(credit * mult, 2),
            "max_profit": round(credit * mult, 2),
            "be_low": round(be_low, 4),
            "be_high": round(be_high, 4),
            "r": rate,
            "t_years": round(T, 5),
            "x": [round(v, 3) for v in xs],
            "expiry": [round(v, 2) for v in expiry],
            "today": [round(v, 2) for v in today],
        })
    def serve_send(self, query):
        """Append/upsert the strategy legs in active_symbols.json for the RTD portfolio."""
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        try:
            expiration = first("expiration")
            dte = first("dte")
            strategy = (first("strategy") or "short_straddle").lower()
            anchor = float(first("strike")) if first("strike") not in (None, "") else None
            put_strike = float(first("put_strike")) if first("put_strike") not in (None, "") else None
            call_strike = float(first("call_strike")) if first("call_strike") not in (None, "") else None
            ticker = first("ticker") or "MU"
            spot = request_spot_or_none(ticker)
            payload, *_ = build_payload_with_chain_refresh(
                ticker=ticker,
                expiration=expiration,
                dte=int(dte) if dte not in (None, "") else None,
                spot=spot,
            )
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))
            return

        def entry_for(strike):
            return next((s for s in payload["strikes"] if float(s["strike"]) == float(strike)), None)

        if strategy == "short_strangle" and put_strike is not None and call_strike is not None:
            ps, cs = put_strike, call_strike
        else:
            strategy = "short_straddle"
            if anchor is None:
                self._send_json({"error": "Falta el strike para enviar a Excel."})
                return
            ps = cs = anchor

        call_entry, put_entry = entry_for(cs), entry_for(ps)
        if call_entry is None or put_entry is None:
            self._send_json({"error": "Los strikes no estan en la cadena para esa expiracion."})
            return

        call_sym, put_sym = call_entry["call"], put_entry["put"]
        if not call_sym or not put_sym:
            missing = "call" if not call_sym else "put"
            self._send_json({"error": f"Sin contrato/simbolo para la {missing}."})
            return

        sid = strategy_id(payload["symbol"], payload["expiration"], strategy, ps, cs)
        label_strike = f"{fmt_strike(ps)}/{fmt_strike(cs)}" if strategy == "short_strangle" else fmt_strike(cs)
        label = f"{strategy_display_name(strategy)} - {payload['symbol']} {payload['expiration']} @ {label_strike}"
        record = {
            "id": sid,
            "strategy": strategy,
            "label": label,
            "expiration": payload["expiration"],
            "dte": payload["dte"],
            "put_strike": ps,
            "call_strike": cs,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "legs": [
                {"symbol": call_sym, "role": "CALL", "type": "CALL", "strike": cs, "expiration": payload["expiration"], "side": "SHORT", "qty": -1, "leg_index": 1},
                {"symbol": put_sym, "role": "PUT", "type": "PUT", "strike": ps, "expiration": payload["expiration"], "side": "SHORT", "qty": -1, "leg_index": 2},
            ],
        }

        active = read_active_book()
        strategies = [s for s in active.get("strategies", []) if s.get("id") != sid]
        strategies.append(record)
        active = {
            "underlying": payload["symbol"],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "strategies": strategies,
            "options": flatten_strategy_options(strategies),
        }
        ACTIVE_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_SYMBOLS_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")
        self._send_json({"ok": True, "path": str(ACTIVE_SYMBOLS_FILE), "added": record, **active})

    def serve_expirations(self, query):
        """Live expirations (date + DTE today) from Yahoo, dropping past ones."""
        ticker = (query.get("ticker") or ["MU"])[0]
        try:
            import yfinance as yf
            from get_option_chain import expiration_dte
            exps = list(yf.Ticker(yahoo_ticker_symbol(ticker)).options)
            out = [{"expiration": e, "dte": expiration_dte(e)}
                   for e in sorted(exps, key=expiration_dte) if expiration_dte(e) >= 0]
            self._send_json({"ticker": ticker, "expirations": out})
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ticker": ticker, "expirations": [], "error": str(exc)})

    def serve_start(self, query):
        """Start/reset: nearest expiration -> ATM -> RECORD_LEVELS strikes each side -> all legs.

        Reuses build_payload (chain -> nearest DTE -> spot/ATM -> levels). Writes the full ladder
        (call+put per strike) so the collector subscribes the whole ladder for the session.
        """
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        levels = int(first("levels")) if first("levels") not in (None, "") else RECORD_LEVELS
        refresh = (first("refresh") or "1").lower() not in ("0", "false", "no")
        single = first("ticker")  # optional: anchor just one ticker
        try:
            if single:
                result = ladder.anchor_ladder(ticker=single, levels=levels, refresh=refresh, mode="start")
            else:
                result = ladder.anchor_all(TICKERS, levels=levels, refresh=refresh, mode="start")
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))
            return
        self._send_json(result)

    def serve_premium(self, query):
        """Intraday evolution of the option premium (LAST) for one strike, from the live CSV.

        Returns CALL and PUT time series (timestamp + normalized LAST) so the dashboard
        can chart how the premium moved during the session.
        """
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        ticker = first("ticker") or "MU"
        day = first("date")
        expiration = first("expiration")
        try:
            target = float(first("strike"))
        except (TypeError, ValueError):
            self._send_json({"error": "Falta strike valido."})
            return

        # Folder-first: per-contract session; fall back to legacy flat CSV.
        folder_day = day or store.latest_session_day(ticker)
        res = session_premium(ticker, folder_day, target, expiration) if folder_day else None
        if res and (res["call"]["t"] or res["put"]["t"]):
            self._send_json(res)
            return

        csv_path = live_csv_file(ticker, day) if day else latest_live_csv(ticker)
        if not csv_path.exists():
            self._send_json({"error": f"No hay CSV de sesion para {day or 'la ultima sesion'}."})
            return

        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            self._send_json({"error": "CSV de sesion vacio."})
            return

        header = [h.strip() for h in rows[0]]
        idx = {name: i for i, name in enumerate(header)}

        def cell(row, name):
            i = idx.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        greek_cols = ("DELTA", "GAMMA", "THETA", "VEGA", "IMPL_VOL")
        series = {leg: {"t": [], "last": [], "delta": [], "gamma": [], "theta": [], "vega": [], "impl_vol": []} for leg in ("CALL", "PUT")}
        under = {}  # timestamp -> underlying bid (one per timestamp, shared by both legs)
        for row in rows[1:]:
            if not row:
                continue
            ts = cell(row, "timestamp")
            if ts and ts not in under:
                ub = parse_live_price(cell(row, "UNDERLYING_BID"), "UNDERLYING_BID")
                if ub:
                    under[ts] = round(ub, 4)
            try:
                if float(cell(row, "strike")) != target:
                    continue
            except ValueError:
                continue
            # Premium series now uses BID (LAST/ASK/MARK no longer stored).
            last = parse_live_price(cell(row, "BID"), "BID")
            if last is None:
                continue
            # Legacy data: the collector stripped decimal commas, inflating option prices by 100
            # (e.g. 54,60 -> 5460). Near-ATM premiums never reach 1000, so this safely de-scales.
            if abs(last) >= 1000:
                last /= 100.0
            leg = cell(row, "leg_type").upper()
            if leg in series:
                series[leg]["t"].append(ts)
                series[leg]["last"].append(round(last, 4))
                for gc in greek_cols:
                    try:
                        series[leg][gc.lower()].append(round(float(cell(row, gc)), 6))
                    except ValueError:
                        series[leg][gc.lower()].append(None)

        self._send_json({
            "strike": target,
            "source": csv_path.name,
            "date": day or "",
            "call": series["CALL"],
            "put": series["PUT"],
            "underlying": {"t": list(under.keys()), "bid": list(under.values())},
        })

    def serve_sessions(self, query):
        """List available session days (one live CSV per day)."""
        ticker = (query.get("ticker") or ["MU"])[0]
        token = safe_symbol_token(ticker)
        days = set(store.list_session_days(ticker))  # per-contract session folders
        if LIVE_DATA_DIR.exists():
            for f in LIVE_DATA_DIR.glob(f"{token}_*_{CSV_FILE_NAME}"):  # legacy flat CSVs
                m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
                if m:
                    days.add(m.group(1))
        days = sorted(days)
        self._send_json({"days": days, "latest": days[-1] if days else None})

    def serve_session_expirations(self, query):
        """List option expirations (ISO) recorded for a ticker on a session day."""
        ticker = (query.get("ticker") or ["MU"])[0]
        day = (query.get("date") or [None])[0] or store.latest_session_day(ticker)
        exps = session_expirations(ticker, day) if day else []
        self._send_json({"date": day or "", "expirations": exps})

    def serve_gamma(self, query):
        """Gamma-by-strike from data/gamma/<TICKER>_GAMM_by_strikes_<date>.csv.

        Returns the latest snapshot: {timestamp, spot, walls, strikes:[{strike, gamma}]}.
        gamma keeps its sign (positive/negative) per strike.
        """
        ticker = (query.get("ticker") or ["MU"])[0].upper()
        day = (query.get("date") or [None])[0] or date.today().isoformat()
        path = GAMMA_DIR / f"{safe_symbol_token(ticker)}_GAMM_by_strikes_{day}.csv"
        if not path.exists():
            self._send_json({"ticker": ticker, "date": day, "strikes": [], "error": f"Sin gamma para {ticker} {day}."})
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            self._send_json({"ticker": ticker, "date": day, "strikes": []})
            return

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        last_ts = rows[-1].get("timestamp", "")
        latest = [r for r in rows if r.get("timestamp") == last_ts]
        strikes = [
            {
                "strike": num(r.get("strike")), "gamma": num(r.get("gamma")),
                "g5": num(r.get("gamma_5m")), "g15": num(r.get("gamma_15m")), "g30": num(r.get("gamma_30m")),
            }
            for r in latest
            if num(r.get("strike")) is not None and num(r.get("gamma")) is not None
        ]
        meta = latest[0] if latest else {}
        self._send_json({
            "ticker": ticker, "date": day, "timestamp": last_ts,
            "spot": num(meta.get("spot")),
            "major_positive": num(meta.get("major_positive")),
            "major_negative": num(meta.get("major_negative")),
            "major_long_gamma": num(meta.get("major_long_gamma")),
            "major_short_gamma": num(meta.get("major_short_gamma")),
            "gamma_flip": num(meta.get("gamma_flip")),
            "strikes": strikes,
        })

    def serve_gamma_history(self, query):
        """Time series of the session-level gamma levels for a day.

        One point per snapshot (the level fields repeat across each snapshot's
        strike rows, so we keep the first row per distinct timestamp). Feeds the
        Long/Short gamma-wall migration lines on the chart.
        """
        ticker = (query.get("ticker") or ["MU"])[0].upper()
        day = (query.get("date") or [None])[0] or date.today().isoformat()
        path = GAMMA_DIR / f"{safe_symbol_token(ticker)}_GAMM_by_strikes_{day}.csv"
        if not path.exists():
            self._send_json({"ticker": ticker, "date": day, "points": [], "error": f"Sin gamma para {ticker} {day}."})
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        points = []
        seen = set()
        for r in rows:
            ts = r.get("timestamp", "")
            if not ts or ts in seen:
                continue
            seen.add(ts)
            points.append({
                "t": ts,
                "spot": num(r.get("spot")),
                "major_positive": num(r.get("major_positive")),
                "major_negative": num(r.get("major_negative")),
                "major_long_gamma": num(r.get("major_long_gamma")),
                "major_short_gamma": num(r.get("major_short_gamma")),
                "gamma_flip": num(r.get("gamma_flip")),
            })
        self._send_json({"ticker": ticker, "date": day, "points": points})

    def serve_strikes(self, query):
        """List the strikes present in a session day's live CSV."""
        ticker = (query.get("ticker") or ["MU"])[0]
        day = (query.get("date") or [None])[0]
        # Folder-first: per-contract session; fall back to legacy flat CSV.
        folder_day = day or store.latest_session_day(ticker)
        res = session_strikes(ticker, folder_day) if folder_day else None
        if res and res.get("strikes"):
            self._send_json(res)
            return
        csv_path = live_csv_file(ticker, day) if day else latest_live_csv(ticker)
        if not csv_path.exists():
            self._send_json({"date": day or "", "strikes": [], "error": "Sin CSV para esa sesion."})
            return
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            self._send_json({"date": day or "", "source": csv_path.name, "strikes": []})
            return
        header = [h.strip() for h in rows[0]]
        idx = {name: i for i, name in enumerate(header)}
        si = idx.get("strike")
        ubi = idx.get("UNDERLYING_BID")
        strikes = set()
        underlying = None
        for row in rows[1:]:
            if si is not None and si < len(row):
                try:
                    strikes.add(float(row[si].strip()))
                except ValueError:
                    pass
            if ubi is not None and ubi < len(row):
                val = parse_live_price(row[ubi].strip(), "UNDERLYING_BID")
                if val:
                    underlying = val  # keep the most recent valid underlying bid
        out = [int(s) if s.is_integer() else s for s in sorted(strikes)]
        atm = min(out, key=lambda s: abs(s - underlying)) if out and underlying else None
        self._send_json({
            "date": day or "",
            "source": csv_path.name,
            "strikes": out,
            "underlying": round(underlying, 2) if underlying else None,
            "atm": atm,
        })

    def serve_previous_close(self, query):
        ticker = (query.get("ticker") or ["MU"])[0]
        day = (query.get("date") or [None])[0]
        payload = previous_session_underlying_close(ticker, day)
        if payload is None:
            self._send_json({"ticker": safe_symbol_token(ticker), "date": day or "", "close": None})
            return
        self._send_json(payload)

    def serve_health_check(self, query):
        ticker = (query.get("ticker") or [None])[0] or read_active_book().get("underlying") or "MU"
        day = (query.get("date") or [None])[0] or date.today().isoformat()
        try:
            timeout = float((query.get("timeout") or [2.5])[0])
        except (TypeError, ValueError):
            timeout = 2.5
        try:
            fresh_seconds = int((query.get("fresh_seconds") or [180])[0])
        except (TypeError, ValueError):
            fresh_seconds = 180
        self._send_json(run_checks(ticker=ticker, day=day, timeout=timeout, fresh_seconds=fresh_seconds))
    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_tos_csv(self, query=None):
        query = query or {}
        ticker = (query.get("ticker") or [None])[0] or read_active_book().get("underlying") or "MU"
        day = (query.get("date") or [None])[0] or date.today().isoformat()

        data, source_path = session_underlying_csv_bytes(ticker, day)
        if data is not None:
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Source", str(source_path.name))
            self.end_headers()
            self.wfile.write(data)
            return

        csv_path = live_csv_file(ticker, day) if day else latest_live_csv(ticker)
        if not csv_path.exists():
            csv_path = latest_live_csv(ticker)
        if not csv_path.exists():
            self.send_error(404, f"CSV not found: {csv_path}")
            return
        data = csv_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Source", str(csv_path.name))
        self.end_headers()
        self.wfile.write(data)

def main():
    server = ThreadingHTTPServer((HOST, PORT), TosLiveHandler)
    print(f"TOS live dashboard: http://{HOST}:{PORT}/outputs/tos_live_underlying.html")
    print(f"CSV endpoint:        http://{HOST}:{PORT}/api/tos-live-csv")
    print(f"near-ATM endpoint:   http://{HOST}:{PORT}/api/near-atm?expiration=YYYY-MM-DD")
    print(f"payoff endpoint:     http://{HOST}:{PORT}/api/payoff?expiration=YYYY-MM-DD&strike=1215")
    print(f"send-to-excel:       http://{HOST}:{PORT}/api/send-to-excel?expiration=YYYY-MM-DD&strike=1215")
    server.serve_forever()


if __name__ == "__main__":
    main()

