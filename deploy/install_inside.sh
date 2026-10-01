#!/bin/bash
# Build the patched NeurDB and prepare the experiment environment. Runs inside
# the container as root; started by deploy/create_container.sh and re-runnable:
# a finished step leaves a stamp in /opt/.ga_stamps and is skipped next time.
#
# Settings (environment variables):
#   JOBS                parallel make jobs            default: number of cores
#   PG_SHARED_BUFFERS   shared_buffers                default 4GB
#   PG_WORK_MEM         work_mem                      default 64MB
#   PG_EFFECTIVE_CACHE  effective_cache_size          default 8GB
#   PG_MAX_CONNECTIONS  max_connections               default 100
#   TORCH_INDEX         wheel index for CPU PyTorch   default https://download.pytorch.org/whl/cpu
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ROOT=/neuragent
SRC=$ROOT/NeuralDB
PREFIX=/opt/neurdb
BUILD=/build
VENV=/opt/venv
NQO_HOME=/opt/nqo
PGDATA=/data/pg
STAMPS=/opt/.ga_stamps
# Settings given on the first run are remembered in $STAMPS/settings.env, so that
# a re-run after a failure does not silently fall back to the defaults.
mkdir -p $STAMPS
for v in PG_SHARED_BUFFERS PG_WORK_MEM PG_EFFECTIVE_CACHE PG_MAX_CONNECTIONS TORCH_INDEX; do
    [ -n "${!v:-}" ] && eval "GIVEN_$v=\${$v}"
done
[ -f $STAMPS/settings.env ] && . $STAMPS/settings.env
for v in PG_SHARED_BUFFERS PG_WORK_MEM PG_EFFECTIVE_CACHE PG_MAX_CONNECTIONS TORCH_INDEX; do
    g="GIVEN_$v"; [ -n "${!g:-}" ] && eval "$v=\${$g}"
done
JOBS=${JOBS:-$(nproc)}
PG_SHARED_BUFFERS=${PG_SHARED_BUFFERS:-4GB}
PG_WORK_MEM=${PG_WORK_MEM:-64MB}
PG_EFFECTIVE_CACHE=${PG_EFFECTIVE_CACHE:-8GB}
PG_MAX_CONNECTIONS=${PG_MAX_CONNECTIONS:-100}
TORCH_INDEX=${TORCH_INDEX:-https://download.pytorch.org/whl/cpu}
cat > $STAMPS/settings.env <<EOT
PG_SHARED_BUFFERS=${PG_SHARED_BUFFERS}
PG_WORK_MEM=${PG_WORK_MEM}
PG_EFFECTIVE_CACHE=${PG_EFFECTIVE_CACHE}
PG_MAX_CONNECTIONS=${PG_MAX_CONNECTIONS}
TORCH_INDEX=${TORCH_INDEX}
EOT
PG_HINT_PLAN_REPO=https://github.com/ossc-db/pg_hint_plan.git
PG_HINT_PLAN_BRANCH=PG16
PG=$PREFIX/bin

mkdir -p $BUILD
log() { echo "[$(date +%H:%M:%S)] $*"; }
step() {
    local name=$1; shift
    if [ -f "$STAMPS/$name" ]; then log "skip  $name (done before)"; return 0; fi
    log "start $name"
    "$@"
    touch "$STAMPS/$name"
    log "done  $name"
}
as_neurdb() { su neurdb -c "export PATH=$PG:\$PATH; $1"; }

# ---------------------------------------------------------------- 0. sources
check_sources() {
    [ -f $SRC/dbengine/configure ] || { echo "no NeurDB source at $SRC" >&2; exit 1; }
    grep -q nrindex_stats $SRC/dbengine/nr_kernel/nr_am/sql/nram--1.0.sql ||
        { echo "nr_am is not patched (nrindex_stats missing); run scripts/setup_neurdb.sh" >&2; exit 1; }
    grep -q expert_filter $SRC/dbengine/nr_kernel/nr_molqo/src/nr_molqo.c ||
        { echo "nr_molqo is not patched (molqo.expert_filter missing)" >&2; exit 1; }
    grep -q SharedStats $SRC/aiengine/neurqo_frame/run.py ||
        { echo "neurqo_frame is not patched (run.py without SharedStats)" >&2; exit 1; }
    [ -d $SRC/dbengine/nr_kernel/nr_am/src/nram_storage/selix/src ] ||
        { echo "SELIX submodule is empty; run: git -C NeuralDB submodule update --init" >&2; exit 1; }
    [ ! -f $SRC/dbengine/config.status ] ||
        { echo "NeuralDB/dbengine was configured in-tree; run 'make distclean' there first" >&2; exit 1; }
}
step check_sources check_sources

# ---------------------------------------------------------------- 1. packages
install_packages() {
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        build-essential flex bison libreadline-dev zlib1g-dev libicu-dev pkg-config \
        librocksdb-dev locales cmake git curl ca-certificates \
        python3 python3-venv python3-dev libpq-dev procps > /tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 1; }
    locale-gen en_US.UTF-8 > /dev/null
}
step packages install_packages

