"""Experiment configuration, loaded from a JSON file.

Every field has a default, so a config file only lists what differs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional


@dataclass
class DbConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "neurdb"
    user: str = "neurdb"
    password: Optional[str] = None


@dataclass
class ContainerConfig:
    # Name of the database container when the experiment runs on the host.
    # Empty means the experiment runs inside the container (the normal case).
    name: str = ""
    # CPU cores the container may use; the denominator of cpu_util.
    # 0 derives it from the container's cpu.max or cpuset.
    ncpus: float = 0.0
    # Directory holding cpu.stat of the container's cgroup; derived when empty.
    cgroup_dir: str = ""
    # per-process CPU and memory accounting from /proc (only inside the container)
    proc_stats: bool = True


@dataclass
class NqoConfig:
    stats_url: str = "http://127.0.0.1:8666/stats"
    timeout_s: float = 2.0


@dataclass
class YcsbConfig:
    table: str = "ycsb"
    seed_table: str = "ycsb_seed"
    index: str = "ycsb_k_nr"
    initial_keys: int = 1_000_000
    # keys = lognormal(mu, sigma) * scale, rounded, below 2**53
    key_mu: float = 0.0
    key_sigma: float = 2.0
    key_scale: float = 1e9
    key_seed: int = 20260929
    value: str = "v"
    synchronous_commit: str = "off"
    # operations between two looks at the command pipe
    batch_ops: int = 64


@dataclass
class JobConfig:
    query_dir: str = "queries"
    statement_timeout_ms: int = 60_000
    max_clients: int = 8


@dataclass
class PhaseConfig:
    name: str = "A"
    steps: int = 30
    job_clients: int = 8
    ycsb_read_ratio: float = 0.9
    # operations per second; 0 means unlimited
    ycsb_rate: float = 2000.0
    # directory with this phase's JOB queries; empty = job.query_dir
    query_dir: str = ""


@dataclass
class RewardConfig:
    w_job: float = 0.5
    w_selix: float = 0.5
    w_mem: float = 0.5
    switch_penalty: float = 0.05


def default_phases() -> List[PhaseConfig]:
    return [
        PhaseConfig("A", 30, 8, 0.9, 2000.0),
        PhaseConfig("B", 30, 2, 0.3, 0.0),
    ]


@dataclass
class Config:
    db: DbConfig = field(default_factory=DbConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    nqo: NqoConfig = field(default_factory=NqoConfig)
    ycsb: YcsbConfig = field(default_factory=YcsbConfig)
    job: JobConfig = field(default_factory=JobConfig)
    reward: RewardConfig = field(default_factory=RewardConfig)
    phases: List[PhaseConfig] = field(default_factory=default_phases)
    step_s: float = 30.0
    # steps run under the original action before the first observation
    warmup_steps: int = 1
    # seconds to wait after ALTER SYSTEM + reload before the step starts
    nqo_settle_s: float = 0.2
    refs_path: str = "refs.json"
    log_dir: str = "runs"

    @property
    def episode_steps(self) -> int:
        return sum(p.steps for p in self.phases)

    def query_dirs(self) -> List[str]:
        """Distinct query directories used by the phases, in order of first use."""
        out: List[str] = []
        for p in self.phases:
            d = p.query_dir or self.job.query_dir
            if d not in out:
                out.append(d)
        return out

    def phase_of_step(self, step: int) -> int:
        """Index of the phase that step (0-based, within the episode) belongs to."""
        acc = 0
        for i, p in enumerate(self.phases):
            acc += p.steps
            if step < acc:
                return i
        return len(self.phases) - 1


def _build(cls, data: Dict[str, Any], path: str):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys at {path or 'top level'}: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        ftype = known[name].type
        target = _DATACLASSES.get(ftype if isinstance(ftype, str) else getattr(ftype, "__name__", ""))
        if target is not None and isinstance(value, dict):
            kwargs[name] = _build(target, value, f"{path}.{name}" if path else name)
        elif name == "phases":
            kwargs[name] = [_build(PhaseConfig, p, f"phases[{i}]") for i, p in enumerate(value)]
        else:
            kwargs[name] = value
    return cls(**kwargs)


_DATACLASSES = {
    c.__name__: c
    for c in (DbConfig, ContainerConfig, NqoConfig, YcsbConfig, JobConfig, RewardConfig)
}


def validate(cfg: Config) -> None:
    if cfg.step_s <= 0:
        raise ValueError("step_s must be positive")
    if not cfg.phases:
        raise ValueError("at least one phase is required")
    for p in cfg.phases:
        if p.steps < 1:
            raise ValueError(f"phase {p.name}: steps must be at least 1")
        if not 0.0 <= p.ycsb_read_ratio <= 1.0:
            raise ValueError(f"phase {p.name}: ycsb_read_ratio must be within [0, 1]")
        if not 0 <= p.job_clients <= cfg.job.max_clients:
            raise ValueError(f"phase {p.name}: job_clients must be within [0, job.max_clients]")
        if p.ycsb_rate < 0:
            raise ValueError(f"phase {p.name}: ycsb_rate must not be negative")
    if cfg.container.ncpus < 0:
        raise ValueError("container.ncpus must not be negative (0 = detect)")
    if cfg.ycsb.initial_keys < 1:
        raise ValueError("ycsb.initial_keys must be at least 1")


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Config:
    data: Dict[str, Any] = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if overrides:
        data = {**data, **overrides}
    cfg = _build(Config, data, "")
    validate(cfg)
    return cfg


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    return obj
