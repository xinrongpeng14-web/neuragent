"""Run arms through the hierarchical environment: the baseline, the sweep and the evaluation.

  baseline  arm O only, no references; writes refs.json (median latency and result of every query)
  sweep     the nine fixed commands, one episode each; writes static_best.json
  eval      the arms of plan section 7, alternating, seeds x episodes

Usage (inside the container, in experiment/):
  python -m gaproto.hier.evaluate --config config/hier_long.json --mode baseline --episodes 3
  python -m gaproto.hier.evaluate --config config/hier_long.json --mode sweep
  python -m gaproto.hier.evaluate --config config/hier_long.json --mode eval \\
      --arm G=ppo:runs/hier_long/train/model.zip --arm O=original --arm static-best=static-best \\
      --arm none=fixed:off/cost --arm PG=fixed:off/btree --seeds 2001,2002,2003 --episodes-per-seed 2
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from . import commands as C
from .config import load_hier_config
from .env import make_env
from .policies import make_policy


def episode_plan(arms: List[str], seeds: List[int], episodes_per_seed: int) -> List[Tuple[str, int, int]]:
    """Arm order rotates from one (seed, repeat) to the next, so drift affects all arms alike."""
    plan, k = [], 0
    for rep in range(episodes_per_seed):
        for seed in seeds:
            order = arms[k % len(arms):] + arms[:k % len(arms)]
            k += 1
            plan += [(a, seed, rep) for a in order]
    return plan


def summarize(arm: str, seed: int, rep: int, records: List[Dict[str, Any]], wall_s: float) -> Dict[str, Any]:
    results = [r for rec in records for r in rec["results"]]
    lat = [r["net_s"] for r in results]
    cmds = collections.Counter(rec["command"] for rec in records)
    per_query: Dict[str, List[float]] = collections.defaultdict(list)
    for r in results:
        per_query[r["query"]].append(r["net_s"])
    return {"arm": arm, "seed": seed, "repeat": rep, "steps": len(records), "queries": len(results),
            "total_net_s": round(float(sum(lat)), 4),
            "total_wall_s": round(float(sum(r["wall_s"] for r in results)), 4),
            "return": float(sum(rec["reward"] for rec in records)),
            "mean_reward": float(np.mean([rec["reward"] for rec in records])) if records else 0.0,
            "p50_s": float(np.percentile(lat, 50)) if lat else None,
            "p99_s": float(np.percentile(lat, 99)) if lat else None,
            "wrong_results": sum(1 for r in results if r.get("correct") is False),
            "not_ok": sum(1 for r in results if r["status"] != "ok"),
            "switches": int(sum(rec["switched"] for rec in records)),
            "commands": dict(cmds), "per_query_s": {q: v for q, v in sorted(per_query.items())},
            "wall_s": round(wall_s, 1)}


def run_episodes(env, plan, policies) -> List[Dict[str, Any]]:
    out = []
    for i, (arm, seed, rep) in enumerate(plan):
        env.set_tag(arm)
        t0 = time.time()
        obs, _ = env.reset(seed=seed)
        records, done = [], False
        while not done:
            obs, reward, terminated, truncated, info = env.step(policies[arm].act(obs))
            records.append(info)
            done = terminated or truncated
            res = info["results"]
            print(f"  [{i + 1}/{len(plan)} {arm} seed {seed}] step {info['step']:3d} {info['command']:<12s} "
                  f"{' '.join('%s:%.2fs' % (r['query'], r['net_s']) for r in res)}  r {reward:+.3f}"
                  + ("  WRONG RESULT" if any(r.get("correct") is False for r in res) else ""), flush=True)
        s = summarize(arm, seed, rep, records, time.time() - t0)
        out.append(s)
        print(f"  -> {arm} seed {seed}: total {s['total_net_s']:.2f} s, mean reward {s['mean_reward']:+.3f}, "
              f"commands {s['commands']}, wrong results {s['wrong_results']}", flush=True)
    return out


def write_refs(path: str, episodes_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    lat: Dict[str, List[float]] = collections.defaultdict(list)
    res: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for rec in episodes_records:
        for r in rec["results"]:
            if r["status"] == "ok":
                lat[r["query"]].append(r["net_s"])
                res[r["query"]][r["result"]] += 1
    queries = {}
    for q in sorted(lat):
        queries[q] = {"net_s": statistics.median(lat[q]), "runs": len(lat[q]),
                      "result": res[q].most_common(1)[0][0], "distinct_results": len(res[q])}
    data = {"arm": C.ORIGINAL, "queries": queries}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return data


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Baseline, sweep and evaluation of the hierarchical GA")
    ap.add_argument("--config", required=True)
    ap.add_argument("--clients", type=int, default=0, help="override the config's clients (keep it equal in every stage)")
    ap.add_argument("--mode", required=True, choices=("baseline", "sweep", "eval"))
    ap.add_argument("--run-name", default="")
    ap.add_argument("--arm", action="append", default=[], help="<name>=<policy> (eval), repeatable")
    ap.add_argument("--seeds", default="2001,2002,2003")
    ap.add_argument("--episodes-per-seed", type=int, default=2)
    ap.add_argument("--episodes", type=int, default=3, help="baseline episodes")
    ap.add_argument("--sweep-seed", type=int, default=1001)
    args = ap.parse_args(argv)

    cfg = load_hier_config(args.config, {"clients": args.clients} if args.clients else None)
    run_name = args.run_name or args.mode
    os.makedirs(cfg.log_dir, exist_ok=True)

    if args.mode == "baseline":
        arms = {"O": make_policy("original")}
        plan = [("O", 1 + i, 0) for i in range(args.episodes)]
        refs_path = None
    elif args.mode == "sweep":
        arms = {C.name(c): make_policy(f"fixed:{C.name(c)}") for c in range(C.NUM_COMMANDS)}
        plan = [(a, args.sweep_seed, 0) for a in arms]
        refs_path = cfg.refs_path
    else:
        if not args.arm:
            raise SystemExit("eval needs at least one --arm")
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        arms = {}
        for text in args.arm:
            n, _, spec = text.partition("=")
            arms[n.strip()] = make_policy(spec, cfg.static_best_path, seeds[0])
        plan = episode_plan(list(arms), seeds, args.episodes_per_seed)
        refs_path = cfg.refs_path
    if refs_path and not os.path.isfile(refs_path):
        raise SystemExit(f"references {refs_path} not found: run --mode baseline first")

    print(f"{args.mode}: {len(plan)} episodes; arms: " + ", ".join(f"{n}={p.name}" for n, p in arms.items()),
          flush=True)
    env = make_env(cfg, refs_path, run_name)
    print(f"{env.episode_steps} steps per episode ({len(env.names)} queries x {cfg.repeats}, "
          f"{cfg.step_queries} per step)", flush=True)
    episodes_path = os.path.join(cfg.log_dir, run_name, "episodes.json")
    episodes: List[Dict[str, Any]] = []
    all_records: List[Dict[str, Any]] = []
    try:
        if args.mode == "baseline":
            # keep the step records for the references
            orig_step = env.step

            def recording_step(a):
                out = orig_step(a)
                all_records.append(out[4])
                return out
            env.step = recording_step
        episodes = run_episodes(env, plan, arms)
    finally:
        with open(episodes_path, "w", encoding="utf-8") as f:
            json.dump(episodes, f, indent=1)
        env.close()

    if args.mode == "baseline":
        data = write_refs(cfg.refs_path, all_records)
        unstable = [q for q, v in data["queries"].items() if v["distinct_results"] > 1]
        print(f"references of {len(data['queries'])} queries written to {cfg.refs_path}"
              + (f"; WARNING: results differ between runs for {unstable}" if unstable else ""))
    elif args.mode == "sweep":
        best = min(episodes, key=lambda e: e["total_net_s"])
        with open(cfg.static_best_path, "w", encoding="utf-8") as f:
            json.dump({"command": best["arm"], "total_net_s": best["total_net_s"],
                       "sweep": {e["arm"]: e["total_net_s"] for e in episodes}}, f, indent=1)
        for e in sorted(episodes, key=lambda e: e["total_net_s"]):
            print(f"  {e['arm']:<12s} {e['total_net_s']:9.2f} s  wrong results {e['wrong_results']}")
        print(f"static-best: {best['arm']} ({best['total_net_s']:.2f} s), written to {cfg.static_best_path}")
    print(f"episode summaries in {episodes_path}; steps in {os.path.join(cfg.log_dir, run_name, 'steps.jsonl')}")


if __name__ == "__main__":
    main()
