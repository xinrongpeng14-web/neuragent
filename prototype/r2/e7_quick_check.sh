#!/bin/bash
# Quick check of the SELIX fix E7 on imdb_ori: an inner bitmap scan of
# nr_char_name_id under a nested loop whose outer key (cast_info.person_role_id)
# is often NULL. Before the fix, a NULL key returned the previous key's rows.
# Needs the SELIX index nr_char_name_id. Runs inside the container as neurdb.
export PATH=/opt/neurdb/bin:$PATH
DB=imdb_ori
Q="FROM cast_info ci JOIN char_name chn ON chn.id = ci.person_role_id WHERE ci.movie_id BETWEEN 1 AND 3000"
F="SET client_min_messages = warning; SET enable_hashjoin = off; SET enable_mergejoin = off; SET enable_indexscan = off; SET enable_indexonlyscan = off;"
run() { psql -X -q -At -d $DB -c "$F $1" -c "$2" 2>&1 | grep -v '^SET'; }

plan=$(run "SET selix.enable_index = on; SET selix.index_cost_scale = 0.01;" "EXPLAIN SELECT count(*) $Q")
echo "plan uses: $(echo "$plan" | grep -o 'Bitmap Index Scan on nr_char_name_id' | head -1)"
if ! echo "$plan" | grep -q 'Bitmap Index Scan on nr_char_name_id'; then
    echo "FAIL  the plan does not use a bitmap scan of nr_char_name_id (does the index exist?)"; echo "$plan"; exit 1
fi
b=$(run "SET selix.enable_index = off;" "SELECT count(*), sum((ci.person_role_id IS NULL)::int) $Q")
s=$(run "SET selix.enable_index = on; SET selix.index_cost_scale = 0.01;" "SELECT count(*), sum((ci.person_role_id IS NULL)::int) $Q")
echo "btree: $b"
echo "selix: $s"
if [ "$b" = "$s" ] && [ "${s#*|}" = "0" ]; then
    echo "PASS  E7 fixed: SELIX returns the same rows as btree and no row for a NULL key"
else
    echo "FAIL  SELIX returns rows for NULL keys: the database still runs the old nram extension (stop the services, then start them again)"
    exit 1
fi
