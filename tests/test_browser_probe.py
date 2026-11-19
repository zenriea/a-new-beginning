"""Run with pip install playwright and playwright install chromium (or QD_BROWSER_CHANNEL=msedge)."""
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from conftest import make_strategy
from test_probes import _experiment


def test_cash_only_probe_from_overview_to_detail(env, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    from quantdesk import db
    import quantdesk.api.app as appmod
    from quantdesk.worker import acquire_lock

    acquire_lock("test")
    sid = make_strategy(allocation=3000)
    db.ex("UPDATE strategies SET probe=1, expectations=? WHERE id=?",
          (db.dumps({"test": {"total_return": 0.12}}), sid))
    monkeypatch.setattr(appmod, "cfg", env)
    client = TestClient(appmod.app)

    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname != "desk.test" or url.path == "/api/stream":
            return route.fulfill(status=204)
        r = client.get(url.path + ("?" + url.query if url.query else ""))
        route.fulfill(status=r.status_code, content_type=r.headers.get("content-type"), body=r.content)

    with pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel=os.getenv("QD_BROWSER_CHANNEL"))
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", serve)
        page.goto("http://desk.test/")
        overview = page.locator("#checkin")
        pw.expect(overview).to_contain_text("All $100,000 is in cash.", timeout=30000)
        pw.expect(overview).to_contain_text("1 probe")
        pw.expect(overview).to_contain_text("No orders or fills yet.")
        probes = page.get_by_role("heading", name="Active paper probes").locator("+ ul")
        pw.expect(probes).to_contain_text("NASDAQ")
        probes.get_by_role("link").click()
        drawer = page.locator("#drawer-root")
        pw.expect(drawer).to_contain_text("No fills yet. The $3,000 allocation is a trading budget.")
        historical = drawer.get_by_role("heading", name="Historical sealed test").locator("..")
        pw.expect(historical).to_contain_text("12.0")
        browser.close()


def test_research_shows_historical_overlap(env, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    from quantdesk import db
    import quantdesk.api.app as appmod

    eid = _experiment()
    db.ex("UPDATE experiments SET leakage=? WHERE id=?",
          (db.dumps({"holdout_reuse_count": 31, "deflation_window_days": 7}), eid))
    monkeypatch.setattr(appmod, "cfg", env)
    client = TestClient(appmod.app)

    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname != "desk.test" or url.path == "/api/stream":
            return route.fulfill(status=204)
        r = client.get(url.path + ("?" + url.query if url.query else ""))
        route.fulfill(status=r.status_code, content_type=r.headers.get("content-type"), body=r.content)

    with pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel=os.getenv("QD_BROWSER_CHANNEL"))
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", serve)
        page.goto("http://desk.test/#research")
        cell = page.locator('[data-label="Historical return"]')
        pw.expect(cell).to_contain_text("31 overlapping tests", timeout=30000)
        pw.expect(cell).to_contain_text("+5.00%")
        page.get_by_role("link", name="Idea", exact=True).click()
        drawer = page.locator("#drawer-root")
        pw.expect(drawer).to_contain_text("not forward paper evidence")
        pw.expect(drawer).to_contain_text("31 overlapping tests")
        browser.close()
