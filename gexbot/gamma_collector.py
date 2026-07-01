"""Poll the friend's gexbot server for gamma-by-strike and append it to a daily CSV.

Independent of the RTD recorder / dashboard: it only reads the friend's server
(:9765/{TICKER}/state/gamma_zero via X-API-Key) and writes to data/gamma/.

Run:
    python gexbot/gamma_collector.py --once       # one cycle (test)
    python gexbot/gamma_collector.py              # loop every 30s
    python gexbot/gamma_collector.py --seconds 60 # loop every 60s
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # so `gexbot` imports work when run as a script

from gexbot.api_client import fetch_gamma_zero, fetch_classic_zero, parse_greek_rows

GAMMA_DIR = PROJECT_ROOT / "data" / "gamma"

GAMMA_TICKERS = ["MU"]     # solo MU por ahora (lista para extender mas adelante)
POLL_SECONDS = 30

# Mismo formato que el proyecto origen + gamma_flip (zero gamma del endpoint classic/zero).
CSV_HEADER = [
    "timestamp", "spot",
    "major_positive", "major_negative", "major_long_gamma", "major_short_gamma", "gamma_flip",
    "strike", "w_oi", "w_vol",
    "gamma", "gamma_5m", "gamma_15m", "gamma_30m",
]


def _ts_local(epoch) -> str:
    """Unix epoch (segundos) -> 'YYYY-MM-DD HH:MM:SS' en hora local (Barcelona),

    para que cuadre con los timestamps del recorder. Si falla, usa la hora actual.
    """
    try:
        return datetime.fromtimestamp(int(float(epoch))).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _num(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return ""


def _csv_path(ticker: str, ts_text: str) -> Path:
    """data/gamma/<TICKER>_GAMM_by_strikes_<YYYY-MM-DD>.csv (dia = fecha del timestamp)."""
    return GAMMA_DIR / f"{ticker}_GAMM_by_strikes_{ts_text[:10]}.csv"


_last_ts: dict[str, str] = {}  # ultimo timestamp escrito por ticker (evita filas duplicadas)


def collect_once(ticker: str) -> int:
    """Un ciclo: fetch -> parse -> append. Devuelve nº de strikes con gamma escritos
    (0 si el snapshot no ha cambiado desde el ultimo, p.ej. con mercado cerrado)."""
    payload = fetch_gamma_zero(ticker)
    ts_text = _ts_local(payload.get("timestamp"))
    if _last_ts.get(ticker) == ts_text:
        return 0  # mismo snapshot -> no reescribir
    _last_ts[ticker] = ts_text
    spot = payload.get("spot")
    mp = payload.get("major_positive")
    mn = payload.get("major_negative")
    mlg = payload.get("major_long_gamma")
    msg = payload.get("major_short_gamma")

    # Gamma flip (zero gamma) desde el endpoint classic/zero. Si falla, se deja vacio.
    flip = None
    try:
        flip = fetch_classic_zero(ticker).get("zero_gamma")
    except Exception:  # noqa: BLE001
        flip = None

    # Solo strikes con gamma != 0 (descarta las alas lejanas a 0, mantiene el perfil).
    rows = [r for r in parse_greek_rows(payload) if r[3]]

    path = _csv_path(ticker, ts_text)
    GAMMA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(CSV_HEADER)
        for strike, w_oi, w_vol, gamma, g5, g15, g30 in rows:
            writer.writerow([
                ts_text, _num(spot), _num(mp), _num(mn), _num(mlg), _num(msg), _num(flip),
                _num(strike), _num(w_oi), _num(w_vol),
                _num(gamma), _num(g5), _num(g15), _num(g30),
            ])
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gamma-by-strike collector (gexbot -> data/gamma).")
    parser.add_argument("--once", action="store_true", help="Un solo ciclo y salir (test).")
    parser.add_argument("--seconds", type=int, default=POLL_SECONDS, help="Segundos entre ciclos.")
    args = parser.parse_args()

    print(f"Gamma collector -> {GAMMA_DIR}")
    print(f"Tickers: {', '.join(GAMMA_TICKERS)}   (cada {args.seconds}s)")
    while True:
        for ticker in GAMMA_TICKERS:
            try:
                n = collect_once(ticker)
                print(f"[{datetime.now():%H:%M:%S}] {ticker}: {n} strikes con gamma -> {_csv_path(ticker, datetime.now().strftime('%Y-%m-%d')).name}")
            except Exception as exc:  # noqa: BLE001 - registrar y seguir
                print(f"[{datetime.now():%H:%M:%S}] {ticker} ERROR: {type(exc).__name__} {str(exc)[:160]}")
        if args.once:
            break
        time.sleep(args.seconds)


if __name__ == "__main__":
    main()
