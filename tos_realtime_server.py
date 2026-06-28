import csv
import json
import re
import time
from pathlib import Path
from datetime import datetime

import win32com.client as win32


# =========================
# CONFIGURACION
# =========================

UNDERLYING_SYMBOL = "MU"
INTERVAL_SECONDS = 60              # 60 = guarda una fila cada minuto

# Si no hay active_symbols.json, se usa esto como fallback.
DEFAULT_OPTION_SYMBOLS = [".MU260626P1195"]

OUTPUT_DIR = Path(__file__).resolve().parent / "RTD_live_excel"
DATA_DIR = Path(__file__).resolve().parent / "data"
LIVE_DATA_DIR = DATA_DIR / "live"
CSV_FILE_NAME = "registro_opcion_minuto_a_minuto.csv"
EXCEL_FILE = OUTPUT_DIR / "tos_live_option.xlsx"
ACTIVE_SYMBOLS_FILE = OUTPUT_DIR / "active_symbols.json"  # lo escribe el boton SEND del dashboard

FIELDS = [
    "LAST",
    "BID",
    "ASK",
    "MARK",
    "VOLUME",
    "DELTA",
    "GAMMA",
    "THETA",
    "VEGA",
    "IMPL_VOL",
    "OPEN_INT",
    "HIGH",
    "LOW",
]

UNDERLYING_FIELDS = ["LAST", "BID", "ASK", "MARK", "VOLUME"]
UNDERLYING_CSV_FIELDS = [f"UNDERLYING_{field}" for field in UNDERLYING_FIELDS]
OPTION_HEADER_ROW = 5
OPTION_FIRST_VALUE_ROW = 6
MAX_OPTION_ROWS = 80              # portfolio intradia: varias estrategias/patas desde A6
UNDERLYING_HEADER_ROW = 1
UNDERLYING_VALUE_ROW = 2
OPTION_METADATA_FIELDS = [
    "strategy_id",
    "strategy",
    "strategy_label",
    "expiration",
    "dte",
    "strike",
    "leg_type",
    "side",
    "qty",
    "leg_index",
]
CSV_HEADERS = [
    "timestamp",
    "symbol",
    *OPTION_METADATA_FIELDS,
    "underlying_symbol",
    *UNDERLYING_CSV_FIELDS,
    *FIELDS,
    "MID_MANUAL",
    "SPREAD",
    "VOLUME_1M",
]


def safe_symbol_token(symbol):
    token = "".join(ch if ch.isalnum() else "_" for ch in str(symbol or "").upper()).strip("_")
    return token or "UNKNOWN"


def option_underlying_symbol(symbol):
    match = re.match(r"^\.?([A-Z]+)\d{6}[CP]", str(symbol or "").strip().upper())
    return match.group(1) if match else None


def live_csv_file(symbol=UNDERLYING_SYMBOL, now=None):
    now = now or datetime.now()
    return LIVE_DATA_DIR / f"{safe_symbol_token(symbol)}_{now:%Y-%m-%d}_{CSV_FILE_NAME}"

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


def create_csv_if_needed(csv_file):
    csv_file.parent.mkdir(parents=True, exist_ok=True)

    if not csv_file.exists():
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        return

    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        if reader.fieldnames is None:
            rows = []
            existing_headers = []
        else:
            existing_headers = [header.strip() for header in reader.fieldnames]
            rows = [{key.strip(): value for key, value in row.items() if key is not None} for row in reader]

    if existing_headers == CSV_HEADERS:
        return

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_option_row(ws, value_row, leg, previous_volume):
    """Lee una fila de opcion y anade metadata de estrategia para filtrar el CSV."""
    symbol = clean_value(ws.Cells(value_row, 1).Value)
    if not symbol:
        return None, previous_volume

    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "underlying_symbol": leg.get("underlying_symbol") or option_underlying_symbol(symbol) or UNDERLYING_SYMBOL,
    }
    for field in OPTION_METADATA_FIELDS:
        row[field] = leg.get(field, "")
    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        row[f"UNDERLYING_{field}"] = clean_value(ws.Cells(UNDERLYING_VALUE_ROW, col).Value)
    for col, field in enumerate(FIELDS, start=2):
        row[field] = clean_value(ws.Cells(value_row, col).Value)

    bid = safe_float(row.get("BID"))
    ask = safe_float(row.get("ASK"))
    volume = safe_float(row.get("VOLUME"))

    if bid is not None and ask is not None:
        row["MID_MANUAL"] = round((bid + ask) / 2, 6)
        row["SPREAD"] = round(ask - bid, 6)
    else:
        row["MID_MANUAL"] = ""
        row["SPREAD"] = ""

    if previous_volume is not None and volume is not None:
        row["VOLUME_1M"] = max(volume - previous_volume, 0)
    else:
        row["VOLUME_1M"] = ""

    return row, volume


def append_to_csv(row, csv_file):
    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writerow(row)


def main():
    print("Abre thinkorswim y deja la plataforma conectada.")
    print("Creando Excel RTD...")
    print(f"Excel live: {EXCEL_FILE}")
    csv_file = live_csv_file()
    print(f"CSV salida: {csv_file}")
    print(f"Simbolos:   {ACTIVE_SYMBOLS_FILE} (lo escribe el boton SEND)")

    legs = load_active_symbols()
    excel, wb, ws = get_or_create_excel(legs)
    create_csv_if_needed(csv_file)
    csv_files_ready = {csv_file}

    previous_volumes = {}
    last_signature = legs_signature(legs)
    print(f"\nMonitorizando {len(legs)} patas:")
    for leg in legs:
        print(f"- {leg.get('strategy_label')} | {leg.get('leg_type')} {leg.get('strike')} | {leg.get('symbol')}")
    print("Pulsa CTRL + C para parar.\n")

    while True:
        try:
            next_csv_file = live_csv_file()
            if next_csv_file != csv_file:
                csv_file = next_csv_file
                create_csv_if_needed(csv_file)
                csv_files_ready = {csv_file}
                previous_volumes = {}
                print(f"[CSV] nuevo archivo diario: {csv_file}")

            # Aplica nuevos simbolos si el dashboard los cambio (SEND).
            current = load_active_symbols()
            current_signature = legs_signature(current)
            if current_signature != last_signature:
                apply_symbols(ws, current, build_formulas=True)
                legs = current
                last_signature = current_signature
                previous_volumes = {}
                print(f"[SEND] portfolio actualizado: {len(legs)} patas")
                for leg in legs:
                    print(f"- {leg.get('strategy_label')} | {leg.get('leg_type')} {leg.get('strike')} | {leg.get('symbol')}")

            for i, leg in enumerate(legs):
                value_row = OPTION_FIRST_VALUE_ROW + i
                key = leg_volume_key(leg)
                row, current_volume = read_option_row(ws, value_row, leg, previous_volumes.get(key))
                previous_volumes[key] = current_volume
                if row is not None:
                    row_csv_file = live_csv_file(row.get("underlying_symbol") or UNDERLYING_SYMBOL)
                    if row_csv_file not in csv_files_ready:
                        create_csv_if_needed(row_csv_file)
                        csv_files_ready.add(row_csv_file)
                        print(f"[CSV] archivo activo: {row_csv_file}")
                    append_to_csv(row, row_csv_file)
                    print(row)

            time.sleep(INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\nMonitorizacion detenida por el usuario.")
            wb.Save()
            break

        except Exception as e:
            print(f"Error temporal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

