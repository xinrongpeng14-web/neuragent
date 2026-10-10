"""Show the re-measured 26a cells of F4: latency without rebuilds per run, and
whether each result equals the result of off/btree (NQO off, btree only).

Usage: python3 prototype/r2/recheck_26a_view.py experiment/runs/imdb_r2/recheck_26a_f4_long.json
"""
import collections
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
recs = [r for r in d["records"] if r["query"] == "26a"]
ref = {r["result"] for r in recs if r["variant"] == "off/btree" and r["status"] == "ok"}
if len(ref) != 1:
    sys.exit(f"off/btree gave {len(ref)} distinct results: {ref}")
ref = ref.pop()

cells = collections.OrderedDict()
for r in recs:
    cells.setdefault(r["variant"], []).append(r)
bad = 0
print(f"{'variant':20s} {'run 1':>9s} {'run 2':>9s}  result   NQO action")
for v, rs in cells.items():
    rs.sort(key=lambda r: r["rep"])
    times = [f"{r['net_s']:8.3f}s" if r["status"] == "ok" else f"{r['status']:>9s}" for r in rs]
    same = all(r["status"] == "ok" and r["result"] == ref for r in rs)
    bad += not same
    print(f"{v:20s} {' '.join(times):>19s}  {'same' if same else 'DIFF':6s}   {','.join(r['nqo_action'] for r in rs)}")
n = len(recs)
print(f"\nall {n} executions return the same result as off/btree" if not bad
      else f"\n{bad} variant(s) differ from off/btree: stop and send this output")
