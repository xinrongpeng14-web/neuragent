"""Show what the NQO service receives and what it answers for one query.

This makes the input state and the output action of NQO visible: the input is
the SQL text (the only thing the database sends), the output is either a
pg_hint_plan hint prefix (JoinOrder expert) or a list of SET statements
(HintPlanSel expert) in front of the unchanged SQL.

Usage:
  python tools/nqo_probe.py --sql-file queries/job_all/1a.sql
  python tools/nqo_probe.py --sql "SELECT ..." --filters hint,join --url http://127.0.0.1:8666
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def post(url: str, sql: str, expert_filter: str, timeout_s: float) -> dict:
    body = json.dumps({"sql": sql, "expert_filter": expert_filter}).encode("utf-8")
    req = urllib.request.Request(url + "/optimize", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode("utf-8") or "{}")
        data.setdefault("error", f"HTTP {e.code}")
    data["round_trip_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return data


def describe_action(original: str, optimized: str) -> str:
    """The part of the answer that differs from the input."""
    if optimized == original:
        return "(unchanged: cost-based optimizer)"
    if optimized.endswith(original):
        prefix = optimized[: len(optimized) - len(original)].strip()
        return prefix
    return optimized


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe the NQO service with one query")
    ap.add_argument("--url", default="http://127.0.0.1:8666")
    ap.add_argument("--sql", default="")
    ap.add_argument("--sql-file", default="")
    ap.add_argument("--filters", default="all,hint,join")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--json", action="store_true", help="print the raw responses")
    args = ap.parse_args(argv)

    if args.sql_file:
        with open(args.sql_file, encoding="utf-8") as f:
            sql = f.read().strip()
    elif args.sql:
        sql = args.sql.strip()
    else:
        ap.error("--sql or --sql-file is required")

    try:
        with urllib.request.urlopen(args.url + "/stats", timeout=5) as r:
            before = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"NQO service not reachable at {args.url}: {e}", file=sys.stderr)
        return 1

    print(f"input state sent by the database: SQL text, {len(sql)} characters")
    print("  " + " ".join(sql.split())[:160] + (" ..." if len(sql) > 160 else ""))
    failures = 0
    for flt in [x.strip() for x in args.filters.split(",") if x.strip()]:
        data = post(args.url, sql, flt, args.timeout)
        if args.json:
            print(json.dumps(data, indent=2))
        # an expert may legitimately leave the SQL unchanged (JoinOrder hints only
        # when it predicts the baseline above 100 ms and its order faster); only an
        # error or a missing expert counts as a failure
        ok = "error" not in data and data.get("expert_name") not in (None, "none")
        failures += 0 if ok else 1
        print(f"\nexpert_filter={flt}: expert {data.get('expert_name')}, applied {data.get('optimization_applied')}, "
              f"inference {data.get('opt_time_ms', '?')} ms, round trip {data['round_trip_ms']} ms"
              + (f", error: {data['error']}" if "error" in data else ""))
        action = describe_action(sql, data.get("optimized_sql", sql))
        for line in action.splitlines()[:12]:
            print("  action: " + line)
    with urllib.request.urlopen(args.url + "/stats", timeout=5) as r:
        after = json.loads(r.read().decode("utf-8"))
    print("\n/stats increments: " + ", ".join(
        f"{k} +{after[k] - before.get(k, 0):.0f}" for k in after
        if k not in ("workers", "uptime_s", "opt_time_ms") and after[k] - before.get(k, 0)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
