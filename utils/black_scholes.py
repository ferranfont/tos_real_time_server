"""Black-Scholes option pricing helpers (no external deps, stdlib math only).

Used to draw the "today" (pre-expiration) payoff curve of an option strategy,
using the implied volatility that comes from the Yahoo option chain.
"""

from math import log, sqrt, exp, erf, pi

DEFAULT_RATE = 0.04  # annual risk-free rate assumption


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))

def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-Scholes price of a European call/put.

    S: spot, K: strike, T: years to expiry, r: rate, sigma: implied vol (decimal).
    Falls back to intrinsic value when T or sigma are non-positive.
    option_type: "C"/"CALL" or "P"/"PUT".
    """
    is_call = option_type.upper() in ("C", "CALL")
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)

    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def straddle_value(S: float, K: float, T: float, r: float,
                   call_sigma: float, put_sigma: float) -> float:
    """Combined price of a call + put at the same strike (a long straddle)."""
    call = bs_price(S, K, T, r, call_sigma, "C")
    put = bs_price(S, K, T, r, put_sigma, "P")
    return call + put


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> dict[str, float]:
    """Black-Scholes greeks for one option on one underlying share.

    Theta is returned per calendar day. Vega and rho are returned per 1 percentage-point
    move in volatility/rates, which is the practical display convention.
    """
    is_call = option_type.upper() in ("C", "CALL")
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if is_call:
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrt_t = sqrt(T)
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (S * sigma * sqrt_t)
    vega = S * pdf_d1 * sqrt_t / 100.0
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2.0 * sqrt_t) - r * K * exp(-r * T) * _norm_cdf(d2)) / 365.0
        rho = K * T * exp(-r * T) * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2.0 * sqrt_t) + r * K * exp(-r * T) * _norm_cdf(-d2)) / 365.0
        rho = -K * T * exp(-r * T) * _norm_cdf(-d2) / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}