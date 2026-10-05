#!/bin/bash
# Ask the NQO service for its decision on every JOB query, for both experts.
# Runs inside the container as neurdb. Output: one JSON line per query and filter.
#   docker exec neurdb-ga bash /neuragent/prototype/r2/nqo_sweep.sh > nqo_sweep.jsonl
set -u
if [ "$(id -u)" = 0 ]; then exec su neurdb -c "bash $0 $*"; fi
cd /neuragent/experiment
for f in queries/job_all/*.sql; do
  q=$(basename "$f" .sql)
  for flt in join hint; do
    /opt/venv/bin/python - "$f" "$flt" "$q" <<'PY'
import json, sys, time, urllib.request
path, flt, q = sys.argv[1:4]
sql = open(path, encoding="utf-8").read().strip()
body = json.dumps({"sql": sql, "expert_filter": flt}).encode()
req = urllib.request.Request("http://127.0.0.1:8666/optimize", data=body, headers={"Content-Type": "application/json"})
t0 = time.perf_counter()
try:
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read().decode())
except Exception as e:
    d = {"error": str(e), "optimized_sql": sql, "optimization_applied": False, "expert_name": "?"}
rt = (time.perf_counter() - t0) * 1000
opt = d.get("optimized_sql", sql)
prefix = opt[: len(opt) - len(sql)].strip() if opt.endswith(sql) else "(rewritten)"
# HintPlanSel: arm 0 = everything switched back on = no change of plan
sets = [s.strip() for s in prefix.split(";") if s.strip().upper().startswith("SET")]
arm_on = sorted(s.split()[1] for s in sets if s.upper().endswith(" ON"))
arm_off = sorted(s.split()[1] for s in sets if s.upper().endswith(" OFF"))
no_change = bool(sets) and set(arm_on) == set(arm_off)
print(json.dumps({"query": q, "filter": flt, "expert": d.get("expert_name"), "applied": d.get("optimization_applied"),
                  "plan_changes": bool(d.get("optimization_applied")) and not no_change,
                  "action": prefix[:200], "opt_ms": d.get("opt_time_ms"), "round_trip_ms": round(rt, 1),
                  "error": d.get("error")}))
PY
  done
done
