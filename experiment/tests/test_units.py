"""Unit tests that need neither a database nor the container."""
import json
import math
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto import actions as A  # noqa: E402
from gaproto import metrics as M  # noqa: E402
from gaproto.cgroup import CgroupReader, CgroupSample, candidate_dirs, parse_cpu_stat  # noqa: E402
from gaproto.config import RewardConfig, load_config, to_dict  # noqa: E402
from gaproto.job_driver import load_queries  # noqa: E402
from gaproto.keys import MAX_KEY, lognormal_keys  # noqa: E402
from gaproto.ycsb_driver import TokenPacer, density_sql  # noqa: E402


class ActionsTest(unittest.TestCase):
    def test_twelve_actions_round_trip(self):
        self.assertEqual(A.NUM_ACTIONS, 12)
        seen = set()
        for a in range(A.NUM_ACTIONS):
            mode, preset = A.decode(a)
            self.assertEqual(A.encode(mode, preset), a)
            seen.add((mode, preset))
        self.assertEqual(len(seen), 12)

    def test_original_action(self):
        self.assertEqual(A.decode(A.ORIGINAL_ACTION), ("auto", "default"))
        self.assertEqual(A.SELIX_PRESETS["default"], (0.70, 0.80, 0.60))

    def test_out_of_range(self):
        for bad in (-1, 12):
            with self.assertRaises(ValueError):
                A.decode(bad)

    def test_presets_satisfy_selix_constraint(self):
        for init_d, max_d, min_d in A.SELIX_PRESETS.values():
            self.assertTrue(0 < min_d < init_d < max_d <= 1)

    def test_nqo_settings(self):
        self.assertEqual(A.nqo_settings("off")["enable_molqo"], "off")
        self.assertEqual(A.nqo_settings("auto"), {"enable_molqo": "on", "molqo.expert_filter": "all"})
        self.assertEqual(A.nqo_settings("hint")["molqo.expert_filter"], "hint")
        self.assertEqual(A.nqo_settings("join")["molqo.expert_filter"], "join")


class KeysTest(unittest.TestCase):
    def test_deterministic_and_in_range(self):
        a = lognormal_keys(50000, seed=7)
        b = lognormal_keys(50000, seed=7)
        self.assertTrue(np.array_equal(a, b))
        self.assertEqual(a.dtype, np.int64)
        self.assertGreaterEqual(int(a.min()), 1)
        self.assertLessEqual(int(a.max()), MAX_KEY)
        self.assertFalse(np.array_equal(a, lognormal_keys(50000, seed=8)))

    def test_unique(self):
        k = lognormal_keys(20000, seed=3, unique=True)
        self.assertEqual(k.size, 20000)
        self.assertEqual(np.unique(k).size, 20000)
        self.assertFalse(np.all(np.diff(k) > 0), "keys must not come back sorted")

    def test_extreme_sigma_is_clipped(self):
        k = lognormal_keys(10000, seed=1, sigma=12.0)
        self.assertLessEqual(int(k.max()), MAX_KEY)
        self.assertGreaterEqual(int(k.min()), 1)


class ConfigTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg.step_s, 30.0)
        self.assertEqual(cfg.episode_steps, 60)
        self.assertEqual([p.name for p in cfg.phases], ["A", "B"])
        self.assertEqual([cfg.phase_of_step(s) for s in (0, 29, 30, 59, 99)], [0, 0, 1, 1, 1])

    def test_partial_file(self):
        cfg = load_config(self.write({
            "step_s": 5, "db": {"host": "10.0.0.2"},
            "phases": [{"name": "A", "steps": 2, "job_clients": 1, "ycsb_read_ratio": 0.5, "ycsb_rate": 100}],
        }))
        self.assertEqual((cfg.step_s, cfg.db.host, cfg.db.port, cfg.episode_steps), (5, "10.0.0.2", 5432, 2))
        self.assertEqual(to_dict(cfg)["db"]["host"], "10.0.0.2")

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "stepp_s"):
            load_config(self.write({"stepp_s": 5}))
        with self.assertRaisesRegex(ValueError, "db"):
            load_config(self.write({"db": {"hostname": "x"}}))

    def test_validation(self):
        for bad in ({"step_s": 0}, {"phases": []}, {"container": {"ncpus": -1}},
                    {"phases": [{"name": "A", "ycsb_read_ratio": 1.5}]},
                    {"phases": [{"name": "A", "job_clients": 99}]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                load_config(self.write(bad))


class CgroupTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_cpu_stat("usage_usec 123456\nuser_usec 100\nsystem_usec 23\n"), 123456)
        with self.assertRaises(ValueError):
            parse_cpu_stat("user_usec 1\n")

    def test_candidates(self):
        dirs = candidate_dirs("abc")
        self.assertIn("/sys/fs/cgroup/system.slice/docker-abc.scope", dirs)
        self.assertIn("/sys/fs/cgroup/docker/abc", dirs)

    def test_reader_on_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "cpu.stat"), "w") as f:
                f.write("usage_usec 2000000\nuser_usec 1\n")
            with open(os.path.join(d, "memory.current"), "w") as f:
                f.write("1048576\n")
            r = CgroupReader("none", ncpus=4, cgroup_dir=d)
            self.assertEqual(r.source, d)
            self.assertEqual(r.sample(), CgroupSample(2000000, 1048576))

    def test_cpu_util(self):
        r = CgroupReader.__new__(CgroupReader)
        r.ncpus = 4.0
        # 6 CPU-seconds in 3 s on 4 cores
        self.assertAlmostEqual(r.cpu_util(CgroupSample(0, 0), CgroupSample(6_000_000, 0), 3.0), 0.5)
        self.assertEqual(r.cpu_util(CgroupSample(0, 0), CgroupSample(1, 0), 0.0), 0.0)


