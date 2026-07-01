import json
import re
import time
from pathlib import Path
from datetime import date, datetime

import win32com.client as win32
import pywintypes

import ladder
import normalize
import session_manager
import session_store as store
from symbol_map import underlying_symbol_from_option_root
from config import (
    TICKERS,
    SERVER_REQUEST_COOLDOWN_FREQUENCY,
    RECORD_ONLY_MARKET_HOURS,
    AUTO_ANCHOR_AT_OPEN,
    RECORD_LEVELS,
)


# =========================
# CONFIGURACION
# =========================

UNDERLYING_SYMBOL = TICKERS[0]

# Si no hay active_symbols.json, se usa este contrato historico como fallback.
DEFAULT_OPTION_SYMBOLS = [".MU260626P1195"]

OUTPUT_DIR = Path(__file__).resolve().parent / "RTD_live_excel"
EXCEL_FILE = OUTPUT_DIR / "tos_live_option.xlsx"
ACTIVE_SYMBOLS_FILE = OUTPUT_DIR / "active_symbols.json"  # lo escribe el boton SEND del dashboard

# BID es el unico precio de opcion que almacenamos. Cabeceras en session_store.
FIELDS = store.CONTRACT_FIELDS               # BID, VOLUME, DELTA, GAMMA, THETA, VEGA, IMPL_VOL, OPEN_INT
UNDERLYING_FIELDS = store.UNDERLYING_FIELDS  # LAST, BID, ASK, MARK, VOLUME (subyacente intacto)
OPTION_HEADER_ROW = 5
OPTION_FIRST_VALUE_ROW = 6
MAX_OPTION_ROWS = 140             # per-ticker option rows from A6 (RECORD_LEVELS=30 -> 61 strikes -> 122 legs)
UNDERLYING_HEADER_ROW = 1
UNDERLYING_VALUE_ROW = 2

# Excel rechaza llamadas COM mientras esta ocupado (procesando UI o un tick RTD).
# Es transitorio: reintentamos en vez de dejar que "La llamada fue rechazada por el
# destinatario" (RPC_E_CALL_REJECTED) mate el recorder, sobre todo en el arranque.
_COM_BUSY_HRESULTS = {
    -2147418111,  # RPC_E_CALL_REJECTED  ("La llamada fue rechazada por el destinatario")
    -2147417846,  # RPC_E_SERVERCALL_RETRYLATER
}


def com_call(fn, *args, attempts=40, delay=0.25, **kwargs):
    """Ejecuta una llamada COM reintentando mientras Excel este momentaneamente ocupado."""
    last = None
    for _ in range(attempts):
        try:
            return fn(*args, **kwargs)
        except pywintypes.com_error as exc:
            if (exc.args[0] if exc.args else None) not in _COM_BUSY_HRESULTS:
                raise
            last = exc
            time.sleep(delay)
    if last is not None:
        raise last


def cell_set_value(ws, row, col, value):
    com_call(lambda: setattr(ws.Cells(row, col), "Value", value))


def cell_set_formula(ws, row, col, formula):
    com_call(lambda: setattr(ws.Cells(row, col), "Formula", formula))


def cell_get_value(ws, row, col):
    return com_call(lambda: ws.Cells(row, col).Value)


def safe_symbol_token(symbol):
    token = "".join(ch if ch.isalnum() else "_" for ch in str(symbol or "").upper()).strip("_")
    return token or "UNKNOWN"


def option_underlying_symbol(symbol):
    match = re.match(r"^\.?([A-Z]+)\d{6}[CP]", str(symbol or "").strip().upper())
    return underlying_symbol_from_option_root(match.group(1)) if match else None


def clean_value(value):
    """Limpia valores que vienen de Excel/RTD."""
    if value is None:
        return ""

    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in ["", "-", "N/A", "#N/A"]:
            return ""
        try:
            return float(value)
        except ValueError:
            return value

    return value


def safe_float(value):
    try:
        if value == "":
            return None
        return float(value)
    except Exception:
        return None


def normalize_leg(option, strategy=None, index=1):
    symbol = str(option.get("symbol") or "").strip()
    if not symbol:
        return None
    strategy = strategy or {}
    role = option.get("role") or option.get("type") or option.get("leg_type") or ""
    return {
        "symbol": symbol,
        "strategy_id": option.get("strategy_id") or strategy.get("id") or "manual",
        "strategy": option.get("strategy") or strategy.get("strategy") or "manual",
        "strategy_label": option.get("strategy_label") or strategy.get("label") or "Manual",
        "expiration": option.get("expiration") or strategy.get("expiration") or "",
        "dte": option.get("dte") if option.get("dte") not in (None, "") else strategy.get("dte", ""),
        "strike": option.get("strike") if option.get("strike") not in (None, "") else strategy.get("strike", ""),
        "leg_type": role,
        "side": option.get("side") or "SHORT",
        "qty": option.get("qty", -1),
        "leg_index": option.get("leg_index", index),
        "underlying_symbol": option.get("underlying_symbol")
        or strategy.get("underlying")
        or option_underlying_symbol(symbol)
        or UNDERLYING_SYMBOL,
    }


