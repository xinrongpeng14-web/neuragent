-- P1: SELIX 通路验证 (路径 A: heap 表 + nrindex, 单连接)
\set ON_ERROR_STOP off
\timing on
\echo ===== V0 扩展与访问方法 =====
CREATE EXTENSION IF NOT EXISTS nram;
SELECT amname, amtype FROM pg_am WHERE amname IN ('nram','nrindex');

\echo ===== V1 heap 表上建 nrindex =====
DROP TABLE IF EXISTS ycsb;
CREATE TABLE ycsb (k int8 NOT NULL, v text);
INSERT INTO ycsb SELECT g*2, md5(g::text) FROM generate_series(1,200000) g;
ANALYZE ycsb;
CREATE INDEX ycsb_k_nr ON ycsb USING nrindex (k);
SELECT indexrelid::regclass, indisvalid, indisready FROM pg_index WHERE indrelid='ycsb'::regclass;

\echo ===== V2 同一连接点查 =====
\echo -- (a) 默认规划器设置, bigint 常量
EXPLAIN (ANALYZE, COSTS ON) SELECT v FROM ycsb WHERE k = 123456::int8;
\echo -- (b) 默认规划器设置, int4 常量 (opclass 只含 int8=int8, 预期用不上索引)
EXPLAIN (ANALYZE, COSTS ON) SELECT v FROM ycsb WHERE k = 123456;
\echo -- (c) 关闭 seqscan
SET enable_seqscan = off;
EXPLAIN (ANALYZE, COSTS ON) SELECT v FROM ycsb WHERE k = 123456::int8;
\echo -- (d) 再关闭 bitmapscan
SET enable_bitmapscan = off;
EXPLAIN (ANALYZE, COSTS ON) SELECT v FROM ycsb WHERE k = 123456::int8;
\echo -- 正确性: 命中 / 未命中 / 与顺序扫描对比
SELECT k, v = md5((k/2)::text) AS ok FROM ycsb WHERE k = 123456::int8;
SELECT count(*) AS miss_should_be_0 FROM ycsb WHERE k = 123457::int8;
DO $$
DECLARE bad int := 0; r record; kk int8; vv text;
BEGIN
  FOR i IN 1..2000 LOOP
    kk := (1 + floor(random()*200000))::int8 * 2;
    SELECT v INTO vv FROM ycsb WHERE k = kk;
    IF vv IS DISTINCT FROM md5((kk/2)::text) THEN bad := bad + 1; END IF;
  END LOOP;
  RAISE NOTICE 'V2 random point lookups: 2000 checked, % wrong', bad;
END $$;

\echo ===== V3 同一连接持续插入后点查 =====
INSERT INTO ycsb SELECT g*2+1, md5('new'||g) FROM generate_series(1,100000) g;
DO $$
DECLARE bad int := 0; kk int8; vv text; g int;
BEGIN
  FOR i IN 1..2000 LOOP
    g := 1 + floor(random()*100000)::int; kk := g::int8*2+1;
    SELECT v INTO vv FROM ycsb WHERE k = kk;
    IF vv IS DISTINCT FROM md5('new'||g) THEN bad := bad + 1; END IF;
  END LOOP;
  RAISE NOTICE 'V3 lookups of newly inserted keys: 2000 checked, % wrong', bad;
END $$;
SELECT count(*) FROM ycsb;

\echo ===== V2b 范围查询(已知限制) =====
SELECT count(*) AS range_via_index FROM ycsb WHERE k BETWEEN 1000::int8 AND 1100::int8;
RESET enable_seqscan; RESET enable_bitmapscan;
SET enable_indexscan = off; SET enable_bitmapscan = off;
SELECT count(*) AS range_via_seqscan FROM ycsb WHERE k BETWEEN 1000::int8 AND 1100::int8;
