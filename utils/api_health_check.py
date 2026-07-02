from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LIVE_DATA_DIR = DATA_DIR / "live"
GAMMA_DIR = DATA_DIR / "gamma"
GEXBOT_ENV = PROJECT_ROOT / "gexbot" / ".env"
CSV_FILE_NAME = "registro_opcion_minuto_a_minuto.csv"
HOST = "127.0.0.1"
PORT = 8898
GEXBOT_HOST = "135.148.46.22"
GEXBOT_STATE_PORT = 9765
GEXBOT_OI_PORT = 8765
DEFAULT_TIMEOUT = 4.0
FRESH_SECONDS = 180


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    latency_ms: int | None = None


def safe_symbol(symbol: str | None) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in str(symbol or "MU").upper()).strip("_")
    return token or "MU"


def today_text() -> str:
    return date.today().isoformat()


def live_csv_path(ticker: str, day: str | None = None) -> Path:
    return LIVE_DATA_DIR / f"{safe_symbol(ticker)}_{day or today_text()}_{CSV_FILE_NAME}"

def session_underlying_path(ticker: str, day: str | None = None) -> Path:
    token = safe_symbol(ticker)
    session_day = day or today_text()
    return LIVE_DATA_DIR / f"{token}_intraday_{session_day}_tick_by_tick" / f"_underlying_{token}.csv"


def gamma_csv_path(ticker: str, day: str | None = None) -> Path:
    return GAMMA_DIR / f"{safe_symbol(ticker)}_GAMM_by_strikes_{day or today_text()}.csv"


