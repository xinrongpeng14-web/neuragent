#!/bin/bash
# Check the installation inside the container before running the experiment.
#
#     docker exec <container> bash /neuragent/deploy/check_deploy.sh
#
# Needs the services running (deploy/start_services.sh). Every check prints
# PASS or FAIL; the script exits non-zero when any check failed.
set -uo pipefail
if [ "$(id -u)" = 0 ]; then exec su neurdb -c "bash $0 $*"; fi
export PATH=/opt/neurdb/bin:$PATH
ROOT=/neuragent
PY=/opt/venv/bin/python
NQO=http://127.0.0.1:8666
DB=imdb_ori
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; pass=$((pass+1)); else echo "  FAIL  $1  (got: $2, want: $3)"; fail=$((fail+1)); fi; }
q() { psql -X -q -At -d "$1" -c "$2" 2>&1 | tr '\n' '/' | sed 's#/$##'; }

echo "=== database ==="
check "D1 server answers" "$(pg_isready -q && echo up)" "up"
check "D2 server is the NeurDB build (PostgreSQL 16 fork)" "$(q template1 'SELECT version()' | cut -d' ' -f1)" "NeurDB"
echo "  INFO  $(q template1 'SELECT version()' | cut -c1-90)"
check "D3 preload order pg_hint_plan, nr_molqo, nram" "$(q template1 'SHOW shared_preload_libraries')" "pg_hint_plan, nr_molqo, nram"
check "D4 database $DB exists" "$(q template1 "SELECT 1 FROM pg_database WHERE datname='$DB'")" "1"
check "D5 21 IMDB tables" "$(q $DB "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN ('aka_name','aka_title','cast_info','char_name','comp_cast_type','company_name','company_type','complete_cast','info_type','keyword','kind_type','link_type','movie_companies','movie_info','movie_info_idx','movie_keyword','movie_link','name','person_info','role_type','title')")" "21"
check "D6 title has 2528312 rows" "$(q $DB 'SELECT count(*) FROM title')" "2528312"
check "D7 extensions nram, nr_molqo" "$(q $DB "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('nram','nr_molqo')")" "nr_molqo,nram"
check "D8 molqo_status() lists 5 settings" "$(q $DB 'SELECT count(*) FROM molqo_status()')" "5"
check "D9 parallel query off, jit off" "$(q $DB 'SHOW max_parallel_workers_per_gather')/$(q $DB 'SHOW jit')" "0/off"

echo "=== SELIX (nrindex) in one session ==="
out=$(psql -X -q -At -d $DB -v ON_ERROR_STOP=1 2>&1 <<'SQL'
SET client_min_messages = warning;
DROP TABLE IF EXISTS ga_check;
CREATE TABLE ga_check (k int8 NOT NULL, v text);
INSERT INTO ga_check SELECT g * 7919, 'v' FROM generate_series(1, 1000) g;
SET selix.init_density = 0.85; SET selix.max_density = 0.95; SET selix.min_density = 0.75;
CREATE INDEX ga_check_k ON ga_check USING nrindex (k);
SET enable_seqscan = off; SET enable_bitmapscan = off;
PREPARE g(bigint) AS SELECT v FROM ga_check WHERE k = $1;
SELECT count(*) FROM (SELECT g FROM generate_series(1, 1000) g) s WHERE (SELECT v FROM ga_check WHERE k = (s.g * 7919)::int8) = 'v';
EXECUTE g(7919);
INSERT INTO ga_check VALUES (123456789012, 'new');
EXECUTE g(123456789012);
SELECT n_get >= 1001, n_put >= 1, n_keys, init_density, max_density, min_density FROM nrindex_stats();
DROP TABLE ga_check;
SQL
)
check "S1 1000 point lookups through nrindex return the right rows" "$(echo "$out" | sed -n 1p)" "1000"
check "S2 lookup of one key" "$(echo "$out" | sed -n 2p)" "v"
check "S3 insert then lookup of a new key" "$(echo "$out" | sed -n 3p)" "new"
check "S4 nrindex_stats counters and dense preset in effect" "$(echo "$out" | sed -n 4p)" "t|t|1001|0.85|0.95|0.75"

