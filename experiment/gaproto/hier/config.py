"""Configuration of the hierarchical experiment (one query group per file)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional

from ..config import DbConfig, NqoConfig


@dataclass
class HierConfig:
    db: DbConfig = field(default_factory=DbConfig)
    nqo: NqoConfig = field(default_factory=NqoConfig)
    group: str = "long"                 # long | short
    query_dir: str = "queries/job_long"
    step_queries: int = 1               # k: queries per GA step (plan 5.1: long 1, short 5)
    clients: int = 1                    # sessions that run the queries of one step in parallel
    repeats: int = 3                    # how often each query occurs in one episode
    statement_timeout_ms: int = 300_000
    density: str = "dense"              # SELIX density, fixed (plan 5.2)
    warm_queries: bool = True           # run every query once under arm O before measuring (not counted)
    refs_path: str = "runs/hier_long/refs.json"
    static_best_path: str = "runs/hier_long/static_best.json"
    log_dir: str = "runs/hier_long"
    # bounds of the latency ratio inside the reward (a timeout must not dominate the update)
    ratio_min: float = 0.02
    ratio_max: float = 50.0

    @property
    def workers(self) -> int:
        """Sessions actually needed: a step never has more queries than step_queries."""
        return max(1, min(self.clients, self.step_queries))


def validate(cfg: HierConfig) -> None:
    if cfg.group not in ("long", "short"):
        raise ValueError("group must be long or short")
    if cfg.step_queries < 1 or cfg.clients < 1 or cfg.repeats < 1:
        raise ValueError("step_queries, clients and repeats must be at least 1")
    if cfg.density not in ("dense", "mid", "default"):
        raise ValueError("density must be dense, mid or default")
    if not 0 < cfg.ratio_min < 1 < cfg.ratio_max:
        raise ValueError("need 0 < ratio_min < 1 < ratio_max")


def load_hier_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> HierConfig:
    data: Dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if overrides:
        data = {**data, **overrides}
    known = {f.name for f in fields(HierConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    kwargs = dict(data)
    if isinstance(kwargs.get("db"), dict):
        kwargs["db"] = DbConfig(**kwargs["db"])
    if isinstance(kwargs.get("nqo"), dict):
        kwargs["nqo"] = NqoConfig(**kwargs["nqo"])
    cfg = HierConfig(**kwargs)
    validate(cfg)
    return cfg


def to_dict(cfg: HierConfig) -> Dict[str, Any]:
    out = {}
    for f in fields(cfg):
        v = getattr(cfg, f.name)
        out[f.name] = v.__dict__ if hasattr(v, "__dict__") else v
    return out
