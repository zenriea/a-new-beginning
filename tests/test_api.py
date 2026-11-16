import pytest


@pytest.fixture()
def client(env, monkeypatch):
    import importlib
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "correct horse")
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_auth_and_csrf(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 200
    r = client.get("/api/state")
    assert r.status_code == 200 and r.json()["mode"]["paper"] is True
    assert client.post("/api/control/pause").status_code == 403            # missing X-QD header
    assert client.post("/api/control/pause", headers={"X-QD": "1"}).status_code == 200
    r = client.post("/api/settings/risk", json={"max_gross_pct": 3}, headers={"X-QD": "1"})
    assert r.status_code == 400                                              # above hard ceiling
    r = client.post("/api/settings/agent", json={"paused": False}, headers={"X-QD": "1"})
    assert r.status_code == 400
    from quantdesk import db
    text = client.get("/api/system").text + client.get("/api/state").text
    assert "correct horse" not in text                      # password never echoed
    assert db.get_meta("session_secret") not in text        # signing key never exposed


def test_stale_data_alert_ignores_symbols_dropped_from_universe(client):
    from quantdesk.marketdata import store
    store.set_status("bars:ROKU", False, "delisted")
    store.set_status("bars:BTC-USD", False, "stale")
    client.post("/api/login", json={"password": "correct horse"})
    keys = [d["key"] for d in client.get("/api/state").json()["data"]["problems"]]
    assert "bars:BTC-USD" in keys and "bars:ROKU" not in keys


def test_login_rate_limit(client):
    codes = [client.post("/api/login", json={"password": "x"}).status_code for _ in range(7)]
    assert 429 in codes


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "QuantDesk" in r.text
    assert "Content-Security-Policy" in r.headers


def test_gate_presets_require_auth_csrf_and_write_an_audit_record(client):
    from quantdesk import db, settings
    from quantdesk.config import GATE_PRESETS
    path = "/api/gate-presets/strict"
    assert client.post(path, headers={"X-QD": "1"}).status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post(path).status_code == 403
    assert client.post("/api/gate-presets/unknown", headers={"X-QD": "1"}).status_code == 400
    assert client.post(path, headers={"X-QD": "1"}).json() == GATE_PRESETS["strict"]
    assert settings.get("gates") == GATE_PRESETS["strict"]
    assert db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='settings.gates'")["n"] == 1
    system = client.get("/api/system").json()
    assert system["gate_profile"] == "strict" and system["gate_presets"] == GATE_PRESETS
    client.post("/api/settings/gates", json={"min_test_sharpe": 0.6}, headers={"X-QD": "1"})
    assert client.get("/api/system").json()["gate_profile"] == "custom"


def test_causal_dsl_requirement_must_be_boolean(client):
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post("/api/settings/gates", json={"require_causal_dsl": "false"},
                       headers={"X-QD": "1"}).status_code == 400
