"""YCSB keys: integers drawn from a log-normal distribution.

Uniform keys are the easiest case for the linear models of a learned index and
leave the least room for tuning, so the workload uses a skewed distribution.
All keys stay below 2**53: SELIX computes positions in double precision and
cannot tell larger integers apart.
"""
from __future__ import annotations

import numpy as np

MAX_KEY = 2**53 - 1


def lognormal_keys(n: int, seed: int, mu: float = 0.0, sigma: float = 2.0,
                   scale: float = 1e9, unique: bool = False) -> np.ndarray:
    """n keys as int64. With unique=True the result has no duplicates."""
    rng = np.random.default_rng(seed)
    if not unique:
        return _draw(rng, n, mu, sigma, scale)

    out = np.empty(0, dtype=np.int64)
    while out.size < n:
        batch = _draw(rng, max(1024, int((n - out.size) * 1.2)), mu, sigma, scale)
        out = np.unique(np.concatenate([out, batch]))
    # np.unique sorts; shuffle so that the first n are not the n smallest
    rng.shuffle(out)
    return out[:n]


def _draw(rng: np.random.Generator, n: int, mu: float, sigma: float, scale: float) -> np.ndarray:
    values = rng.lognormal(mu, sigma, n) * scale
    values = np.clip(np.rint(values), 1, MAX_KEY)
    return values.astype(np.int64)