def fallback_legs():
    return [
        normalize_leg({"symbol": symbol, "leg_index": i}, {"id": "fallback", "strategy": "manual", "label": "Fallback"}, i)
        for i, symbol in enumerate(DEFAULT_OPTION_SYMBOLS, start=1)
    ]


def load_active_symbols():
    """Lee active_symbols.json y devuelve patas dinamicas para el portfolio RTD."""
    try:
        data = json.loads(ACTIVE_SYMBOLS_FILE.read_text(encoding="utf-8"))
        legs = []
        strategies = data.get("strategies") or []
        if isinstance(strategies, list) and strategies:
            for strat in strategies:
                for i, option in enumerate(strat.get("legs", []), start=1):
                    leg = normalize_leg(option, strat, i)
                    if leg:
                        legs.append(leg)
        else:
            strategy = {
                "id": data.get("strategy_id") or data.get("strategy") or "manual",
                "strategy": data.get("strategy") or "manual",
                "label": data.get("strategy_label") or data.get("strategy") or "Manual",
                "expiration": data.get("expiration") or "",
                "dte": data.get("dte", ""),
            }
            for i, option in enumerate(data.get("options", []), start=1):
                leg = normalize_leg(option, strategy, i)
                if leg:
                    legs.append(leg)
        if legs:
            return legs  # capped per-ticker in legs_by_ticker (combined list spans all tickers)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"active_symbols.json ilegible ({exc}); uso fallback.")
    return [leg for leg in fallback_legs() if leg]


def legs_signature(legs):
    return json.dumps([
        [leg.get("strategy_id"), leg.get("symbol"), leg.get("side"), leg.get("qty")]
        for leg in legs
    ], sort_keys=True)


def leg_volume_key(leg):
    return f"{leg.get('strategy_id', '')}|{leg.get('symbol', '')}|{leg.get('leg_index', '')}"


def save_workbook_as(excel, wb, path):
    previous_alerts = excel.DisplayAlerts
    excel.DisplayAlerts = False
    try:
        wb.SaveAs(str(path))
    finally:
        excel.DisplayAlerts = previous_alerts


def legs_by_ticker(legs):
    """Group legs by their underlying ticker (only tickers in config.TICKERS).

    Each ticker is capped to MAX_OPTION_ROWS so it fits its Excel sheet (one RTD
    row per leg from A6). With RECORD_LEVELS=30 that's 122 legs, well under the cap.
    """
    out = {t.upper(): [] for t in TICKERS}
    for leg in legs:
        und = (leg.get("underlying_symbol") or option_underlying_symbol(leg.get("symbol")) or "").upper()
        if und in out:
            out[und].append(leg)
    return {tk: legs_tk[:MAX_OPTION_ROWS] for tk, legs_tk in out.items()}


def setup_sheet(ws, ticker, legs):
    """Lay out one ticker's RTD sheet: underlying block + option ladder."""
    com_call(lambda: ws.Cells.Clear())
    cell_set_value(ws, UNDERLYING_HEADER_ROW, 1, "UNDERLYING_SYMBOL")
    cell_set_value(ws, UNDERLYING_VALUE_ROW, 1, ticker)
    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        cell_set_value(ws, UNDERLYING_HEADER_ROW, col, f"UNDERLYING_{field}")
        cell_set_formula(ws, UNDERLYING_VALUE_ROW, col, f'=RTD("tos.rtd",,"{field}","{ticker}")')
    cell_set_value(ws, OPTION_HEADER_ROW, 1, "SYMBOL")
    for col, field in enumerate(FIELDS, start=2):
        cell_set_value(ws, OPTION_HEADER_ROW, col, field)
    apply_symbols(ws, legs, build_formulas=True)


def get_or_create_sheet(wb, ticker):
    for sh in wb.Worksheets:
        if str(sh.Name).upper() == ticker.upper():
            return sh
    ws = wb.Worksheets.Add(After=wb.Worksheets(wb.Worksheets.Count))
    ws.Name = ticker
    return ws


def cleanup_sheets(excel, wb):
    """Drop leftover non-ticker sheets (old 'LIVE'/'Sheet1')."""
    keep = {t.upper() for t in TICKERS}
    to_delete = [sh for sh in wb.Worksheets if str(sh.Name).upper() not in keep]
    previous = excel.DisplayAlerts
    excel.DisplayAlerts = False
    try:
        for sh in to_delete:
            if wb.Worksheets.Count > 1:
                sh.Delete()
    finally:
        excel.DisplayAlerts = previous


