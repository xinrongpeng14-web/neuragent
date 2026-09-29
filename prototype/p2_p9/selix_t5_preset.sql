-- 用法: psql -v init=.. -v max=.. -v min=.. -v name=..   同一负载下比较三档
\i /data/t/selix_setup.sql
\set QUIET on
SET selix.init_density = :init; SET selix.max_density = :max; SET selix.min_density = :min;
CREATE TEMP TABLE snap AS SELECT * FROM nrindex_stats();
SELECT ycsb_run(150000, 0.3, 0.7) AS hits \gset
SELECT :'name' AS preset, a.init_density AS init_d, a.max_density AS max_d, a.min_density AS min_d,
       a.n_put - b.n_put AS puts, a.n_smo - b.n_smo AS d_smo,
       round(a.mem_bytes / 1048576.0, 1) AS mem_mb,
       round(((a.t_get_ns - b.t_get_ns) + (a.t_put_ns - b.t_put_ns))::numeric / nullif((a.c_get - b.c_get) + (a.c_put - b.c_put), 0)) AS c_idx_ns
FROM nrindex_stats() a, snap b;
