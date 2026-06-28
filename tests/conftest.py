"""
Ensures the project root is on sys.path when running `pytest` from the
repo root, so `from detection.rolling_zscore import ...` and
`from ingest.fetch_feed import ...` resolve correctly without needing
the package installed in editable mode.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
