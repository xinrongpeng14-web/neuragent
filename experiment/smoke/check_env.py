"""End-to-end check of the environment against a running NeurDB container.

1. gymnasium's environment checker
2. one episode with random actions; after every step the measurements are
   compared with what the action should have caused
"""
import argparse
import os
import sys

import numpy as np
from gymnasium.utils.env_checker import check_action_space, check_observation_space
from gymnasium.utils.passive_env_checker import env_reset_passive_checker, env_step_passive_checker

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto import actions as A  # noqa: E402
from gaproto import metrics as M  # noqa: E402
from gaproto.config import load_config  # noqa: E402
from gaproto.env import NQO_SETTING_NAMES, make_env  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--skip-checker", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if not args.skip_checker:
        # gymnasium's check_env also demands identical observations for identical
        # seeds and actions. Measurements of a running database never repeat
        # exactly, so only the interface checks are used.
        print("=== gymnasium interface checks ===", flush=True)
        env = make_env(cfg, cfg.refs_path, run_name="checker")
        try:
            check_observation_space(env.observation_space)
            check_action_space(env.action_space)
            env_reset_passive_checker(env, seed=args.seed)
            for action in (A.ORIGINAL_ACTION, A.encode("off", "mid")):
                env_step_passive_checker(env, action)
            check("spaces, reset() and step() follow the gymnasium interface", True)
        except Exception as e:
            check("spaces, reset() and step() follow the gymnasium interface", False,
                  f"{type(e).__name__}: {e}")
        finally:
            env.close()
        from gaproto.db import Admin as _Admin
        a = _Admin(cfg.db)
        left = a.fetchall("SELECT application_name, state FROM pg_stat_activity "
                          "WHERE application_name IN ('ga_job', 'ga_ycsb')")
        check("close: no workload connection left on the server", not left, str(left))
        a.close()

    print("=== one episode with random actions ===", flush=True)
    env = make_env(cfg, cfg.refs_path, run_name="random")
    rng = np.random.default_rng(args.seed)
    try:
        obs, info = env.reset(seed=args.seed)
        check("reset: observation inside the space", env.observation_space.contains(obs))
        check("reset: index holds every seed key", info["index_keys"] == cfg.ycsb.initial_keys,
              f"{info['index_keys']} keys, built in {info['index_build_s']:.2f} s, "
              f"table reloaded in {info['reload_s']:.2f} s")
        check("reset: original action in effect",
              (env.nqo_mode, env.selix_preset) == A.decode(A.ORIGINAL_ACTION))

        # visit every NQO mode and every preset at least once
        plan = [A.encode("off", "dense"), A.encode("hint", "mid"), A.encode("join", "default")]
        plan += [int(a) for a in rng.integers(0, A.NUM_ACTIONS, cfg.episode_steps)]
        total, done, step = 0.0, False, 0
        while not done:
            action = plan[step]
            mode, preset = A.decode(action)
            obs, reward, terminated, truncated, info = env.step(action)
            done = terminated or truncated
            m = info["metrics"]
            total += reward
            c_idx = m["c_idx_ns"]
            print(f" step {info['step']} phase {info['phase']} action {action:2d} = {mode:>4}/{preset:<7} "
                  f"reward {reward:+.3f} (job {info['r_job']:+.2f} cost {info['r_selix_cost']:+.2f} "
                  f"mem {info['r_selix_mem']:+.2f})  cpu {m['cpu_util']:.2f}  job {len(m['job_completed'])} "
                  f"ycsb {m['ycsb_ops_per_s']:.0f}/s  c_idx {0 if c_idx is None else c_idx:.0f} ns  "
                  f"mem {m['idx_mem_bytes'] / 1048576:.1f} MB  smo {m['idx_smo']}  "
                  f"nqo {m['nqo_requests']:.0f} req", flush=True)
            tag = f"step {info['step']}"
            check(f"{tag}: observation inside the space", env.observation_space.contains(obs))
            check(f"{tag}: reward is finite", bool(np.isfinite(reward)))
            check(f"{tag}: SELIX densities follow the action",
                  tuple(round(x, 4) for x in m["idx_density"]) == A.SELIX_PRESETS[preset],
                  str(m["idx_density"]))
            want = A.nqo_settings(mode)
            got = {n: env.admin.file_setting(n) for n in NQO_SETTING_NAMES}
            check(f"{tag}: server settings follow the action", got == want, str(got))
            check(f"{tag}: observation reports the action",
                  abs(obs[7] - A.NQO_MODES.index(mode) / 3) < 1e-6
                  and abs(obs[8] - A.SELIX_PRESET_NAMES.index(preset) / 2) < 1e-6)
            check(f"{tag}: every lookup found its key, no YCSB error",
                  m["ycsb_misses"] == 0 and m["ycsb_errors"] == 0,
                  f"misses {m['ycsb_misses']} errors {m['ycsb_errors']}")
            check(f"{tag}: index counters equal client counters",
                  m["idx_gets"] == m["ycsb_reads"] and m["idx_puts"] == m["ycsb_inserts"],
                  f"index {m['idx_gets']}/{m['idx_puts']} client {m['ycsb_reads']}/{m['ycsb_inserts']}")
            check(f"{tag}: JOB queries ran without error", m["job_errors"] == 0 and len(m["job_completed"]) > 0,
                  f"{len(m['job_completed'])} done, {m['job_errors']} errors")
            phase = cfg.phases[cfg.phase_of_step(info["step"])]
            check(f"{tag}: YCSB read ratio follows the phase",
                  abs((1 - m["ycsb_insert_ratio"]) - phase.ycsb_read_ratio) < 0.08,
                  f"measured {1 - m['ycsb_insert_ratio']:.2f}, phase {phase.ycsb_read_ratio}")
            if phase.ycsb_rate > 0:
                check(f"{tag}: YCSB rate limit holds", m["ycsb_ops_per_s"] <= phase.ycsb_rate * 1.1,
                      f"{m['ycsb_ops_per_s']:.0f}/s, limit {phase.ycsb_rate:.0f}/s")
            check(f"{tag}: NQO service reachable", m["nqo_reachable"])
            if mode == "off":
                check(f"{tag}: NQO off: at most the queries already under way reach the service",
                      m["nqo_requests"] <= phase.job_clients, f"{m['nqo_requests']:.0f} requests")
            else:
                check(f"{tag}: NQO on: the service is asked", m["nqo_requests"] > 0,
                      f"{m['nqo_requests']:.0f} requests")
            step += 1
        check("episode has the configured length", step == cfg.episode_steps, f"{step} steps")
        print(f" episode return {total:+.3f}", flush=True)
        admin = env.admin
    finally:
        env.close()

    from gaproto.db import Admin
    a = Admin(cfg.db)
    left = [n for n in NQO_SETTING_NAMES if a.file_setting(n) is not None]
    check("close: no NQO setting left in postgresql.auto.conf", not left, str(left))
    a.close()

    steps_log = os.path.join(cfg.log_dir, "random", "steps.jsonl")
    n_lines = sum(1 for _ in open(steps_log))
    check("step log written", n_lines >= cfg.episode_steps + 1, f"{n_lines} lines in {steps_log}")

    print(f"=== {'all checks passed' if not failures else str(len(failures)) + ' checks FAILED'} ===")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
