\i /data/t/selix_setup.sql
\echo ===== T1 统计函数与计数器 =====
\echo -- 建索引后: n_keys 应等于表行数, mem_bytes > 0
SELECT (SELECT count(*) FROM ycsb_seed) >= s.n_keys AS keys_le_seed, s.n_keys, s.n_indexes, s.mem_bytes > 0 AS has_mem, s.n_get, s.n_put FROM nrindex_stats() s;
CREATE TEMP TABLE snap AS SELECT * FROM nrindex_stats();
\echo -- 执行 4 万次操作, 读比例 0.5
SELECT ycsb_run(40000, 0.5, 0.1) >= 0 AS ran;
SELECT a.n_get - b.n_get AS d_get, a.n_put - b.n_put AS d_put, (a.n_get - b.n_get) + (a.n_put - b.n_put) AS d_total,
       a.c_get - b.c_get AS d_cget, a.c_put - b.c_put AS d_cput,
       round((a.c_get - b.c_get)::numeric / nullif(a.n_get - b.n_get, 0), 4) AS get_sample_rate,
       round((a.c_put - b.c_put)::numeric / nullif(a.n_put - b.n_put, 0), 4) AS put_sample_rate,
       round((a.t_get_ns - b.t_get_ns)::numeric / nullif(a.c_get - b.c_get, 0)) AS get_ns_per_op,
       round((a.t_put_ns - b.t_put_ns)::numeric / nullif(a.c_put - b.c_put, 0)) AS put_ns_per_op,
       a.n_keys - b.n_keys AS d_keys
FROM nrindex_stats() a, snap b;
\echo ===== T2 采样率开关 =====
DROP TABLE snap; CREATE TEMP TABLE snap AS SELECT * FROM nrindex_stats();
SET selix.timing_sample_every = 0;
SELECT ycsb_run(8000, 0.5, 0.2) >= 0 AS ran;
SELECT 'every=0' AS cfg, a.n_get - b.n_get + a.n_put - b.n_put AS ops, a.c_get - b.c_get + a.c_put - b.c_put AS sampled FROM nrindex_stats() a, snap b;
DROP TABLE snap; CREATE TEMP TABLE snap AS SELECT * FROM nrindex_stats();
SET selix.timing_sample_every = 1;
SELECT ycsb_run(8000, 0.5, 0.3) >= 0 AS ran;
SELECT 'every=1' AS cfg, a.n_get - b.n_get + a.n_put - b.n_put AS ops, a.c_get - b.c_get + a.c_put - b.c_put AS sampled FROM nrindex_stats() a, snap b;
RESET selix.timing_sample_every;
\echo ===== T3 非法密度组合: 应告警并保持原值 =====
SELECT init_density, max_density, min_density FROM nrindex_stats();
SET selix.init_density = 0.9;
INSERT INTO ycsb VALUES (7, 'x');
SELECT init_density, max_density, min_density FROM nrindex_stats();
\echo -- 补齐另外两个参数后应生效
SET selix.max_density = 0.95; SET selix.min_density = 0.75; SET selix.init_density = 0.85;
INSERT INTO ycsb VALUES (9, 'x');
SELECT init_density, max_density, min_density FROM nrindex_stats();
\echo -- 越界取值应被 GUC 范围检查拒绝
\set ON_ERROR_STOP off
SET selix.max_density = 1.5;
\set ON_ERROR_STOP on
\echo ===== T4 点查正确性 (切换参数并插入之后) =====
DO $$ DECLARE bad int := 0; r record; vv text; n int := 0;
BEGIN
  FOR r IN SELECT k FROM ycsb_seed ORDER BY k LIMIT 3000 LOOP
    n := n + 1; SELECT v INTO vv FROM ycsb WHERE k = r.k;
  END LOOP;
  FOR r IN SELECT k FROM (SELECT k FROM ycsb TABLESAMPLE SYSTEM (1)) t LIMIT 3000 LOOP
    SELECT v INTO vv FROM ycsb WHERE k = r.k; n := n + 1;
    IF NOT FOUND THEN bad := bad + 1; END IF;
  END LOOP;
  RAISE NOTICE 'T4: % lookups, % keys present in table but missing from index', n, bad;
END $$;
