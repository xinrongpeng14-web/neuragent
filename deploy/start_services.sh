#!/bin/bash
# Start the database and the NQO service inside the container.
#
#     docker exec <container> bash /neuragent/deploy/start_services.sh
#
#   NQO_WORKERS   worker processes of the NQO service (each loads its own models)   default 4
#   NQO_DATABASE  database the service reads statistics from                          default imdb_ori
#   NQO_PORT      port                                                                default 8666
#   NQO_CACHE     1 = the service answers repeated queries from a decision cache       default 1
set -euo pipefail
if [ "$(id -u)" = 0 ]; then exec su neurdb -c "NQO_WORKERS=${NQO_WORKERS:-} NQO_DATABASE=${NQO_DATABASE:-} NQO_PORT=${NQO_PORT:-} NQO_CACHE=${NQO_CACHE:-} bash $0 $*"; fi
export PATH=/opt/neurdb/bin:$PATH
NQO_WORKERS=${NQO_WORKERS:-4}
NQO_DATABASE=${NQO_DATABASE:-imdb_ori}
NQO_PORT=${NQO_PORT:-8666}
NQO_CACHE=${NQO_CACHE:-1}
NQO_DIR=/opt/nqo/neurqo_frame
LOG=/data/nqo.log

if pg_isready -q; then
    echo "database already running"
else
    pg_ctl -D /data/pg -l /data/pg.log -w start > /dev/null && echo "database started"
fi
psql -X -q -d template1 -tAc "SELECT 1 FROM pg_database WHERE datname='$NQO_DATABASE'" | grep -q 1 ||
    { echo "database $NQO_DATABASE does not exist; run deploy/load_imdb.sh first" >&2; exit 1; }

if curl -s --max-time 2 "http://127.0.0.1:$NQO_PORT/health" > /dev/null; then
    echo "NQO service already running on port $NQO_PORT"
else
    cd $NQO_DIR
    PYTHON=/opt/venv/bin/python DATABASE=$NQO_DATABASE CACHE=$NQO_CACHE setsid nohup bash run_moqoe_prototype.sh "$NQO_WORKERS" "$NQO_PORT" > $LOG 2>&1 < /dev/null &
    printf "starting the NQO service with %s workers%s (loading the models) " "$NQO_WORKERS" "$([ "$NQO_CACHE" = 1 ] && echo ', decision cache on')"
    for i in $(seq 1 300); do
        if curl -s --max-time 2 "http://127.0.0.1:$NQO_PORT/health" > /dev/null; then echo " up after ${i}s"; break; fi
        if ! pgrep -f "[r]un.py --workers" > /dev/null; then echo; echo "service exited, see $LOG:"; tail -20 $LOG; exit 1; fi
        sleep 1; printf .
    done
    curl -s --max-time 2 "http://127.0.0.1:$NQO_PORT/health" > /dev/null || { echo "service not up after 300 s, see $LOG"; exit 1; }
fi
echo "NQO /stats: $(curl -s --max-time 2 http://127.0.0.1:$NQO_PORT/stats)"
echo "NQO memory (RSS, MB): $(ps -o rss= -C python | awk '{s+=$1} END {printf "%.0f", s/1024}')"
