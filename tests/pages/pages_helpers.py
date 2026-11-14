"""Shared readers for the static console checks."""
import json
import importlib.util
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
KEYS = ["1", "S", "2", "X", "K", "S", "W", "R", "U", "M", "V"]
FORBIDDEN = ("mongodb", "sk-", "lsv2_", "AQ.", "/Users/")


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recording():
    path = DOCS / "recording.json"
    assert path.is_file(), "Capture the running console first"
    return json.loads(path.read_text())


class References(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.references = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"}:
                self.references.append((tag, value))
