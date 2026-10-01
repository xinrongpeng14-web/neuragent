#!/bin/bash
# Stop the NQO service and the database inside the container.
set -uo pipefail
if [ "$(id -u)" = 0 ]; then exec su neurdb -c "bash $0 $*"; fi
export PATH=/opt/neurdb/bin:$PATH
pkill -f "[r]un.py --workers" && echo "NQO service stopped" || echo "NQO service was not running"
sleep 0.5
if pg_isready -q; then pg_ctl -D /data/pg -m fast -w stop > /dev/null && echo "database stopped"; else echo "database was not running"; fi
