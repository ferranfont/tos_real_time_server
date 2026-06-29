"""Build and persist the ATM strike ladder (active_symbols.json) for a session.

Shared by the dashboard START button (serve_start) and the collector's automatic
anchor at the market open: refresh the chain from Yahoo (nearest expiration),
find the ATM, take `levels` strikes each side, and write all legs to
active_symbols.json so the collector subscribes the whole ladder.
"""
import json
from datetime import datetime
from pathlib import Path

from config import DEFAULT_LEVELS
from get_near_ATM_strikes import build_payload
from get_option_chain import fetch_and_save_nearest

ACTIVE_SYMBOLS_FILE = Path(__file__).resolve().parent / "RTD_live_excel" / "active_symbols.json"


def anchor_ladder(ticker="MU", levels=DEFAULT_LEVELS, refresh=True, spot=None, mode="auto"):
    """Yahoo chain (nearest expiration) -> ATM -> `levels` strikes each side -> active_symbols.json.

    Returns a summary dict. If refresh and Yahoo fails, falls back to the saved chain.
    """
    chain_note = "cadena guardada"
    if refresh:
        try:
            _path, exp, dte = fetch_and_save_nearest(ticker)
            chain_note = f"Yahoo {exp} (DTE {dte})"
        except Exception as exc:  # noqa: BLE001 - sin red: usar la cadena guardada
            chain_note = f"Yahoo fallo ({exc}); cadena guardada"

    payload, *_ = build_payload(ticker=ticker, levels=levels, spot=spot)  # no dte -> nearest
    legs = []
    for s in payload["strikes"]:
        for typ, key in (("CALL", "call"), ("PUT", "put")):
            sym = s.get(key)
            if sym:
                legs.append({
                    "symbol": sym, "role": typ, "strike": s["strike"],
                    "expiration": payload["expiration"], "underlying_symbol": ticker,
                })

    active = {
        "underlying": ticker, "strategy": "ladder", "mode": mode,
        "expiration": payload["expiration"], "dte": payload["dte"], "atm": payload["atm"],
        "spot": payload["spot"], "levels": levels,
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "options": legs,
    }
    ACTIVE_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_SYMBOLS_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")
    return {
        "ok": True, "ticker": ticker, "expiration": payload["expiration"], "dte": payload["dte"],
        "atm": payload["atm"], "spot": payload["spot"], "levels": levels,
        "contracts": len(legs), "chain": chain_note, "path": str(ACTIVE_SYMBOLS_FILE),
    }
