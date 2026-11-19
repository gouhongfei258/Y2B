"""Pytest bootstrap for the domain test folders.

Collection is ``python -m pytest tests/``. The repository root stays at the
front of ``sys.path`` so ``import app`` and ``import y2b`` resolve to the
application packages rather than a test directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
root = str(ROOT)
if root in sys.path:
    sys.path.remove(root)
sys.path.insert(0, root)
