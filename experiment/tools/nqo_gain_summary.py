"""Net effect of an NQO expert on a whole query set, from nqo_plan_gain.py's output.

The per-query ratios of nqo_gain.md do not add up by themselves: a 50x win on a
60 s query and twenty 1.5x losses on 3 s queries can net out either way. This
tool sums seconds per pass over each query set (fast / long / all), counts the
expert's inference time against it, and lists the queries that dominate.

Usage (inside the container, in experiment/):
  python tools/nqo_gain_summary.py --gain runs/imdb_r2/nqo_gain.json \\
      --sets queries/job_fast,queries/job_long [--expert hint] [--max-long 40]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys


def load_set(path):
    names = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(path, "*.sql"))}
    lat = {}
    try:
        lat = json.load(open(os.path.join(path, "latencies.json"), encoding="utf-8")).get("results", {})
    except Exception:
        pass
    return names, lat


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sum the expert's effect over query sets")
    ap.add_argument("--gain", required=True, help="nqo_gain.json")
    ap.add_argument("--sets", default="queries/job_fast,queries/job_long")
    ap.add_argument("--expert", default="hint", help="hint or join")
    ap.add_argument("--max-long", type=float, default=40.0,
                    help="LONG_MAX used for the long set; wins above it are reported separately")
    args = ap.parse_args(argv)

    rows = json.load(open(args.gain, encoding="utf-8"))
    sets = {os.path.basename(p.rstrip("/")): load_set(p) for p in args.sets.split(",") if p}

    def membership(q):
        return [name for name, (names, _) in sets.items() if q in names]

    per_q = []
    for r in rows:
        e = r["experts"].get(args.expert)
        off = r.get("off_s")
        if off is None:
            continue
        if e is None:                       # expert leaves the plan: only the inference is paid
            exp_s, status, opt = off, "unchanged", None
        else:
            exp_s, status, opt = e.get("s"), e.get("status"), e.get("opt_ms")
        if exp_s is None:
            continue
        per_q.append({"q": r["query"], "off": off, "exp": exp_s, "opt_s": (opt or 0.0) / 1000.0,
                      "status": status, "off_status": r.get("off_status", "ok"),
                      "sets": membership(r["query"]) or ["(not in a set)"]})

    def summarize(items, title):
        if not items:
            return
        off = sum(x["off"] for x in items)
        exp = sum(x["exp"] for x in items)
        opt = sum(x["opt_s"] for x in items)
        n_to = sum(1 for x in items if x["off_status"] == "timeout" or x["status"] == "timeout")
        print(f"\n{title}: {len(items)} queries" + (f" ({n_to} involve a timeout, lower bounds)" if n_to else ""))
        print(f"  one pass, cost-based plans:            {off:9.1f} s")
        print(f"  one pass, expert plans:                {exp:9.1f} s  ({(exp / off - 1) * 100:+.1f}%)")
        print(f"  + inference once per query:            {exp + opt:9.1f} s  ({((exp + opt) / off - 1) * 100:+.1f}%)")
        changed = [x for x in items if x["status"] != "unchanged"]
        print(f"  plan changed on {len(changed)} queries; biggest effects in seconds per pass:")
        for x in sorted(changed, key=lambda x: x["exp"] - x["off"])[:5]:
            print(f"    {x['q']:5s} {x['off']:8.2f} -> {x['exp']:8.2f} s  ({x['exp'] - x['off']:+8.2f})  {'/'.join(x['sets'])}")
        for x in sorted(changed, key=lambda x: x["off"] - x["exp"])[:5]:
            d = x["exp"] - x["off"]
            if d > 0:
                print(f"    {x['q']:5s} {x['off']:8.2f} -> {x['exp']:8.2f} s  ({d:+8.2f})  {'/'.join(x['sets'])}")

    for name in sets:
        summarize([x for x in per_q if name in x["sets"]], f"set {name}")
    summarize(per_q, "all measured queries")

    # where do the wins live relative to the long-set cut-off?
    wins = [x for x in per_q if x["status"] != "unchanged" and x["exp"] < 0.8 * x["off"]]
    above = [x for x in wins if x["off"] > args.max_long]
    print(f"\nexpert wins (>= 20% faster): {len(wins)}; of these {len(above)} have a cost-based baseline above "
          f"LONG_MAX={args.max_long:.0f} s and are therefore NOT in the long set: "
          + (", ".join(f"{x['q']} ({x['off']:.0f} s -> {x['exp']:.1f} s)" for x in above) or "none"))
    if above:
        need = max(x["off"] for x in above)
        print(f"  to include them: LONG_MAX >= {need:.0f} and job.statement_timeout_ms >= {int(need * 2.5 * 1000)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
