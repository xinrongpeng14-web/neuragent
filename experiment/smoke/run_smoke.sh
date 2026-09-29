#!/bin/bash
# End-to-end check of the experiment program on a small stand-in workload.
#
# It needs a container that holds a NeurDB build with the prototype patches
# (see prototype/p1/setup_container.sh) and a data directory in /data/pg.
# The NQO service is replaced by a stand-in that answers in the same formats
# as the real experts, so this checks the plumbing, not the optimizer.
#
# Usage: smoke/run_smoke.sh [container]      KEEP=1 leaves the services running
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$(dirname "$ROOT")"
CONTAINER="${1:-neurdb-p1}"
PY="$PROJECT/.venv/bin/python"
RUN_PY="$PROJECT/NeuralDB/aiengine/neurqo_frame/run.py"
FAKE="$PROJECT/prototype/p2_p9/pytests/fake_controller.py"
OUT="$ROOT/runs/smoke"
PG=/opt/neurdb/bin

in_container() { docker exec "$CONTAINER" bash -c "$1"; }
as_db_user()   { docker exec "$CONTAINER" su neurdb -c "export PATH=$PG:\$PATH; $1"; }

DB_HOST="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTAINER")"
echo "container $CONTAINER at $DB_HOST"
mkdir -p "$OUT"
sed -e "s#@DB_HOST@#$DB_HOST#g" -e "s#@CONTAINER@#$CONTAINER#g" -e "s#@ROOT@#$ROOT#g" \
    "$ROOT/config/smoke.json" > "$OUT/config.json"

echo "--- database configuration"
as_db_user "pg_ctl -D /data/pg -m fast -w stop > /dev/null 2>&1 || true"
in_container "
  sed -i '/^# --- ga smoke begin/,/^# --- ga smoke end/d; /^shared_preload_libraries/d; /^compute_query_id/d' /data/pg/postgresql.conf
  cat >> /data/pg/postgresql.conf <<CONF
# --- ga smoke begin
shared_preload_libraries = 'pg_hint_plan, nr_molqo, nram'
listen_addresses = '*'
molqo.server_url = 'http://127.0.0.1:8666/optimize'
molqo.report_decisions = off
molqo.timeout_ms = 5000
# --- ga smoke end
CONF
  grep -q 'ga smoke' /data/pg/pg_hba.conf || echo 'host all all 172.16.0.0/12 trust  # ga smoke' >> /data/pg/pg_hba.conf
  : > /data/pg/postgresql.auto.conf; chown neurdb /data/pg/postgresql.auto.conf"
as_db_user "pg_ctl -D /data/pg -l /data/logfile -w start > /dev/null && pg_isready -q && echo database up"

echo "--- stand-in NQO service (2 workers)"
in_container "mkdir -p /data/nq && pkill -f '[r]un.py --port 8666' || true"
docker cp "$RUN_PY" "$CONTAINER:/data/nq/run.py"
docker cp "$FAKE" "$CONTAINER:/data/nq/fake_controller.py"
in_container "chown -R neurdb /data/nq"
docker exec -d "$CONTAINER" su neurdb -c "cd /data/nq && MOQOE_CONTROLLER_FACTORY=fake_controller:make \
  PYTHONPATH=/data/nq FAKE_REAL_FORMAT=1 python3 run.py --port 8666 --workers 2 --freeze --quiet \
  --torch-threads 0 > /data/nq/server.log 2>&1"
for i in $(seq 1 50); do
  curl -s --max-time 2 "http://$DB_HOST:8666/health" > /dev/null && break; sleep 0.2
done
curl -s --max-time 2 "http://$DB_HOST:8666/health" > /dev/null && echo "service up"

echo "--- stand-in JOB data"
docker cp "$ROOT/smoke/prepare.sql" "$CONTAINER:/data/nq/prepare.sql"
as_db_user "psql -d neurdb -X -q -v ON_ERROR_STOP=1 -f /data/nq/prepare.sql > /dev/null && echo loaded"

cd "$ROOT"
status=0
echo "--- seed table and reset script"
"$PY" -m gaproto.reset --config "$OUT/config.json" --prepare || status=1
echo "--- original system: 1 episode, writes the references"
"$PY" -m gaproto.baseline --config "$OUT/config.json" --episodes 1 --run-name baseline || status=1
echo "--- environment checks"
"$PY" smoke/check_env.py --config "$OUT/config.json" ${SKIP_CHECKER:+--skip-checker} || status=1

L=$(in_container "grep -c 'terminated by signal' /data/logfile || true")
echo "--- server log: $L lines with 'terminated by signal' in total (compare with the count before the run)"

if [ "${KEEP:-0}" != "1" ]; then
  in_container "pkill -f '[r]un.py --port 8666' || true"
  as_db_user "pg_ctl -D /data/pg -m fast -w stop > /dev/null" && echo "services stopped"
fi
exit $status
