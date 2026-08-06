"""Test package.

Puts ``collector/`` on the import path so the tests exercise the collector
exactly as the ``bin/oxfscope`` entry point does: no install, no packaging.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = REPO_ROOT / "collector"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))
