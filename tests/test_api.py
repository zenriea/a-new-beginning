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


def test_login_rate_limit(client):
    codes = [client.post("/api/login", json={"password": "x"}).status_code for _ in range(7)]
    assert 429 in codes


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "QuantDesk" in r.text
    assert "Content-Security-Policy" in r.headers
