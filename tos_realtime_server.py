import json
import re
import time
from pathlib import Path
from datetime import date, datetime

import win32com.client as win32

import ladder
import session_manager
import session_store as store
from config import (
    TICKERS,
    SERVER_REQUEST_COOLDOWN_FREQUENCY,
    RECORD_ONLY_MARKET_HOURS,
    AUTO_ANCHOR_AT_OPEN,
    DEFAULT_LEVELS,
)


# =========================
# CONFIGURACION
# =========================

UNDERLYING_SYMBOL = TICKERS[0]     # de momento solo MU (config.TICKERS)

# Si no hay active_symbols.json, se usa esto como fallback.
DEFAULT_OPTION_SYMBOLS = [".MU260626P1195"]

OUTPUT_DIR = Path(__file__).resolve().parent / "RTD_live_excel"
EXCEL_FILE = OUTPUT_DIR / "tos_live_option.xlsx"
ACTIVE_SYMBOLS_FILE = OUTPUT_DIR / "active_symbols.json"  # lo escribe el boton SEND del dashboard

# BID es el unico precio de opcion que almacenamos. Cabeceras en session_store.
FIELDS = store.CONTRACT_FIELDS               # BID, VOLUME, DELTA, GAMMA, THETA, VEGA, IMPL_VOL, OPEN_INT
UNDERLYING_FIELDS = store.UNDERLYING_FIELDS  # LAST, BID, ASK, MARK, VOLUME (subyacente intacto)
OPTION_HEADER_ROW = 5
OPTION_FIRST_VALUE_ROW = 6
MAX_OPTION_ROWS = 80              # portfolio intradia: varias patas desde A6
UNDERLYING_HEADER_ROW = 1
UNDERLYING_VALUE_ROW = 2


def safe_symbol_token(symbol):
    token = "".join(ch if ch.isalnum() else "_" for ch in str(symbol or "").upper()).strip("_")
    return token or "UNKNOWN"


def option_underlying_symbol(symbol):
    match = re.match(r"^\.?([A-Z]+)\d{6}[CP]", str(symbol or "").strip().upper())
    return match.group(1) if match else None


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
            return legs[:MAX_OPTION_ROWS]
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


def get_or_create_excel(symbols):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    excel = win32.gencache.EnsureDispatch("Excel.Application")
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

    ws = wb.Worksheets(1)
    ws.Name = "LIVE"
    ws.Cells.Clear()

    # --- Subyacente (filas 1-2) ---
    ws.Cells(UNDERLYING_HEADER_ROW, 1).Value = "UNDERLYING_SYMBOL"
    ws.Cells(UNDERLYING_VALUE_ROW, 1).Value = UNDERLYING_SYMBOL
    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        ws.Cells(UNDERLYING_HEADER_ROW, col).Value = f"UNDERLYING_{field}"
        ws.Cells(UNDERLYING_VALUE_ROW, col).Formula = f'=RTD("tos.rtd",,"{field}","{UNDERLYING_SYMBOL}")'

    # --- Opciones (cabecera fila 5; valores dinamicos desde A6) ---
    # Las formulas referencian $A{row}, asi que basta cambiar la columna A para re-apuntar el RTD.
    ws.Cells(OPTION_HEADER_ROW, 1).Value = "SYMBOL"
    for col, field in enumerate(FIELDS, start=2):
        ws.Cells(OPTION_HEADER_ROW, col).Value = field

    apply_symbols(ws, symbols, build_formulas=True)

    wb.Save()
    return excel, wb, ws


def apply_symbols(ws, legs, build_formulas=False):
    """Escribe las patas desde A6 y reconstruye formulas RTD para cada simbolo activo."""
    last_col = len(FIELDS) + 1
    for i in range(MAX_OPTION_ROWS):
        row = OPTION_FIRST_VALUE_ROW + i
        if i < len(legs):
            symbol = legs[i].get("symbol", "")
            ws.Cells(row, 1).Value = symbol
            if build_formulas:
                for col, field in enumerate(FIELDS, start=2):
                    ws.Cells(row, col).Formula = f'=RTD("tos.rtd",,"{field}",$A${row})'
        else:
            ws.Range(ws.Cells(row, 1), ws.Cells(row, last_col)).ClearContents()


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
        "contracts": contracts,
    }