echo "=== NQO service ==="
check "N1 /health" "$(curl -s --max-time 3 $NQO/health | $PY -c 'import sys,json; print(json.load(sys.stdin)["status"])' 2>&1)" "ok"
workers=$(curl -s --max-time 3 $NQO/stats | $PY -c 'import sys,json; print(json.load(sys.stdin)["workers"])' 2>&1)
check "N2 /stats reports workers" "$([ "$workers" -ge 1 ] 2>/dev/null && echo yes)" "yes"
probe=$ROOT/experiment/queries/job_all/1a.sql      # a fast query: HintPlanSel answers, JoinOrder usually declines
heavy=$ROOT/experiment/queries/job_all/19d.sql     # a slow query: JoinOrder may give a Leading hint
if [ -f "$probe" ]; then
    out=$(cd $ROOT/experiment && $PY tools/nqo_probe.py --sql-file "$probe" --filters hint,join 2>&1); rc=$?
    echo "$out" | sed 's/^/        /'
    check "N3 both experts answer query 1a without error" "$rc" "0"
    # Whether JoinOrder hints is the expert's decision (its KNN gate depends on the
    # latencies recorded on the authors' machine), not a property of the deployment:
    # reported, not judged.
    out=$(cd $ROOT/experiment && $PY tools/nqo_probe.py --sql-file "$heavy" --filters join --json 2>&1); rc=$?
    check "N4 JoinOrder answers query 19d without error" "$rc" "0"
    echo "  INFO  JoinOrder on query 19d: $(echo "$out" | grep -q 'action: /\*+Leading' && echo "Leading hint given" || echo "no hint (declined; expected on a machine the experts were not calibrated on)")"
else
    echo "  SKIP  N3-N4 (no JOB queries yet; run deploy/load_imdb.sh)"
fi

echo "=== end to end: a JOB query planned through nr_molqo ==="
# The query is executed, not EXPLAINed: nr_molqo sends the whole statement text to the
# service, and the experts cannot work on a text that starts with EXPLAIN.
if [ -f "$probe" ]; then
    sql=$(cat "$probe")
    PRE="LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET client_min_messages = log; SET molqo.report_decisions = on; SET statement_timeout = 300000;"
    out=$(psql -X -q -At -d $DB -c "$PRE SET enable_molqo = on; SET molqo.expert_filter = 'join';" -c "$sql" 2>&1)
    check "E1 query 1a: nr_molqo reports the JoinOrder decision" "$(echo "$out" | grep -c 'MoLQO: sql -> moqoe -> JoinOrder')" "1"
    echo "  INFO  query 1a, JoinOrder: $(echo "$out" | grep -q 'optimization applied (hint)' && echo 'hint applied' || echo 'no hint (expert declined, expected for a fast query)')"
    out=$(psql -X -q -At -d $DB -c "$PRE SET enable_molqo = on; SET molqo.expert_filter = 'hint';" -c "$sql" -c "SHOW enable_nestloop" -c "SHOW enable_hashjoin" 2>&1)
    check "E2 query 1a, HintPlanSel: SET format applied for the statement" "$(echo "$out" | grep -c 'optimization applied ([0-9]* settings)')" "1"
    check "E3 planner settings back to on after the statement" "$(echo "$out" | tail -2 | tr -d ' ' | tr '\n' '/')" "on/on/"
    # The Leading-hint path through pg_hint_plan is exercised with a hint written by hand,
    # so that the check does not depend on the JoinOrder expert's decision.
    plain=$(psql -X -q -At -d $DB -c "SET enable_molqo = off;" -c "EXPLAIN (COSTS OFF) $sql" 2>&1 | md5sum | cut -c1-8)
    hinted=$(psql -X -q -At -d $DB -c "SET enable_molqo = off;" -c "EXPLAIN (COSTS OFF) /*+ Leading(mc ct) */ $sql" 2>&1 | md5sum | cut -c1-8)
    check "E4 pg_hint_plan applies a hand-written Leading hint (plan changes)" "$([ "$plain" != "$hinted" ] && echo yes)" "yes"
else
    echo "  SKIP  E1-E4 (no JOB queries yet)"
fi

echo "=== resource accounting ==="
check "R1 cgroup v2 cpu.stat readable" "$([ -r /sys/fs/cgroup/cpu.stat ] && grep -c usage_usec /sys/fs/cgroup/cpu.stat)" "1"
check "R2 memory.current readable" "$([ -r /sys/fs/cgroup/memory.current ] && echo yes)" "yes"
echo "  INFO  cpu.max: $(cat /sys/fs/cgroup/cpu.max 2>/dev/null)  memory.max: $(cat /sys/fs/cgroup/memory.max 2>/dev/null)  cores detected: $(cd $ROOT/experiment && $PY -c 'from gaproto.cgroup import CgroupReader; print(CgroupReader().ncpus)')"
pgpid=$(pgrep -o -u neurdb postgres)
check "R3 PSS of a database process readable" "$(grep -c '^Pss:' /proc/$pgpid/smaps_rollup 2>/dev/null)" "1"

echo "=== python environment ==="
check "P1 imports" "$($PY -c 'import torch, gymnasium, stable_baselines3, psycopg2, numpy, psqlparse, pglast; print("ok")' 2>&1 | tail -1)" "ok"
check "P2 unit tests" "$(cd $ROOT/experiment && $PY -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -c '^OK')" "1"

echo
echo "=== $pass passed, $fail failed ==="
[ $fail = 0 ]