def get_or_create_excel(legs_by_tk):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Late binding (Dispatch) avoids the gen_py/makepy step, so it also works when
    # launched from a non-interactive/background process.
    excel = win32.Dispatch("Excel.Application")
    excel.Visible = True

    try:
        excel.RTD.ThrottleInterval = 1000
    except Exception:
        pass

    target_path = str(EXCEL_FILE).lower()
    target_name = EXCEL_FILE.name.lower()
    open_same_name = None
    open_target = None

    for open_wb in excel.Workbooks:
        try:
            open_path = str(Path(open_wb.FullName)).lower()
        except Exception:
            open_path = ""
        try:
            open_name = str(open_wb.Name).lower()
        except Exception:
            open_name = ""

        if open_path == target_path:
            open_target = open_wb
            break
        if open_name == target_name:
            open_same_name = open_wb

    if open_target is not None:
        wb = open_target
    elif open_same_name is not None:
        wb = open_same_name
        save_workbook_as(excel, wb, EXCEL_FILE)
    elif EXCEL_FILE.exists():
        wb = excel.Workbooks.Open(str(EXCEL_FILE))
    else:
        wb = excel.Workbooks.Add()
        save_workbook_as(excel, wb, EXCEL_FILE)

    # Una hoja (pestana) por ticker, cada una con su subyacente + escalera.
    sheets = {}
    for ticker in TICKERS:
        ws = get_or_create_sheet(wb, ticker)
        setup_sheet(ws, ticker, legs_by_tk.get(ticker.upper(), []))
        sheets[ticker.upper()] = ws
    cleanup_sheets(excel, wb)

    com_call(wb.Save)
    return excel, wb, sheets


def apply_symbols(ws, legs, build_formulas=False):
    """Escribe las patas desde A6 y reconstruye formulas RTD para cada simbolo activo."""
    last_col = len(FIELDS) + 1
    for i in range(MAX_OPTION_ROWS):
        row = OPTION_FIRST_VALUE_ROW + i
        if i < len(legs):
            symbol = legs[i].get("symbol", "")
            cell_set_value(ws, row, 1, symbol)
            if build_formulas:
                for col, field in enumerate(FIELDS, start=2):
                    cell_set_formula(ws, row, col, f'=RTD("tos.rtd",,"{field}",$A${row})')
        else:
            com_call(lambda r=row: ws.Range(ws.Cells(r, 1), ws.Cells(r, last_col)).ClearContents())


def build_session_index(symbol, day, legs):
    """Session metadata written to _index.json (contracts + expiration)."""
    contracts = []
    expiration = ""
    for leg in legs:
        sym = leg.get("symbol")
        if not sym:
            continue
        expiration = expiration or (leg.get("expiration") or "")
        contracts.append({
            "symbol": sym,
            "type": (leg.get("leg_type") or "").upper(),
            "strike": leg.get("strike"),
            "expiration": leg.get("expiration") or "",
            "file": store.contract_file_name(sym),
        })
    return {
        "symbol": symbol,
        "date": store._day_text(day),
        "expiration": expiration,
        "underlying_file": store.underlying_path(symbol, day).name,
        "normalized": True,  # values stored in real units (see normalize.py)
        "contracts": contracts,
    }


def ensure_session_files(symbol, day, legs):
    """Create the session tape files with headers as soon as the session index exists."""
    store.ensure_header(store.underlying_path(symbol, day), store.UNDERLYING_HEADER)
    for leg in legs:
        sym = leg.get("symbol")
        if not sym:
            continue
        und = leg.get("underlying_symbol") or option_underlying_symbol(sym) or symbol
        store.ensure_header(store.contract_path(und, day, sym), store.CONTRACT_HEADER)

def normalize_option_field(field, raw):
    """Real-unit value for one option column (normalized at the source)."""
    if field == "BID":
        return normalize.option_price(raw)
    if field == "IMPL_VOL":
        return normalize.iv_decimal(raw)
    if field in ("DELTA", "GAMMA", "THETA", "VEGA"):
        return normalize.greek(field, raw)
    return normalize.count(raw)  # VOLUME, OPEN_INT


def read_underlying_row(ws):
    """One underlying tape row (real units) from the Excel block (row 2)."""
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        name = f"UNDERLYING_{field}"
        value = normalize.underlying_value(cell_get_value(ws, UNDERLYING_VALUE_ROW, col), name)
        row[name] = value if value is not None else ""
    return row


