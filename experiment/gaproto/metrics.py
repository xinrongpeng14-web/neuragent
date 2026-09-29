"""Per-step metrics, reference values of the original system, reward and observation."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .actions import NQO_MODES, SELIX_PRESET_NAMES
from .config import RewardConfig

OBS_DIM = 9
OBS_NAMES = ("cpu_util", "olap_share", "ycsb_insert_ratio", "nqo_overhead", "idx_cost",
             "idx_mem", "smo_rate", "prev_nqo", "prev_selix")
OBS_LOW, OBS_HIGH = -3.0, 3.0
# structure modifications per operation are of the order of 1e-3
SMO_SCALE = 1000.0
_EPS = 1e-12


@dataclass
class StepMetrics:
    elapsed_s: float = 0.0
    cpu_util: float = 0.0
    container_mem_bytes: int = 0
    # JOB, measured at the clients
    job_completed: List[Tuple[str, float]] = field(default_factory=list)
    job_errors: int = 0
    job_lat_sum_s: float = 0.0
    job_p50_s: float = 0.0
    job_p99_s: float = 0.0
    # YCSB at the SQL level, measured at the client (guard metric)
    ycsb_reads: int = 0
    ycsb_inserts: int = 0
    ycsb_errors: int = 0
    ycsb_misses: int = 0
    ycsb_lat_sum_s: float = 0.0
    ycsb_p50_s: float = 0.0
    ycsb_p99_s: float = 0.0
    ycsb_alive: bool = True
    # SELIX, measured inside the index
    idx_gets: int = 0
    idx_puts: int = 0
    idx_sampled: int = 0
    idx_sampled_ns: int = 0
    idx_smo: int = 0
    idx_mem_bytes: int = 0
    idx_keys: int = 0
    idx_density: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    # NQO service
    nqo_requests: float = 0.0
    nqo_opt_time_ms: float = 0.0
    nqo_reachable: bool = True

    @property
    def ycsb_ops(self) -> int:
        return self.ycsb_reads + self.ycsb_inserts

    @property
    def ycsb_ops_per_s(self) -> float:
        return self.ycsb_ops / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def c_idx_ns(self) -> Optional[float]:
        """Mean time of one SELIX operation, structure modifications included."""
        return self.idx_sampled_ns / self.idx_sampled if self.idx_sampled > 0 else None

    @property
    def smo_rate(self) -> float:
        ops = self.idx_gets + self.idx_puts
        return self.idx_smo / ops if ops > 0 else 0.0

    @property
    def ycsb_insert_ratio(self) -> float:
        ops = self.idx_gets + self.idx_puts
        return self.idx_puts / ops if ops > 0 else 0.0

    @property
    def olap_share(self) -> float:
        total = self.job_lat_sum_s + self.ycsb_lat_sum_s
        return self.job_lat_sum_s / total if total > 0 else 0.0

    @property
    def nqo_overhead(self) -> float:
        if self.job_lat_sum_s <= 0:
            return 0.0
        return (self.nqo_opt_time_ms / 1000.0) / self.job_lat_sum_s

    def to_record(self) -> Dict[str, Any]:
        rec = asdict(self)
        rec["idx_density"] = list(self.idx_density)
        rec.update(ycsb_ops_per_s=self.ycsb_ops_per_s, c_idx_ns=self.c_idx_ns,
                   smo_rate=self.smo_rate, ycsb_insert_ratio=self.ycsb_insert_ratio,
                   olap_share=self.olap_share, nqo_overhead=self.nqo_overhead)
        return rec


def fill_job(m: StepMetrics, snap: Dict[str, Any]) -> None:
    m.job_completed = [(t, float(x)) for t, x in snap["completed"]]
    m.job_errors = int(snap["n_error"])
    lat = np.asarray([x for _, x in m.job_completed], dtype=np.float64)
    if lat.size:
        m.job_lat_sum_s = float(lat.sum())
        m.job_p50_s = float(np.percentile(lat, 50))
        m.job_p99_s = float(np.percentile(lat, 99))


def fill_ycsb(m: StepMetrics, before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> None:
    """after holds the client counters of the step; the index counters are cumulative."""
    m.ycsb_alive = bool(after["alive"])
    m.ycsb_reads = int(after["n_read"])
    m.ycsb_inserts = int(after["n_insert"])
    m.ycsb_errors = int(after["n_error"])
    m.ycsb_misses = int(after["n_miss"])
    m.ycsb_lat_sum_s = float(after["lat_sum_s"])
    m.ycsb_p50_s = float(after["lat_p50_s"])
    m.ycsb_p99_s = float(after["lat_p99_s"])
    s1 = after.get("stats")
    s0 = before.get("stats") if before else None
    if not s1:
        return
    m.idx_mem_bytes = int(s1["mem_bytes"])
    m.idx_keys = int(s1["n_keys"])
    m.idx_density = (float(s1["init_density"]), float(s1["max_density"]), float(s1["min_density"]))
    if not s0:
        return
    m.idx_gets = int(s1["n_get"] - s0["n_get"])
    m.idx_puts = int(s1["n_put"] - s0["n_put"])
    m.idx_sampled = int((s1["c_get"] - s0["c_get"]) + (s1["c_put"] - s0["c_put"]))
    m.idx_sampled_ns = int((s1["t_get_ns"] - s0["t_get_ns"]) + (s1["t_put_ns"] - s0["t_put_ns"]))
    m.idx_smo = int(s1["n_smo"] - s0["n_smo"])


# --------------------------------------------------------------------------
# Reference values measured on the original system
# --------------------------------------------------------------------------
@dataclass
class Refs:
    base_lat: Dict[str, float] = field(default_factory=dict)   # template -> seconds
    q_j_ref: Dict[str, float] = field(default_factory=dict)    # phase -> standardised throughput
    c_ref_ns: Dict[str, float] = field(default_factory=dict)   # phase -> ns per SELIX operation
    mem_ref: List[float] = field(default_factory=list)         # step index -> index bytes
    meta: Dict[str, Any] = field(default_factory=dict)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, sort_keys=True)

    @staticmethod
    def load(path: str) -> "Refs":
        with open(path, encoding="utf-8") as f:
            return Refs(**json.load(f))

    def mem_at(self, step: int) -> Optional[float]:
        if not self.mem_ref:
            return None
        return self.mem_ref[min(step, len(self.mem_ref) - 1)]


def standardised_job_throughput(completed: Sequence[Tuple[str, float]],
                                base_lat: Dict[str, float], elapsed_s: float) -> float:
    """q_J: completed queries weighted by their baseline latency, per second.

    Weighting makes queries of different sizes comparable: finishing one query
    that takes 2 s on the original system counts as much as two 1 s queries.
    A template without a baseline is weighted by its own latency.
    """
    if elapsed_s <= 0:
        return 0.0
    return sum(base_lat.get(t, lat) for t, lat in completed) / elapsed_s


def _log_ratio(num: Optional[float], den: Optional[float]) -> float:
    if num is None or den is None or num <= _EPS or den <= _EPS:
        return 0.0
    return math.log(num / den)


@dataclass
class RewardParts:
    r_job: float
    r_selix_cost: float
    r_selix_mem: float
    switched: int
    reward: float
    q_j: float

    @property
    def r_selix(self) -> float:
        return self.r_selix_cost + self.r_selix_mem


def compute_reward(m: StepMetrics, refs: Refs, phase: str, step: int, switched: bool,
                   w: RewardConfig) -> RewardParts:
    """r = w_job*R_J + w_selix*R_S - penalty*switched

    R_J = log(q_J / q_J_ref)
    R_S = log(c_ref / c_idx) - w_mem * log(mem / mem_ref)

    A term whose measurement or reference is missing contributes 0.
    """
    q_j = standardised_job_throughput(m.job_completed, refs.base_lat, m.elapsed_s)
    q_ref = refs.q_j_ref.get(phase)
    if q_ref is not None and q_ref > _EPS and q_j <= _EPS:
        # no query finished although the original system finishes some:
        # bounded penalty instead of log(0)
        r_job = OBS_LOW
    else:
        r_job = _log_ratio(q_j, q_ref)
    r_cost = _log_ratio(refs.c_ref_ns.get(phase), m.c_idx_ns)
    r_mem = -w.w_mem * _log_ratio(float(m.idx_mem_bytes) if m.idx_mem_bytes else None,
                                  refs.mem_at(step))
    reward = w.w_job * r_job + w.w_selix * (r_cost + r_mem) - w.switch_penalty * int(switched)
    return RewardParts(r_job, r_cost, r_mem, int(switched), reward, q_j)


def observation(m: StepMetrics, refs: Optional[Refs], phase: str, step: int,
                nqo_mode: str, selix_preset: str) -> np.ndarray:
    idx_cost = _log_ratio(m.c_idx_ns, refs.c_ref_ns.get(phase)) if refs else 0.0
    idx_mem = _log_ratio(float(m.idx_mem_bytes) if m.idx_mem_bytes else None,
                         refs.mem_at(step)) if refs else 0.0
    obs = np.array([
        m.cpu_util,
        m.olap_share,
        m.ycsb_insert_ratio,
        m.nqo_overhead,
        idx_cost,
        idx_mem,
        m.smo_rate * SMO_SCALE,
        NQO_MODES.index(nqo_mode) / (len(NQO_MODES) - 1),
        SELIX_PRESET_NAMES.index(selix_preset) / (len(SELIX_PRESET_NAMES) - 1),
    ], dtype=np.float32)
    return np.clip(np.nan_to_num(obs, nan=0.0, posinf=OBS_HIGH, neginf=OBS_LOW), OBS_LOW, OBS_HIGH)


# --------------------------------------------------------------------------
# Building the references from runs of the original system
# --------------------------------------------------------------------------
def build_refs(steps: Sequence[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> Refs:
    """steps: records with keys episode, step, phase and metrics (StepMetrics.to_record())."""
    by_template: Dict[str, List[float]] = {}
    for s in steps:
        for t, lat in s["metrics"]["job_completed"]:
            by_template.setdefault(t, []).append(float(lat))
    base_lat = {t: float(np.median(v)) for t, v in by_template.items()}

    q_sum: Dict[str, List[float]] = {}
    ns_sum: Dict[str, float] = {}
    cnt_sum: Dict[str, int] = {}
    mem: Dict[int, List[float]] = {}
    for s in steps:
        m, ph = s["metrics"], s["phase"]
        q_sum.setdefault(ph, []).append(
            standardised_job_throughput(m["job_completed"], base_lat, m["elapsed_s"]))
        ns_sum[ph] = ns_sum.get(ph, 0.0) + m["idx_sampled_ns"]
        cnt_sum[ph] = cnt_sum.get(ph, 0) + m["idx_sampled"]
        if m["idx_mem_bytes"]:
            mem.setdefault(int(s["step"]), []).append(float(m["idx_mem_bytes"]))

    refs = Refs(base_lat=base_lat, meta=dict(meta or {}))
    refs.q_j_ref = {ph: float(np.mean(v)) for ph, v in q_sum.items()}
    refs.c_ref_ns = {ph: ns_sum[ph] / cnt_sum[ph] for ph in ns_sum if cnt_sum[ph] > 0}
    if mem:
        refs.mem_ref = [float(np.mean(mem[i])) if i in mem else float("nan")
                        for i in range(max(mem) + 1)]
        # a step without a measurement takes the value of its predecessor
        for i, v in enumerate(refs.mem_ref):
            if math.isnan(v):
                refs.mem_ref[i] = refs.mem_ref[i - 1] if i > 0 else float(np.nanmean(refs.mem_ref))
    return refs


def noise_report(steps: Sequence[Dict[str, Any]], refs: Refs, window: int = 1) -> Dict[str, Dict[str, float]]:
    """Coefficient of variation of q_J and c_idx per phase, over windows of `window` steps."""
    series: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for s in steps:
        series.setdefault((int(s["episode"]), s["phase"]), []).append(s)
    values: Dict[str, Dict[str, List[float]]] = {}
    for (_, ph), items in series.items():
        items.sort(key=lambda s: s["step"])
        for i in range(0, len(items) - window + 1, window):
            chunk = [x["metrics"] for x in items[i:i + window]]
            elapsed = sum(c["elapsed_s"] for c in chunk)
            done = [p for c in chunk for p in c["job_completed"]]
            sampled = sum(c["idx_sampled"] for c in chunk)
            v = values.setdefault(ph, {"q_j": [], "c_idx": []})
            v["q_j"].append(standardised_job_throughput(done, refs.base_lat, elapsed))
            if sampled:
                v["c_idx"].append(sum(c["idx_sampled_ns"] for c in chunk) / sampled)

    def cv(x: List[float]) -> float:
        a = np.asarray(x, dtype=np.float64)
        return float(a.std(ddof=1) / a.mean()) if a.size > 1 and a.mean() > 0 else float("nan")

    return {ph: {"cv_q_j": cv(v["q_j"]), "cv_c_idx": cv(v["c_idx"]), "windows": len(v["q_j"])}
            for ph, v in values.items()}
