"""Is 26c's speed-up under the NQO plan real? Run sequentially (no concurrency):
each variant executes the query, records the result row and wall time, and the
plan's join order. Variants: cost-based, the HintPlanSel arm (mergejoin and
indexscan off), and each of the two switches alone. cost-based and the arm are
repeated at the end to expose cache-warmth effects."""
import json, sys, time
import psycopg2

sql = open("/neuragent/experiment/queries/job_all/26c.sql").read().strip()
VARIANTS = {
    "cost_based": [],
    "nqo_arm": ["SET LOCAL enable_mergejoin = off", "SET LOCAL enable_indexscan = off"],
    "indexscan_off_only": ["SET LOCAL enable_indexscan = off"],
    "mergejoin_off_only": ["SET LOCAL enable_mergejoin = off"],
}
ORDER = ["cost_based", "nqo_arm", "indexscan_off_only", "mergejoin_off_only", "cost_based", "nqo_arm"]

conn = psycopg2.connect(host="127.0.0.1", dbname="imdb_ori", user="neurdb", options="-c enable_molqo=off")
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET statement_timeout = 1800000")
out = []
for name in ORDER:
    sets = VARIANTS[name]
    cur.execute("BEGIN")
    for s in sets:
        cur.execute(s)
    cur.execute("EXPLAIN (COSTS OFF) " + sql)
    plan = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT blks_read, blks_hit FROM pg_stat_database WHERE datname = current_database()")
    b0 = cur.fetchone()
    t0 = time.perf_counter()
    cur.execute(sql)
    row = cur.fetchone()
    el = time.perf_counter() - t0
    cur.execute("COMMIT")
    cur.execute("SELECT pg_stat_clear_snapshot()")
    cur.execute("SELECT blks_read, blks_hit FROM pg_stat_database WHERE datname = current_database()")
    b1 = cur.fetchone()
    # join order: relations in the order they appear in the plan (outermost first)
    rels = [l.split(" on ")[1].split()[1] if " on " in l and len(l.split(" on ")[1].split()) > 1 else "" for l in plan if " Scan on " in l]
    rec = {"variant": name, "seconds": round(el, 2), "result": list(row),
           "blks_read": b1[0] - b0[0], "blks_hit": b1[1] - b0[1], "scan_order": [r for r in rels if r]}
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)
json.dump(out, open("/neuragent/prototype/r2/q26c_check.json", "w"), indent=1, ensure_ascii=False)
