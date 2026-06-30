"""
Descarga el logo de cada ticker de config.TICKERS y lo guarda en data/logos/.

Adaptado de D:\\PYTHON\\ALGOS\\GEX_momentum_marc\\utils\\download_logos.py.
Aqui no hay CSV con dominios: se usa el mapa SYMBOL_DOMAIN de abajo. Por cada
dominio prueba varias fuentes SIN API key, en orden:
    1) logo.dev (solo si hay token)  -> logo de marca cuadrado de alta calidad
    2) Google favicon (256 px)
    3) DuckDuckGo favicon
Guarda el primero valido en data/logos/<symbol>.<ext>.

Reanudable: si ya existe un logo para ese symbol, no lo vuelve a descargar
(usa --overwrite para forzar).

Requisitos:
    python -m pip install requests

Uso desde la raiz del proyecto:
    python utils/download_logos.py
    python utils/download_logos.py --overwrite      # re-descarga todo
"""

from pathlib import Path
import argparse
import os
import sys

import requests

# Permite importar config.py desde la raiz del proyecto.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import TICKERS  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
LOGOS_DIR = DATA_DIR / "logos"

# Dominio de marca por simbolo. SPX/NDX son indices: usamos la marca del proveedor.
SYMBOL_DOMAIN = {
    "SPX": "spglobal.com",
    "NDX": "nasdaq.com",
    "MU": "micron.com",
    "SNDK": "sandisk.com",
    "WDC": "westerndigital.com",
    "STX": "seagate.com",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (logo-fetcher)"}
TIMEOUT = 10  # segundos por peticion

# Extension de fichero segun el content-type devuelto.
CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


def logo_sources(domain: str, logodev_token: str = ""):
    """URLs candidatas para un dominio, de mayor a menor calidad."""
    urls = []
    if logodev_token:
        urls.append(
            f"https://img.logo.dev/{domain}"
            f"?token={logodev_token}&size=256&format=png&retina=true"
        )
    urls += [
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
    ]
    return urls


def existing_logo(symbol: str):
    """Devuelve el fichero de logo ya descargado para ese symbol, si existe."""
    for f in LOGOS_DIR.glob(f"{symbol}.*"):
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


def download_one(session: requests.Session, symbol: str, domain: str,
                 overwrite: bool, logodev_token: str = ""):
    """Descarga el logo de un ticker. Devuelve (symbol, ruta_relativa | None)."""
    if not overwrite:
        found = existing_logo(symbol)
        if found:
            return symbol, f"data/logos/{found.name}"

    for url in logo_sources(domain, logodev_token):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        # Solo aceptamos imagenes reales (descarta paginas de error HTML).
        if r.status_code != 200 or not ctype.startswith("image") or len(r.content) < 100:
            continue
        ext = CONTENT_TYPE_EXT.get(ctype, "png")
        dest = LOGOS_DIR / f"{symbol}.{ext}"
        # Limpia versiones previas con otra extension.
        for old in LOGOS_DIR.glob(f"{symbol}.*"):
            if old != dest:
                old.unlink(missing_ok=True)
        dest.write_bytes(r.content)
        return symbol, f"data/logos/{dest.name}"

    return symbol, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga logos de los tickers de config.TICKERS.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-descarga aunque ya exista el logo.")
    parser.add_argument("--logodev-token", default=os.environ.get("LOGODEV_TOKEN", ""),
                        help="Token de logo.dev para logos de marca de alta calidad "
                             "(o variable de entorno LOGODEV_TOKEN).")
    args = parser.parse_args()

    LOGOS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Tickers: {', '.join(TICKERS)}")
    print(f"Guardando logos en: {LOGOS_DIR}")

    results = {}
    with requests.Session() as session:
        for sym in TICKERS:
            sym = str(sym).strip().upper()
            domain = SYMBOL_DOMAIN.get(sym)
            if not domain:
                print(f"  {sym:6s} -> sin dominio en SYMBOL_DOMAIN, omitido")
                results[sym] = None
                continue
            _, rel = download_one(session, sym, domain, args.overwrite,
                                  args.logodev_token)
            results[sym] = rel
            print(f"  {sym:6s} -> {rel or 'FALLIDO'}")

    ok = sum(1 for v in results.values() if v)
    fail = len(results) - ok
    print(f"\nDescargados: {ok}   Fallidos: {fail}")


if __name__ == "__main__":
    main()