def read_env_key() -> str:
    if not GEXBOT_ENV.exists():
        return ""
    for line in GEXBOT_ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("GEXBOT_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def timed_http(url: str, timeout: float, headers: dict[str, str] | None = None) -> tuple[bool, int | None, str]:
    req = Request(url, headers=headers or {"Accept": "application/json"})
    start = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(700)
            elapsed = int((time.perf_counter() - start) * 1000)
            status = getattr(resp, "status", 200)
            if 200 <= status < 300:
                return True, elapsed, f"HTTP {status}, {len(body)} bytes sample"
            return False, elapsed, f"HTTP {status}"
    except HTTPError as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return False, elapsed, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        reason = getattr(exc, "reason", exc)
        return False, elapsed, f"{type(reason).__name__}: {str(reason)[:140]}"


def tcp_check(host: str, port: int, timeout: float) -> CheckResult:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = int((time.perf_counter() - start) * 1000)
            return CheckResult(f"tcp:{host}:{port}", True, "TCP open", elapsed)
    except OSError as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return CheckResult(f"tcp:{host}:{port}", False, f"TCP fail: {str(exc)[:140]}", elapsed)


def latest_csv_row(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    last: dict[str, str] | None = None
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if any((v or "").strip() for v in row.values()):
                last = row
    return last


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def check_tos_rtd_csv(ticker: str, day: str | None, fresh_seconds: int) -> CheckResult:
    session_path = session_underlying_path(ticker, day)
    legacy_path = live_csv_path(ticker, day)
    path = session_path if session_path.exists() else legacy_path
    row = latest_csv_row(path)
    if row is None:
        return CheckResult("tos_rtd_csv", False, f"sin CSV RTD: {session_path.name} / {legacy_path.name}")
    ts = parse_ts(row.get("timestamp"))
    if not ts:
        return CheckResult("tos_rtd_csv", False, f"CSV sin timestamp valido: {path.name}")
    age = int((datetime.now() - ts).total_seconds())
    bid = row.get("UNDERLYING_BID") or row.get("bid") or ""
    ok = age <= fresh_seconds if (day or today_text()) == today_text() else True
    detail = f"{path.name}, last={ts:%H:%M:%S}, age={age}s, bid={bid}"
    if not ok:
        detail = f"stale {detail}"
    return CheckResult("tos_rtd_csv", ok, detail)


def check_gamma_csv(ticker: str, day: str | None) -> CheckResult:
    path = gamma_csv_path(ticker, day)
    row = latest_csv_row(path)
    if row is None:
        return CheckResult("gamma_csv", False, f"sin CSV gamma: {path.name}")
    ts = row.get("timestamp") or ""
    strike = row.get("strike") or ""
    gamma = row.get("gamma") or ""
    return CheckResult("gamma_csv", True, f"{path.name}, last={ts}, strike={strike}, gamma={gamma}")


def check_local_endpoint(name: str, url: str, timeout: float) -> CheckResult:
    ok, elapsed, detail = timed_http(url, timeout)
    return CheckResult(name, ok, detail, elapsed)


def check_gexbot_endpoint(name: str, url: str, timeout: float) -> CheckResult:
    key = read_env_key()
    if not key:
        return CheckResult(name, False, f"GEXBOT_API_KEY vacio en {GEXBOT_ENV}")
    headers = {"Accept": "application/json", "X-API-Key": key, "User-Agent": "tos-realtime-health-check"}
    ok, elapsed, detail = timed_http(url, timeout, headers=headers)
    return CheckResult(name, ok, detail, elapsed)


def summarize(checks: list[CheckResult]) -> dict[str, Any]:
    total = len(checks)
    ok_count = sum(1 for c in checks if c.ok)
    if ok_count == total and total:
        status = "green"
    elif ok_count == 0:
        status = "red"
    else:
        status = "orange"
    return {
        "status": status,
        "ok": ok_count,
        "total": total,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "checks": [asdict(c) for c in checks],
    }


def run_checks(ticker: str = "MU", day: str | None = None, timeout: float = DEFAULT_TIMEOUT, fresh_seconds: int = FRESH_SECONDS) -> dict[str, Any]:
    ticker = safe_symbol(ticker)
    day = day or today_text()
    checks: list[CheckResult] = []
    checks.append(check_tos_rtd_csv(ticker, day, fresh_seconds))
    checks.append(check_local_endpoint("local_tos_csv_api", f"http://{HOST}:{PORT}/api/tos-live-csv?ticker={ticker}&date={day}&health=1", timeout))
    checks.append(check_local_endpoint("local_gamma_api", f"http://{HOST}:{PORT}/api/gamma?ticker={ticker}&date={day}&health=1", timeout))
    checks.append(check_gamma_csv(ticker, day))
    state_tcp = tcp_check(GEXBOT_HOST, GEXBOT_STATE_PORT, timeout)
    oi_tcp = tcp_check(GEXBOT_HOST, GEXBOT_OI_PORT, timeout)
    checks.append(state_tcp)
    checks.append(oi_tcp)
    if state_tcp.ok:
        checks.append(check_gexbot_endpoint("gexbot_gamma_zero", f"http://{GEXBOT_HOST}:{GEXBOT_STATE_PORT}/{ticker}/state/gamma_zero", timeout))
        checks.append(check_gexbot_endpoint("gexbot_classic_zero", f"http://{GEXBOT_HOST}:{GEXBOT_STATE_PORT}/{ticker}/classic/zero", timeout))
    else:
        checks.append(CheckResult("gexbot_gamma_zero", False, "omitido: TCP 9765 no responde"))
        checks.append(CheckResult("gexbot_classic_zero", False, "omitido: TCP 9765 no responde"))
    result = summarize(checks)
    result["ticker"] = ticker
    result["date"] = day
    return result


def print_report(result: dict[str, Any]) -> None:
    label = {"green": "OK", "orange": "PARTIAL", "red": "DOWN"}.get(result["status"], result["status"])
    print(f"API health {label} {result['ok']}/{result['total']}  ticker={result['ticker']} date={result['date']} updated={result['updated_at']}")
    for c in result["checks"]:
        mark = "OK" if c["ok"] else "FAIL"
        latency = f" {c['latency_ms']}ms" if c.get("latency_ms") is not None else ""
        print(f"[{mark}] {c['name']}{latency} - {c['detail']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test local TOS RTD/GEX APIs and friend's gexbot proxy.")
    parser.add_argument("--ticker", default="MU", help="Ticker/root to test, default MU.")
    parser.add_argument("--date", default=None, help="Session date YYYY-MM-DD, default today.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Seconds per HTTP/TCP check.")
    parser.add_argument("--fresh-seconds", type=int, default=FRESH_SECONDS, help="Max age for today's RTD CSV last tick.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of readable report.")
    args = parser.parse_args(argv)
    result = run_checks(args.ticker, args.date, args.timeout, args.fresh_seconds)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_report(result)
    return 0 if result["status"] == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