# ---------------------------------------------------------------- 2. user
# neurdb owns the database and runs the experiment; it gets the uid of the
# mounted project folder so that files written to /neuragent belong to the host user.
make_user() {
    local uid gid
    uid=$(stat -c %u $ROOT); gid=$(stat -c %g $ROOT)
    getent group "$gid" > /dev/null || groupadd -g "$gid" neurdb
    id neurdb > /dev/null 2>&1 || useradd -m -s /bin/bash -u "$uid" -g "$gid" neurdb
    mkdir -p /data && chown neurdb /data
}
step user make_user

# ---------------------------------------------------------------- 3. database engine
build_engine() {
    rm -rf $BUILD/pg && mkdir -p $BUILD/pg && cd $BUILD/pg
    bash $SRC/dbengine/configure --prefix=$PREFIX --without-icu > configure.log 2>&1 || { tail -30 configure.log; exit 1; }
    make -j"$JOBS" > make.log 2>&1 || { tail -30 make.log; exit 1; }
    make install > install.log 2>&1
    make -C contrib/auto_explain install > auto_explain.log 2>&1
}
step engine build_engine

# ---------------------------------------------------------------- 4. pg_hint_plan (PG16 branch)
build_hint_plan() {
    rm -rf $BUILD/pg_hint_plan
    git clone -q --depth 1 -b $PG_HINT_PLAN_BRANCH $PG_HINT_PLAN_REPO $BUILD/pg_hint_plan
    cd $BUILD/pg_hint_plan
    make PG_CONFIG=$PG/pg_config > make.log 2>&1 || { tail -30 make.log; exit 1; }
    make PG_CONFIG=$PG/pg_config install > install.log 2>&1
}
step pg_hint_plan build_hint_plan

# ---------------------------------------------------------------- 5. nram (SELIX bridge) and nr_molqo
build_extension() {   # name
    rm -rf $BUILD/$1 && cp -r $SRC/dbengine/nr_kernel/$1 $BUILD/$1 && cd $BUILD/$1
    make PG_CONFIG=$PG/pg_config > make.log 2>&1 || { tail -30 make.log; exit 1; }
    make PG_CONFIG=$PG/pg_config install > install.log 2>&1
}
step nr_am     build_extension nr_am
step nr_molqo  build_extension nr_molqo

# ---------------------------------------------------------------- 6. Python environment
# One environment for the NQO service and the experiment program. The pins
# follow aiengine/neurqo_frame/environment_moqoe.yml (python 3.8 there, 3.10
# here; every pinned wheel exists for 3.10).
build_venv() {
    rm -rf $VENV && python3 -m venv $VENV
    $VENV/bin/pip install -q --no-cache-dir --upgrade pip wheel setuptools
    $VENV/bin/pip install -q --no-cache-dir "psqlparse==1.0rc7"
    $VENV/bin/pip install -q --no-cache-dir --extra-index-url "$TORCH_INDEX" \
        -r $ROOT/deploy/requirements-container.txt
    $VENV/bin/python - <<'PY'
import torch, psqlparse, pglast, sklearn, pandas, numpy, psycopg2, gymnasium, stable_baselines3
print("  torch", torch.__version__, " numpy", numpy.__version__, " gymnasium", gymnasium.__version__,
      " sb3", stable_baselines3.__version__)
PY
}
step venv build_venv

