"""Cliente HTTP del servidor proxy del amigo (no gexbot.com directamente).

El servidor en 135.148.46.22 expone DOS puertos:

  :9765  -> Endpoints estilo gexbot (gamma, delta, vanna, charm por strike).
           Ej: GET /NDX/state/gamma_zero
  :8765  -> Endpoints estilo optioncharts (gex, dex, price, oi, history-*).
           Ya consumidos por option_charts_endpoints/api_client.py.

Auth comun para ambos: header `X-API-Key`. La misma key que ya tenias en
option_charts_endpoints/.env (API_KEY). Aqui se llama GEXBOT_API_KEY pero
el valor es el mismo. El acceso requiere ademas tu IP en whitelist en el
servidor (sin VPN).

Migracion desde cookie privada de gexbot.com:

  Antes                                                Ahora
  -----                                                -----
  POST app.gexbot.com/chart/NQ_NDX/state/gamma_zero    GET :9765/NDX/state/gamma_zero
  POST app.gexbot.com/chart/NQ_NDX/state/volume_zero   GET :8765/oi/NDX/strikes?exp=...
  Cookie: __Secure-NFA=... (caduca cada X horas)       X-API-Key (estable)

`fetch_gamma_zero` mantiene su payload identico al viejo (mismo
mini_contracts de 7 elementos, mismos top-level major_*), asi que
parse_gamma_rows y compute_levels no cambian.

`fetch_volume_zero` cambia a un endpoint con estructura distinta
(strikes con dicts {strike, call_vol, put_vol, ...}); parse_volume_rows
parsea el nuevo formato directamente.

La key vive en gexbot_endpoints/.env (variable GEXBOT_API_KEY). El cliente la
relee en CADA llamada para que rotarla no requiera reiniciar main.py.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values

ENV_PATH = Path(__file__).with_name(".env")
BASE_STATE_URL = "http://135.148.46.22:9765"   # estilo gexbot
BASE_OI_URL = "http://135.148.46.22:8765"      # estilo optioncharts (oi/strikes para volumen)
TIMEOUT_SECONDS = 30


class GexbotAuthError(RuntimeError):
    """401 desde el servidor. La API key esta invalida o ha sido revocada."""


# Alias para no romper imports antiguos (codigo de main.py captura este nombre).
CookieExpiredError = GexbotAuthError


def _load_api_key() -> str:
    """Relee .env desde disco. Devuelve la API key (sin validar nada)."""
    if not ENV_PATH.exists():
        return ""
    env = dotenv_values(ENV_PATH)
    return (env.get("GEXBOT_API_KEY") or "").strip()


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-API-Key": api_key,
        "User-Agent": "option-charts-scrap/api",
    }


def _pair_to_ticker(ticker_pair: str) -> str:
    """NQ_NDX -> NDX, ES_SPX -> SPX. Si ya es un ticker simple lo devuelve igual."""
    if "_" in ticker_pair:
        return ticker_pair.split("_", 1)[1]
    return ticker_pair


def _get_json(url: str, ticker_label: str) -> dict[str, Any]:
    """GET con auth X-API-Key. Maneja 401 con mensaje claro."""
    api_key = _load_api_key()
    if not api_key:
        raise RuntimeError(
            f"GEXBOT_API_KEY vacio en {ENV_PATH}. "
            "Ponla en una linea como  GEXBOT_API_KEY=...  (32 hex chars)."
        )
    session = requests.Session()
    session.trust_env = False
    resp = session.get(url, headers=_headers(api_key), timeout=TIMEOUT_SECONDS)
    if resp.status_code == 401:
        raise GexbotAuthError(
            f"401 en {ticker_label}. API key invalida o revocada -> revisa "
            f"GEXBOT_API_KEY en {ENV_PATH}."
        )
    resp.raise_for_status()
    return resp.json()


def _fetch_greek_zero(greek: str, ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/{greek}_zero. Generico para gamma/vanna/charm/delta.

    Payload identico entre los 4 greeks: top-level
    {spot, ticker, timestamp, major_positive, major_negative,
    major_long_gamma, major_short_gamma, mini_contracts}.

    `mini_contracts` es lista de tuplas
    [strike, w_oi, w_vol, value, [hist_5m, hist_15m, hist_30m], 0, None]
    donde `value` es el greek instantaneo (gamma/vanna/charm/delta) segun el
    endpoint que se llame.
    """
    ticker = _pair_to_ticker(ticker_pair)
    url = f"{BASE_STATE_URL}/{ticker}/state/{greek}_zero"
    return _get_json(url, ticker)


