"""Transaction-cost model shared by the backtester and the live paper simulator.

Price impact uses the square-root law: impact = coef * sigma_daily * sqrt(order / ADV),
plus a base slippage. Spread is paid explicitly (buy at ask, sell at bid; backtests use a
configured half-spread because historical quotes are not stored).
"""
from __future__ import annotations

import math


def fees(venue: str, side: str, qty: float, price: float, c: dict) -> float:
    notional = abs(qty) * price
    if venue == "CRYPTO":
        return notional * c["taker_fee_bps"] / 1e4
    fee = abs(qty) * c.get("commission_per_share", 0.0)
    if side == "sell":
        fee += notional * c["sec_fee_per_million"] / 1e6
        fee += min(abs(qty) * c["finra_taf_per_share"], c["finra_taf_max"])
    return round(fee, 6)


def impact_bps(notional: float, adv_usd: float | None, sigma_daily: float | None, c: dict) -> float:
    base = c["base_slippage_bps"]
    if not adv_usd or adv_usd <= 0 or not sigma_daily or not math.isfinite(sigma_daily):
        return base + 10.0  # unknown liquidity: be conservative
    return base + c["impact_coef"] * sigma_daily * math.sqrt(max(notional, 0.0) / adv_usd) * 1e4


def apply_slippage(side: str, ref_price: float, bps: float) -> float:
    return ref_price * (1 + bps / 1e4) if side == "buy" else ref_price * (1 - bps / 1e4)


def scaled(c: dict, mult: float) -> dict:
    """Cost-stress variant: all cost components multiplied."""
    out = dict(c)
    for k in ("taker_fee_bps", "base_slippage_bps", "impact_coef", "backtest_half_spread_bps",
              "sec_fee_per_million", "finra_taf_per_share", "commission_per_share"):
        if k in out:
            out[k] = out[k] * mult
    if "finra_taf_max" in out:
        out["finra_taf_max"] = out["finra_taf_max"] * mult
    return out
