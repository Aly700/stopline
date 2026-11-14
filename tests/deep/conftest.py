"""These tests reuse the agents tests' fakes, so the repository root must be importable."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