def fetch_gamma_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/gamma_zero."""
    return _fetch_greek_zero("gamma", ticker_pair)


def fetch_classic_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/classic/zero.

    Fuente oficial de Gexbot Classic para:
      - zero_gamma      -> gamma flip
      - major_pos_oi    -> call wall OI
      - major_neg_oi    -> put wall OI

    El payload tambien puede traer aliases compactos en /majors
    (mpos_oi/mneg_oi), asi que el caller debe aceptar ambos nombres.
    """
    ticker = _pair_to_ticker(ticker_pair)
    url = f"{BASE_STATE_URL}/{ticker}/classic/zero"
    return _get_json(url, ticker)


def fetch_vanna_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/vanna_zero."""
    return _fetch_greek_zero("vanna", ticker_pair)


def fetch_charm_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/charm_zero."""
    return _fetch_greek_zero("charm", ticker_pair)


def fetch_delta_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/delta_zero."""
    return _fetch_greek_zero("delta", ticker_pair)


def fetch_gex_zero(ticker_pair: str) -> dict[str, Any]:
    """GET :9765/{TICKER}/state/gex_zero.

    Es el GEX profile del state view de gexbot (la barra naranja en
    state sin filtros). Estructura distinta a los _zero greeks:

        {timestamp, ticker, spot, min_dte, sec_min_dte, zero_gamma,
         major_pos_vol, major_pos_oi, major_neg_vol, major_neg_oi,
         strikes: [[strike, value, 0, [hist5, hist4, hist3, hist2, hist1]], ...],
         sum_gex_vol, sum_gex_oi, delta_risk_reversal, max_priors}

    NOTA el campo `strikes` (no `mini_contracts`): es una lista de 4-tuplas.
    """
    ticker = _pair_to_ticker(ticker_pair)
    url = f"{BASE_STATE_URL}/{ticker}/state/gex_zero"
    return _get_json(url, ticker)


def parse_gex_rows(payload: dict[str, Any]) -> list[tuple[float, float]]:
    """Convierte payload de /state/gex_zero en filas (strike, gex_value).

    Cada fila del campo `strikes` es [strike, value, 0, hist[5]]. Solo
    extraemos strike y value (la columna 2). El campo de ceros y el array
    history no se persisten en CSV.
    """
    out: list[tuple[float, float]] = []
    for row in payload.get("strikes", []):
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            strike = float(row[0])
            value = float(row[1])
        except (TypeError, ValueError):
            continue
        out.append((strike, value))
    out.sort(key=lambda r: r[0])
    return out


def fetch_volume_zero(ticker_pair: str, exp: str | None = None) -> dict[str, Any]:
    """GET :8765/oi/{TICKER}/strikes?exp=YYYY-MM-DD.

    Devuelve {ticker, expiration_date_id, strikes: [{strike, call_oi, put_oi,
    call_vol, put_vol, ...}, ...]}. Es la nueva fuente para volumen call/put
    por strike (el viejo volume_zero ya no existe en este servidor).

    Si `exp` no se pasa, usa la fecha de hoy en formato ISO.
    """
    ticker = _pair_to_ticker(ticker_pair)
    if exp is None:
        exp = date.today().isoformat()
    url = f"{BASE_OI_URL}/oi/{ticker}/strikes?exp={exp}"
    return _get_json(url, ticker)


def parse_volume_rows(payload: dict[str, Any]) -> list[tuple[float, float, float, float, float]]:
    """Convierte payload de /oi/{ticker}/strikes en (strike, call_vol, put_vol, call_oi, put_oi).

    Estructura nueva (puerto :8765):
        {"strikes": [
            {"strike": 7325.0, "call_oi": ..., "put_oi": ...,
             "call_vol": 12, "put_vol": 3, "total_vol": 15, ...},
            ...
        ]}

    Antes (cookie viejo, puerto :443 app.gexbot.com) traia mini_contracts con
    tuplas; ahora es lista de dicts. Mantenemos el orden ascendente por strike.
    """
    out: list[tuple[float, float, float, float, float]] = []
    for row in payload.get("strikes", []):
        if not isinstance(row, dict):
            continue
        try:
            strike = float(row.get("strike"))
            call_vol = float(row.get("call_vol") or 0)
            put_vol = float(row.get("put_vol") or 0)
            call_oi = float(row.get("call_oi") or 0)
            put_oi = float(row.get("put_oi") or 0)
        except (TypeError, ValueError):
            continue
        out.append((strike, call_vol, put_vol, call_oi, put_oi))
    out.sort(key=lambda r: r[0])
    return out


def parse_classic_rows(payload: dict[str, Any]) -> list[tuple[float, float, float]]:
    """Convierte /classic/zero.strikes en (strike, gex_oi, gex_vol).

    Estructura documentada:
        [strike, gex_oi, gex_vol, [hist_1m, hist_5m, hist_10m, hist_15m, hist_30m]]

    Para reproducir Gexbot Classic con Open Interest usamos `gex_oi`.
    """
    out: list[tuple[float, float, float]] = []
    for row in payload.get("strikes", []):
        if not isinstance(row, list) or len(row) < 3:
            continue
        try:
            strike = float(row[0])
            gex_oi = float(row[1] or 0)
            gex_vol = float(row[2] or 0)
        except (TypeError, ValueError):
            continue
        out.append((strike, gex_oi, gex_vol))
    out.sort(key=lambda r: r[0])
    return out


def compute_levels(
    strike_rows: list[tuple[float, float, float, float, float | None, float | None, float | None]],
    spot: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Calcula los 3 niveles canonicos del libro de gamma para replicar
    la UI de gexbot (seccion VOLUME).

    Importante: gexbot llama "major positive" a la pared SUPERIOR (call wall,
    encima del spot) y "major negative" a la pared INFERIOR (put wall, debajo
    del spot). Internamente la gamma del dealer es NEGATIVA donde los calls
    estan vendidos (arriba) y POSITIVA donde los puts estan vendidos (abajo),
    asi que:

        call_wall  (GREEN, sobre spot)  = strike con min(gamma) [mas negativa]
                                          entre los strikes >= spot.
        put_wall   (RED, debajo spot)   = strike con max(gamma) [mas positiva]
                                          entre los strikes <= spot.
        zero_gamma (YELLOW, gamma flip) = strike interpolado donde la gamma
                                          cruza cero subiendo (negativa->positiva
                                          no aparece en datos reales, asi que
                                          probamos ambos signos).

    Usa el campo `gamma` (campo 3 de strike_rows) que es la exposicion
    instantanea, en lugar de w_vol (volumen acumulado) porque w_vol esta
    cuantizado a contratos y deja huecos.
    """
    if not strike_rows or spot is None:
        return None, None, None

    # call_wall: min(gamma) entre strikes >= spot.
    above = [(s, g) for s, _, _, g, *_ in strike_rows if g is not None and s >= spot]
    call_wall = min(above, key=lambda p: p[1])[0] if above else None
    # put_wall: max(gamma) entre strikes <= spot.
    below = [(s, g) for s, _, _, g, *_ in strike_rows if g is not None and s <= spot]
    put_wall = max(below, key=lambda p: p[1])[0] if below else None

    # zero_gamma: centro de masa del libro de gamma ponderado por |gamma|.
    # gexbot lo computa exactamente asi (verificado empiricamente: diff +2.65
    # vs el valor de la UI usando esta formula). Tiene sentido economico: el
    # "balance point" del libro, el strike donde la actividad gamma a ambos
    # lados queda equilibrada.
    pairs = [(s, g) for s, _, _, g, *_ in strike_rows if g is not None]
    weight_total = sum(abs(g) for _, g in pairs)
    if weight_total > 0:
        zero_gamma = sum(s * abs(g) for s, g in pairs) / weight_total
    else:
        zero_gamma = None
    return call_wall, put_wall, zero_gamma


