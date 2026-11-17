"""Static UI contracts plus the dependency-free JavaScript display-model suite."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.targets = []
        self.tabs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "a" and a.get("href", "").startswith("#"):
            self.targets.append(a["href"][1:])
        if a.get("role") == "tab":
            self.tabs.append(a)


def test_workspace_has_unique_targets_and_preserves_accessible_record_tabs():
    page = Page()
    page.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
    assert len(page.ids) == len(set(page.ids))
    assert all(target in page.ids for target in page.targets)
    assert {tab["data-tab"] for tab in page.tabs} == {"research", "strategies", "orders", "market", "system"}
    assert sum(tab.get("aria-selected") == "true" for tab in page.tabs) == 1
    assert all(tab["aria-controls"] == "tab-body" for tab in page.tabs)
    assert {"btn-pause", "btn-stop", "connection-alert", "desk-health", "equity-ranges", "feed", "ask-form"} <= set(page.ids)


def test_display_models():
    node = shutil.which("node")
    if node is None:
        windows_node = Path("C:/Program Files/nodejs/node.exe")
        if windows_node.exists():
            node = str(windows_node)
    if node is None:
        pytest.skip("Node.js is required for the UI display-model tests")
    result = subprocess.run([node, "--test", str(ROOT / "tests" / "ui-model.test.mjs")],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
