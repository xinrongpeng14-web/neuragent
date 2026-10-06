"""Does the SELIX index's memory affect the JOB queries?

Runs the read-heavy phase of the experiment twice through the same environment,
once with a small YCSB index and once with a large one, under the current
container memory limit, and compares JOB throughput and latency. When the index
is large enough to push IMDB pages out of the page cache, JOB slows down; that
is the coupling the Global Agent's memory term needs.

Usage (inside the container, in experiment/):
  python tools/mem_coupling.py --config config/imdb_r2.json --keys 1000000,20000000 --steps 6

Each size costs: seed table of that many keys (once), a reload and an index build
per run, then `steps` steps of the first phase under the original action.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto import actions as A  # noqa: E402
from gaproto.config import load_config, to_dict  # noqa: E402
from gaproto.env import GaEnv  # noqa: E402


def run_size(cfg_path, keys, steps, seed, log_dir):
    cfg = load_config(cfg_path, overrides={
        "ycsb": {**to_dict(load_config(cfg_path).ycsb), "initial_keys": keys,
                 "seed_table": f"ycsb_seed_{keys // 1000}k", "table": "ycsb"},
        "phases": [dict(to_dict(load_config(cfg_path).phases[0]), steps=steps)],
        "warmup_steps": 1,
        "log_dir": log_dir,
    })
    os.makedirs(os.path.join(log_dir, f"memcal_{keys}"), exist_ok=True)
    env = GaEnv(cfg, refs=None, fixed_action=A.encode("off", "default"),
                log_path=os.path.join(log_dir, f"memcal_{keys}", "steps.jsonl"))
    env.set_tag(f"memcal_{keys}")
    out = {"keys": keys, "steps": []}
    try:
        t0 = time.time()
        _, info = env.reset(seed=seed)
        out["prepare_s"] = round(time.time() - t0, 1)
        out["index_keys"] = info["index_keys"]
        done = False
        while not done:
            _, _, terminated, truncated, info = env.step(A.encode("off", "default"))
            done = terminated or truncated
            m = info["metrics"]
            out["steps"].append({"job_done": len(m["job_completed"]),
                                 "job_lat_mean_s": float(np.mean([l for _, l in m["job_completed"]])) if m["job_completed"] else None,
                                 "job_p99_s": m["job_p99_s"], "idx_mem_mb": m["idx_mem_bytes"] / 1048576,
                                 "container_mem_mb": m["container_mem_bytes"] / 1048576, "cpu_util": m["cpu_util"]})
            print(f"  keys {keys:>9d} step {info['step']:2d}: job {len(m['job_completed']):4d} done, "
                  f"mean {out['steps'][-1]['job_lat_mean_s'] or 0:.3f} s, index {m['idx_mem_bytes'] / 1048576:7.1f} MB, "
                  f"container {m['container_mem_bytes'] / 1048576:7.0f} MB", flush=True)
    finally:
        env.close()
    done_steps = [s["job_done"] for s in out["steps"]]
    out["job_done_per_step"] = float(np.mean(done_steps)) if done_steps else 0.0
    out["job_p99_s"] = float(np.mean([s["job_p99_s"] for s in out["steps"]])) if out["steps"] else None
    out["idx_mem_mb_end"] = out["steps"][-1]["idx_mem_mb"] if out["steps"] else None
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Calibrate the coupling between index memory and JOB throughput")
    ap.add_argument("--config", required=True)
    ap.add_argument("--keys", default="1000000,20000000", help="index sizes to compare (keys)")
    ap.add_argument("--steps", type=int, default=6, help="steps of the first phase per size")
    ap.add_argument("--seed", type=int, default=3001)
    ap.add_argument("--out", default="", help="JSON output (default: <log_dir>/memcal.json)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    sizes = [int(x) for x in args.keys.split(",") if x]
    log_dir = cfg.log_dir
    results = []
    for k in sizes:
        print(f"=== index of {k} keys ===", flush=True)
        results.append(run_size(args.config, k, args.steps, args.seed, log_dir))
    out = args.out or os.path.join(log_dir, "memcal.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    base = results[0]
    print("\nindex size -> JOB throughput (first phase, original NQO off / SELIX default)")
    print(f"{'keys':>10s} {'index MB':>9s} {'JOB done/step':>14s} {'vs smallest':>12s} {'JOB p99 s':>10s}")
    for r in results:
        rel = (r["job_done_per_step"] / base["job_done_per_step"] - 1) * 100 if base["job_done_per_step"] else 0.0
        print(f"{r['keys']:>10d} {r['idx_mem_mb_end'] or 0:>9.0f} {r['job_done_per_step']:>14.1f} {rel:>+11.1f}% {r['job_p99_s'] or 0:>10.2f}")
    if len(results) > 1:
        rel = (results[-1]["job_done_per_step"] / base["job_done_per_step"] - 1) * 100 if base["job_done_per_step"] else 0.0
        verdict = ("coupling present: the large index costs JOB throughput" if rel <= -5 else
                   "no measurable coupling: tighten the container memory (deploy/set_memory.sh) or use a larger index")
        print(f"\n{verdict} ({rel:+.1f}% at {results[-1]['keys']} keys)")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
