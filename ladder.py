"""Build and persist the ATM strike ladder(s) (active_symbols.json) for a session.

Multi-ticker: anchors every ticker (nearest expiration -> ATM -> `levels` strikes
each side -> call+put legs) and writes them all together, so the collector
subscribes the whole portfolio. Used by the START button and the auto-anchor.
"""
import json
from datetime import datetime
from pathlib import Path

from config import RECORD_LEVELS, TICKERS
from get_near_ATM_strikes import build_payload
from get_option_chain import fetch_and_save_nearest

ACTIVE_SYMBOLS_FILE = Path(__file__).resolve().parent / "RTD_live_excel" / "active_symbols.json"


def _build_ticker(ticker, levels=RECORD_LEVELS, refresh=True, spot=None):
    """Build one ticker's ladder. Returns (summary_dict, legs)."""
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
    summary = {
        "ticker": ticker, "expiration": payload["expiration"], "dte": payload["dte"],
        "atm": payload["atm"], "spot": payload["spot"], "levels": levels,
        "contracts": len(legs), "chain": chain_note,
    }
    return summary, legs


def anchor_all(tickers=None, levels=RECORD_LEVELS, refresh=True, mode="auto"):
    """Anchor every ticker and write the combined active_symbols.json."""
    tickers = tickers or TICKERS
    by_ticker = {}
    options = []
    errors = {}
    for tk in tickers:
        try:
            summary, legs = _build_ticker(tk, levels=levels, refresh=refresh)
            by_ticker[tk] = summary
            options.extend(legs)
        except Exception as exc:  # noqa: BLE001
            errors[tk] = str(exc)

    active = {
        "strategy": "ladder",
        "mode": mode,
        "levels": levels,
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": by_ticker,
        "options": options,  # flat list (each leg has underlying_symbol)
    }
    ACTIVE_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_SYMBOLS_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")
    return {
        "ok": True, "tickers": by_ticker, "errors": errors,
        "contracts": len(options), "path": str(ACTIVE_SYMBOLS_FILE),
    }


def anchor_ladder(ticker="MU", levels=RECORD_LEVELS, refresh=True, spot=None, mode="start"):
    """Single-ticker anchor (kept for compatibility). Writes only this ticker."""
    summary, legs = _build_ticker(ticker, levels=levels, refresh=refresh, spot=spot)
    active = {
        "strategy": "ladder", "mode": mode, "levels": levels,
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": {ticker: summary}, "options": legs,
    }
    ACTIVE_SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_SYMBOLS_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")
    return {"ok": True, **summary, "path": str(ACTIVE_SYMBOLS_FILE)}