def read_underlying_row(ws):
    """One underlying tape row from the Excel block (row 2)."""
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        row[f"UNDERLYING_{field}"] = clean_value(ws.Cells(UNDERLYING_VALUE_ROW, col).Value)
    return row


def read_leg_row(ws, value_row, previous_volume):
    """One option-contract tape row (BID + greeks/IV) for the leg at value_row."""
    symbol = clean_value(ws.Cells(value_row, 1).Value)
    if not symbol:
        return None, None, previous_volume
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for col, field in enumerate(FIELDS, start=2):
        row[field] = clean_value(ws.Cells(value_row, col).Value)
    volume = safe_float(row.get("VOLUME"))
    row["VOLUME_1M"] = max(volume - previous_volume, 0) if (previous_volume is not None and volume is not None) else ""
    return symbol, row, volume


def main():
    symbol = UNDERLYING_SYMBOL
    day = date.today()
    print("Abre thinkorswim y deja la plataforma conectada.")
    print("Creando Excel RTD...")
    print(f"Excel live:  {EXCEL_FILE}")
    print(f"Sesion:      {store.session_dir(symbol, day)}")
    print(f"Simbolos:    {ACTIVE_SYMBOLS_FILE} (lo escribe el boton SEND)")

    legs = load_active_symbols()
    excel, wb, ws = get_or_create_excel(legs)
    store.write_index(symbol, day, build_session_index(symbol, day, legs))

    previous_volumes = {}
    last_signature = legs_signature(legs)
    recording = None  # None forces a status print on the first loop
    anchored_day = None  # auto-anchor the ladder once per session at the open
    print(f"Sesion:      {session_manager.status()}")
    print(f"\nMonitorizando {len(legs)} contratos (cada {SERVER_REQUEST_COOLDOWN_FREQUENCY}s):")
    for leg in legs:
        print(f"- {leg.get('leg_type')} {leg.get('strike')} | {leg.get('symbol')}")
    print("Pulsa CTRL + C para parar.\n")

    while True:
        try:
            # Cada dia es una carpeta de sesion nueva (sin arrastrar simbolos viejos).
            today = date.today()
            if today != day:
                day = today
                store.write_index(symbol, day, build_session_index(symbol, day, legs))
                previous_volumes = {}
                print(f"[SESION] nueva carpeta: {store.session_dir(symbol, day)}")

            # Aplica nuevos simbolos si el dashboard los cambio (SEND / Start).
            current = load_active_symbols()
            current_signature = legs_signature(current)
            if current_signature != last_signature:
                apply_symbols(ws, current, build_formulas=True)
                legs = current
                last_signature = current_signature
                previous_volumes = {}
                store.write_index(symbol, day, build_session_index(symbol, day, legs))
                print(f"[SEND] portfolio actualizado: {len(legs)} contratos")

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

            # Auto-anclar la escalera al abrir (una vez por dia): refresca cadena + ATM + strikes.
            if AUTO_ANCHOR_AT_OPEN and anchored_day != day:
                for tk in TICKERS:
                    try:
                        res = ladder.anchor_ladder(ticker=tk, levels=DEFAULT_LEVELS, refresh=True, mode="auto")
                        print(f"[AUTO-START] {tk}: {res['expiration']} ATM {res['atm']} | "
                              f"{res['contracts']} contratos ({res['chain']})")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[AUTO-START] error {tk}: {exc}")
                anchored_day = day
                # La proxima vuelta detecta el nuevo active_symbols.json y aplica la escalera.

            # Tape del subyacente (una linea por tick).
            store.append_row(store.underlying_path(symbol, day), store.UNDERLYING_HEADER, read_underlying_row(ws))

            # Tape por contrato.
            for i, leg in enumerate(legs):
                value_row = OPTION_FIRST_VALUE_ROW + i
                sym = leg.get("symbol")
                contract_sym, row, volume = read_leg_row(ws, value_row, previous_volumes.get(sym))
                previous_volumes[sym] = volume
                if contract_sym and row is not None:
                    und = leg.get("underlying_symbol") or option_underlying_symbol(contract_sym) or symbol
                    store.append_row(store.contract_path(und, day, contract_sym), store.CONTRACT_HEADER, row)

            time.sleep(SERVER_REQUEST_COOLDOWN_FREQUENCY)

        except KeyboardInterrupt:
            print("\nMonitorizacion detenida por el usuario.")
            wb.Save()
            break

        except Exception as e:
            print(f"Error temporal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

