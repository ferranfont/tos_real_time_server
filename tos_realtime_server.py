import csv
import time
from pathlib import Path
from datetime import datetime

import win32com.client as win32


# =========================
# CONFIGURACION
# =========================

OPTION_SYMBOL = ".MU260626P1195"   # cambia esto por el simbolo exacto de tu opcion
UNDERLYING_SYMBOL = "MU"
INTERVAL_SECONDS = 60              # 60 = guarda una fila cada minuto

OUTPUT_DIR = Path(__file__).resolve().parent / "RTD_live_excel"
DATA_DIR = Path(__file__).resolve().parent / "data"
EXCEL_FILE = OUTPUT_DIR / "tos_live_option.xlsx"
CSV_FILE = DATA_DIR / "registro_opcion_minuto_a_minuto.csv"

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
OPTION_VALUE_ROW = 6
UNDERLYING_HEADER_ROW = 1
UNDERLYING_VALUE_ROW = 2
CSV_HEADERS = [
    "timestamp",
    "symbol",
    "underlying_symbol",
    *UNDERLYING_CSV_FIELDS,
    *FIELDS,
    "MID_MANUAL",
    "SPREAD",
    "VOLUME_1M",
]


def clean_value(value):
    """
    Limpia valores que vienen de Excel/RTD.
    """
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


def save_workbook_as(excel, wb, path):
    previous_alerts = excel.DisplayAlerts
    excel.DisplayAlerts = False
    try:
        wb.SaveAs(str(path))
    finally:
        excel.DisplayAlerts = previous_alerts


def get_or_create_excel():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    excel = win32.gencache.EnsureDispatch("Excel.Application")
    excel.Visible = True

    # Intenta acelerar el RTD
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

    # Subyacente en la parte superior
    ws.Cells(UNDERLYING_HEADER_ROW, 1).Value = "UNDERLYING_SYMBOL"
    ws.Cells(UNDERLYING_VALUE_ROW, 1).Value = UNDERLYING_SYMBOL

    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        ws.Cells(UNDERLYING_HEADER_ROW, col).Value = f"UNDERLYING_{field}"
        ws.Cells(UNDERLYING_VALUE_ROW, col).Formula = f'=RTD("tos.rtd",,"{field}","{UNDERLYING_SYMBOL}")'

    # Opcion debajo del subyacente
    ws.Cells(OPTION_HEADER_ROW, 1).Value = "SYMBOL"
    ws.Cells(OPTION_VALUE_ROW, 1).Value = OPTION_SYMBOL

    for col, field in enumerate(FIELDS, start=2):
        ws.Cells(OPTION_HEADER_ROW, col).Value = field
        ws.Cells(OPTION_VALUE_ROW, col).Formula = f'=RTD("tos.rtd",,"{field}","{OPTION_SYMBOL}")'

    wb.Save()
    return excel, wb, ws


def create_csv_if_needed():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        return

    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        if reader.fieldnames is None:
            rows = []
            existing_headers = []
        else:
            existing_headers = [header.strip() for header in reader.fieldnames]
            rows = [{key.strip(): value for key, value in row.items() if key is not None} for row in reader]

    if existing_headers == CSV_HEADERS:
        return

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_row_from_excel(ws, previous_volume):
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": OPTION_SYMBOL,
        "underlying_symbol": UNDERLYING_SYMBOL,
    }

    for col, field in enumerate(UNDERLYING_FIELDS, start=2):
        value = ws.Cells(UNDERLYING_VALUE_ROW, col).Value
        row[f"UNDERLYING_{field}"] = clean_value(value)

    for col, field in enumerate(FIELDS, start=2):
        value = ws.Cells(OPTION_VALUE_ROW, col).Value
        row[field] = clean_value(value)

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


def append_to_csv(row):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writerow(row)


def main():
    print("Abre thinkorswim y deja la plataforma conectada.")
    print("Creando Excel RTD...")
    print(f"Excel live: {EXCEL_FILE}")
    print(f"CSV salida: {CSV_FILE}")

    excel, wb, ws = get_or_create_excel()
    create_csv_if_needed()

    previous_volume = None

    print("\nMonitorizando opcion minuto a minuto.")
    print("Pulsa CTRL + C para parar.\n")

    while True:
        try:
            row, current_volume = read_row_from_excel(ws, previous_volume)
            append_to_csv(row)

            previous_volume = current_volume

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