"""Split the JOB queries by how long the original system takes for them.

Every query of --job-dir is run on the database with NQO switched off (the
cost-based optimizer), --runs times; the fastest run counts. Queries at or
below --threshold seconds are copied to --out (the "fast" set); with
--out-long, queries whose baseline lies between --long-min (default: the
threshold) and --long-max seconds are copied there (the "long" set). Latencies go to <out>/latencies.json
and <out-long>/latencies.json.

Usage (inside the container, in experiment/):
  python tools/select_job_queries.py --config config/imdb_r2.json --job-dir queries/job_all \\
      --out queries/job_fast --threshold 1.0 --out-long queries/job_long --long-max 40
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto.config import load_config  # noqa: E402
from gaproto.db import connect  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Select the fast subset of the JOB queries")
    ap.add_argument("--config", required=True)
    ap.add_argument("--job-dir", default="queries/job_all")
    ap.add_argument("--out", default="queries/job_fast")
    ap.add_argument("--threshold", type=float, default=1.0, help="seconds")
    ap.add_argument("--out-long", default="", help="directory for the queries above the threshold")
    ap.add_argument("--long-min", type=float, default=0.0,
                    help="shortest baseline (seconds) admitted to the long set (default: --threshold)")
    ap.add_argument("--long-max", type=float, default=40.0,
                    help="longest baseline (seconds) admitted to the long set")
    ap.add_argument("--runs", type=int, default=2, help="runs per query; the fastest counts")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="statement timeout in seconds (default: 10 x threshold)")
    ap.add_argument("--min-queries", type=int, default=10,
                    help="warn when fewer queries pass the threshold")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    files = sorted(glob.glob(os.path.join(args.job_dir, "*.sql")))
    if not files:
        print(f"no *.sql files in {args.job_dir}", file=sys.stderr)
        return 1
    timeout_s = args.timeout or (1.5 * args.long_max if args.out_long else 10 * args.threshold)
    conn = connect(cfg.db, "ga_tool", molqo_off=True)
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")

    results = {}
    print(f"{len(files)} queries, {args.runs} runs each, timeout {timeout_s:.0f} s, "
          f"threshold {args.threshold} s", flush=True)
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            sql = f.read().strip()
        times = []
        status = "ok"
        for _ in range(args.runs):
            t0 = time.perf_counter()
            try:
                cur.execute(sql)
                cur.fetchall()
                times.append(time.perf_counter() - t0)
            except psycopg2.errors.QueryCanceled:
                conn.rollback()
                status = "timeout"
                break
            except psycopg2.Error as e:
                conn.rollback()
                status = f"error: {str(e).splitlines()[0]}"
                break
        best = min(times) if times else None
        results[name] = {"best_s": best, "runs_s": times, "status": status}
        mark = "fast" if best is not None and best <= args.threshold else "    "
        print(f"  {name:6s} {mark}  {'-' if best is None else f'{best:8.3f} s'}  {status if status != 'ok' else ''}",
              flush=True)
    conn.close()

    fast = sorted(n for n, r in results.items() if r["best_s"] is not None and r["best_s"] <= args.threshold)
    os.makedirs(args.out, exist_ok=True)
    for old in glob.glob(os.path.join(args.out, "*.sql")):
        os.remove(old)
    for name in fast:
        shutil.copy(os.path.join(args.job_dir, name + ".sql"), os.path.join(args.out, name + ".sql"))
    with open(os.path.join(args.out, "latencies.json"), "w", encoding="utf-8") as f:
        json.dump({"threshold_s": args.threshold, "timeout_s": timeout_s, "runs": args.runs,
                   "selected": fast, "results": results}, f, indent=2)

    total = sum(results[n]["best_s"] for n in fast)
    print(f"\n{len(fast)} of {len(files)} queries at or below {args.threshold} s, copied to {args.out}; "
          f"one pass over them takes {total:.1f} s on one client")
    if args.out_long:
        long_min = args.long_min or args.threshold
        long_q = sorted(n for n, r in results.items()
                        if r["best_s"] is not None and long_min <= r["best_s"] <= args.long_max)
        os.makedirs(args.out_long, exist_ok=True)
        for old in glob.glob(os.path.join(args.out_long, "*.sql")):
            os.remove(old)
        for name in long_q:
            shutil.copy(os.path.join(args.job_dir, name + ".sql"), os.path.join(args.out_long, name + ".sql"))
        with open(os.path.join(args.out_long, "latencies.json"), "w", encoding="utf-8") as f:
            json.dump({"long_min_s": long_min, "long_max_s": args.long_max, "selected": long_q,
                       "results": {n: results[n] for n in long_q}}, f, indent=2)
        total_long = sum(results[n]["best_s"] for n in long_q)
        print(f"{len(long_q)} queries between {long_min} and {args.long_max} s, copied to {args.out_long}; "
              f"one pass over them takes {total_long:.1f} s on one client")
        if len(long_q) < 8:
            print(f"warning: only {len(long_q)} long queries; a phase built on them depends on very few queries",
                  file=sys.stderr)
    if len(fast) < args.min_queries:
        print(f"warning: fewer than {args.min_queries} queries; raise --threshold or check the machine",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
