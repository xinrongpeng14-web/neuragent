#!/bin/bash
# Load the IMDB data set of the Join Order Benchmark (JOB) into the database
# imdb_ori and fetch the 113 JOB queries. Runs inside the container:
#
#     docker exec <container> bash /neuragent/deploy/load_imdb.sh
#
# Settings:
#   IMDB_URL   archive with the 21 CSV files        default https://event.cwi.nl/da/job/imdb.tgz (1.2 GB)
#   JOB_REPO   repository with the JOB queries      default https://github.com/gregrahn/join-order-benchmark
#   PARALLEL   tables loaded at the same time       default 4
#   IMDB_DIR   where the archive is kept            default /data/imdb
#
# Re-runnable: finished steps leave a stamp in /data/.stamps. Needs about 20 GB
# in /data (archive 1.2 GB, CSV files 3.7 GB, database 9 GB).
set -euo pipefail
if [ "$(id -u)" = 0 ]; then exec su neurdb -c "bash $0 $*"; fi
export PATH=/opt/neurdb/bin:$PATH

ROOT=/neuragent
IMDB_URL=${IMDB_URL:-https://event.cwi.nl/da/job/imdb.tgz}
JOB_REPO=${JOB_REPO:-https://github.com/gregrahn/join-order-benchmark}
PARALLEL=${PARALLEL:-4}
IMDB_DIR=${IMDB_DIR:-/data/imdb}
DB=imdb_ori
SQL=$ROOT/NeuralDB/aiengine/neurqo_frame/script/load_to_db/imdb   # schema of the Balsa project, Apache-2.0
STAMPS=/data/.stamps
TABLES="aka_name aka_title cast_info char_name comp_cast_type company_name company_type complete_cast
        info_type keyword kind_type link_type movie_companies movie_info movie_info_idx movie_keyword
        movie_link name person_info role_type title"

mkdir -p $STAMPS $IMDB_DIR
log() { echo "[$(date +%H:%M:%S)] $*"; }
step() {
    local name=$1; shift
    if [ -f "$STAMPS/$name" ]; then log "skip  $name (done before)"; return 0; fi
    log "start $name"; "$@"; touch "$STAMPS/$name"; log "done  $name"
}

free_gb=$(df -BG --output=avail /data | tail -1 | tr -dc 0-9)
[ -f $STAMPS/imdb_load ] || [ "$free_gb" -ge 20 ] || { echo "only ${free_gb} GB free in /data, 20 GB needed" >&2; exit 1; }

pg_isready -q || pg_ctl -D /data/pg -l /data/pg.log -w start > /dev/null

ARCHIVE_SIZE=1263193115   # bytes, as served by event.cwi.nl

download() {
    local size=0
    [ -f $IMDB_DIR/imdb.tgz ] && size=$(stat -c %s $IMDB_DIR/imdb.tgz)
    if [ "$size" -ge "$ARCHIVE_SIZE" ]; then
        echo "  archive already complete ($size bytes)"
    else
        curl -L --fail --retry 5 -C - -o $IMDB_DIR/imdb.tgz "$IMDB_URL"
        size=$(stat -c %s $IMDB_DIR/imdb.tgz)
    fi
    [ "$size" -gt 1000000000 ] || { echo "archive too small ($size bytes)" >&2; exit 1; }
}
step imdb_download download

extract() {
    mkdir -p $IMDB_DIR/csv && tar -xzf $IMDB_DIR/imdb.tgz -C $IMDB_DIR/csv
    for t in $TABLES; do [ -f $IMDB_DIR/csv/$t.csv ] || { echo "missing $t.csv in the archive" >&2; exit 1; }; done
    # the CSV files of this archive have no header row (first line of name.csv is a record)
    head -c 300 $IMDB_DIR/csv/name.csv | head -2
}
step imdb_extract extract

create_schema() {
    psql -X -q -d template1 -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 || createdb $DB
    psql -X -q -d $DB -v ON_ERROR_STOP=1 -f $SQL/schema.sql
    psql -X -q -d $DB -v ON_ERROR_STOP=1 -f $SQL/fkindexes.sql
}
step imdb_schema create_schema

load_one() {
    # NeurDB's own loader passes "csv header", which would drop the first record of
    # every table of this archive; the archive has no header line, so none here.
    # TRUNCATE first, so that a re-run after an interrupted load does not duplicate rows
    psql -X -q -d "$DB" -v ON_ERROR_STOP=1 -c "TRUNCATE $1" \
        -c "\\copy $1 FROM '$IMDB_DIR/csv/$1.csv' WITH (FORMAT csv, ESCAPE '\\')" \
        && echo "  loaded $1"
}
export -f load_one
export DB IMDB_DIR

load_tables() {
    printf '%s\n' $TABLES | xargs -P "$PARALLEL" -I{} bash -c 'load_one "$1"' _ {}
    psql -X -q -d $DB -v ON_ERROR_STOP=1 -f $SQL/add_fks.sql
    psql -X -q -d $DB -c "ANALYZE" > /dev/null
}
step imdb_load load_tables

verify_counts() {
    # row counts of the JOB data set as published with the benchmark
    local expected="aka_name:901343 aka_title:361472 cast_info:36244344 char_name:3140339 comp_cast_type:4
        company_name:234997 company_type:4 complete_cast:135086 info_type:113 keyword:134170 kind_type:7
        link_type:18 movie_companies:2609129 movie_info:14835720 movie_info_idx:1380035 movie_keyword:4523930
        movie_link:29997 name:4167491 person_info:2963664 role_type:12 title:2528312"
    local bad=0
    for pair in $expected; do
        local t=${pair%%:*} want=${pair##*:} got
        got=$(psql -X -q -d $DB -tAc "SELECT count(*) FROM $t")
        if [ "$got" != "$want" ]; then echo "  WARN  $t has $got rows, expected $want"; bad=1; fi
    done
    [ $bad = 0 ] && echo "  all 21 tables have the expected row counts"
    return 0
}
step imdb_verify verify_counts

extensions() {
    psql -X -q -d $DB -c 'CREATE EXTENSION IF NOT EXISTS nram' -c 'CREATE EXTENSION IF NOT EXISTS nr_molqo'
}
step imdb_extensions extensions

job_queries() {
    rm -rf /data/job && git clone -q --depth 1 "$JOB_REPO" /data/job
    local out=$ROOT/experiment/queries/job_all
    mkdir -p "$out" && rm -f "$out"/*.sql
    for f in /data/job/*.sql; do
        case $(basename "$f") in schema.sql|fkindexes.sql) continue;; esac
        cp "$f" "$out/"
    done
    echo "  $(ls "$out" | wc -l) query files in $out"
}
step job_queries job_queries

size=$(psql -X -q -d $DB -tAc "SELECT pg_size_pretty(pg_database_size('$DB'))")
echo
echo "IMDB loaded: database $DB ($size); JOB queries in $ROOT/experiment/queries/job_all"
echo "next: deploy/start_services.sh, then deploy/check_deploy.sh"
