import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from get_near_ATM_strikes import build_payload, DEFAULT_LEVELS
from utils.black_scholes import bs_price, bs_greeks, DEFAULT_RATE

PROJECT_ROOT = Path(__file__).resolve().parent
TOS_CSV = PROJECT_ROOT / "data" / "registro_opcion_minuto_a_minuto.csv"
HOST = "127.0.0.1"
PORT = 8898


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
            spot = first("spot")
            payload, *_ = build_payload(
                ticker=first("ticker") or "MU",
                expiration=expiration,
                dte=int(dte) if dte not in (None, "") else None,
                levels=int(levels) if levels not in (None, "") else DEFAULT_LEVELS,
                spot=float(spot) if spot not in (None, "") else None,
            )
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any compute error to the client
            self.send_error(500, str(exc))
            return

        self._send_json(payload)

    def serve_payoff(self, query):
        """Short Straddle payoff: expiration (kinked) + 'today' (Black-Scholes) curves."""
        def first(key):
            vals = query.get(key)
            return vals[0] if vals else None

        try:
            expiration = first("expiration")
            dte = first("dte")
            strike = float(first("strike"))
            contracts = int(first("contracts") or 1)
            rate = float(first("r")) if first("r") not in (None, "") else DEFAULT_RATE
            payload, *_ = build_payload(
                ticker=first("ticker") or "MU",
                expiration=expiration,
                dte=int(dte) if dte not in (None, "") else None,
            )
        except ValueError as exc:
            self.send_error(404, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))
            return

        entry = next((s for s in payload["strikes"] if float(s["strike"]) == strike), None)
        if entry is None:
            self._send_json({"error": f"strike {strike:g} not in chain for {payload['expiration']}"})
            return

        call_mid, put_mid = entry["call_mid"], entry["put_mid"]
        if call_mid is None or put_mid is None:
            missing = "call" if call_mid is None else "put"
            self._send_json({"error": f"Sin prima en Yahoo para la {missing} de {strike:g}."})
            return

        K = strike
        mult = 100 * contracts
        credit = call_mid + put_mid
        dte_v = payload["dte"]
        T = max(dte_v, 0) / 365.0
        today_T = max(dte_v, 1) / 365.0
        greeks_T = today_T
        call_iv = entry["call_iv"] or 0.0
        put_iv = entry["put_iv"] or 0.0

        half = max(credit * 1.3, K * 0.05)
        n = 121
        xs = [K - half + i * (2 * half) / (n - 1) for i in range(n)]
        expiry = [(credit - abs(S - K)) * mult for S in xs]
        today = [
            (credit - (bs_price(S, K, today_T, rate, call_iv, "C") + bs_price(S, K, today_T, rate, put_iv, "P"))) * mult
            for S in xs
        ]

        spot = float(payload["spot"])
        call_g = bs_greeks(spot, K, greeks_T, rate, call_iv, "C")
        put_g = bs_greeks(spot, K, greeks_T, rate, put_iv, "P")

        def scale_short(g):
            return {k: round(-v * mult, 4) for k, v in g.items()}

        short_call_g = scale_short(call_g)
        short_put_g = scale_short(put_g)
        net_g = {k: round(short_call_g[k] + short_put_g[k], 4) for k in short_call_g}

        self._send_json({
            "strategy": "short_straddle",
            "symbol": payload["symbol"],
            "expiration": payload["expiration"],
            "dte": dte_v,
            "strike": K,
            "contracts": contracts,
            "call_mid": call_mid,
            "put_mid": put_mid,
            "call_iv": call_iv,
            "put_iv": put_iv,
            "avg_iv": round((call_iv + put_iv) / 2.0, 4),
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
            "be_low": round(K - credit, 4),
            "be_high": round(K + credit, 4),
            "r": rate,
            "t_years": round(T, 5),
            "x": [round(v, 3) for v in xs],
            "expiry": [round(v, 2) for v in expiry],
            "today": [round(v, 2) for v in today],
        })

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
    server.serve_forever()


if __name__ == "__main__":
    main()