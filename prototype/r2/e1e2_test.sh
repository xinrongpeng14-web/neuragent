#!/bin/bash
# Tests of the nrindex fixes E1 (per-backend lazy build), E2 (cost estimate)
# and E5 (a key may hold many rows). Runs inside the container as neurdb, on imdb_ori.
export PATH=/opt/neurdb/bin:$PATH
DB=imdb_ori
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; pass=$((pass+1)); else echo "  FAIL  $1  (got: $2, want: $3)"; fail=$((fail+1)); fi; }
q() { psql -X -q -At -d $DB -c "SET client_min_messages = warning" -c "$1" 2>&1 | tail -1; }

echo "=== setup: copy of title without btree indexes ==="
psql -X -q -d $DB -c "SET client_min_messages = warning" \
  -c "DROP TABLE IF EXISTS e_title, e_mk" \
  -c "CREATE TABLE e_title AS SELECT id, title, production_year FROM title" \
  -c "ANALYZE e_title" > /dev/null 2>&1
check "setup: e_title rows" "$(q 'SELECT count(*) FROM e_title')" "2528312"
# nrindex_build_time() is in nram--1.0.sql, but a database whose extension was
# created before the function was added does not have it: register it here (D2)
out=$(psql -X -q -d $DB -c "CREATE OR REPLACE FUNCTION nrindex_build_time(OUT builds bigint, OUT build_ms double precision)
  RETURNS record AS 'nram', 'nrindex_build_time' LANGUAGE C STRICT VOLATILE" 2>&1)
check "setup: nrindex_build_time() registered" "$(echo "$out" | grep -ci error)" "0"

echo "=== repeated keys (foreign-key column): every row is kept ==="
# full copy of movie_keyword: 4.5M rows, a movie has up to hundreds of keywords
psql -X -q -d $DB -c "SET client_min_messages = warning" -c "DROP TABLE IF EXISTS e_mk" \
  -c "CREATE TABLE e_mk AS SELECT movie_id, keyword_id FROM movie_keyword" -c "ANALYZE e_mk" > /dev/null 2>&1
t0=$(date +%s%N)
out=$(psql -X -q -d $DB -c "CREATE INDEX e_mk_nr ON e_mk USING nrindex (movie_id)" -c "ANALYZE e_mk" 2>&1)
echo "  INFO  build of $(q 'SELECT count(*) FROM e_mk') rows: $(( ($(date +%s%N) - t0) / 1000000 )) ms"
check "R1 nrindex on a foreign-key column is built without error" "$(echo "$out" | grep -ci error)" "0"
top=$(q "SELECT movie_id FROM movie_keyword GROUP BY movie_id ORDER BY count(*) DESC LIMIT 1")
check "R2 the movie with the most keywords: all rows, index scan" \
  "$(psql -X -q -At -d $DB -c 'SET enable_seqscan = off' -c 'SET enable_bitmapscan = off' -c "SELECT count(*), sum(keyword_id) FROM e_mk WHERE movie_id = $top" | tail -1)" \
  "$(q "SELECT count(*), sum(keyword_id) FROM movie_keyword WHERE movie_id = $top")"
check "R3 same movie, bitmap scan" \
  "$(psql -X -q -At -d $DB -c 'SET enable_seqscan = off' -c 'SET enable_indexscan = off' -c "SELECT count(*), sum(keyword_id) FROM e_mk WHERE movie_id = $top" | tail -1)" \
  "$(q "SELECT count(*), sum(keyword_id) FROM movie_keyword WHERE movie_id = $top")"
want=$(q "SELECT count(*), sum(k.keyword_id) FROM generate_series(1, 2528312, 97) g JOIN movie_keyword k ON k.movie_id = g")
got=$(psql -X -q -At -d $DB -c "SET client_min_messages = warning" -c "SET enable_hashjoin = off" -c "SET enable_mergejoin = off" -c "SET enable_seqscan = off" \
      -c "SELECT count(*), sum(k.keyword_id) FROM generate_series(1, 2528312, 97) g JOIN e_mk k ON k.movie_id = g" 2>&1 | tail -1)
check "R4 join over 26,000 movies in a fresh session: same rows as btree" "$got" "$want"
check "R5 a movie without keywords returns no row" "$(q 'SELECT count(*) FROM e_mk WHERE movie_id = -7')" "0"

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

echo "=== D1: btree and SELIX on the same column ==="
psql -X -q -d $DB -c "SET client_min_messages = warning" -c "CREATE INDEX e_title_bt ON e_title USING btree (id)" -c "ANALYZE e_title" > /dev/null 2>&1
idx() { psql -X -q -At -d $DB -c "SET client_min_messages = warning" ${2:+-c "$2"} -c "EXPLAIN $1" 2>&1 | grep -oE "e_title_(nr|bt)" | head -1; }
check "B1 equality, equal estimates: SELIX wins the tie" "$(idx 'SELECT title FROM e_title WHERE id = 4242')" "e_title_nr"
check "B2 selix.enable_index = off: btree" "$(idx 'SELECT title FROM e_title WHERE id = 4242' 'SET selix.enable_index = off')" "e_title_bt"
check "B3 selix.index_cost_scale = 2: btree is cheaper" "$(idx 'SELECT title FROM e_title WHERE id = 4242' 'SET selix.index_cost_scale = 2')" "e_title_bt"
check "B4 range condition: btree" "$(idx 'SELECT count(*) FROM e_title WHERE id BETWEEN 100 AND 200')" "e_title_bt"
check "B5 range result correct" "$(q 'SELECT count(*) FROM e_title WHERE id BETWEEN 100 AND 200')" "101"

echo "=== D2: rebuild when the densities change (read-only table) ==="
out=$(psql -X -q -At -d $DB -c "SET client_min_messages = warning" \
  -c "SELECT title FROM e_title WHERE id = 4242" \
  -c "SELECT builds FROM nrindex_build_time()" \
  -c "SET selix.init_density = 0.85" -c "SET selix.max_density = 0.95" -c "SET selix.min_density = 0.75" \
  -c "SELECT title FROM e_title WHERE id = 4242" \
  -c "SELECT builds FROM nrindex_build_time()" \
  -c "SET selix.rebuild_on_density_change = on" \
  -c "SELECT title FROM e_title WHERE id = 4242" \
  -c "SELECT builds, build_ms > 0 FROM nrindex_build_time()" \
  -c "SELECT title FROM e_title WHERE id = 4242" \
  -c "SELECT builds FROM nrindex_build_time()" 2>&1)
check "R6 first use builds once" "$(echo "$out" | sed -n 2p)" "1"
check "R7 density change without the switch: no rebuild" "$(echo "$out" | sed -n 4p)" "1"
check "R8 with selix.rebuild_on_density_change: rebuilt once, time recorded" "$(echo "$out" | sed -n 6p)" "2|t"
check "R9 no further rebuild while the densities stay" "$(echo "$out" | sed -n 8p)" "2"
check "R10 results correct throughout" "$(echo "$out" | sed -n '1p;3p;5p;7p' | sort -u)" "Pressure Point"

echo "=== cleanup ==="
psql -X -q -d $DB -c "SET client_min_messages = warning" -c "DROP TABLE IF EXISTS e_title, e_mk" > /dev/null 2>&1
echo "=== $pass passed, $fail failed ==="
[ $fail = 0 ]