# ---------------------------------------------------------------- 7. writable copy of the NQO service
# The service writes its SQLite buffer and reads its models relative to its
# own directory, so it gets a copy outside the project folder.
copy_nqo() {
    rm -rf $NQO_HOME && mkdir -p $NQO_HOME
    cp -r $SRC/aiengine/neurqo_frame $NQO_HOME/neurqo_frame
    chown -R neurdb $NQO_HOME
}
step nqo_copy copy_nqo

# ---------------------------------------------------------------- 8. database cluster
init_cluster() {
    # initdb prints "could not stat file .../pg_xlog" on this fork; harmless, it still succeeds
    [ -d $PGDATA/base ] || as_neurdb "initdb -D $PGDATA --locale=en_US.UTF-8 -E UTF8 > /data/initdb.log"
    sed -i '/^# --- ga begin/,/^# --- ga end/d' $PGDATA/postgresql.conf
    cat >> $PGDATA/postgresql.conf <<CONF
# --- ga begin  (written by deploy/install_inside.sh; the block is replaced on re-run)
listen_addresses = '*'
max_connections = $PG_MAX_CONNECTIONS
shared_buffers = $PG_SHARED_BUFFERS
work_mem = $PG_WORK_MEM
effective_cache_size = $PG_EFFECTIVE_CACHE
max_worker_processes = 8
# parallel workers and JIT would blur the per-backend CPU accounting of the experiment
max_parallel_workers_per_gather = 0
jit = off
log_min_messages = warning
log_line_prefix = '%m [%p] %a '
# order matters: pg_hint_plan must be loaded before nr_molqo
shared_preload_libraries = 'pg_hint_plan, nr_molqo, nram'
molqo.server_url = 'http://127.0.0.1:8666/optimize'
molqo.report_decisions = off
molqo.timeout_ms = 10000
# --- ga end
CONF
    sed -i '/# ga$/d' $PGDATA/pg_hba.conf
    cat >> $PGDATA/pg_hba.conf <<HBA
local   all all                 trust  # ga
host    all all 127.0.0.1/32    trust  # ga
host    all all ::1/128         trust  # ga
host    all all 172.16.0.0/12   trust  # ga
HBA
    chown neurdb $PGDATA/postgresql.conf $PGDATA/pg_hba.conf
    # the configuration above needs a (re)start to take effect
    if as_neurdb "pg_isready -q"; then
        as_neurdb "pg_ctl -D $PGDATA -l /data/pg.log -m fast -w restart > /dev/null"
    else
        as_neurdb "pg_ctl -D $PGDATA -l /data/pg.log -w start > /dev/null"
    fi
    # NeurDB's initdb creates the bootstrap database "neurdb" (there is no "postgres" database)
    as_neurdb "psql -X -q -d template1 -tAc \"SELECT 1 FROM pg_database WHERE datname='neurdb'\" | grep -q 1 || createdb neurdb"
    as_neurdb "psql -X -q -d neurdb -c 'CREATE EXTENSION IF NOT EXISTS nram' -c 'CREATE EXTENSION IF NOT EXISTS nr_molqo'"
    as_neurdb "pg_ctl -D $PGDATA -m fast -w stop > /dev/null"
}
step cluster init_cluster

cat <<EOT

install finished
  engine          $PREFIX  ($($PG/pg_config --version))
  extensions      $(ls $($PG/pg_config --pkglibdir) | grep -E '^(pg_hint_plan|nr_molqo|nram|auto_explain)\.so' | tr '\n' ' ')
  python          $VENV
  NQO service     $NQO_HOME/neurqo_frame
  cluster         $PGDATA  (stopped)
EOT
