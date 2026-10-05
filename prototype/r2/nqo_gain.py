"""Does the plan NQO proposes run faster than the cost-based plan?

For every JOB query where an expert changed the plan (nqo_sweep.jsonl), run the
query with NQO off and with the expert's action applied the way nr_molqo applies
it (SET ... for the statement, or the hint prefix), and compare wall times.

Runs inside the container:
  /opt/venv/bin/python nqo_gain.py nqo_sweep.jsonl out.json [timeout_s] [max_base_s] [runs]
Every variant is run `runs` times, interleaved, and the minimum counts; the planner's cost estimate of each variant is recorded as well.
"""
import json
import sys
import time

import psycopg2

sweep_path, out_path = sys.argv[1], sys.argv[2]
timeout_s = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
QDIR = "/neuragent/experiment/queries/job_all"

import urllib.request

rows = [json.loads(l) for l in open(sweep_path, encoding="utf-8")]
changed = {}
for r in rows:
    if r.get("plan_changes"):
        changed.setdefault(r["query"], set()).add(r["filter"])


def ask_nqo(sql, flt):
    """The expert's full action for this query (the sweep file truncates it)."""
    body = json.dumps({"sql": sql, "expert_filter": flt}).encode()
    req = urllib.request.Request("http://127.0.0.1:8666/optimize", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode())
    opt = d.get("optimized_sql", sql)
    return opt[: len(opt) - len(sql)].strip() if opt.endswith(sql) and opt != sql else ""


by_query = {}
for q, filters in changed.items():
    sql = open(f"{QDIR}/{q}.sql", encoding="utf-8").read().strip()
    for flt in filters:
        action = ask_nqo(sql, flt)
        if action:
            by_query.setdefault(q, {})[flt] = action

conn = psycopg2.connect(host="127.0.0.1", dbname="imdb_ori", user="neurdb", options="-c enable_molqo=off")
conn.autocommit = True
cur = conn.cursor()
cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
cur.execute("LOAD 'pg_hint_plan'")


def timed(sql_list):
    """Run the statements in one transaction; SET LOCAL scopes the planner switches."""
    t0 = time.perf_counter()
    try:
        cur.execute("BEGIN")
        for s in sql_list:
            cur.execute(s)
        cur.fetchall()
        cur.execute("COMMIT")
        return time.perf_counter() - t0, "ok"
    except psycopg2.errors.QueryCanceled:
        _rollback()
        return timeout_s, "timeout"
    except psycopg2.Error as e:
        _rollback()
        return None, f"error: {str(e).splitlines()[0]}"


def _rollback():
    # autocommit mode: the BEGIN above opened a server-side transaction that
    # conn.rollback() does not know about, so end it explicitly
    try:
        cur.execute("ROLLBACK")
    except psycopg2.Error:
        pass


def plan_cost(sql_list):
    """Planner's total cost estimate of the last statement, with the SETs in effect."""
    try:
        cur.execute("BEGIN")
        for s in sql_list[:-1]:
            cur.execute(s)
        cur.execute("EXPLAIN (FORMAT JSON) " + sql_list[-1])
        cost = cur.fetchone()[0][0]["Plan"]["Total Cost"]
        cur.execute("COMMIT")
        return cost
    except psycopg2.Error:
        _rollback()
        return None


def best_of(sql_list, runs):
    times, status = [], "ok"
    for _ in range(runs):
        t, st = timed(sql_list)
        if t is None:
            return None, st
        times.append(t)
        if st == "timeout":
            status = "timeout"
    return min(times), status


# which queries to run: those whose baseline on this machine is at most --max-base-s
max_base_s = float(sys.argv[4]) if len(sys.argv) > 4 else 1e9
runs = int(sys.argv[5]) if len(sys.argv) > 5 else 2
try:
    base_lat = json.load(open("/neuragent/experiment/queries/job_fast/latencies.json"))["results"]
except Exception:
    base_lat = {}

results = []
for q in sorted(by_query):
    known = base_lat.get(q, {}).get("best_s")
    if max_base_s < 1e9 and (known is None or known > max_base_s):
        continue
    sql = open(f"{QDIR}/{q}.sql", encoding="utf-8").read().strip()
    variants = {"off": [sql]}
    for flt, action in by_query[q].items():
        if flt == "hint":
            sets = [s.strip().replace("SET ", "SET LOCAL ", 1) for s in action.split(";") if s.strip().upper().startswith("SET")]
            variants[flt] = sets + [sql]
        else:
            variants[flt] = [action + " " + sql]
    rec = {"query": q, "runs": runs}
    for name, stmts in variants.items():
        rec[f"{name}_cost"] = plan_cost(stmts)
    # interleave the variants so that cache warmth does not favour one of them
    for r in range(runs):
        for name, stmts in variants.items():
            t, st = timed(stmts)
            rec.setdefault(f"{name}_times", []).append(t)
            rec[f"{name}_status"] = st if rec.get(f"{name}_status", "ok") == "ok" else rec[f"{name}_status"]
    for name in variants:
        ts = [t for t in rec.get(f"{name}_times", []) if t is not None]
        rec[f"{name}_s"] = min(ts) if ts else None
        rec.setdefault(f"{name}_status", "not run")
    for flt, action in by_query[q].items():
        rec[f"{flt}_action"] = action
    results.append(rec)
    parts = [f"{q:5s}"] + [f"{n} {rec[f'{n}_s'] if rec[f'{n}_s'] is None else round(rec[f'{n}_s'], 2)}s/{rec[f'{n}_status']}/cost {rec[f'{n}_cost'] if rec[f'{n}_cost'] is None else round(rec[f'{n}_cost'])}" for n in variants]
    print("  ".join(parts), flush=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
print(f"{len(results)} queries compared; written to {out_path}")
