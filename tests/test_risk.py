import pathlib

import pytest

from conftest import put_quote

LIM = {"max_position_pct": 0.15, "max_gross_pct": 0.8, "max_crypto_pct": 0.25, "max_strategy_pct": 0.25,
       "max_total_allocation_pct": 0.8, "max_order_usd": 20000.0, "min_order_usd": 25.0, "daily_loss_limit_pct": 0.025,
       "max_drawdown_pct": 0.1, "max_orders_per_hour": 30, "max_quote_age_sec": 120, "max_participation": 0.01,
       "max_spread_bps": 60}


def snap(**kw):
    from quantdesk.trading.risk import Snapshot
    base = dict(now=0, equity=100000, cash=100000, gross=0, crypto_gross=0, symbol_value={}, sleeve_qty=0,
                strategy_allocation=50000, strategy_invested=0, peak_equity=100000, day_start_equity=100000,
                orders_last_hour=0, quote_age=1, quote_ok=True, market_open=True, paused=False, emergency_stop=False,
                drawdown_halt=False)
    base.update(kw)
    return Snapshot(**base)


def req(side="buy", qty=10, price=100, symbol="AAPL", origin="agent"):
    from quantdesk.trading.risk import OrderRequest
    return OrderRequest(symbol, side, qty, price, 1, origin)


def ev(r, s, lim=LIM):
    from quantdesk.trading.risk import evaluate
    return evaluate(r, s, lim)


def test_basic_approve(env):
    d = ev(req(), snap())
    assert d.approved and d.qty == 10


def test_emergency_blocks_everything_but_operator_sells(env):
    assert not ev(req(), snap(emergency_stop=True)).approved
    assert not ev(req("sell"), snap(emergency_stop=True, sleeve_qty=10)).approved
    assert ev(req("sell", origin="operator"), snap(emergency_stop=True, sleeve_qty=10)).approved


def test_pause_and_drawdown_block_buys_not_sells(env):
    for flag in ("paused", "drawdown_halt"):
        assert not ev(req(), snap(**{flag: True})).approved
        assert ev(req("sell"), snap(**{flag: True}, sleeve_qty=10)).approved


def test_data_and_session_checks(env):
    assert "stale" in ev(req(), snap(quote_age=500)).reason
    assert "closed" in ev(req(), snap(market_open=False)).reason
    assert not ev(req(), snap(quote_ok=False)).approved
    assert not ev(req(), snap(bars_ok=False)).approved


def test_position_limit_resizes(env):
    d = ev(req(qty=300, price=100), snap())   # $30k vs 15% of 100k = $15k (also < max order 20k)
    assert d.approved and d.resized and d.qty == 150


def test_gross_and_crypto_limits(env):
    d = ev(req(qty=100, price=100), snap(gross=79000))
    assert d.approved and d.resized and d.qty == 10  # $1k of gross room left
    d = ev(req(qty=100, price=100), snap(gross=79990))
    assert not d.approved  # $10 room < $25 minimum
    d2 = ev(req(qty=1, price=100, symbol="BTC-USD"), snap(crypto_gross=25000))
    assert not d2.approved


def test_loss_limits(env):
    assert "daily loss" in ev(req(), snap(equity=97000, cash=97000, day_start_equity=100000)).reason
    assert not ev(req(), snap(equity=89000, cash=89000, day_start_equity=89000, peak_equity=100000)).approved


def test_rate_and_size(env):
    assert "rate" in ev(req(), snap(orders_last_hour=30)).reason
    assert not ev(req(qty=0.1, price=100), snap()).approved  # $10 < min order
    assert not ev(req("sell", qty=5), snap(sleeve_qty=0)).approved  # no shorting


def test_cash_and_strategy_allocation(env):
    d = ev(req(qty=100, price=100), snap(cash=5000))
    assert d.approved and d.qty * 100 <= 5000
    d = ev(req(qty=100, price=100), snap(strategy_allocation=3000))
    assert d.approved and d.qty * 100 <= 3000


def test_settings_bounds_and_ceilings(env):
    from quantdesk import settings
    with pytest.raises(settings.SettingsError):
        settings.operator_update("risk", {"max_gross_pct": 1.5})  # leverage above hard ceiling
    with pytest.raises(settings.SettingsError):
        settings.operator_update("risk", {"nonexistent": 1})
    settings.operator_update("risk", {"max_position_pct": 0.1})
    assert settings.get("risk")["max_position_pct"] == 0.1


def test_agent_code_cannot_write_settings(env):
    root = pathlib.Path(__file__).resolve().parents[1] / "quantdesk"
    for sub in ("agent", "research", "trading", "marketdata"):
        for f in (root / sub).glob("*.py"):
            src = f.read_text()
            assert "operator_update" not in src, f"{f} must not call operator_update"
            assert "INSERT INTO settings" not in src and "UPDATE settings" not in src, f


def test_check_uses_live_state(env):
    from conftest import make_strategy
    from quantdesk.trading import risk
    put_quote("AAPL", 100, 100.05)
    assert not risk.check(req(qty=10, price=100)).approved   # unknown strategy has no allocation
    make_strategy()
    d = risk.check(req(qty=10, price=100))
    assert d.approved
