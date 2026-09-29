#!/bin/bash
# nr_molqo 功能测试. 用法: molqo_test.sh <标签>   (在容器内以 neurdb 用户运行)
export PATH=/opt/neurdb/bin:$PATH
TAG=$1; U=http://127.0.0.1:8080; RLOG=/data/t/molqo_requests.log
Q="SELECT count(*) FROM t1 a JOIN t2 b ON a.id = b.t1_id WHERE a.x < 100"
PRE="LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET client_min_messages = log; SET molqo.report_decisions = off;"
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; pass=$((pass+1)); else echo "  FAIL  $1  (got: $2, want: $3)"; fail=$((fail+1)); fi; }
join_of() { grep -o "Hash Join\|Merge Join\|Nested Loop" | head -1; }
run() { psql -d neurdb -X -q -At "$@" 2>&1; }

echo "=== [$TAG] preload: $(run -c 'SHOW shared_preload_libraries') ==="
run -c "SET client_min_messages=warning; DROP TABLE IF EXISTS t1, t2; CREATE TABLE t1(id int primary key, x int); CREATE TABLE t2(id int primary key, t1_id int); INSERT INTO t1 SELECT g, g % 1000 FROM generate_series(1,20000) g; INSERT INTO t2 SELECT g, (g % 20000) + 1 FROM generate_series(1,40000) g; CREATE INDEX ON t2(t1_id); ANALYZE t1; ANALYZE t2; CREATE EXTENSION IF NOT EXISTS nr_molqo; CREATE EXTENSION IF NOT EXISTS pg_hint_plan;" > /dev/null
: > $RLOG

check "C1 基线: molqo 关闭" "$(run -c "$PRE $Q" | join_of)" "Hash Join"

out=$(run -c "$PRE SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "$Q" -c "SHOW enable_hashjoin" -c "SHOW enable_mergejoin")
check "C2a SET 格式生效" "$(echo "$out" | join_of)" "Nested Loop"
check "C2b 语句后 enable_hashjoin 已复位" "$(echo "$out" | tail -2 | head -1)" "on"
check "C2c 语句后 enable_mergejoin 已复位" "$(echo "$out" | tail -1)" "on"
out=$(run -c "$PRE SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "$Q" -c "SET enable_molqo=off" -c "$Q")
check "C2d 同会话下一条查询不受影响" "$(echo "$out" | grep -o "Hash Join\|Merge Join\|Nested Loop" | sed -n 2p)" "Hash Join"

check "C3 提示格式生效" "$(run -c "$PRE SET molqo.server_url='$U/hint'; SET enable_molqo=on;" -c "$Q" | join_of)" "Merge Join"

t0=$(date +%s.%N)
out=$(run -c "$PRE SET molqo.server_url='$U/slow'; SET molqo.timeout_ms=1000; SET enable_molqo=on;" -c "$Q")
el=$(echo "$(date +%s.%N) - $t0" | bc)
check "C4a 超时后回退到原生计划" "$(echo "$out" | join_of)" "Hash Join"
check "C4b 给出超时告警" "$(echo "$out" | grep -c 'did not answer within 1000 ms')" "1"
check "C4c 总耗时小于 2.5 秒 (实际 ${el}s)" "$(echo "$el < 2.5" | bc)" "1"

: > $RLOG
for f in all hint join; do run -c "SET molqo.report_decisions=off; SET molqo.server_url='$U/echo'; SET molqo.expert_filter='$f'; SET enable_molqo=on;" -c "$Q" > /dev/null; done
check "C5 expert_filter 随请求发送" "$(grep -o '"expert_filter": "[a-z]*"' $RLOG | cut -d'"' -f4 | tr '\n' ',')" "all,hint,join,"
check "C5b 非法取值被拒绝" "$(run -c "SET molqo.expert_filter='foo'" | grep -c 'invalid value')" "1"

run -c "SET molqo.server_url='$U/echo'" -c "SELECT optimize_query(E'SELECT \"a\".\"x\" AS \"q\\tq\", ''it''''s \\\\ back'' \\n FROM t1 a OFFSET 0') = E'SELECT \"a\".\"x\" AS \"q\\tq\", ''it''''s \\\\ back'' \\n FROM t1 a OFFSET 0'" > /tmp/c6.txt
check "C6 含引号/反斜杠/换行/制表符的 SQL 往返一致" "$(tail -1 /tmp/c6.txt)" "t"
check "C7 20KB 长 SQL 往返一致" "$(run -c "SET molqo.server_url='$U/echo'" -c "SELECT optimize_query(s) = s FROM (SELECT 'SELECT 1 /* ' || repeat('x', 20000) || ' */' AS s) q" | tail -1)" "t"

out=$(run -c "$PRE SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "$Q OFFSET 0")
check "C8 查询体内含 'SET ' 字样 (OFFSET) 不被误解析" "$(echo "$out" | join_of)/$(echo "$out" | grep -c WARNING)" "Nested Loop/0"

out=$(run -c "$PRE SET molqo.server_url='$U/badset'; SET enable_molqo=on;" -c "$Q" -c "SET enable_molqo=off" -c "SELECT 'alive'")
check "C9 未知参数只告警, 其余设置照常生效" "$(echo "$out" | join_of)/$(echo "$out" | grep -c 'unrecognized configuration parameter')/$(echo "$out" | tail -1)" "Nested Loop/1/alive"

out=$(run -c "SET molqo.report_decisions=off; SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "SELECT 1/(a.x - a.x) FROM t1 a JOIN t2 b ON a.id=b.t1_id LIMIT 1" -c "SHOW enable_hashjoin")
check "C10 查询执行报错后设置仍已复位" "$(echo "$out" | grep -c 'division by zero')/$(echo "$out" | tail -1)" "1/on"

{ echo "SET molqo.report_decisions=off; SET enable_molqo=on;"; for i in $(seq 1 150); do echo "SET molqo.server_url='$U/set'; $Q; SET molqo.server_url='$U/hint'; $Q;"; done; echo "SHOW enable_hashjoin; SELECT 'alive';"; } > /tmp/stress.sql
out=$(run -f /tmp/stress.sql)
check "C11 连续 300 条查询后进程存活且设置为默认" "$(echo "$out" | grep -c '^4000$')/$(echo "$out" | tail -2 | tr '\n' '/')" "300/on/alive/"

out=$(run -c "$PRE SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "PREPARE p AS $Q" -c "EXECUTE p" -c "EXECUTE p")
echo "  INFO  C12 预备语句: 第 1 次 EXECUTE = $(echo "$out" | grep -o "Hash Join\|Merge Join\|Nested Loop" | sed -n 1p), 第 2 次 EXECUTE = $(echo "$out" | grep -o "Hash Join\|Merge Join\|Nested Loop" | sed -n 2p)"
echo "  INFO  C13 EXPLAIN 是否触发优化请求: $( : > $RLOG; run -c "SET molqo.report_decisions=off; SET molqo.server_url='$U/set'; SET enable_molqo=on;" -c "EXPLAIN $Q" | join_of), 请求数 = $(wc -l < $RLOG)"
echo "=== [$TAG] 通过 $pass, 失败 $fail ==="
