#!/usr/bin/env python3
"""Copy the console to docs with relative URLs and recorded playback."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "stopline" / "view"
DESTINATION = ROOT / "docs"
BAR = '''  <aside class="recording-bar" aria-label="Recorded demonstration">
    <p>Recorded run from 14 November 2026. Nothing on this page is live: it replays states captured from a real run.</p>
    <nav aria-label="Recording controls and links">
      <button id="autoplay" type="button" aria-pressed="false">Autoplay</button>
      <a href="https://github.com/Aly700/stopline">Source</a>
      <a href="https://www.youtube.com/watch?v=2440YfUVDkE">One-minute video</a>
      <a href="report.html">Reviewer's report</a>
      <a href="architecture.svg">How it is put together</a>
    </nav>
  </aside>
'''


def build():
    DESTINATION.mkdir(exist_ok=True)
    html = (SOURCE / "index.html").read_text()
    html = re.sub(r'((?:src|href)=")/(?!/)([^"]*)"', lambda match: match[1] + (match[2] or "./") + '"', html)
    html = html.replace('  <script src="app.js" defer></script>',
                        '  <link rel="stylesheet" href="pages.css">\n  <script src="replay.js" defer></script>\n  <script src="app.js" defer></script>')
    html = html.replace("<body>\n", "<body>\n" + BAR)
    (DESTINATION / "index.html").write_text(html)
    script = (SOURCE / "app.js").read_text().replace("'/api/", "'api/").replace("'/report/", "'report/")
    (DESTINATION / "app.js").write_text(script)
    (DESTINATION / "style.css").write_bytes((SOURCE / "style.css").read_bytes())
    (DESTINATION / ".nojekyll").write_bytes(b"")


if __name__ == "__main__":
    build()
    print("Built the static console in docs/.")
