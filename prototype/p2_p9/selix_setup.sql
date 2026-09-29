-- 公共准备: 对数正态键的 YCSB 表 + 同连接建 nrindex + 工作负载函数
\set ON_ERROR_STOP on
SET client_min_messages = warning;
DROP TABLE IF EXISTS ycsb;
DROP TABLE IF EXISTS ycsb_seed;
SELECT setseed(0.42);
CREATE TABLE ycsb_seed AS
  SELECT DISTINCT (exp(random_normal(0, 2)) * 1e9)::int8 AS k, 'v'::text AS v
  FROM generate_series(1, 330000);
CREATE TABLE ycsb (k int8 NOT NULL, v text);
INSERT INTO ycsb SELECT k, v FROM ycsb_seed ORDER BY random() LIMIT 300000;
CREATE INDEX ycsb_k_nr ON ycsb USING nrindex (k);
SET enable_seqscan = off; SET enable_bitmapscan = off;
-- n 次操作, 其中 read_ratio 比例为点查(命中已有键), 其余为插入新键(对数正态)
CREATE OR REPLACE FUNCTION ycsb_run(n int, read_ratio float8, seed float8) RETURNS int AS $$
DECLARE kk int8; vv text; hits int := 0;
BEGIN
  PERFORM setseed(seed);
  FOR i IN 1..n LOOP
    IF random() < read_ratio THEN
      SELECT k INTO kk FROM ycsb_seed OFFSET floor(random() * 1000)::int LIMIT 1;
      SELECT v INTO vv FROM ycsb WHERE k = kk;
      IF FOUND THEN hits := hits + 1; END IF;
    ELSE
      kk := (exp(random_normal(0, 2)) * 1e9)::int8 + i;
      INSERT INTO ycsb VALUES (kk, 'n');
    END IF;
  END LOOP;
  RETURN hits;
END $$ LANGUAGE plpgsql;
RESET client_min_messages;
