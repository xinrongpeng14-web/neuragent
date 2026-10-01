"""Run the comparison arms through the same environment and log every step.

Each arm is a policy (see gaproto/policies.py). All arms of one seed share the
episode seed, so they see the same YCSB key sequence and JOB query order. Arms
are run alternately (arm order rotates from episode to episode) so that a slow
drift of the machine affects all arms alike.

Usage:
  python -m gaproto.evaluate --config config/imdb.json --run-name eval \\
      --arm none=fixed:off/default --arm nqo=original --arm selix=fixed:off/dense \\
      --arm both=fixed:auto/dense --arm ga=ppo:runs/imdb/train/model.zip \\
      --seeds 2001,2002,2003 --episodes-per-seed 2

Output: <log_dir>/<run_name>/steps.jsonl (every step, with the arm in "tag")
        <log_dir>/<run_name>/episodes.json (one summary per episode)
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from . import actions as A
from .config import load_config
from .env import make_env
from .policies import make_policy


def parse_arm(text: str) -> Tuple[str, str]:
    name, sep, spec = text.partition("=")
    if not sep or not name.strip() or not spec.strip():
        raise argparse.ArgumentTypeError(f"arm {text!r} must be <name>=<policy spec>")
    return name.strip(), spec.strip()


def episode_plan(arms: List[str], seeds: List[int], episodes_per_seed: int,
                 alternate: bool) -> List[Tuple[str, int, int]]:
    plan = []
    k = 0
    for rep in range(episodes_per_seed):
        for seed in seeds:
            order = arms[k % len(arms):] + arms[:k % len(arms)] if alternate else list(arms)
            k += 1
            for name in order:
                plan.append((name, seed, rep))
    return plan


def summarize_episode(name: str, seed: int, rep: int, records: List[Dict[str, Any]],
                      wall_s: float) -> Dict[str, Any]:
    rewards = [r["reward"] for r in records]
    acts = [r["action"] for r in records]
    by_phase: Dict[str, Dict[str, Any]] = {}
    for ph in sorted({r["phase"] for r in records}):
        rs = [r for r in records if r["phase"] == ph]
        m = [r["metrics"] for r in rs]
        sampled = sum(x["idx_sampled"] for x in m)
        by_phase[ph] = {
            "steps": len(rs),
            "mean_reward": float(np.mean([r["reward"] for r in rs])),
            "job_done": int(sum(len(x["job_completed"]) for x in m)),
            "ycsb_ops_per_s": float(sum(x["ycsb_ops"] if "ycsb_ops" in x else
                                        x["ycsb_reads"] + x["ycsb_inserts"] for x in m)
                                    / max(1e-9, sum(x["elapsed_s"] for x in m))),
            "c_idx_ns": (sum(x["idx_sampled_ns"] for x in m) / sampled) if sampled else None,
            "idx_mem_mb": m[-1]["idx_mem_bytes"] / 1048576,
            "actions": {"/".join(A.decode(a)): int(n) for a, n in
                        zip(*np.unique([r["action"] for r in rs], return_counts=True))},
        }
    return {"arm": name, "seed": seed, "repeat": rep, "steps": len(records),
            "return": float(np.sum(rewards)), "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
            "switches": int(sum(r["switched"] for r in records)),
            "actions": {"/".join(A.decode(a)): int(n) for a, n in zip(*np.unique(acts, return_counts=True))},
            "phases": by_phase, "wall_s": round(wall_s, 1),
            "ended_early": bool(records and records[-1].get("terminated"))}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Evaluate comparison arms on the live environment")
    ap.add_argument("--config", required=True)
    ap.add_argument("--refs", default="", help="reference values (default: refs_path of the config)")
    ap.add_argument("--run-name", default="eval")
    ap.add_argument("--arm", action="append", type=parse_arm, required=True,
                    help="<name>=<policy spec>, repeatable")
    ap.add_argument("--seeds", default="2001,2002,2003")
    ap.add_argument("--episodes-per-seed", type=int, default=1)
    ap.add_argument("--sequential", action="store_true", help="do not rotate the arm order")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    refs_path = args.refs or cfg.refs_path
    if not os.path.isfile(refs_path):
        print(f"warning: references {refs_path} not found; rewards will be 0", flush=True)
        refs_path = None
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    names = [n for n, _ in args.arm]
    if len(set(names)) != len(names):
        raise SystemExit("arm names must be unique")
    policies = {n: make_policy(spec, seed=seeds[0]) for n, spec in args.arm}

    run_dir = os.path.join(cfg.log_dir, args.run_name)
    os.makedirs(run_dir, exist_ok=True)
    episodes_path = os.path.join(run_dir, "episodes.json")
    episodes: List[Dict[str, Any]] = []
    if os.path.isfile(episodes_path):
        with open(episodes_path, encoding="utf-8") as f:
            episodes = json.load(f)

    plan = episode_plan(names, seeds, args.episodes_per_seed, not args.sequential)
    print(f"{len(plan)} episodes: " + ", ".join(f"{n}@{s}" for n, s, _ in plan), flush=True)
    for n in names:
        print(f"  arm {n}: {policies[n].describe()}")

    env = make_env(cfg, refs_path=refs_path, run_name=args.run_name)
    try:
        for i, (name, seed, rep) in enumerate(plan):
            policy = policies[name]
            env.set_tag(name)
            t0 = time.time()
            obs, info = env.reset(seed=seed)
            print(f"\n[{i + 1}/{len(plan)}] arm {name}, seed {seed}, repeat {rep}: index of "
                  f"{info['index_keys']} keys built in {info['index_build_s']:.2f} s", flush=True)
            records: List[Dict[str, Any]] = []
            done = False
            while not done:
                phase = cfg.phases[cfg.phase_of_step(env.step_idx)].name
                action = policy.act(obs, phase)
                obs, reward, terminated, truncated, info = env.step(action)
                info["terminated"] = terminated
                records.append(info)
                done = terminated or truncated
                m = info["metrics"]
                c_idx = m["c_idx_ns"]
                print(f"  step {info['step']:3d} {info['phase']}  {info['nqo_mode']:>4}/{info['selix_preset']:<7} "
                      f"r {reward:+.3f}  job {len(m['job_completed']):3d}  ycsb {m['ycsb_ops_per_s']:6.0f}/s  "
                      f"c_idx {0 if c_idx is None else c_idx:6.0f} ns  mem {m['idx_mem_bytes'] / 1048576:6.1f} MB  "
                      f"cpu {m['cpu_util']:.2f}", flush=True)
            summary = summarize_episode(name, seed, rep, records, time.time() - t0)
            episodes.append(summary)
            with open(episodes_path, "w", encoding="utf-8") as f:
                json.dump(episodes, f, indent=2)
            print(f"  -> return {summary['return']:+.2f}, mean reward {summary['mean_reward']:+.3f}, "
                  f"{summary['switches']} switches, actions {summary['actions']}", flush=True)
    finally:
        env.close()
    print(f"\n{len(episodes)} episode summaries in {episodes_path}; steps in {run_dir}/steps.jsonl")


if __name__ == "__main__":
    main()
