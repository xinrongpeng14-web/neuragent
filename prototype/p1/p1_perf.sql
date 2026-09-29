-- P1-V4: 单连接服务端吞吐 (PL/pgSQL 循环, 不含客户端往返), nrindex 对比 btree
\set ON_ERROR_STOP off
SELECT pg_backend_pid() AS pid \gset
\setenv PID :pid
DROP TABLE IF EXISTS ycsb; DROP TABLE IF EXISTS ycsb_bt;
CREATE TABLE ycsb    (k int8 NOT NULL, v text);
CREATE TABLE ycsb_bt (k int8 NOT NULL, v text);
INSERT INTO ycsb    SELECT g*2, md5(g::text) FROM generate_series(1,1000000) g;
INSERT INTO ycsb_bt SELECT g*2, md5(g::text) FROM generate_series(1,1000000) g;
ANALYZE ycsb; ANALYZE ycsb_bt;
\echo --- 后端进程内存: 建 nrindex 之前
\! grep VmRSS /proc/$PID/status
\timing on
CREATE INDEX ycsb_k_nr ON ycsb USING nrindex (k);
CREATE INDEX ycsb_bt_k ON ycsb_bt USING btree (k);
\timing off
\echo --- 后端进程内存: 建 nrindex 之后 (100 万键)
\! grep VmRSS /proc/$PID/status
SELECT pg_size_pretty(pg_relation_size('ycsb_bt_k')) AS btree_index_size;
SET enable_seqscan = off; SET enable_bitmapscan = off;

CREATE OR REPLACE FUNCTION bench(tbl text, n int, mode text) RETURNS text AS $$
DECLARE t0 timestamptz; kk int8; vv text; hit int := 0; el float8;
BEGIN
  PERFORM setseed(0.42);
  t0 := clock_timestamp();
  IF mode = 'read' THEN
    FOR i IN 1..n LOOP
      kk := (1 + floor(random()*1000000))::int8 * 2;
      EXECUTE format('SELECT v FROM %I WHERE k = $1', tbl) INTO vv USING kk;
      IF vv IS NOT NULL THEN hit := hit + 1; END IF;
    END LOOP;
  ELSE
    FOR i IN 1..n LOOP
      kk := (1 + floor(random()*1000000))::int8 * 2 + 1 + (i::int8 * 4000000);
      EXECUTE format('INSERT INTO %I VALUES ($1, $2)', tbl) USING kk, 'x';
      hit := hit + 1;
    END LOOP;
  END IF;
  el := extract(epoch FROM clock_timestamp() - t0);
  RETURN format('%s %s: %s ops, %s ok, %s ops/s', tbl, mode, n, hit, round(n/el));
END $$ LANGUAGE plpgsql;

\echo --- 点查 20 万次
SELECT bench('ycsb',    200000, 'read');
SELECT bench('ycsb_bt', 200000, 'read');
\echo --- 插入 20 万次 (随机键)
SELECT bench('ycsb',    200000, 'insert');
SELECT bench('ycsb_bt', 200000, 'insert');
\echo --- 插入后再点查 20 万次
SELECT bench('ycsb',    200000, 'read');
SELECT bench('ycsb_bt', 200000, 'read');
\echo --- 后端进程内存: 全部操作之后
\! grep VmRSS /proc/$PID/status
\echo --- 计划确认
EXPLAIN SELECT v FROM ycsb WHERE k = 2468::int8;
