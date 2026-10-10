"""Gymnasium environment of the hierarchical GA (plan v0.6).

Episode: the group's queries, each `repeats` times, in an order fixed by the
episode seed. Step: the GA's command is applied to every worker session, then
the next `step_queries` queries run (in parallel on the workers) and the step
ends when all of them have finished.

Reward (plan 5.4): R = log(L_ref / L) with L the mean over the step's queries
of latency / reference latency, the reference being the median latency of the
same query under the original system (arm O). Latency is the net time, without
SELIX builds (there are none after the warm-up, since the density never changes).
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from ..db import Admin
from ..job_driver import load_queries
from ..nqo_client import delta as nqo_delta
from ..nqo_client import fetch_stats
from . import commands as C
from . import features as F
from .config import HierConfig, to_dict
from .workers import WorkerPool, warmup_probes


class Refs:
    """Reference latency and result of every query under the original system."""

    def __init__(self, queries: Dict[str, Dict[str, Any]]):
        self.queries = queries

    def latency(self, name: str) -> Optional[float]:
        q = self.queries.get(name)
        return float(q["net_s"]) if q and q.get("net_s") else None

    def result(self, name: str) -> Optional[str]:
        q = self.queries.get(name)
        return q.get("result") if q else None

    @classmethod
    def load(cls, path: str) -> "Refs":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f)["queries"])


def step_reward(results: List[Dict[str, Any]], refs: Optional[Refs], lo: float, hi: float
                ) -> Tuple[float, Optional[float]]:
    """(reward, mean latency ratio); (0, None) without references."""
    if refs is None:
        return 0.0, None
    ratios = []
    for r in results:
        ref = refs.latency(r["query"])
        if ref:
            ratios.append(min(hi, max(lo, r["net_s"] / ref)))
    if not ratios:
        return 0.0, None
    mean = float(np.mean(ratios))
    return -math.log(mean), mean


def episode_sequence(names: List[str], repeats: int, seed: int) -> List[str]:
    rng = np.random.default_rng(seed)
    seq = [n for n in names for _ in range(repeats)]
    rng.shuffle(seq)
    return seq


class HierEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, cfg: HierConfig, refs: Optional[Refs] = None, log_path: Optional[str] = None,
                 fixed_command: Optional[int] = None):
        super().__init__()
        self.cfg = cfg
        self.refs = refs
        self.fixed_command = fixed_command
        self.action_space = spaces.Discrete(C.NUM_COMMANDS)
        self.observation_space = spaces.Box(F.OBS_LOW, F.OBS_HIGH, (F.OBS_DIM,), np.float32)
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None

        self.admin = Admin(cfg.db)
        self.catalog = F.Catalog.load(self.admin)
        self.catalog.selix_cols = {tc for tc in self.catalog.selix_cols if tc[0] in F.IMDB_TABLES}
        if not self.catalog.selix_cols:
            raise SystemExit("no SELIX index on the IMDB tables: build them first (pipeline.sh selix_create)")
        self.queries: Dict[str, F.QueryInfo] = {}
        for name, sql in load_queries(cfg.query_dir):
            q = F.QueryInfo(name, sql, F.parse_query(sql))
            q.est_cost = F.native_cost(self.admin, sql)
            self.queries[name] = q
        self.names = sorted(self.queries)

        # only the SELIX columns this group's queries join on: every session holds its own copies
        used = set().union(*(q.parsed.join_cols for q in self.queries.values())) & self.catalog.selix_cols
        t0 = time.time()
        self.pool = WorkerPool(cfg)
        warm = self.pool.warm_up(warmup_probes(self.admin, used))
        warm_pass = []
        if cfg.warm_queries:
            # page cache and NQO decision cache as in a running system; not part of any result
            self.pool.apply(C.parse(C.ORIGINAL))
            warm_pass, _ = self.pool.run_batch([(n, self.queries[n].sql) for n in self.names])
        self._write({"event": "open", "config": to_dict(cfg), "queries": self.names,
                     "selix_columns": sorted(".".join(tc) for tc in self.catalog.selix_cols),
                     "selix_columns_loaded": sorted(".".join(tc) for tc in used),
                     "warmup": warm, "warm_pass": warm_pass, "warmup_s": round(time.time() - t0, 1)})
        print(f"{len(self.names)} queries, {len(self.catalog.selix_cols)} SELIX columns ({len(used)} used by the queries), "
              f"{cfg.workers} session(s) warmed up in {time.time() - t0:.0f} s"
              + (" (SELIX copies loaded, every query run once under arm O)" if cfg.warm_queries else ""), flush=True)

        self.tag = ""
        self.episode = -1
        self.seq: List[str] = []
        self.pos = 0
        self.step_idx = 0
        self.command = C.parse(C.ORIGINAL)
        self.last = F.LastStep()
        self._closed = False

    # ------------------------------------------------------------------
    def set_tag(self, tag: str) -> None:
        self.tag = str(tag)

    @property
    def episode_steps(self) -> int:
        return math.ceil(len(self.names) * self.cfg.repeats / self.cfg.step_queries)

    def _batch(self, pos: int) -> List[F.QueryInfo]:
        return [self.queries[n] for n in self.seq[pos:pos + self.cfg.step_queries]]

    def _obs(self) -> np.ndarray:
        return F.observation(self.command, self._batch(self.pos), self.catalog, self.cfg.group,
                             self.cfg.workers, self.last)

    def _write(self, record: Dict[str, Any]) -> None:
        if self._log:
            self._log.write(json.dumps(record) + "\n")
            self._log.flush()

    # ------------------------------------------------------------------
    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed)
        self.episode += 1
        episode_seed = int(self.np_random.integers(0, 2**31 - 1)) if seed is None else int(seed)
        self.seq = episode_sequence(self.names, self.cfg.repeats, episode_seed)
        self.pos = 0
        self.step_idx = 0
        self.command = C.parse(C.ORIGINAL)
        self.last = F.LastStep()
        obs = self._obs()
        info = {"episode_index": self.episode, "episode_seed": episode_seed, "sequence": self.seq}
        self._write({"event": "reset", "tag": self.tag, **info})
        return obs, info

    def step(self, action):
        command = int(self.fixed_command if self.fixed_command is not None else action)
        obs_in = self._obs()
        batch = self._batch(self.pos)
        switched = command != self.command
        self.pool.apply(command)
        self.command = command

        nq0 = fetch_stats(self.cfg.nqo.stats_url, self.cfg.nqo.timeout_s) if C.decode(command)[0] != "off" else None
        results, step_wall = self.pool.run_batch([(q.name, q.sql) for q in batch])
        nq1 = fetch_stats(self.cfg.nqo.stats_url, self.cfg.nqo.timeout_s) if nq0 is not None else None

        reward, mean_ratio = step_reward(results, self.refs, self.cfg.ratio_min, self.cfg.ratio_max)
        for r in results:
            ref_hash = self.refs.result(r["query"]) if self.refs else None
            r["correct"] = None if ref_hash is None or r["result"] is None else r["result"] == ref_hash
        total_wall = sum(r["wall_s"] for r in results)
        scans = sum(r["index_scans"] for r in results)
        self.last = F.LastStep(
            log_ratio=math.log(mean_ratio) if mean_ratio else 0.0,
            nqo_time_share=(nqo_delta(nq0, nq1, "opt_time_ms") / 1000.0 / total_wall) if nq0 and nq1 and total_wall else 0.0,
            nqo_applied_share=sum(r["nqo_action"] != "none" for r in results) / len(results),
            selix_scan_share=(sum(r["selix_scans"] for r in results) / scans) if scans else 0.0)

        step = self.step_idx
        self.step_idx += 1
        self.pos += len(batch)
        truncated = self.pos >= len(self.seq)
        obs = self._obs()
        info = {"episode_index": self.episode, "step": step, "command": C.name(command), "action": command,
                "switched": int(switched), "reward": reward, "mean_ratio": mean_ratio,
                "step_wall_s": round(step_wall, 4), "results": results,
                "nqo_requests": nqo_delta(nq0, nq1, "requests") if nq0 and nq1 else 0.0,
                "last": self.last.__dict__}
        self._write({"event": "step", "tag": self.tag, **info, "obs_in": obs_in.tolist(), "truncated": truncated})
        return obs, reward, False, truncated, info

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.pool.close()
        finally:
            self.admin.close()
            if self._log:
                self._log.close()


def make_env(cfg: HierConfig, refs_path: Optional[str] = None, run_name: str = "run",
             fixed_command: Optional[int] = None) -> HierEnv:
    run_dir = os.path.join(cfg.log_dir, run_name)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(to_dict(cfg), f, indent=2)
    refs = Refs.load(refs_path) if refs_path and os.path.isfile(refs_path) else None
    return HierEnv(cfg, refs, os.path.join(run_dir, "steps.jsonl"), fixed_command)
