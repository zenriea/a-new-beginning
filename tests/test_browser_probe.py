"""Run with pip install playwright and playwright install chromium (or QD_BROWSER_CHANNEL=msedge)."""
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from conftest import make_strategy


def test_cash_only_probe_from_overview_to_detail(env, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    from quantdesk import db
    from quantdesk.agent import journal
    import quantdesk.api.app as appmod
    from quantdesk.worker import acquire_lock

    acquire_lock("test")
    sid = make_strategy(allocation=3000)
    db.ex("UPDATE strategies SET probe=1, expectations=? WHERE id=?",
          (db.dumps({"test": {"total_return": 0.12}}), sid))
    jid = journal.write("decision", "Not promoted: Test idea", "Analyst interpretation",
                        refs={"experiment_id": 1, "decision_evidence": {
                            "outcome": "reject", "test_return": -0.12, "test_trades": 9,
                            "gates_passed": False, "failed_gates": ["deflated_psr"],
                        }})
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
        page.goto("http://desk.test/#journal")
        decision = page.locator(f'#feed [data-jid="{jid}"]')
        pw.expect(decision.locator(".entry-evidence")).to_contain_text("Trades: 9")
        pw.expect(decision.locator(".entry-evidence")).to_contain_text("Deflated PSR")
        pw.expect(decision.locator(".entry-evidence")).to_contain_text("No regular allocation.")
        pw.expect(decision.get_by_text("Analyst interpretation")).not_to_be_visible()
        decision.get_by_text("Analyst rationale").click()
        pw.expect(decision.get_by_text("Analyst interpretation")).to_be_visible()
        browser.close()
