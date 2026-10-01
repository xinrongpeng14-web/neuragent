"""Gymnasium environment of the Global Agent prototype.

One step applies a joint action (NQO mode, SELIX density preset), lets the
workload run for step_s seconds and returns the metrics of that interval.
An episode walks through the configured workload phases.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from . import actions as A
from . import metrics as M
from .cgroup import CgroupReader
from .config import Config, to_dict
from .db import Admin
from .job_driver import JobDriver, load_queries
from .nqo_client import delta as nqo_delta
from .nqo_client import fetch_stats
from .procstat import ProcSampler, summarize as proc_summarize
from .reset import load_lookup_keys, prepare_seed_table, reset_table
from .ycsb_driver import YcsbDriver

NQO_SETTING_NAMES = ("enable_molqo", "molqo.expert_filter")
# fields of /stats that are not counters
NQO_NON_COUNTERS = ("workers", "uptime_s")
PG_ACTIVITY_SQL = "SELECT pid, backend_type, application_name FROM pg_stat_activity"


class GaEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, cfg: Config, refs: Optional[M.Refs] = None,
                 fixed_action: Optional[int] = None, log_path: Optional[str] = None):
        """
        refs: reference values of the original system. Without them the reward
            is 0 and the observation leaves the two index ratios at 0; this is
            how the original system itself is measured.
        fixed_action: when set, every step uses this action whatever the agent
            passes to step().
        """
        super().__init__()
        self.cfg = cfg
        self.refs = refs
        self.fixed_action = fixed_action
        self.action_space = spaces.Discrete(A.NUM_ACTIONS)
        self.observation_space = spaces.Box(M.OBS_LOW, M.OBS_HIGH, (M.OBS_DIM,), np.float32)

        self.admin = Admin(cfg.db)
        prepare_seed_table(self.admin, cfg.ycsb)
        self.cgroup = CgroupReader(cfg.container.name, cfg.container.ncpus, cfg.container.cgroup_dir)
        # per-process accounting needs the container's own /proc
        self.procs = (ProcSampler(lambda: self.admin.fetchall(PG_ACTIVITY_SQL))
                      if cfg.container.proc_stats and not cfg.container.name else None)
        self.ycsb = YcsbDriver(cfg.db, cfg.ycsb, load_lookup_keys(self.admin, cfg.ycsb))
        self.job = JobDriver(cfg.db, cfg.job, load_queries(cfg.job.query_dir))

        self.episode = -1
        self.step_idx = 0
        self.phase_idx = -1
        self.nqo_mode = ""
        self.selix_preset = ""
        self.last_reset: Dict[str, Any] = {}
        # free-form label written into every log record (the arm or policy being run)
        self.tag = ""
        self._last_obs: Optional[np.ndarray] = None
        self._closed = False
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None
        self._write_log({"event": "open", "cgroup": self.cgroup.limits(),
                         "proc_stats": self.procs is not None})

    def set_tag(self, tag: str) -> None:
        self.tag = str(tag)

    # ------------------------------------------------------------------
    def _apply_phase(self, step: int) -> str:
        idx = self.cfg.phase_of_step(step)
        phase = self.cfg.phases[idx]
        if idx != self.phase_idx:
            self.ycsb.set_phase(phase.ycsb_read_ratio, phase.ycsb_rate)
            self.job.set_clients(phase.job_clients)
            self.phase_idx = idx
        return phase.name

    def _apply_action(self, action: int) -> bool:
        """Returns True when the action differs from the one in effect."""
        mode, preset = A.decode(action)
        changed = False
        if mode != self.nqo_mode:
            self.admin.set_server_settings(A.nqo_settings(mode))
            self.nqo_mode = mode
            changed = True
            time.sleep(self.cfg.nqo_settle_s)
        if preset != self.selix_preset:
            self.ycsb.set_density(A.SELIX_PRESETS[preset])
            self.selix_preset = preset
            changed = True
        return changed

    def _run_interval(self) -> M.StepMetrics:
        """Let the workload run for step_s seconds and measure it."""
        cg0 = self.cgroup.sample()
        pr0 = self.procs.sample() if self.procs else None
        nq0 = fetch_stats(self.cfg.nqo.stats_url, self.cfg.nqo.timeout_s)
        y0 = self.ycsb.snapshot()   # also clears the client counters
        self.job.snapshot()         # clears the completions collected so far
        t0 = time.monotonic()

        time.sleep(max(0.0, self.cfg.step_s - (time.monotonic() - t0)))

        y1 = self.ycsb.snapshot()
        j1 = self.job.snapshot()
        elapsed = time.monotonic() - t0
        cg1 = self.cgroup.sample()
        pr1 = self.procs.sample() if self.procs else None
        nq1 = fetch_stats(self.cfg.nqo.stats_url, self.cfg.nqo.timeout_s)

        m = M.StepMetrics(elapsed_s=elapsed)
        m.cpu_util = self.cgroup.cpu_util(cg0, cg1, elapsed)
        m.container_mem_bytes = cg1.memory_bytes
        M.fill_job(m, j1)
        M.fill_ycsb(m, y0, y1)
        m.nqo_reachable = nq0 is not None and nq1 is not None
        m.nqo_requests = nqo_delta(nq0, nq1, "requests")
        m.nqo_opt_time_ms = nqo_delta(nq0, nq1, "opt_time_ms")
        if nq0 and nq1:
            m.nqo_counters = {k: nqo_delta(nq0, nq1, k) for k in nq1 if k not in NQO_NON_COUNTERS}
        if pr0 is not None and pr1 is not None:
            summary = proc_summarize(pr0, pr1)
            m.proc_cpu_s = summary["cpu_s"]
            m.proc_rss_bytes = summary["rss_bytes"]
            m.proc_pss_bytes = summary["pss_bytes"]
            m.proc_count = summary["count"]
        return m

    def _write_log(self, record: Dict[str, Any]) -> None:
        if self._log:
            self._log.write(json.dumps(record) + "\n")
            self._log.flush()

    # ------------------------------------------------------------------
    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed)
        self.episode += 1
        episode_seed = int(self.np_random.integers(0, 2**31 - 1)) if seed is None else int(seed)
        if options and "episode_seed" in options:
            episode_seed = int(options["episode_seed"])

        # 1. stop the workload; the YCSB backend and its index go away
        self.job.stop()
        self.ycsb.stop()
        # 2. original NQO mode, fresh table without index
        mode, preset = A.decode(A.ORIGINAL_ACTION)
        self.admin.set_server_settings(A.nqo_settings(mode))
        self.nqo_mode = mode
        reload_s = reset_table(self.admin, self.cfg.ycsb)
        # 3. new YCSB connection builds the index with the default densities
        started = self.ycsb.start(episode_seed, A.SELIX_PRESETS[preset])
        self.selix_preset = preset

        self.step_idx = 0
        self.phase_idx = -1
        phase = self._apply_phase(0)
        self.job.start(episode_seed, self.cfg.phases[0].job_clients)

        m = M.StepMetrics()
        for _ in range(max(1, self.cfg.warmup_steps)):
            m = self._run_interval()

        # "episode_index" rather than "episode": stable-baselines3 reserves the info
        # key "episode" for its own episode statistics
        self.last_reset = {"episode_index": self.episode, "episode_seed": episode_seed,
                           "reload_s": reload_s, "index_build_s": started["build_s"],
                           "index_keys": int(started["stats"]["n_keys"])}
        obs = M.observation(m, self.refs, phase, 0, self.nqo_mode, self.selix_preset)
        info = {**self.last_reset, "phase": phase, "nqo_mode": self.nqo_mode,
                "selix_preset": self.selix_preset, "metrics": m.to_record()}
        self._last_obs = obs
        self._write_log({"event": "reset", "tag": self.tag, "episode": self.episode, **info,
                         "obs": obs.tolist()})
        return obs, info

    def step(self, action) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        if self.fixed_action is not None:
            action = self.fixed_action
        action = int(action)
        step = self.step_idx
        obs_in = self._last_obs      # what the agent saw when it chose this action
        phase = self._apply_phase(step)
        switched = self._apply_action(action)

        m = self._run_interval()

        if self.refs is not None:
            parts = M.compute_reward(m, self.refs, phase, step, switched, self.cfg.reward)
            reward = float(parts.reward)
            reward_info = {"r_job": parts.r_job, "r_selix_cost": parts.r_selix_cost,
                           "r_selix_mem": parts.r_selix_mem, "q_j": parts.q_j}
        else:
            reward, reward_info = 0.0, {}

        self.step_idx += 1
        terminated = not m.ycsb_alive     # the index is lost with the YCSB connection
        truncated = self.step_idx >= self.cfg.episode_steps
        next_phase = self.cfg.phases[self.cfg.phase_of_step(min(self.step_idx,
                                                                self.cfg.episode_steps - 1))].name
        obs = M.observation(m, self.refs, next_phase if not truncated else phase,
                            min(self.step_idx, self.cfg.episode_steps - 1),
                            self.nqo_mode, self.selix_preset)
        info = {"episode_index": self.episode, "step": step, "phase": phase, "action": action,
                "nqo_mode": self.nqo_mode, "selix_preset": self.selix_preset,
                "switched": int(switched), "reward": reward, **reward_info,
                "metrics": m.to_record()}
        self._last_obs = obs
        self._write_log({"event": "step", "tag": self.tag, "episode": self.episode, **info,
                         "obs_in": None if obs_in is None else obs_in.tolist(), "obs": obs.tolist(),
                         "terminated": terminated, "truncated": truncated})
        return obs, reward, terminated, truncated, info

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for driver in (self.job, self.ycsb):
            try:
                driver.close()
            except Exception:
                pass
        try:
            # leave the server as it was found: no NQO setting in postgresql.auto.conf
            self.admin.reset_server_settings(NQO_SETTING_NAMES)
        except Exception:
            pass
        self.admin.close()
        if self._log:
            self._log.close()


def make_env(cfg: Config, refs_path: Optional[str] = None, run_name: str = "run",
             fixed_action: Optional[int] = None) -> GaEnv:
    """Environment that logs every step to <log_dir>/<run_name>/steps.jsonl."""
    run_dir = os.path.join(cfg.log_dir, run_name)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(to_dict(cfg), f, indent=2)
    refs = M.Refs.load(refs_path) if refs_path else None
    return GaEnv(cfg, refs, fixed_action, os.path.join(run_dir, "steps.jsonl"))
