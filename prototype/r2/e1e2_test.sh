#!/bin/bash
# Tests of the nrindex fixes E1 (per-backend lazy build), E2 (cost estimate),
# and the duplicate-key refusal. Runs inside the container as neurdb, on imdb_ori.
export PATH=/opt/neurdb/bin:$PATH
DB=imdb_ori
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; pass=$((pass+1)); else echo "  FAIL  $1  (got: $2, want: $3)"; fail=$((fail+1)); fi; }
q() { psql -X -q -At -d $DB -c "SET client_min_messages = warning" -c "$1" 2>&1 | tail -1; }

echo "=== setup: copies of two IMDB tables without btree indexes ==="
psql -X -q -d $DB -c "SET client_min_messages = warning" \
  -c "DROP TABLE IF EXISTS e_title, e_mk" \
  -c "CREATE TABLE e_title AS SELECT id, title, production_year FROM title" \
  -c "CREATE TABLE e_mk AS SELECT movie_id, keyword_id FROM movie_keyword LIMIT 200000" \
  -c "ANALYZE e_title" -c "ANALYZE e_mk" > /dev/null 2>&1
check "setup: e_title rows" "$(q 'SELECT count(*) FROM e_title')" "2528312"

echo "=== duplicate keys are refused ==="
out=$(psql -X -q -d $DB -c "CREATE INDEX e_mk_nr ON e_mk USING nrindex (movie_id)" 2>&1)
check "D1 nrindex on a column with repeated values fails with an error" "$(echo "$out" | grep -c 'rows whose value repeats')" "1"

echo "=== build in one session (connection A) ==="
t0=$(date +%s%N)
psql -X -q -d $DB -c "SET client_min_messages = warning" -c "CREATE INDEX e_title_nr ON e_title USING nrindex (id)" -c "ANALYZE e_title" > /dev/null 2>&1
echo "  INFO  build of 2.5M keys: $(( ($(date +%s%N) - t0) / 1000000 )) ms"

echo "=== E2: the planner chooses by cost, no switches turned off ==="
plan=$(psql -X -q -At -d $DB -c "EXPLAIN SELECT title FROM e_title WHERE id = 4242" 2>&1)
check "P1 equality lookup uses nrindex" "$(echo "$plan" | grep -c 'e_title_nr')" "1"
plan=$(psql -X -q -At -d $DB -c "EXPLAIN SELECT count(*) FROM e_title WHERE id BETWEEN 100 AND 200" 2>&1)
check "P2 range condition does not use nrindex" "$(echo "$plan" | grep -c 'e_title_nr')" "0"
check "P3 range query runs and is correct" "$(q 'SELECT count(*) FROM e_title WHERE id BETWEEN 100 AND 200')" "$(q 'SELECT count(*) FROM title WHERE id BETWEEN 100 AND 200')"
plan=$(psql -X -q -At -d $DB -c "EXPLAIN SELECT count(*) FROM generate_series(1, 2000) g JOIN e_title t ON t.id = g" 2>&1)
echo "  INFO  join plan: $(echo "$plan" | grep -oE 'Nested Loop|Hash Join|Merge Join|Index Scan using e_title_nr|Bitmap Index Scan on e_title_nr|Seq Scan on e_title' | tr '\n' ' ')"

echo "=== E1: other sessions (B, C) that did not build the index ==="
want=$(q "SELECT count(*), sum(length(title)) FROM generate_series(1, 200000, 7) g JOIN title t ON t.id = g")
got=$(psql -X -q -At -d $DB -c "SET client_min_messages = warning" -c "SET enable_hashjoin = off" -c "SET enable_mergejoin = off" -c "SET enable_seqscan = off" \
      -c "SELECT count(*), sum(length(title)) FROM generate_series(1, 200000, 7) g JOIN e_title t ON t.id = g" 2>&1 | tail -1)
check "E1a fresh session B gets the same rows as btree" "$got" "$want"
used=$(psql -X -q -At -d $DB -c "SET enable_hashjoin = off" -c "SET enable_mergejoin = off" -c "SET enable_seqscan = off" \
      -c "EXPLAIN SELECT count(*) FROM generate_series(1, 200000, 7) g JOIN e_title t ON t.id = g" 2>&1 | grep -c 'e_title_nr')
check "E1b ... and the lookups went through nrindex" "$used" "1"
check "E1c fresh session C, single lookups" "$(q 'SELECT title FROM e_title WHERE id = 4242')" "$(q 'SELECT title FROM title WHERE id = 4242')"
check "E1d missing key returns no row" "$(q 'SELECT count(*) FROM e_title WHERE id = -5')" "0"
built=$(grep -c "built its own instance of e_title_nr" /data/pg.log)
echo "  INFO  lazy builds logged so far: $built"
check "E1e lazy build happened in the fresh sessions" "$([ "$built" -ge 2 ] && echo yes)" "yes"

echo "=== two sessions at the same time ==="
r1=$(mktemp); r2=$(mktemp)
for f in $r1 $r2; do
  psql -X -q -At -d $DB -c "SET client_min_messages = warning" -c "SET enable_hashjoin = off" -c "SET enable_mergejoin = off" -c "SET enable_seqscan = off" \
    -c "SELECT count(*), sum(length(title)) FROM generate_series(1, 300000, 3) g JOIN e_title t ON t.id = g" > $f 2>&1 &
done
wait
want=$(q "SELECT count(*), sum(length(title)) FROM generate_series(1, 300000, 3) g JOIN title t ON t.id = g")
check "C1 concurrent session 1 correct" "$(tail -1 $r1)" "$want"
check "C2 concurrent session 2 correct" "$(tail -1 $r2)" "$want"
rm -f $r1 $r2

echo "=== cleanup ==="
psql -X -q -d $DB -c "SET client_min_messages = warning" -c "DROP TABLE IF EXISTS e_title, e_mk" > /dev/null 2>&1
echo "=== $pass passed, $fail failed ==="
[ $fail = 0 ]
