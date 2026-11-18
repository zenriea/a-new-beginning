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


def test_refreshed_ui_assets_and_module_are_served(client):
    page = client.get("/")
    assert "20261118-pipeline" in page.text
    assert 'class="sidebar"' in page.text and 'id="equity-ranges"' in page.text and 'id="pipeline-body"' in page.text
    for path in ("/static/app.css", "/static/app.js", "/static/ui-model.mjs"):
        response = client.get(path)
        assert response.status_code == 200
    assert "javascript" in client.get("/static/ui-model.mjs").headers["content-type"]


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


def test_legacy_gate_settings_inherit_defaults_and_keep_exploratory_label(client):
    from quantdesk import db
    from quantdesk.config import GATE_PRESETS
    legacy = dict(GATE_PRESETS["exploratory"])
    del legacy["require_causal_dsl"]
    db.ex("INSERT INTO settings(key, value) VALUES('gates', ?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (db.dumps(legacy),))
    client.post("/api/login", json={"password": "correct horse"})
    system = client.get("/api/system").json()
    assert system["settings"]["gates"] == GATE_PRESETS["exploratory"]
    assert system["gate_profile"] == "exploratory"
    # Read-time defaults must not rewrite the operator's saved settings.
    assert "require_causal_dsl" not in db.loads(db.q1("SELECT value FROM settings WHERE key='gates'")["value"])


@pytest.mark.parametrize("path", ["/api/equity?points=0", "/api/orders?limit=-1",
                                  "/api/fills?limit=1000000", "/api/candles?symbol=AAPL&limit=0",
                                  "/api/experiments?limit=-1", "/api/journal?limit=-1"])
def test_query_bounds(client, path):
    client.post("/api/login", json={"password": "correct horse"})
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("body", [[], None, "password", {"password": ["wrong type"]}])
def test_malformed_login_is_a_client_error(client, body):
    assert client.post("/api/login", json=body).status_code == 422


def test_cannot_clear_a_broken_ledger_halt(client):
    from quantdesk import db
    db.set_meta("system_halt", "1")
    db.ex("INSERT INTO cash_ledger(ts, delta, kind) VALUES(0, -100, 'fee')")
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post("/api/control/clear_system_halt", headers={"X-QD": "1"}).status_code == 409
    assert db.get_meta("system_halt") == "1"


def test_local_passwordless_api_rejects_foreign_host_and_origin(client, monkeypatch):
    import dataclasses
    import quantdesk.api.app as appmod
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_password=""))
    assert client.get("/api/state", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.post("/api/control/pause", headers={"X-QD": "1", "Origin": "https://attacker.example"}).status_code == 403


def test_journal_catchup_reads_oldest_unseen_first(client):
    from quantdesk.agent import journal
    ids = [journal.write("system", f"entry {i}", "test") for i in range(125)]
    first = journal.since(0, limit=50)
    second = journal.since(first[-1]["id"], limit=50)
    third = journal.since(second[-1]["id"], limit=50)
    assert [r["id"] for r in first + second + third] == ids


def test_historical_detail_routes_are_authenticated_and_do_not_depend_on_list_window(client):
    from conftest import make_strategy
    from test_ledger import _order
    from quantdesk.trading import ledger
    sid = make_strategy()
    old = _order(sid, "AAPL", "buy", 1)
    fid = ledger.apply_fill(old, 1, 100, 0, {})
    newest = _order(sid, "AAPL", "buy", 1)
    assert client.get(f"/api/orders/{old['id']}").status_code == 401
    assert client.get(f"/api/strategies/{sid}").status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    assert client.get("/api/orders?limit=1").json()[0]["id"] == newest["id"]
    detail = client.get(f"/api/orders/{old['id']}").json()
    assert detail["id"] == old["id"] and detail["fills"][0]["id"] == fid
    assert client.get(f"/api/strategies/{sid}").json()["id"] == sid
    assert client.get("/api/orders/9999").status_code == 404
    assert client.get("/api/strategies/9999").status_code == 404


def test_logout_requires_csrf_and_invalidates_browser_cookie(client):
    assert client.post("/api/logout", headers={"X-QD": "1"}).status_code == 401
    client.post("/api/login", json={"password": "correct horse"})
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/logout", headers={"X-QD": "1"}).status_code == 200
    assert client.get("/api/state").status_code == 401
