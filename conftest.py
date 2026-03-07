"""Root conftest.py — adds project root to sys.path for all tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
