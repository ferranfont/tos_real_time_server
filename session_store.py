"""Per-session, per-contract intraday tick storage on disk.

Layout (one folder per ticker per day):

  data/live/{SYMBOL}_intraday_{YYYY-MM-DD}_tick_by_tick/
      _index.json                  # session metadata: atm, expiration, contracts
      _underlying_{SYMBOL}.csv      # underlying tape (timestamp + UNDERLYING_*)
      {CONTRACT}.csv                # one file per option contract (BID-only tape)

Only BID is stored as the option price (plus volume/greeks/IV). The underlying
block keeps LAST/BID/ASK/MARK/VOLUME. Shared by the collector (write) and the
publisher (read).
"""
import csv
import json
import re
from pathlib import Path

LIVE_DATA_DIR = Path(__file__).resolve().parent / "data" / "live"

# Option contract tape (no LAST/ASK/MARK/HIGH/LOW; BID only).
CONTRACT_FIELDS = ["BID", "VOLUME", "DELTA", "GAMMA", "THETA", "VEGA", "IMPL_VOL", "OPEN_INT"]
CONTRACT_HEADER = ["timestamp", *CONTRACT_FIELDS, "VOLUME_1M"]

# Underlying tape (kept full: it is a single line per tick).
UNDERLYING_FIELDS = ["LAST", "BID", "ASK", "MARK", "VOLUME"]
UNDERLYING_HEADER = ["timestamp", *[f"UNDERLYING_{f}" for f in UNDERLYING_FIELDS]]

_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def safe_token(value):
    return _TOKEN_RE.sub("_", str(value or "").upper()).strip("_") or "UNKNOWN"


def _day_text(day):
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def session_dir(symbol, day):
    return LIVE_DATA_DIR / f"{safe_token(symbol)}_intraday_{_day_text(day)}_tick_by_tick"


def index_path(symbol, day):
    return session_dir(symbol, day) / "_index.json"


def underlying_path(symbol, day):
    return session_dir(symbol, day) / f"_underlying_{safe_token(symbol)}.csv"


def contract_file_name(contract_symbol):
    return f"{safe_token(contract_symbol)}.csv"


def contract_path(symbol, day, contract_symbol):
    return session_dir(symbol, day) / contract_file_name(contract_symbol)


def ensure_header(path, header):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)


def append_row(path, header, row):
    ensure_header(path, header)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=header, extrasaction="ignore").writerow(row)


def write_index(symbol, day, payload):
    path = index_path(symbol, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_index(symbol, day):
    path = index_path(symbol, day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_session_days(symbol):
    """Dates (YYYY-MM-DD) that have a session folder for this ticker."""
    token = safe_token(symbol)
    days = set()
    if LIVE_DATA_DIR.exists():
        for d in LIVE_DATA_DIR.glob(f"{token}_intraday_*_tick_by_tick"):
            match = re.search(r"(\d{4}-\d{2}-\d{2})", d.name)
            if match and d.is_dir():
                days.add(match.group(1))
    return sorted(days)


def latest_session_day(symbol):
    days = list_session_days(symbol)
    return days[-1] if days else None


def read_series(path, field):
    """Return [(timestamp, raw_value)] for one column of a tape CSV."""
    if not path.exists():
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append((row.get("timestamp", ""), row.get(field, "")))
    return out
