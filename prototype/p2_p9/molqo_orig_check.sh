#!/bin/bash
# 对照: 只用原版也有的 GUC, 记录原版/新版在几种配置下的行为
export PATH=/opt/neurdb/bin:$PATH
U=http://127.0.0.1:8080
Q="SELECT count(*) FROM t1 a JOIN t2 b ON a.id = b.t1_id WHERE a.x < 100"
PRE="LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET client_min_messages = log;"
run() { psql -d neurdb -X -q -At "$@" 2>&1; }
j() { grep -o "Hash Join\|Merge Join\|Nested Loop" | head -1; }
echo "=== [$1] preload=$(run -c 'SHOW shared_preload_libraries') compute_query_id=$(run -c 'SHOW compute_query_id') ==="
out=$(run -c "$PRE SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "$Q" -c "SHOW enable_hashjoin")
echo "  SET 格式: 计划=$(echo "$out" | j)  语句后 enable_hashjoin=$(echo "$out" | tail -1)"
echo "  提示格式: 计划=$(run -c "$PRE SET molqo.server_url='$U/hint'; SET enable_molqo=on;" -c "$Q" | j)"
t0=$(date +%s.%N); run -c "SET molqo.server_url='$U/slow'; SET enable_molqo=on;" -c "$Q" > /dev/null; echo "  服务端延迟 3 秒时的查询耗时: $(echo "$(date +%s.%N) - $t0" | bc | cut -c1-5) 秒"
