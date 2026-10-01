"""Measure the original system and derive the reference values of the reward.

The original system is NQO in auto mode with a frozen model and SELIX with its
default densities. It is run through the same environment as the agent, with
the action fixed, so that both are measured in exactly the same way.

Usage:
  python -m gaproto.baseline --config cfg.json --episodes 3
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

from . import actions as A
from . import metrics as M
from .config import load_config
from .env import make_env


def run(cfg, episodes: int, seed: int, run_name: str) -> List[Dict[str, Any]]:
    env = make_env(cfg, refs_path=None, run_name=run_name, fixed_action=A.ORIGINAL_ACTION)
    env.set_tag("original")
    records: List[Dict[str, Any]] = []
    try:
        for ep in range(episodes):
            _, info = env.reset(seed=seed + ep)
            print(f"[episode {ep}] reload {info['reload_s']:.1f} s, index of "
                  f"{info['index_keys']} keys built in {info['index_build_s']:.2f} s", flush=True)
            done = False
            while not done:
                _, _, terminated, truncated, info = env.step(A.ORIGINAL_ACTION)
                done = terminated or truncated
                m = info["metrics"]
                records.append({"episode": ep, "step": info["step"], "phase": info["phase"],
                                "metrics": m})
                c_idx = m["c_idx_ns"]
                print(f"  step {info['step']:3d} phase {info['phase']}  cpu {m['cpu_util']:.2f}  "
                      f"job {len(m['job_completed']):4d} done p99 {m['job_p99_s'] * 1000:7.1f} ms  "
                      f"ycsb {m['ycsb_ops_per_s']:7.0f}/s  "
                      f"c_idx {c_idx if c_idx is None else round(c_idx):>6} ns  "
                      f"mem {m['idx_mem_bytes'] / 1048576:6.1f} MB  smo {m['idx_smo']}", flush=True)
                if terminated:
                    print("  YCSB connection lost, episode ended early", flush=True)
    finally:
        env.close()
    return records


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Measure the original system and write the references")
    ap.add_argument("--config", required=True)
    ap.add_argument("--episodes", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--run-name", default="baseline")
    ap.add_argument("--out", default="", help="where to write the references (default: refs_path of the config)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    records = run(cfg, args.episodes, args.seed, args.run_name)
    if not records:
        raise SystemExit("no step was measured")

    refs = M.build_refs(records, meta={"episodes": args.episodes, "seed": args.seed,
                                       "step_s": cfg.step_s,
                                       "phases": [p.name for p in cfg.phases]})
    out = args.out or cfg.refs_path
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    refs.save(out)

    print(f"\nreferences written to {out}")
    print(f"  templates with a baseline latency: {len(refs.base_lat)}")
    for ph in refs.q_j_ref:
        c = refs.c_ref_ns.get(ph)
        print(f"  phase {ph}: q_J_ref = {refs.q_j_ref[ph]:.3f}   "
              f"c_ref = {'n/a' if c is None else f'{c:.0f} ns'}")
    print("\nnoise check (coefficient of variation; the step length is long enough "
          "when both stay below 0.10)")
    for window in (1, 2):
        report = M.noise_report(records, refs, window)
        for ph, r in report.items():
            print(f"  window {window * cfg.step_s:5.0f} s  phase {ph}: "
                  f"q_J {r['cv_q_j']:.3f}   c_idx {r['cv_c_idx']:.3f}   ({r['windows']} windows)")
        with open(os.path.join(cfg.log_dir, args.run_name, f"noise_w{window}.json"), "w") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
