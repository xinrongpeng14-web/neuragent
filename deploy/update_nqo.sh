#!/bin/bash
# Refresh the NQO service's copy (/opt/nqo/neurqo_frame) from the patched source in
# /neuragent/NeuralDB after the patches changed, then restart the services.
# Runs inside the container:  docker exec <container> bash /neuragent/deploy/update_nqo.sh
set -euo pipefail
SRC=/neuragent/NeuralDB/aiengine/neurqo_frame
DST=/opt/nqo/neurqo_frame
grep -q "cache-decisions" $SRC/run.py || { echo "source at $SRC is not the current patched version (run scripts/setup_neurdb.sh or re-apply patches/neurdb/0003)" >&2; exit 1; }
bash /neuragent/deploy/stop_services.sh || true
# keep the service's writable state (SQLite buffer) and replace the code
for f in run.py run_moqoe_prototype.sh; do cp $SRC/$f $DST/$f; done
rm -rf $DST/src && cp -r $SRC/src $DST/src
chown -R neurdb $DST
echo "NQO service code refreshed in $DST"
bash /neuragent/deploy/start_services.sh