class YcsbHelpersTest(unittest.TestCase):
    def test_pacer(self):
        # times and rates are exact in binary, so the expected counts are exact
        p = TokenPacer(64.0, now=8.0, burst_s=0.25)
        self.assertEqual(p.allowance(8.0, 64), 0)
        self.assertEqual(p.allowance(8.125, 64), 8)
        p.spent(8)
        self.assertEqual(p.allowance(8.125, 64), 0)
        self.assertEqual(p.allowance(8.25, 4), 4)          # capped by what the caller wants
        self.assertEqual(TokenPacer(0, 0.0).allowance(0.0, 64), 64)   # unlimited

    def test_pacer_drops_what_a_stall_missed(self):
        p = TokenPacer(64.0, now=0.0, burst_s=0.25)        # keeps at most 16 operations
        self.assertEqual(p.allowance(0.125, 64), 8)
        p.spent(8)
        # 4 s without sending: 256 operations were due, 16 may be caught up
        self.assertEqual(p.allowance(4.125, 64), 16)
        p.spent(16)
        self.assertEqual(p.allowance(4.125, 64), 0)
        self.assertEqual(p.allowance(4.25, 64), 8)

    def test_pacer_never_exceeds_the_rate_after_stalls(self):
        p, sent, t = TokenPacer(500.0, now=0.0), 0, 0.0
        for k in range(1, 2001):
            t += 1.5 if k % 400 == 0 else 0.005            # a stall every 2 s
            n = p.allowance(t, 64)
            p.spent(n)
            sent += n
        self.assertLessEqual(sent, 500.0 * t)
        self.assertGreater(sent, 500.0 * (t - 5 * 1.5) * 0.95)

    def test_density_sql_is_one_message(self):
        sql = density_sql(0.85, 0.95, 0.75)
        self.assertEqual(sql.count("SET selix."), 3)
        self.assertIn("selix.init_density = 0.8500", sql)
        self.assertIn("selix.max_density = 0.9500", sql)
        self.assertIn("selix.min_density = 0.7500", sql)


