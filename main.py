from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
HOST = "127.0.0.1"
PORT = 8898

COLLECTOR_SCRIPT = PROJECT_ROOT / "tos_realtime_server.py"
DASHBOARD_SCRIPT = PROJECT_ROOT / "server_tos_live_publisher.py"
COLLECTOR_PID_FILE = LOG_DIR / "tos_realtime_server.pid"
DASHBOARD_PID_FILE = LOG_DIR / "server_tos_live_publisher.pid"
SPOT_URL = f"http://{HOST}:{PORT}/outputs/tos_live_underlying.html"
STRAT_URL = f"{SPOT_URL}?strat=1"


def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def pid_is_running(pid: int, script: str | None = None) -> bool:
    """True only if `pid` is a live python process running `script`.

    Uses psutil (os.kill(pid, 0) is unreliable/unsafe on Windows and PIDs get
    reused, which made main wrongly think the collector was already running).
    """
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if script:
            cmdline = " ".join(proc.cmdline()).lower()
            if "python" not in (proc.name() or "").lower() and "python" not in cmdline:
                return False
            return script.lower() in cmdline
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


def pid_from_file(pid_file: Path, script: str | None = None) -> int | None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    if pid_is_running(pid, script):
        return pid
    try:
        pid_file.unlink()
    except OSError:
        pass
    return None


def write_pid_file(pid_file: Path, pid: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")


def remove_pid_file(pid_file: Path, pid: int | None = None) -> None:
    try:
        current = int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return
    if pid is not None and current != pid:
        return
    try:
        pid_file.unlink()
    except OSError:
        pass


def start_script(script: Path, log_name: str, pid_file: Path) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / log_name
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    log_file.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} {script.name} ---\n")
    proc = subprocess.Popen(
        [sys.executable, "-B", str(script)],
        cwd=PROJECT_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    write_pid_file(pid_file, proc.pid)
    return proc


def open_dashboards() -> None:
    webbrowser.open(SPOT_URL)
    time.sleep(0.4)
    webbrowser.open(STRAT_URL)


def cleanup_pid_files(started: list[tuple[str, subprocess.Popen]]) -> None:
    for name, proc in started:
        if name == "server_tos_live_publisher.py":
            remove_pid_file(DASHBOARD_PID_FILE, proc.pid)
        elif name == "tos_realtime_server.py":
            remove_pid_file(COLLECTOR_PID_FILE, proc.pid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch TOS RTD collector and live dashboards.")
    parser.add_argument("--no-collector", action="store_true", help="Do not start tos_realtime_server.py")
    parser.add_argument("--no-dashboard-server", action="store_true", help="Do not start server_tos_live_publisher.py")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser tabs")
    args = parser.parse_args()

    started: list[tuple[str, subprocess.Popen]] = []

    print("TOS realtime launcher")
    print(f"Proyecto: {PROJECT_ROOT}")
    print(f"Spot:     {SPOT_URL}")
    print(f"STRAT:    {STRAT_URL}")
    print("")

    if args.no_dashboard_server:
        print("Servidor dashboard: omitido por --no-dashboard-server")
    elif port_open(HOST, PORT):
        print(f"Servidor dashboard: ya hay algo escuchando en {HOST}:{PORT}; no arranco otro.")
    else:
        proc = start_script(DASHBOARD_SCRIPT, "server_tos_live_publisher.log", DASHBOARD_PID_FILE)
        started.append(("server_tos_live_publisher.py", proc))
        print(f"Servidor dashboard: arrancado PID {proc.pid} (logs/server_tos_live_publisher.log)")

    if not args.no_dashboard_server:
        if wait_for_port(HOST, PORT):
            print(f"Servidor dashboard: OK en {HOST}:{PORT}")
        else:
            print(f"Servidor dashboard: no responde todavia en {HOST}:{PORT}; revisa logs/server_tos_live_publisher.log")

    if args.no_collector:
        print("Colector RTD: omitido por --no-collector")
    else:
        existing_pid = pid_from_file(COLLECTOR_PID_FILE, COLLECTOR_SCRIPT.name)
        if existing_pid:
            print(f"Colector RTD: PID file activo {existing_pid}; no arranco otro.")
        else:
            proc = start_script(COLLECTOR_SCRIPT, "tos_realtime_server.log", COLLECTOR_PID_FILE)
            started.append(("tos_realtime_server.py", proc))
            print(f"Colector RTD: arrancado PID {proc.pid} (logs/tos_realtime_server.log)")

    if not args.no_browser and port_open(HOST, PORT):
        open_dashboards()
        print("Navegador: abiertas pestanas Spot y STRAT")
    elif args.no_browser:
        print("Navegador: omitido por --no-browser")
    else:
        print("Navegador: no abro pestanas porque el servidor aun no responde.")

    if not started:
        print("\nNo he arrancado procesos nuevos. Fin.")
        return 0

    print("\nProcesos lanzados desde este main:")
    for name, proc in started:
        print(f"- {name}: PID {proc.pid}")
    print("\nDeja esta ventana abierta. CTRL+C detiene los procesos lanzados por este main.")

    try:
        cleaned: set[int] = set()
        while True:
            for name, proc in started:
                if proc.poll() is not None and proc.pid not in cleaned:
                    if name == "server_tos_live_publisher.py":
                        remove_pid_file(DASHBOARD_PID_FILE, proc.pid)
                    elif name == "tos_realtime_server.py":
                        remove_pid_file(COLLECTOR_PID_FILE, proc.pid)
                    cleaned.add(proc.pid)
            alive = [(name, proc) for name, proc in started if proc.poll() is None]
            if not alive:
                print("Todos los procesos lanzados terminaron.")
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDeteniendo procesos lanzados por main...")
        for name, proc in started:
            if proc.poll() is None:
                print(f"- terminando {name} PID {proc.pid}")
                proc.terminate()
        time.sleep(1)
        for name, proc in started:
            if proc.poll() is None:
                print(f"- forzando {name} PID {proc.pid}")
                proc.kill()
        cleanup_pid_files(started)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
