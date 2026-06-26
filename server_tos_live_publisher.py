import csv
import json
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from get_near_ATM_strikes import build_payload, DEFAULT_LEVELS
from utils.black_scholes import bs_price, bs_greeks, DEFAULT_RATE

PROJECT_ROOT = Path(__file__).resolve().parent
TOS_CSV = PROJECT_ROOT / "data" / "registro_opcion_minuto_a_minuto.csv"
ACTIVE_SYMBOLS_FILE = PROJECT_ROOT / "RTD_live_excel" / "active_symbols.json"
HOST = "127.0.0.1"
PORT = 8898


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
    """Read the latest live UNDERLYING_BID from the collector CSV."""
    if not TOS_CSV.exists():
        raise ValueError(f"CSV live no existe: {TOS_CSV}")

    with open(TOS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, skipinitialspace=True))

    for row in reversed(rows):
        symbol = (row.get("underlying_symbol") or "").strip().upper()
        if ticker and symbol and symbol != ticker.upper():
            continue
        for key in ("UNDERLYING_BID", "UNDERLYING_LAST", "UNDERLYING_MARK"):
            price = parse_live_price(row.get(key), key)
            if price is not None and price > 0:
                return price

    raise ValueError(f"No hay UNDERLYING_BID valido en {TOS_CSV}")


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
    """Return latest normalized live quote for one option symbol from the collector CSV."""
    if not symbol or not TOS_CSV.exists():
        return None
    wanted = str(symbol).strip().upper()
    with open(TOS_CSV, newline="", encoding="utf-8") as f:
        for row in reversed(list(csv.DictReader(f, skipinitialspace=True))):
            if str(row.get("symbol") or "").strip().upper() != wanted:
                continue
            bid = parse_option_price(row.get("BID"), "BID")
            ask = parse_option_price(row.get("ASK"), "ASK")
            mark = parse_option_price(row.get("MARK"), "MARK")
            last = parse_option_price(row.get("LAST"), "LAST")
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                source = "tos_live_mid"
            elif mark is not None and mark > 0:
                mid = mark
                source = "tos_live_mark"
            elif last is not None and last > 0:
                mid = last
                source = "tos_live_last"
            else:
                return None
            return {
                "symbol": symbol,
                "timestamp": row.get("timestamp", ""),
                "bid": bid,
                "ask": ask,
                "mark": mark,
                "last": last,
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


class TosLiveHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tos-live-csv":
            self.serve_tos_csv()
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
            spot = float(spot_arg) if spot_arg not in (None, "") else latest_live_underlying_bid(ticker)
            payload, *_ = build_payload(
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
            spot = float(spot_arg) if spot_arg not in (None, "") else latest_live_underlying_bid(ticker)
            payload, *_ = build_payload(
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
        premium_source = "tos_live" if (call_quote or {}).get("source", "").startswith("tos_live") and (put_quote or {}).get("source", "").startswith("tos_live") else "yahoo_snapshot"

        mult = 100 * contracts
        credit = call_mid + put_mid
        dte_v = payload["dte"]
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
            spot = latest_live_underlying_bid(ticker)
            payload, *_ = build_payload(
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

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_tos_csv(self):
        if not TOS_CSV.exists():
            self.send_error(404, f"CSV not found: {TOS_CSV}")
            return
        data = TOS_CSV.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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