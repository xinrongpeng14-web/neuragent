#!/bin/bash
# 端到端联调: nr_molqo(C) <-> run.py(多进程, 假控制器)。容器内以 neurdb 用户运行。
export PATH=/opt/neurdb/bin:$PATH
U=http://127.0.0.1:8666
Q="SELECT count(*) FROM t1 a JOIN t2 b ON a.id = b.t1_id WHERE a.x < 100"
PRE="LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET client_min_messages = log; SET molqo.report_decisions = off; SET molqo.server_url='$U/optimize'; SET enable_molqo=on;"
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; pass=$((pass+1)); else echo "  FAIL  $1  (got: $2, want: $3)"; fail=$((fail+1)); fi; }
j() { grep -o "Hash Join\|Merge Join\|Nested Loop" | head -1; }
run() { psql -d neurdb -X -q -At "$@" 2>&1; }
stat() { curl -s $U/stats | python3 -c "import sys,json; d=json.load(sys.stdin); print(*[d[k] for k in sys.argv[1:]], sep='/')" "$@"; }
start() { pkill -f "run.py --host" 2>/dev/null; sleep 0.5; cd /data/t/nq && MOQOE_CONTROLLER_FACTORY=fake_controller:make PYTHONPATH=/data/t/nq FAKE_REAL_FORMAT=1 FAKE_HINT="/*+MergeJoin(a b)*/" "$@" nohup python3 run.py --host 127.0.0.1 --port 8666 --torch-threads 0 --quiet --freeze > /data/t/nq/server.log 2>&1 &
  for i in $(seq 1 50); do curl -s $U/health > /dev/null && break; sleep 0.2; done; }

echo "=== E1 两个工作进程, 真实输出格式 ==="
start env WORKERS=2; pkill -f "run.py --host"; sleep 0.5
cd /data/t/nq && (MOQOE_CONTROLLER_FACTORY=fake_controller:make PYTHONPATH=/data/t/nq FAKE_REAL_FORMAT=1 FAKE_HINT="/*+MergeJoin(a b)*/" nohup python3 run.py --host 127.0.0.1 --port 8666 --torch-threads 0 --quiet --freeze --workers 2 > /data/t/nq/server.log 2>&1 &)
for i in $(seq 1 50); do curl -s $U/health > /dev/null && break; sleep 0.2; done
out=$(run -c "$PRE SET molqo.expert_filter='hint';" -c "$Q" -c "SHOW enable_hashjoin" -c "SHOW enable_nestloop")
check "E1a hint 档: 9 条 SET (先关后开) 生效" "$(echo "$out" | j)" "Nested Loop"
check "E1b 语句后两个开关均为 on" "$(echo "$out" | tail -2 | tr '\n' '/')" "on/on/"
check "E1c join 档: 提示格式生效" "$(run -c "$PRE SET molqo.expert_filter='join';" -c "$Q" | j)" "Merge Join"
check "E1d all 档" "$(run -c "$PRE SET molqo.expert_filter='all';" -c "$Q" | j)" "Merge Join"
check "E1e /stats: 请求/优化/错误" "$(stat requests optimized errors)" "3/3/0"
check "E1f /stats: 各过滤档计数 all/hint/join" "$(stat filter_all filter_hint filter_join)" "1/1/1"
check "E1g /stats: 各专家计数 Hint/Join" "$(stat expert_HintPlanSel expert_JoinOrder)" "1/2"
check "E1h /stats: 工作进程数" "$(stat workers)" "2"
pkill -f "run.py --host"; sleep 0.5

echo "=== E2 以启动参数关闭 molqo 的机制 (服务端默认已开启) ==="
run -c "ALTER SYSTEM SET enable_molqo = on" -c "SELECT pg_reload_conf()" > /dev/null; sleep 0.5
check "E2a 普通连接: 服务端默认值" "$(run -c 'SHOW enable_molqo')" "on"
check "E2b 带启动参数的连接, 经过 DISCARD ALL 与 RESET ALL 后" "$(PGOPTIONS='-c enable_molqo=off' psql -d neurdb -X -q -At -c 'SHOW enable_molqo' -c 'DISCARD ALL' -c 'SHOW enable_molqo' -c 'RESET ALL' -c 'SHOW enable_molqo' 2>&1 | tr '\n' '/')" "off/off/off/"

echo "=== E3 专家回连数据库执行 EXPLAIN (单工作进程, 服务端默认开启 molqo) ==="
run -c "ALTER SYSTEM SET molqo.server_url = '$U/optimize'" -c "ALTER SYSTEM SET molqo.timeout_ms = 3000" -c "ALTER SYSTEM SET molqo.report_decisions = off" -c "SELECT pg_reload_conf()" > /dev/null; sleep 0.5
for mode in guarded unguarded; do
  cd /data/t/nq && (MOQOE_CONTROLLER_FACTORY=fake_controller:make PYTHONPATH=/data/t/nq FAKE_CALLBACK=$mode FAKE_HINT="/*+MergeJoin(a b)*/" nohup python3 run.py --host 127.0.0.1 --port 8666 --torch-threads 0 --quiet --freeze > /data/t/nq/server_$mode.log 2>&1 &)
  for i in $(seq 1 50); do curl -s $U/health > /dev/null && break; sleep 0.2; done
  t0=$(date +%s.%N); out=$(run -c "LOAD 'auto_explain'; SET auto_explain.log_min_duration = 0; SET client_min_messages = log;" -c "$Q"); el=$(echo "$(date +%s.%N) - $t0" | bc | cut -c1-5)
  echo "  INFO  回连方式=$mode: 计划=$(echo "$out" | j)  耗时=${el}s  超时告警=$(echo "$out" | grep -c 'did not answer')  服务端收到请求数=$(stat requests)"
  [ $mode = guarded ] && check "E3a 回连带启动参数: 无递归, 计划已优化" "$(echo "$out" | j)/$(echo "$el < 1.5" | bc)/$(stat requests)" "Merge Join/1/1"
  [ $mode = unguarded ] && check "E3b 回连不带参数: 发生递归, 靠超时回退" "$(echo "$out" | j)/$(echo "$el >= 3" | bc)" "Hash Join/1"
  pkill -f "run.py --host"; sleep 0.5
done
run -c "ALTER SYSTEM RESET enable_molqo" -c "ALTER SYSTEM RESET molqo.server_url" -c "ALTER SYSTEM RESET molqo.timeout_ms" -c "ALTER SYSTEM RESET molqo.report_decisions" -c "SELECT pg_reload_conf()" > /dev/null
echo "=== 通过 $pass, 失败 $fail ==="