def parse_greek_rows(
    payload: dict[str, Any],
) -> list[tuple[float, float, float, float, float | None, float | None, float | None]]:
    """Convierte mini_contracts en filas de greek (gamma/vanna/charm) por strike.

    Estructura cruda de cada fila (identica para los 4 greeks; el valor
    `value` cambia segun el endpoint usado: gamma_zero, vanna_zero, etc):
        [strike, w_oi, w_vol, value, [hist_5m, hist_15m, hist_30m], 0, null]

    Devuelve:
        (strike, w_oi, w_vol, value, value_5m, value_15m, value_30m)
    """
    out: list[tuple[float, float, float, float, float | None, float | None, float | None]] = []
    for row in payload.get("mini_contracts", []):
        if not isinstance(row, list) or len(row) < 5:
            continue
        history = row[4] if isinstance(row[4], list) else []
        try:
            strike = float(row[0])
            w_oi = float(row[1])
            w_vol = float(row[2])
            value = float(row[3])
            v_5m = float(history[0]) if len(history) > 0 and history[0] is not None else None
            v_15m = float(history[1]) if len(history) > 1 and history[1] is not None else None
            v_30m = float(history[2]) if len(history) > 2 and history[2] is not None else None
        except (TypeError, ValueError):
            continue
        out.append((strike, w_oi, w_vol, value, v_5m, v_15m, v_30m))
    out.sort(key=lambda r: r[0])
    return out


# Alias para no romper imports antiguos (get_netGEX.py y otros consumidores).
parse_gamma_rows = parse_greek_rows