class QueriesTest(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("2b.sql", "SELECT 2;\n"), ("1a.sql", " SELECT 1 "), ("empty.sql", "  "),
                               ("note.txt", "x")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            self.assertEqual(load_queries(d), [("1a", "SELECT 1"), ("2b", "SELECT 2;")])
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            load_queries(d)


def metrics(**kw):
    m = M.StepMetrics(elapsed_s=10.0)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


REFS = M.Refs(base_lat={"q1": 1.0, "q2": 3.0}, q_j_ref={"A": 2.0, "B": 1.0},
              c_ref_ns={"A": 400.0, "B": 800.0}, mem_ref=[100.0, 200.0])


class MetricsTest(unittest.TestCase):
    def test_standardised_throughput(self):
        done = [("q1", 0.5), ("q1", 0.7), ("q2", 9.9), ("new", 4.0)]
        # weights come from the baseline: 1 + 1 + 3, the unknown template uses its own 4.0
        self.assertAlmostEqual(M.standardised_job_throughput(done, REFS.base_lat, 10.0), 0.9)
        self.assertEqual(M.standardised_job_throughput(done, REFS.base_lat, 0.0), 0.0)

    def test_derived_values(self):
        m = metrics(idx_gets=300, idx_puts=700, idx_smo=5, idx_sampled=100, idx_sampled_ns=50_000,
                    job_lat_sum_s=30.0, ycsb_lat_sum_s=10.0, nqo_opt_time_ms=6000.0,
                    ycsb_reads=300, ycsb_inserts=700)
        self.assertAlmostEqual(m.c_idx_ns, 500.0)
        self.assertAlmostEqual(m.smo_rate, 0.005)
        self.assertAlmostEqual(m.ycsb_insert_ratio, 0.7)
        self.assertAlmostEqual(m.olap_share, 0.75)
        self.assertAlmostEqual(m.nqo_overhead, 0.2)
        self.assertAlmostEqual(m.ycsb_ops_per_s, 100.0)
        empty = M.StepMetrics()
        self.assertIsNone(empty.c_idx_ns)
        self.assertEqual((empty.smo_rate, empty.olap_share, empty.nqo_overhead), (0.0, 0.0, 0.0))
        json.dumps(m.to_record())

    def test_reward_matches_formula(self):
        w = RewardConfig()
        m = metrics(job_completed=[("q1", 0.4)] * 20 + [("q2", 2.0)] * 20,   # q_J = (20 + 60) / 10 = 8
                    idx_sampled=10, idx_sampled_ns=2000,                     # c_idx = 200 ns
                    idx_mem_bytes=400)
        p = M.compute_reward(m, REFS, "A", step=1, switched=True, w=w)
        self.assertAlmostEqual(p.q_j, 8.0)
        self.assertAlmostEqual(p.r_job, math.log(8.0 / 2.0))
        self.assertAlmostEqual(p.r_selix_cost, math.log(400.0 / 200.0))      # faster than the reference
        self.assertAlmostEqual(p.r_selix_mem, -0.5 * math.log(400.0 / 200.0))  # twice the memory
        self.assertAlmostEqual(p.reward, 0.5 * math.log(4) + 0.5 * (math.log(2) - 0.5 * math.log(2)) - 0.05)

    def test_original_system_scores_zero(self):
        m = metrics(job_completed=[("q1", 1.0)] * 20, idx_sampled=10, idx_sampled_ns=4000, idx_mem_bytes=100)
        p = M.compute_reward(m, REFS, "A", 0, False, RewardConfig())
        self.assertAlmostEqual(p.reward, 0.0)

    def test_direction_of_each_term(self):
        w = RewardConfig()
        base = dict(job_completed=[("q1", 1.0)] * 20, idx_sampled=10, idx_sampled_ns=4000, idx_mem_bytes=100)
        zero = M.compute_reward(metrics(**base), REFS, "A", 0, False, w).reward
        slower_index = M.compute_reward(metrics(**{**base, "idx_sampled_ns": 8000}), REFS, "A", 0, False, w)
        more_memory = M.compute_reward(metrics(**{**base, "idx_mem_bytes": 200}), REFS, "A", 0, False, w)
        fewer_queries = M.compute_reward(metrics(**{**base, "job_completed": [("q1", 1.0)] * 10}),
                                         REFS, "A", 0, False, w)
        switched = M.compute_reward(metrics(**base), REFS, "A", 0, True, w)
        for worse in (slower_index, more_memory, fewer_queries, switched):
            self.assertLess(worse.reward, zero)

    def test_missing_measurements_contribute_zero(self):
        p = M.compute_reward(metrics(job_completed=[("q1", 1.0)] * 20), REFS, "A", 0, False, RewardConfig())
        self.assertEqual((p.r_selix_cost, p.r_selix_mem), (0.0, 0.0))
        p = M.compute_reward(metrics(), M.Refs(), "A", 0, False, RewardConfig())
        self.assertEqual(p.reward, 0.0)

    def test_no_finished_query_is_a_bounded_penalty(self):
        p = M.compute_reward(metrics(), REFS, "A", 0, False, RewardConfig())
        self.assertEqual(p.r_job, M.OBS_LOW)
        self.assertTrue(math.isfinite(p.reward))

    def test_mem_reference_beyond_last_step(self):
        self.assertEqual(REFS.mem_at(0), 100.0)
        self.assertEqual(REFS.mem_at(7), 200.0)
        self.assertIsNone(M.Refs().mem_at(0))

    def test_observation(self):
        m = metrics(cpu_util=0.5, idx_gets=1, idx_puts=1, idx_smo=1, idx_sampled=1, idx_sampled_ns=800,
                    idx_mem_bytes=100, job_lat_sum_s=1.0, ycsb_lat_sum_s=1.0)
        obs = M.observation(m, REFS, "A", 0, "join", "sparse")
        self.assertEqual(obs.shape, (M.OBS_DIM,))
        self.assertEqual(obs.dtype, np.float32)
        self.assertEqual(len(M.OBS_NAMES), M.OBS_DIM)
        named = dict(zip(M.OBS_NAMES, obs.tolist()))
        self.assertAlmostEqual(named["cpu_util"], 0.5)
        self.assertAlmostEqual(named["idx_cost"], math.log(2.0), places=5)
        self.assertAlmostEqual(named["idx_mem"], 0.0)
        self.assertEqual(named["smo_rate"], M.OBS_HIGH)          # 0.5 * 1000, clipped
        self.assertEqual((named["prev_nqo"], named["prev_selix"]), (1.0, 1.0))
        first = M.observation(m, REFS, "A", 0, "off", "dense")
        self.assertEqual((first[7], first[8]), (0.0, 0.0))
        no_refs = M.observation(m, None, "A", 0, "auto", "default")
        self.assertEqual((no_refs[4], no_refs[5]), (0.0, 0.0))
        self.assertTrue(np.all(obs >= M.OBS_LOW) and np.all(obs <= M.OBS_HIGH))

    def test_fill_ycsb_takes_differences_of_cumulative_counters(self):
        s0 = dict(n_get=100, n_put=50, t_get_ns=1000, c_get=10, t_put_ns=4000, c_put=5, mem_bytes=1,
                  n_smo=3, n_keys=50, n_indexes=1, init_density=0.7, max_density=0.8, min_density=0.6)
        s1 = dict(s0, n_get=400, n_put=150, t_get_ns=7000, c_get=40, t_put_ns=9000, c_put=15,
                  mem_bytes=2048, n_smo=9, n_keys=150, init_density=0.85, max_density=0.95, min_density=0.75)
        after = dict(alive=True, n_read=300, n_insert=100, n_error=1, n_miss=2, lat_sum_s=0.5,
                     lat_p50_s=0.001, lat_p99_s=0.003, stats=s1)
        m = M.StepMetrics(elapsed_s=1.0)
        M.fill_ycsb(m, {"stats": s0}, after)
        self.assertEqual((m.idx_gets, m.idx_puts, m.idx_smo, m.idx_sampled), (300, 100, 6, 40))
        self.assertEqual(m.idx_sampled_ns, 11000)
        self.assertEqual((m.idx_mem_bytes, m.idx_keys), (2048, 150))
        self.assertEqual(m.idx_density, (0.85, 0.95, 0.75))
        self.assertEqual((m.ycsb_reads, m.ycsb_inserts, m.ycsb_errors, m.ycsb_misses), (300, 100, 1, 2))

    def test_build_refs_and_noise(self):
        def rec(ep, step, phase, n_q1, ns, mem):
            m = metrics(job_completed=[("q1", 1.0 + 0.1 * ep)] * n_q1, idx_sampled=10,
                        idx_sampled_ns=ns, idx_mem_bytes=mem)
            return {"episode": ep, "step": step, "phase": phase, "metrics": m.to_record()}

        steps = [rec(0, 0, "A", 10, 4000, 100), rec(0, 1, "B", 4, 8000, 200),
                 rec(1, 0, "A", 30, 6000, 300), rec(1, 1, "B", 4, 8000, 400)]
        refs = M.build_refs(steps, meta={"x": 1})
        # median over all completions: 14 at 1.0 s and 34 at 1.1 s
        self.assertAlmostEqual(refs.base_lat["q1"], 1.1)
        self.assertAlmostEqual(refs.q_j_ref["A"], (10 * 1.1 / 10 + 30 * 1.1 / 10) / 2)
        self.assertAlmostEqual(refs.c_ref_ns["A"], 500.0)
        self.assertAlmostEqual(refs.c_ref_ns["B"], 800.0)
        self.assertEqual(refs.mem_ref, [200.0, 300.0])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "refs.json")
            refs.save(path)
            self.assertEqual(M.Refs.load(path), refs)
        report = M.noise_report(steps, refs)
        self.assertEqual(report["B"]["cv_q_j"], 0.0)
        self.assertGreater(report["A"]["cv_q_j"], 0.5)
        self.assertEqual(report["A"]["windows"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
