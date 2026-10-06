#!/bin/bash
# Change the memory available to the experiment container and the database's
# shared_buffers, so that the SELIX index competes with the page cache for the
# IMDB data. Runs on the host; restarts the database (and the NQO service).
#
#     deploy/set_memory.sh <container> <memory> <shared_buffers> [effective_cache_size]
#     deploy/set_memory.sh neurdb-ga 6g 1GB 3GB
#
# docker also sets --memory-swap to the same value so that the container cannot
# fall back to swap when the limit is reached.
set -euo pipefail
NAME=${1:?container}; MEM=${2:?memory, e.g. 6g}; SB=${3:?shared_buffers, e.g. 1GB}; EC=${4:-}
docker update --memory "$MEM" --memory-swap "$MEM" "$NAME" > /dev/null
docker exec "$NAME" bash /neuragent/deploy/stop_services.sh > /dev/null || true
docker exec "$NAME" bash -c "
  sed -i 's/^shared_buffers = .*/shared_buffers = $SB/' /data/pg/postgresql.conf
  ${EC:+sed -i 's/^effective_cache_size = .*/effective_cache_size = $EC/' /data/pg/postgresql.conf}
  grep -E '^(shared_buffers|effective_cache_size) ' /data/pg/postgresql.conf"
echo "container memory: $(docker exec "$NAME" cat /sys/fs/cgroup/memory.max | awk '{printf "%.0f MB", $1/1048576}')"
echo "restart the services with: docker exec $NAME bash /neuragent/deploy/start_services.sh"
