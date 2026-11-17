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
    assert not ev(req(), snap(quote_age=-30)).approved
    assert not ev(req(), snap(unpriced_positions=True)).approved
    assert ev(req("sell"), snap(unpriced_positions=True, sleeve_qty=10)).approved


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


def test_gate_thresholds_are_operator_editable_within_bounds(env):
    import pytest
    from quantdesk import settings
    g = settings.get("gates")
    assert (g["min_test_sharpe"], g["min_deflated_psr"], g["max_top5_concentration"]) == (0.0, 0.25, 1.0)
    assert settings.operator_update("gates", {"min_test_sharpe": 0.4})["min_test_sharpe"] == 0.4
    with pytest.raises(settings.SettingsError):
        settings.operator_update("gates", {"min_test_sharpe": 50})
    with pytest.raises(settings.SettingsError):
        settings.operator_update("gates", {"min_deflated_psr": 1.5})


def test_pending_buys_reserve_symbol_and_crypto_room(env):
    d = ev(req(qty=100), snap(open_buy_notional=14000, open_buy_by_symbol={"AAPL": 14000}))
    assert d.approved and d.qty == 10
    d = ev(req(qty=1, price=10000, symbol="BTC-USD"),
           snap(open_buy_notional=24000, open_crypto_buy_notional=24000))
    assert d.approved and d.qty == pytest.approx(0.1)


def test_snapshot_excludes_only_current_order_reservation(env):
    from conftest import make_strategy
    from quantdesk.trading import oms, risk
    sid = make_strategy()
    put_quote("AAPL", 100, 100)
    orders = [oms.create_order(symbol="AAPL", side="buy", qty=50, reason="entry", rationale="test",
                               idem_key=f"reservation-{i}", strategy_id=sid) for i in range(2)]
    request = risk.OrderRequest("AAPL", "buy", 50, 100, sid)
    full = risk.snapshot_for(request)
    filling = risk.snapshot_for(request, exclude_order_id=orders[0]["id"])
    assert full.open_buy_notional == full.open_buy_by_symbol["AAPL"] == 10000
    assert full.strategy_invested == 10000
    assert filling.open_buy_notional == filling.open_buy_by_symbol["AAPL"] == 5000
    assert filling.strategy_invested == 5000


def test_queued_orders_cannot_overbook_symbol_across_strategies(env):
    from conftest import make_strategy
    from quantdesk.trading import oms
    put_quote("AAPL", 100, 100)
    orders = [oms.create_order(symbol="AAPL", side="buy", qty=100, reason="entry", rationale="test",
                               idem_key=f"symbol-{i}", strategy_id=make_strategy()) for i in range(3)]
    assert [o["qty"] for o in orders[:2]] == [100, 50]
    assert orders[2]["status"] == "rejected"


def test_queued_orders_cannot_overbook_crypto_across_symbols(env):
    from conftest import make_strategy
    from quantdesk.trading import oms, risk
    sid = make_strategy(venue="CRYPTO", allocation=50000)
    orders = []
    for symbol in ("BTC-USD", "ETH-USD", "SOL-USD"):
        put_quote(symbol, 100, 100)
        orders.append(oms.create_order(symbol=symbol, side="buy", qty=150, reason="entry", rationale="test",
                                      idem_key=symbol, strategy_id=sid))
    assert orders[0]["qty"] == 150 and orders[1]["qty"] == 100
    assert orders[2]["status"] == "rejected"
    snapshot = risk.snapshot_for(risk.OrderRequest("BTC-USD", "buy", 1, 100, sid))
    assert snapshot.open_crypto_buy_notional == 25000


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1.0])
def test_nonfinite_or_negative_prices_are_rejected(env, price):
    assert not ev(req(price=price), snap()).approved


def test_pending_reservation_survives_price_fall_and_partial_fill(env):
    from conftest import make_strategy
    from quantdesk import db
    from quantdesk.trading import oms, risk
    sid = make_strategy()
    put_quote("AAPL", 100, 100)
    order = oms.create_order(symbol="AAPL", side="buy", qty=100, reason="entry", rationale="test",
                             idem_key="partial", strategy_id=sid)
    db.ex("UPDATE orders SET filled_qty=40, status='partially_filled' WHERE id=?", (order["id"],))
    put_quote("AAPL", 80, 80)
    snapshot = risk.snapshot_for(risk.OrderRequest("AAPL", "buy", 1, 80, sid))
    assert snapshot.open_buy_notional == 6000


def test_unpriced_legacy_pending_order_blocks_new_buys_but_not_sells(env):
    from conftest import make_strategy
    from quantdesk import db
    from quantdesk.trading import oms, risk
    sid = make_strategy()
    put_quote("AAPL", 100, 100)
    order = oms.create_order(symbol="AAPL", side="buy", qty=10, reason="entry", rationale="test",
                             idem_key="legacy", strategy_id=sid)
    db.ex("UPDATE orders SET signal_snapshot='{}' WHERE id=?", (order["id"],))
    db.ex("DELETE FROM quotes WHERE symbol='AAPL'")
    put_quote("MSFT", 100, 100)
    request = risk.OrderRequest("MSFT", "buy", 1, 100, sid)
    snapshot = risk.snapshot_for(request)
    assert snapshot.unpriced_pending and not snapshot.paused
    decision = risk.evaluate(request, snapshot, LIM)
    assert not decision.approved
    assert decision.reason == "pending buy without a reference price blocks new risk"
    assert any(c["check"] == "pending_buy_priced" and not c["pass"] for c in decision.checks)
    assert not any(c["check"] == "paused" for c in decision.checks)
    snapshot.sleeve_qty = 1
    sell = risk.OrderRequest("MSFT", "sell", 1, 100, sid)
    assert risk.evaluate(sell, snapshot, LIM).approved
    oms.close_order(order, "cancelled", "Remove unpriced legacy order.")
    assert not risk.snapshot_for(request).unpriced_pending
    assert risk.check(request).approved
