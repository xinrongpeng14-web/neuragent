"""Reads the cumulative counters of the NQO service (GET /stats)."""
from __future__ import annotations

import json
import urllib.request
from typing import Dict, Optional


def fetch_stats(url: str, timeout_s: float = 2.0) -> Optional[Dict[str, float]]:
    """The counters, or None when the service cannot be reached."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def delta(before: Optional[Dict[str, float]], after: Optional[Dict[str, float]],
          key: str) -> float:
    if not before or not after:
        return 0.0
    return float(after.get(key, 0.0)) - float(before.get(key, 0.0))
