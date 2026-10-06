"""Unit tests of the modules added for the comparison experiment: per-process
accounting, cgroup limits, policies, evaluation plan and report aggregation."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto import actions as A  # noqa: E402
from gaproto import metrics as M  # noqa: E402
from gaproto import procstat as P  # noqa: E402
from gaproto import report as R  # noqa: E402
from gaproto.cgroup import CgroupReader, parse_cpu_max, parse_cpuset  # noqa: E402
from gaproto.config import RewardConfig  # noqa: E402
from gaproto.evaluate import episode_plan, summarize_episode  # noqa: E402
from gaproto.policies import FixedPolicy, PhaseTablePolicy, make_policy, parse_action  # noqa: E402


class ProcstatTest(unittest.TestCase):
    def test_parse_stat(self):
        line = "1234 (postgres: neurdb imdb_ori) S 1 1234 1234 0 -1 4194560 100 0 0 0 250 150 0 0 20 0 1 0 5 1000 200 18446744073709551615"
        comm, cpu = P.parse_stat(line)
        self.assertEqual(comm, "postgres: neurdb imdb_ori")
        self.assertAlmostEqual(cpu, 400 / P._CLK_TCK)

    def test_parse_pss(self):
        self.assertEqual(P.parse_pss("Rss:  1828 kB\nPss:   566 kB\n"), 566 * 1024)
        self.assertIsNone(P.parse_pss("Rss: 1 kB\n"))

    def test_classify(self):
        act = {10: ("client backend", "ga_job"), 11: ("client backend", "ga_ycsb"),
               12: ("client backend", ""), 13: ("checkpointer", ""), 14: ("client backend", "ga_admin")}
        self.assertEqual(P.classify("postgres", "postgres: neurdb imdb_ori", 10, act), "pg_job")
        self.assertEqual(P.classify("postgres", "postgres: ...", 11, act), "pg_ycsb")
        self.assertEqual(P.classify("postgres", "postgres: ...", 12, act), "pg_nqo_client")
        self.assertEqual(P.classify("postgres", "postgres: ...", 13, act), "pg_background")
        self.assertEqual(P.classify("postgres", "postgres: ...", 14, act), "pg_admin")
        self.assertEqual(P.classify("postgres", "/opt/neurdb/bin/postgres -D /data/pg", 1, act), "pg_background")
        self.assertEqual(P.classify("python", "/opt/venv/bin/python run.py --workers 4 --port 8666", 20, act), "nqo_server")
        self.assertEqual(P.classify("python", "/opt/venv/bin/python -m gaproto.train --config x", 21, act), "harness")
        self.assertEqual(P.classify("python", "/opt/venv/bin/python -c from multiprocessing.spawn import spawn_main; "
                                    "spawn_main(tracker_fd=5, pipe_handle=7) --multiprocessing-fork", 23, act), "drivers")
        self.assertEqual(P.classify("bash", "bash", 22, act), "other")

    def test_delta_and_groups(self):
        before = P.ProcSample(cpu_s={1: 10.0, 2: 5.0}, group={1: "pg_job", 2: "nqo_server"},
                              rss={1: 100, 2: 200}, pss={1: 50, 2: None})
        after = P.ProcSample(cpu_s={1: 12.5, 3: 1.0}, group={1: "pg_job", 3: "pg_job"},
                             rss={1: 100, 3: 300}, pss={1: 50, 3: 70})
        s = P.summarize(before, after)
        self.assertAlmostEqual(s["cpu_s"]["pg_job"], 3.5)       # 2.5 + the new process's 1.0
        self.assertNotIn("nqo_server", s["cpu_s"])                # ended processes are lost
        self.assertEqual(s["rss_bytes"]["pg_job"], 400)
        self.assertEqual(s["pss_bytes"]["pg_job"], 120)
        self.assertEqual(s["count"]["pg_job"], 2)

    def test_sampler_on_fake_proc(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "77"))
            with open(os.path.join(d, "77", "stat"), "w") as f:
                f.write("77 (python) R 1 77 77 0 -1 4194304 0 0 0 0 300 100 0 0 20 0 1 0 1 1000 50 0\n")
            with open(os.path.join(d, "77", "cmdline"), "wb") as f:
                f.write(b"/opt/venv/bin/python\0run.py\0--workers\0")
            with open(os.path.join(d, "77", "statm"), "w") as f:
                f.write("1000 50 10 1 0 40 0\n")
            with open(os.path.join(d, "77", "smaps_rollup"), "w") as f:
                f.write("Rss: 200 kB\nPss: 120 kB\n")
            os.makedirs(os.path.join(d, "notapid"))
            s = P.ProcSampler(None, proc_root=d).sample()
        self.assertEqual(s.group, {77: "nqo_server"})
        self.assertAlmostEqual(s.cpu_s[77], 400 / P._CLK_TCK)
        self.assertEqual(s.rss[77], 50 * P._PAGE)
        self.assertEqual(s.pss[77], 120 * 1024)


class CgroupLimitsTest(unittest.TestCase):
    def test_parse_cpu_max(self):
        self.assertAlmostEqual(parse_cpu_max("150000 100000\n"), 1.5)
        self.assertIsNone(parse_cpu_max("max 100000\n"))

    def test_parse_cpuset(self):
        self.assertEqual(parse_cpuset("0-3,8\n"), 5)
        self.assertEqual(parse_cpuset("2"), 1)

    def test_detect_from_directory(self):
        with tempfile.TemporaryDirectory() as d:
            for name, content in (("cpu.stat", "usage_usec 100\n"), ("memory.current", "4096\n"),
                                  ("cpu.max", "400000 100000\n"), ("memory.max", "2147483648\n")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(content)
            r = CgroupReader("", 0, cgroup_dir=d)
            self.assertEqual(r.ncpus, 4.0)
            self.assertEqual(r.limits()["memory_max"], 2147483648)
            self.assertEqual(r.sample().memory_bytes, 4096)


class PolicyTest(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(make_policy("original").act(None, "A"), A.ORIGINAL_ACTION)
        self.assertEqual(make_policy("fixed:off/dense").act(None, "B"), A.encode("off", "dense"))
        t = make_policy("table:A=off/dense,B=auto/default")
        self.assertEqual(t.act(None, "A"), A.encode("off", "dense"))
        self.assertEqual(t.act(None, "B"), A.encode("auto", "default"))
        with self.assertRaises(KeyError):
            t.act(None, "C")
        self.assertEqual(parse_action(" hint / mid "), A.encode("hint", "mid"))
        for bad in ("fixed:", "fixed:off", "fixed:up/down", "table:A", "nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                make_policy(bad)
        self.assertIsInstance(FixedPolicy(0), FixedPolicy)
        self.assertIn("A=off/dense", PhaseTablePolicy({"A": A.encode("off", "dense")}).name)

    def test_random_in_range(self):
        p = make_policy("random", seed=3)
        self.assertTrue(all(0 <= p.act(None, "A") < A.NUM_ACTIONS for _ in range(50)))


class EvaluatePlanTest(unittest.TestCase):
    def test_alternating_plan(self):
        plan = episode_plan(["x", "y", "z"], [1, 2], 2, alternate=True)
        self.assertEqual(len(plan), 12)
        self.assertEqual([p[0] for p in plan[:3]], ["x", "y", "z"])
        self.assertEqual([p[0] for p in plan[3:6]], ["y", "z", "x"])       # rotated
        self.assertEqual({p[1] for p in plan}, {1, 2})
        seq = episode_plan(["x", "y"], [1], 2, alternate=False)
        self.assertEqual([p[0] for p in seq], ["x", "y", "x", "y"])


def _metrics(job=((("1a", 0.5),) * 4), elapsed=10.0, c_ns=300.0, sampled=100, mem=20 * 1048576, smo=2,
             cpu=None):
    return {"elapsed_s": elapsed, "cpu_util": 0.5, "container_mem_bytes": 500 * 1048576,
            "job_completed": [list(x) for x in job], "job_errors": 0, "job_lat_sum_s": sum(l for _, l in job),
            "job_p50_s": 0.5, "job_p99_s": 0.9, "ycsb_reads": 900, "ycsb_inserts": 100, "ycsb_errors": 0,
            "ycsb_misses": 0, "ycsb_lat_sum_s": 1.0, "ycsb_p50_s": 0.001, "ycsb_p99_s": 0.002, "ycsb_alive": True,
            "idx_gets": 900, "idx_puts": 100, "idx_sampled": sampled, "idx_sampled_ns": int(c_ns * sampled),
            "idx_smo": smo, "idx_mem_bytes": mem, "idx_keys": 1000, "idx_density": [0.7, 0.8, 0.6],
            "nqo_requests": 4, "nqo_opt_time_ms": 40.0, "nqo_reachable": True,
            "nqo_counters": {"requests": 4, "expert_JoinOrder": 4},
            "proc_cpu_s": cpu or {"pg_job": 5.0, "nqo_server": 1.0, "harness": 0.1},
            "proc_rss_bytes": {"pg_job": 1048576}, "proc_pss_bytes": {"pg_job": 1048576, "nqo_server": 2097152},
            "proc_count": {"pg_job": 2}, "c_idx_ns": c_ns}


def _records(tag, episode, src="eval", obs=None, action=A.ORIGINAL_ACTION, **kw):
    recs = []
    for step in range(4):
        ph = "A" if step < 2 else "B"
        recs.append({"event": "step", "tag": tag, "episode": episode, "step": step, "phase": ph,
                     "action": action, "switched": 0, "reward": 0.0,
                     "obs_in": obs or [0.5, 0.2, 0.1, 0.05, 0.0, 0.0, 2.0, 0.33, 0.5],
                     "obs": obs or [0.5, 0.2, 0.1, 0.05, 0.0, 0.0, 2.0, 0.33, 0.5],
                     "metrics": _metrics(**kw), "_src": src})
    return recs


class ReportTest(unittest.TestCase):
    def refs(self):
        return M.Refs(base_lat={"1a": 0.5}, q_j_ref={"A": 0.2, "B": 0.2}, c_ref_ns={"A": 300.0, "B": 300.0},
                      mem_ref=[20 * 1048576] * 4)

    def test_reward_parts_zero_for_reference_behaviour(self):
        rec = _records("none", 0)[0]
        parts = R.reward_parts(rec, self.refs(), RewardConfig())
        self.assertAlmostEqual(parts["q_j"], 4 * 0.5 / 10.0)
        self.assertAlmostEqual(parts["r_job"], 0.0)
        self.assertAlmostEqual(parts["r_total"], 0.0)

    def test_reward_parts_improvement(self):
        rec = _records("x", 0, c_ns=150.0, mem=10 * 1048576)[0]
        parts = R.reward_parts(rec, self.refs(), RewardConfig())
        self.assertAlmostEqual(parts["r_selix_cost"], np.log(2.0))
        self.assertAlmostEqual(parts["r_selix_mem"], 0.5 * np.log(2.0))
        self.assertAlmostEqual(parts["r_total"], 0.5 * 1.5 * np.log(2.0))

    def test_phase_summary_and_arm_summary(self):
        recs = _records("ga", 0) + _records("ga", 1, c_ns=200.0)
        s = R.phase_summary([r for r in recs if r["episode"] == 0 and r["phase"] == "A"], self.refs(),
                            RewardConfig(), ncpus=2.0)
        self.assertEqual(s["steps"], 2)
        self.assertAlmostEqual(s["cores"]["pg_job"], 10.0 / 20.0)
        self.assertAlmostEqual(s["cores"]["total"], 6.1 * 2 / 20.0)
        self.assertAlmostEqual(s["pss_mb"]["nqo_server"], 2.0)
        self.assertAlmostEqual(s["ycsb_ops_per_s"], 2000 / 20.0)
        self.assertEqual(s["nqo_counters"]["expert_JoinOrder"], 8)
        arm = R.arm_summary(recs, self.refs(), RewardConfig(), 2.0, ["A", "B"])
        self.assertEqual(arm["episodes"], 2)
        self.assertEqual(set(arm["phases"]), {"A", "B", "all"})
        mean, sd = arm["phases"]["all"]["c_idx_ns"]
        self.assertAlmostEqual(mean, 250.0)
        self.assertGreater(sd, 0)
        self.assertEqual(arm["phases"]["all"]["actions"], {"auto/default": 8})

    def test_render_and_main(self):
        with tempfile.TemporaryDirectory() as d:
            refs = self.refs()
            refs_path = os.path.join(d, "refs.json")
            refs.save(refs_path)
            log = os.path.join(d, "steps.jsonl")
            with open(log, "w") as f:
                f.write(json.dumps({"event": "open", "cgroup": {"ncpus": 2.0, "cpu_max": "200000 100000",
                                                                 "memory_max": None}}) + "\n")
                for tag, ep, kw in (("none", 0, {}), ("nqo", 1, {"c_ns": 280.0}),
                                    ("ga", 2, {"c_ns": 150.0, "mem": 15 * 1048576})):
                    f.write(json.dumps({"event": "reset", "tag": tag, "episode": ep, "episode_seed": 7}) + "\n")
                    for r in _records(tag, ep, action=A.encode("off", "dense") if tag == "ga" else A.ORIGINAL_ACTION, **kw):
                        r = dict(r)
                        r.pop("_src")
                        f.write(json.dumps(r) + "\n")
            out = os.path.join(d, "report.md")
            js = os.path.join(d, "summary.json")
            with contextlib.redirect_stdout(io.StringIO()):
                R.main(["--runs", log, "--refs", refs_path, "--reference", "none", "--out", out, "--json", js])
            with open(out, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("## 1. 收益", text)
            self.assertIn("## 5. 判定", text)
            self.assertIn("off/dense", text)
            with open(js, encoding="utf-8") as f:
                summary = json.load(f)
            self.assertEqual(summary["reference"], "none")
            self.assertEqual(set(summary["arms"]), {"none", "nqo", "ga"})
            ga = summary["arms"]["ga"]["phases"]["all"]
            self.assertGreater(ga["r_total"][0], summary["arms"]["none"]["phases"]["all"]["r_total"][0])

    def test_summarize_episode(self):
        recs = _records("ga", 0)
        for r in recs:
            r["reward"] = 0.1
        s = summarize_episode("ga", 2001, 0, recs, 12.0)
        self.assertEqual(s["steps"], 4)
        self.assertAlmostEqual(s["return"], 0.4)
        self.assertEqual(set(s["phases"]), {"A", "B"})
        self.assertEqual(s["actions"], {"auto/default": 4})


if __name__ == "__main__":
    unittest.main(verbosity=1)


class Round2Test(unittest.TestCase):
    def test_phase_query_dirs(self):
        from gaproto.config import load_config
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"job": {"query_dir": "q/fast"},
                           "phases": [{"name": "A", "steps": 2, "job_clients": 2, "query_dir": "q/long"},
                                      {"name": "B", "steps": 2, "job_clients": 4},
                                      {"name": "C", "steps": 1, "job_clients": 1, "query_dir": "q/long"}]}, f)
            cfg = load_config(path)
        self.assertEqual(cfg.query_dirs(), ["q/long", "q/fast"])
        self.assertEqual(cfg.phases[1].query_dir, "")

    def test_presets(self):
        self.assertEqual(tuple(A.SELIX_PRESETS), ("dense", "mid", "default"))
        self.assertEqual(A.decode(A.ORIGINAL_ACTION), ("auto", "default"))
        for name, (i, x, n) in A.SELIX_PRESETS.items():
            self.assertTrue(0 < n < i < x <= 1, name)

    def test_plan_gain_helpers(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
        import nqo_plan_gain as G
        prefix = "SET enable_nestloop TO off;\nSET enable_hashjoin TO off;\nSET enable_hashjoin TO on;"
        self.assertEqual(G.arm_label(prefix), "off: nestloop")
        stmts = G.variant_statements("SELECT 1", "hint", prefix)
        self.assertEqual(stmts[-1], "SELECT 1")
        self.assertTrue(all(s.startswith("SET LOCAL ") for s in stmts[:-1]))
        self.assertEqual(G.variant_statements("SELECT 1", "join", "/*+Leading(a b)*/"), ["/*+Leading(a b)*/ SELECT 1"])
        results = [{"query": "1a", "experts": {}, "off_s": None, "off_status": "not run", "off_cost": None},
                   {"query": "2a", "experts": {"hint": {"expert": "HintPlanSel", "action": prefix, "label": "off: nestloop",
                                                        "opt_ms": 100.0, "cost": 200.0, "s": 0.5, "status": "ok"}},
                    "off_s": 1.0, "off_status": "ok", "off_cost": 100.0}]
        text, wins = G.render(results, 2)
        self.assertIn("| 2a | 1.00 s | hint | 0.50 s | 0.50 | 2.00 | 100 | off: nestloop |", text)
        self.assertEqual(wins, [("2a", "hint", 0.5)])