def read_leg_row(ws, value_row, previous_volume):
    """One option-contract tape row (real-unit BID + greeks/IV) for the leg at value_row."""
    symbol = clean_value(cell_get_value(ws, value_row, 1))
    if not symbol:
        return None, None, previous_volume
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for col, field in enumerate(FIELDS, start=2):
        value = normalize_option_field(field, cell_get_value(ws, value_row, col))
        row[field] = value if value is not None else ""
    volume = safe_float(row.get("VOLUME"))
    row["VOLUME_1M"] = max(volume - previous_volume, 0) if (previous_volume is not None and volume is not None) else ""
    return symbol, row, volume


def index_all(day, lbt):
    """Write the session index + tape headers for every ticker."""
    for ticker in TICKERS:
        tk = ticker.upper()
        tlegs = lbt.get(tk, [])
        store.write_index(tk, day, build_session_index(tk, day, tlegs))
        ensure_session_files(tk, day, tlegs)


def main():
    day = date.today()
    print("Abre thinkorswim y deja la plataforma conectada.")
    print("Creando Excel RTD (una pestana por ticker)...")
    print(f"Excel live:  {EXCEL_FILE}")
    print(f"Tickers:     {', '.join(TICKERS)}")
    print(f"Simbolos:    {ACTIVE_SYMBOLS_FILE} (lo escribe START / auto-anchor)")

    legs = load_active_symbols()
    lbt = legs_by_ticker(legs)
    excel, wb, sheets = get_or_create_excel(lbt)
    index_all(day, lbt)

    previous_volumes = {}
    last_signature = legs_signature(legs)
    recording = None  # None forces a status print on the first loop
    anchored_day = None  # auto-anchor the ladder once per session at the open
    print(f"Sesion:      {session_manager.status()}")
    print(f"Monitorizando {len(legs)} contratos en {len(TICKERS)} tickers "
          f"(cada {SERVER_REQUEST_COOLDOWN_FREQUENCY}s). CTRL+C para parar.\n")

    while True:
        try:
            today = date.today()
            if today != day:
                day = today
                index_all(day, lbt)
                previous_volumes = {}
                print(f"[SESION] nuevo dia: {day}")

            # Aplica nuevos simbolos si el dashboard los cambio (START / auto-anchor).
            current = load_active_symbols()
            current_signature = legs_signature(current)
            if current_signature != last_signature:
                legs = current
                lbt = legs_by_ticker(legs)
                for ticker in TICKERS:
                    apply_symbols(sheets[ticker.upper()], lbt.get(ticker.upper(), []), build_formulas=True)
                last_signature = current_signature
                previous_volumes = {}
                index_all(day, lbt)
                print(f"[PORTFOLIO] actualizado: {len(legs)} contratos")

            # Siempre abierto, pero graba solo en sesion (NYSE). Captura desde el primer tick.
            market_open = (not RECORD_ONLY_MARKET_HOURS) or session_manager.is_market_open()
            if not market_open:
                if recording is not False:
                    print(f"[SESION] {session_manager.status()} - en espera, no grabo")
                    recording = False
                time.sleep(SERVER_REQUEST_COOLDOWN_FREQUENCY)
                continue
            if not recording:
                print(f"[SESION] {session_manager.status()} - grabando")
                recording = True

            # Auto-anclar las escaleras de TODOS los tickers al abrir (una vez por dia).
            if AUTO_ANCHOR_AT_OPEN and anchored_day != day:
                try:
                    res = ladder.anchor_all(TICKERS, levels=RECORD_LEVELS, refresh=True, mode="auto")
                    for tk, s in (res.get("tickers") or {}).items():
                        print(f"[AUTO-START] {tk}: {s['expiration']} ATM {s['atm']} | {s['contracts']} ({s['chain']})")
                    for tk, err in (res.get("errors") or {}).items():
                        print(f"[AUTO-START] error {tk}: {err}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[AUTO-START] error: {exc}")
                anchored_day = day

            # Por ticker: tape del subyacente + tape por contrato.
            for ticker in TICKERS:
                tk = ticker.upper()
                ws = sheets[tk]
                store.append_row(store.underlying_path(tk, day), store.UNDERLYING_HEADER, read_underlying_row(ws))
                for i, leg in enumerate(lbt.get(tk, [])):
                    value_row = OPTION_FIRST_VALUE_ROW + i
                    sym = leg.get("symbol")
                    contract_sym, row, volume = read_leg_row(ws, value_row, previous_volumes.get(sym))
                    previous_volumes[sym] = volume
                    if contract_sym and row is not None:
                        store.append_row(store.contract_path(tk, day, contract_sym), store.CONTRACT_HEADER, row)

            time.sleep(SERVER_REQUEST_COOLDOWN_FREQUENCY)

        except KeyboardInterrupt:
            print("\nMonitorizacion detenida por el usuario.")
            com_call(wb.Save)
            break

        except Exception as e:
            print(f"Error temporal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

