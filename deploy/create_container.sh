#!/bin/bash
# Create the experiment container and build everything inside it.
#
# Usage:  deploy/create_container.sh            (all settings through environment variables)
#
#   NAME        container name                       default neurdb-ga
#   CPUS        CPU quota of the container           default 4
#   CPUSET      cores to pin the container to        default none, e.g. "0-3"
#   MEMORY      memory limit                         default 16g
#   DATA_VOLUME docker volume mounted at /data       default <NAME>-data
#   IMAGE       base image                           default ubuntu:22.04
#   The variables of deploy/install_inside.sh (PG_SHARED_BUFFERS, JOBS, ...) are passed through.
#
# The project folder is mounted read-write at /neuragent, so the experiment
# program runs inside the container and its outputs appear under experiment/runs/
# on the host. The database cluster and the IMDB data live in the docker volume.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${NAME:-neurdb-ga}"
CPUS="${CPUS:-4}"
CPUSET="${CPUSET:-}"
MEMORY="${MEMORY:-16g}"
DATA_VOLUME="${DATA_VOLUME:-${NAME}-data}"
IMAGE="${IMAGE:-ubuntu:22.04}"

if [ ! -f "$ROOT/NeuralDB/dbengine/configure" ]; then
    echo "NeuralDB/ is missing or incomplete; run scripts/setup_neurdb.sh first" >&2
    exit 1
fi
if ! grep -q nrindex_stats "$ROOT/NeuralDB/dbengine/nr_kernel/nr_am/sql/nram--1.0.sql" 2>/dev/null; then
    echo "NeuralDB/ does not carry the prototype patches; run scripts/setup_neurdb.sh" >&2
    exit 1
fi

if docker inspect "$NAME" > /dev/null 2>&1; then
    echo "container $NAME already exists; remove it first with: docker rm -f $NAME" >&2
    exit 1
fi

docker volume create "$DATA_VOLUME" > /dev/null
docker run -d --init --name "$NAME" \
    --cpus "$CPUS" ${CPUSET:+--cpuset-cpus "$CPUSET"} --memory "$MEMORY" --shm-size 1g \
    -v "$ROOT:/neuragent" -v "$DATA_VOLUME:/data" \
    "$IMAGE" sleep infinity > /dev/null
echo "container $NAME created (cpus=$CPUS${CPUSET:+ cpuset=$CPUSET} memory=$MEMORY volume=$DATA_VOLUME)"

# install_inside.sh reads its settings from the environment; pass the ones that are set
pass=()
for v in JOBS PG_SHARED_BUFFERS PG_WORK_MEM PG_EFFECTIVE_CACHE PG_MAX_CONNECTIONS TORCH_INDEX; do
    [ -n "${!v:-}" ] && pass+=(-e "$v=${!v}")
done
docker exec "${pass[@]}" "$NAME" bash /neuragent/deploy/install_inside.sh
echo
echo "next: deploy/load_imdb.sh $NAME"
