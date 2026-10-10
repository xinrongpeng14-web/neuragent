"""Unit tests of the hierarchical GA (gaproto/hier, plan v0.6); no database needed."""
import json
import math
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto.hier import commands as C  # noqa: E402
from gaproto.hier import features as F  # noqa: E402
from gaproto.hier.config import load_hier_config  # noqa: E402
from gaproto.hier.env import Refs, episode_sequence, step_reward  # noqa: E402
from gaproto.hier.evaluate import episode_plan, summarize, write_refs  # noqa: E402
from gaproto.hier.policies import make_policy  # noqa: E402
from gaproto.hier.report import arm_table, verdict  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
Q26A_PATH = os.path.join(HERE, "..", "queries", "job_all", "26a.sql")   # created by deploy/load_imdb.sh
Q26A = ""
if os.path.isfile(Q26A_PATH):
    with open(Q26A_PATH) as _f:
        Q26A = _f.read()


class CommandsTest(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(C.NUM_COMMANDS, 9)
        for c in range(9):
            self.assertEqual(C.parse(C.name(c)), c)
        self.assertEqual(C.name(C.parse("auto/cost")), "auto/cost")
        with self.assertRaises(ValueError):
            C.parse("join/cost")
        with self.assertRaises(ValueError):
            C.decode(9)

    def test_session_sql(self):
        self.assertEqual(C.session_sql(C.parse("off/btree")),
                         ["SET enable_molqo = off", "SET selix.enable_index = off"])
        hp = C.session_sql(C.parse("hint/prefer"))
        self.assertIn("SET molqo.expert_filter = 'hint'", hp)
        self.assertIn("SET selix.index_cost_scale = 0.01", hp)
        self.assertIn("SET molqo.expert_filter = 'all'", C.session_sql(C.parse("auto/cost")))
        self.assertIn("SET selix.index_cost_scale = 0.999", C.session_sql(C.parse("auto/cost")))
        self.assertEqual(C.density_sql("dense")[0], "SET selix.init_density = 0.85")


@unittest.skipUnless(Q26A, "queries/job_all/26a.sql not present")
class FeaturesTest(unittest.TestCase):
    def setUp(self):
        rows = {t: 1000.0 for t in F.IMDB_TABLES}
        rows["cast_info"] = 36_261_908.0
        rows["title"] = 2_528_233.0
        selix = {("cast_info", "movie_id"), ("title", "id")}
        stats = {("cast_info", "movie_id"): F.ColumnStats(36_261_908.0, 2_331_601.0),
                 ("title", "id"): F.ColumnStats(2_528_233.0, 2_528_233.0)}
        self.cat = F.Catalog(rows, selix, stats)

    def test_parse_26a(self):
        p = F.parse_query(Q26A)
        self.assertEqual(p.n_relations, 12)
        self.assertEqual(len(p.tables), 11)            # comp_cast_type is joined twice
        self.assertIn(("cast_info", "movie_id"), p.join_cols)
        self.assertIn(("title", "id"), p.join_cols)
        self.assertIn(("cast_info", "person_role_id"), p.join_cols)

    def test_query_vector(self):
        q = F.QueryInfo("26a", Q26A, F.parse_query(Q26A), est_cost=1e6)
        v = F.query_vector(q, self.cat)
        self.assertAlmostEqual(v[0], 0.6)
        self.assertAlmostEqual(v[1], 12 / 17)
        self.assertAlmostEqual(v[2 + F.IMDB_TABLES.index("cast_info")], math.log10(1 + 36_261_908) / 8)
        self.assertEqual(v[2 + F.IMDB_TABLES.index("aka_name")], 0.0)
        self.assertGreater(v[23], 0.0)
        self.assertLess(v[23], 1.0)
        self.assertGreater(v[25], 0.0)                 # cast_info.movie_id repeats keys

    def test_observation(self):
        q = F.QueryInfo("26a", Q26A, F.parse_query(Q26A), est_cost=1e6)
        obs = F.observation(C.parse("hint/btree"), [q], self.cat, "short", 4,
                            F.LastStep(log_ratio=-9.0, nqo_time_share=0.1, nqo_applied_share=0.5,
                                       selix_scan_share=0.25))
        self.assertEqual(obs.shape, (F.OBS_DIM,))
        self.assertEqual(obs.dtype, np.float32)
        self.assertEqual(list(obs[:6]), [0, 1, 0, 1, 0, 0])
        self.assertEqual(obs[32], 1.0)
        self.assertAlmostEqual(float(obs[33]), 0.5)
        self.assertEqual(obs[34], -3.0)                # clipped
        self.assertAlmostEqual(float(obs[37]), 0.25)
        empty = F.observation(0, [], self.cat, "long", 1)
        self.assertTrue(np.all(empty[6:32] == 0))


class EnvLogicTest(unittest.TestCase):
    def test_reward(self):
        refs = Refs({"a": {"net_s": 2.0}, "b": {"net_s": 1.0}})
        r, m = step_reward([{"query": "a", "net_s": 1.0}, {"query": "b", "net_s": 0.5}], refs, 0.02, 50)
        self.assertAlmostEqual(m, 0.5)
        self.assertAlmostEqual(r, math.log(2))
        r, m = step_reward([{"query": "a", "net_s": 2.0}], refs, 0.02, 50)
        self.assertAlmostEqual(r, 0.0)
        r, _ = step_reward([{"query": "a", "net_s": 1e6}], refs, 0.02, 50)
        self.assertAlmostEqual(r, -math.log(50))      # bounded
        self.assertEqual(step_reward([{"query": "a", "net_s": 1.0}], None, 0.02, 50), (0.0, None))

    def test_sequence(self):
        s1 = episode_sequence(["a", "b", "c", "d"], 3, 7)
        self.assertEqual(sorted(s1), sorted(["a", "b", "c", "d"] * 3))
        self.assertEqual(s1, episode_sequence(["a", "b", "c", "d"], 3, 7))
        self.assertNotEqual(s1, episode_sequence(["a", "b", "c", "d"], 3, 8))

    def test_config(self):
        cfg = load_hier_config(os.path.join(HERE, "..", "config", "hier_short.json"))
        self.assertEqual((cfg.step_queries, cfg.workers, cfg.group), (5, 4, "short"))
        cfg = load_hier_config(os.path.join(HERE, "..", "config", "hier_long.json"))
        self.assertEqual((cfg.step_queries, cfg.workers, cfg.repeats), (1, 1, 3))
        self.assertEqual(load_hier_config(None, {"step_queries": 1, "clients": 4}).workers, 1)
        with self.assertRaises(ValueError):
            load_hier_config(None, {"group": "medium"})
        with self.assertRaises(ValueError):
            load_hier_config(None, {"nonsense": 1})


def _rec(cmd, results, reward=0.0):
    return {"command": cmd, "results": results, "reward": reward, "switched": 0}


def _res(q, t, correct=True, status="ok", result="h"):
    return {"query": q, "net_s": t, "wall_s": t, "status": status, "correct": correct, "result": result}


class EvaluateReportTest(unittest.TestCase):
    def test_plan_rotates(self):
        plan = episode_plan(["G", "O", "PG"], [1, 2], 1)
        self.assertEqual([a for a, _, _ in plan], ["G", "O", "PG", "O", "PG", "G"])

    def test_summarize_and_refs(self):
        recs = [_rec("auto/cost", [_res("26a", 15.0)], -0.1), _rec("hint/btree", [_res("28a", 0.4, False)], 0.5)]
        s = summarize("G", 2001, 0, recs, 20.0)
        self.assertEqual(s["total_net_s"], 15.4)
        self.assertEqual(s["wrong_results"], 1)
        self.assertEqual(s["commands"], {"auto/cost": 1, "hint/btree": 1})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "refs.json")
            data = write_refs(path, [_rec("auto/cost", [_res("26a", 15.0, result="x")]),
                                     _rec("auto/cost", [_res("26a", 17.0, result="x")]),
                                     _rec("auto/cost", [_res("26a", 16.0, result="y")])])
            self.assertEqual(data["queries"]["26a"]["net_s"], 16.0)
            self.assertEqual(data["queries"]["26a"]["result"], "x")
            self.assertEqual(data["queries"]["26a"]["distinct_results"], 2)
            self.assertEqual(Refs.load(path).latency("26a"), 16.0)

    def _eps(self, g, o):
        out = []
        for seed, (gt, ot) in enumerate(zip(g, o)):
            out.append({"arm": "G", "seed": seed, "total_net_s": gt, "wrong_results": 0, "not_ok": 0,
                        "commands": {"hint/btree": 1}, "per_query_s": {"q": [gt]}})
            out.append({"arm": "O", "seed": seed, "total_net_s": ot, "wrong_results": 0, "not_ok": 0,
                        "commands": {"auto/cost": 1}, "per_query_s": {"q": [ot]}})
        return out

    def test_verdict(self):
        self.assertIn("**可行**", "\n".join(verdict(self._eps([70, 71, 69], [85, 86, 84]))))
        self.assertIn("**有潜力**", "\n".join(verdict(self._eps([80, 90, 80], [85, 86, 84]))))
        self.assertIn("**不可行**", "\n".join(verdict(self._eps([90, 91, 92], [85, 86, 84]))))
        eps = self._eps([70, 71, 69], [85, 86, 84])
        eps[0]["wrong_results"] = 1
        self.assertIn("**结果有误**", "\n".join(verdict(eps)))
        table = "\n".join(arm_table(self._eps([70], [100])))
        self.assertIn("-30.0%", table)

    def test_policies(self):
        self.assertEqual(make_policy("original").act(None), C.parse("auto/cost"))
        self.assertEqual(make_policy("fixed:off/btree").act(None), C.parse("off/btree"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sb.json")
            with open(p, "w") as f:
                json.dump({"command": "hint/prefer"}, f)
            self.assertEqual(make_policy("static-best", p).act(None), C.parse("hint/prefer"))
        with self.assertRaises(SystemExit):
            make_policy("static-best", "/nonexistent.json")
        r = make_policy("random", seed=1)
        self.assertTrue(all(0 <= r.act(None) < 9 for _ in range(50)))


if __name__ == "__main__":
    unittest.main()
