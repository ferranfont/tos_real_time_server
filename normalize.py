"""Single source of truth for normalizing TOS/Excel RTD values to real units.

TOS RTD (via Excel, Spanish locale) delivers prices/greeks scaled (e.g. 43,15 ->
4315). Normalizing HERE, at the collector's write step, makes the CSV store real
values (43.15), so the CSV, the premium series, the dashboard and the frontend
are all coherent without any read-time de-scaling.
"""


def _to_float(value):
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if raw in {"-", "--", "N/A", "#N/A"}:
        return None
    raw = raw.replace(",", "")  # locale/thousands artifact left by Excel
    try:
        return float(raw)
    except ValueError:
        return None


def option_price(value):
    """Option price (BID): real dollars. TOS delivers it x100 (4315 -> 43.15)."""
    price = _to_float(value)
    if price is None:
        return None
    return price / 100.0 if abs(price) >= 10 else price


def underlying_value(value, field):
    """Underlying field, de-scaled by kind (LAST/BID/ASK/MARK/VOLUME)."""
    price = _to_float(value)
    if price is None:
        return None
    field = (field or "").upper()
    if field in {"UNDERLYING_BID", "UNDERLYING_ASK"}:
        while abs(price) >= 10000:
            price /= 100.0
    elif field == "UNDERLYING_LAST":
        price = price / 10000.0 if abs(price) >= 1_000_000 else (price / 100.0 if abs(price) >= 10000 else price)
    elif field == "UNDERLYING_MARK":
        price = price / 1000.0 if abs(price) >= 1_000_000 else (price / 100.0 if abs(price) >= 10000 else price)
    return price  # UNDERLYING_VOLUME stays a raw count


def greek(field, value):
    """DELTA/GAMMA/THETA/VEGA to real units (TOS sends some x100)."""
    number = _to_float(value)
    if number is None:
        return None
    field = (field or "").upper()
    if field in {"DELTA", "GAMMA"} and abs(number) > 1:
        return number / 100.0
    if field in {"THETA", "VEGA"} and abs(number) > 100:
        return number / 100.0
    return number


def iv_decimal(value):
    """Implied vol as a decimal (118.18% -> 1.1818)."""
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


def count(value):
    """Volume / open interest as a plain number."""
    return _to_float(value)
