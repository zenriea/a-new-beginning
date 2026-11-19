"""Exercise the workspace navigation at desktop and phone widths."""
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient


def test_workspace_navigation(env, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    import quantdesk.api.app as appmod

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
        page = browser.new_page(viewport={"width": 320, "height": 740})
        page.route("**/*", serve)
        page.goto("http://desk.test/")
        toggle = page.get_by_role("button", name="Menu", exact=True)
        menu = page.locator("#desk-menu")
        journal = menu.get_by_role("link", name="Analyst journal", include_hidden=True)
        pw.expect(toggle).to_be_visible()
        pw.expect(menu).to_be_hidden()
        pw.expect(page.get_by_role("button", name="Emergency stop", exact=True)).to_be_visible()
        toggle.focus()
        toggle.press("Enter")
        pw.expect(toggle).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("Tab")
        pw.expect(menu.get_by_role("link", name="Overview")).to_be_focused()
        page.keyboard.press("Escape")
        pw.expect(toggle).to_be_focused()
        pw.expect(menu).to_be_hidden()

        toggle.click()
        journal.click()
        pw.expect(menu).to_be_hidden()
        pw.expect(page.locator("#journal")).to_be_focused()
        pw.expect(journal).to_have_attribute("aria-current", "location")
        assert page.evaluate("document.documentElement.scrollWidth") == 320

        page.set_viewport_size({"width": 1440, "height": 900})
        pw.expect(toggle).to_be_hidden()
        pw.expect(menu).to_be_visible()
        research = menu.locator('[data-nav="research"]')
        research.click()
        pw.expect(research).to_have_attribute("aria-current", "location")
        pw.expect(page.locator("#records")).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth") == 1440
        browser.close()
