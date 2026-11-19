"""Check that real Pipeline candidate rows share an alignment grid."""
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from test_probes import _experiment


def test_pipeline_candidate_columns_align_with_different_gate_counts(env, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    from quantdesk import db
    import quantdesk.api.app as appmod

    good = _experiment(ret=0.025, failed=("param_stability", "subperiods", "deflated_psr"))
    _experiment(ret=-0.04, failed=("deflated_psr",))
    _experiment(ret=0.03, status="passed", failed=())
    title = "Monthly Buffered Trend in Eight Majors With Inverse Volatility Weighting"
    db.ex("UPDATE hypotheses SET title=? WHERE id=(SELECT hypothesis_id FROM experiments WHERE id=?)", (title, good))
    db.ex("UPDATE experiments SET leakage=? WHERE id=?", (db.dumps({"holdout_reuse_count": 22}), good))
    monkeypatch.setattr(appmod, "cfg", env)
    client = TestClient(appmod.app)

    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname != "desk.test" or url.path == "/api/stream":
            return route.fulfill(status=204)
        response = client.get(url.path + ("?" + url.query if url.query else ""))
        route.fulfill(status=response.status_code, content_type=response.headers.get("content-type"),
                      body=response.content)

    with pw.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel=os.getenv("QD_BROWSER_CHANNEL"))
        page = browser.new_page(viewport={"width": 1920, "height": 960})
        page.route("**/*", serve)
        page.goto("http://desk.test/#pipeline")
        candidates = page.locator(".review-candidates")
        pw.expect(candidates.locator("li")).to_have_count(3, timeout=30000)
        pw.expect(candidates.get_by_text(title, exact=False)).to_have_css("white-space", "normal")

        for width in (1920, 1440, 390):
            page.set_viewport_size({"width": width, "height": 960})
            results = candidates.locator(".cand-result").evaluate_all("(cells) => cells.map(c => c.getBoundingClientRect().x)")
            gates = candidates.locator(".cand-gates").evaluate_all("(cells) => cells.map(c => c.getBoundingClientRect().x)")
            buttons = candidates.get_by_role("button", name="Start manual probe…").evaluate_all(
                "(buttons) => buttons.map(b => [b.getBoundingClientRect().x, b.getBoundingClientRect().width])")
            assert len(results) == len(buttons) == 3 and len(gates) == 2
            assert len(set(results)) == len(set(gates)) == len(set(map(tuple, buttons))) == 1
            assert results[0] < gates[0]
            assert page.evaluate("document.documentElement.scrollWidth") == width
        browser.close()
