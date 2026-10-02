#!/bin/bash
# Make the offline bundle deps/ for a machine whose containers cannot reach
# GitHub, PyPI or event.cwi.nl. Run it on any machine that has internet access
# and docker, then copy deps/ into the neuragent folder of the experiment machine:
#
#     deploy/fetch_deps.sh
#     scp -r deps/ user@experiment-machine:/path/to/neuragent/
#
# Contents (about 1.7 GB):
#   deps/wheels/                          every Python package of deploy/requirements-container.txt
#                                         as wheels for ubuntu 22.04 / Python 3.10 (psqlparse prebuilt)
#   deps/pg_hint_plan-PG16.tar.gz         pg_hint_plan source, PG16 branch
#   deps/join-order-benchmark.tar.gz      the 113 JOB queries
#   deps/imdb.tgz                         the IMDB data (skipped with WITH_IMDB=0)
#
# install_inside.sh and load_imdb.sh use these files when they exist and fall
# back to downloading otherwise. deps/ is not part of the git repository.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/deps"
WITH_IMDB="${WITH_IMDB:-1}"
IMDB_URL="${IMDB_URL:-https://event.cwi.nl/da/job/imdb.tgz}"
mkdir -p "$OUT"

echo "--- Python wheels, pg_hint_plan and JOB queries (inside a throwaway ubuntu:22.04 container)"
docker run --rm -v "$OUT:/deps" -v "$ROOT/deploy:/deploy:ro" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    ubuntu:22.04 bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python3-venv python3-dev build-essential \
        git ca-certificates > /dev/null
    python3 -m venv /v && /v/bin/pip install -q --upgrade pip wheel setuptools
    rm -rf /deps/wheels && mkdir -p /deps/wheels
    /v/bin/pip download -q -d /deps/wheels pip wheel setuptools
    /v/bin/pip wheel -q -w /deps/wheels "psqlparse==1.0rc7"
    /v/bin/pip wheel -q -w /deps/wheels --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /deploy/requirements-container.txt
    echo "  $(ls /deps/wheels | wc -l) wheels, $(du -sh /deps/wheels | cut -f1)"
    git clone -q --depth 1 -b PG16 https://github.com/ossc-db/pg_hint_plan.git /tmp/pg_hint_plan
    tar -czf /deps/pg_hint_plan-PG16.tar.gz -C /tmp pg_hint_plan
    git clone -q --depth 1 https://github.com/gregrahn/join-order-benchmark /tmp/join-order-benchmark
    tar -czf /deps/join-order-benchmark.tar.gz -C /tmp join-order-benchmark
    chown -R "$HOST_UID:$HOST_GID" /deps'

if [ "$WITH_IMDB" = 1 ]; then
    echo "--- IMDB archive"
    if [ "$(stat -c %s "$OUT/imdb.tgz" 2>/dev/null || echo 0)" -ge 1263193115 ]; then
        echo "  already complete"
    else
        curl -L --fail --retry 5 -C - -o "$OUT/imdb.tgz" "$IMDB_URL"
    fi
fi
echo
ls -la "$OUT"
echo "total $(du -sh "$OUT" | cut -f1). Copy $OUT to the neuragent folder of the experiment machine."
