"""moqoe.py 的专家过滤与冻结逻辑。重依赖全部用桩模块替代。"""
import os
import sys
import types
import unittest

REPO = os.environ.get("NEURQO", "/home/zhanhao/neuragent/NeuralDB/aiengine/neurqo_frame")


def stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _Any:
    def __init__(self, *a, **k):
        pass


stub("torch", device=lambda *_: None)
stub("common", BaseConfig=_Any, get_config=lambda *_: None)
stub("db")
stub("db.pg_conn", PostgresConnector=_Any)
stub("exp_buffer")
stub("exp_buffer.buffer_mngr", BufferManager=_Any)
stub("expert_pool")
stub("expert_pool.hint_plan_sel_expert", TreeCNNConfig=_Any, TreeCNNRegExpert=_Any)
stub("expert_pool.join_order_expert", MCTSConfig=_Any, MCTSOptimizerExpert=_Any)
stub("expert_router")
stub("expert_router.controller_offline", ModelBuilder=_Any)
sys.path.insert(0, os.path.join(REPO, "src"))
import moqoe  # noqa: E402


class FakeExpert:
    def __init__(self, tag):
        self.tag = tag
        self.trained = 0

    def sql_enhancement(self, sql, db_cli):
        return f"/*{self.tag}*/ {sql}", self.tag

    def train_and_save(self, train_data_limit):
        self.trained += 1
        return {}


class FakeRouter:
    def __init__(self, rank):
        self.rank = rank
        self.calls = 0

    def optimizer_selection(self, sql, db_cli):
        self.calls += 1
        return list(self.rank)

    @staticmethod
    def online_update(performance=None):
        return


def controller(rank, pool=("HintPlanSel", "JoinOrder"), online=True, trigger=10):
    c = object.__new__(moqoe.MoQOEController)
    c.config = None
    c.db_cli = None
    c.enable_online_training = online
    c.online_update_sql_trigger = trigger
    c.expert_pools = {name: FakeExpert(name) for name in pool}
    c.expert_chosen_count = {name: 0 for name in pool}
    c.routing_network = FakeRouter(rank)
    return c


RANK = ["PostgreSQL", "JoinOrder", "PlanGen", "HintPlanSel", "PlanGenSim"]
_out = open(os.devnull, "w")


class FilterTest(unittest.TestCase):
    def setUp(self):
        self._stdout, sys.stdout = sys.stdout, _out

    def tearDown(self):
        sys.stdout = self._stdout

    def test_default_argument_behaves_like_all(self):
        c = controller(RANK)
        self.assertEqual(c.inference("q"), ("/*JoinOrder*/ q", "JoinOrder"))
        self.assertEqual(c.routing_network.calls, 1)

    def test_all_uses_router_ranking(self):
        c = controller(["HintPlanSel", "JoinOrder"])
        self.assertEqual(c.inference("q", expert_filter="all")[1], "HintPlanSel")
        self.assertEqual(c.routing_network.calls, 1)

    def test_hint_skips_router(self):
        c = controller(RANK)
        self.assertEqual(c.inference("q", expert_filter="hint"), ("/*HintPlanSel*/ q", "HintPlanSel"))
        self.assertEqual(c.routing_network.calls, 0)

    def test_join_skips_router(self):
        c = controller(["HintPlanSel", "JoinOrder"])
        self.assertEqual(c.inference("q", expert_filter="join")[1], "JoinOrder")
        self.assertEqual(c.routing_network.calls, 0)

    def test_filter_without_loaded_expert_falls_back(self):
        c = controller(RANK, pool=("JoinOrder",))
        self.assertEqual(c.inference("q", expert_filter="hint"), ("q", "cost-based optimizer"))

    def test_unknown_filter_falls_back(self):
        c = controller(RANK)
        self.assertEqual(c.inference("q", expert_filter="bogus"), ("q", "cost-based optimizer"))
        self.assertEqual(c.routing_network.calls, 0)

    def test_router_error_falls_back(self):
        c = controller(RANK)
        c.routing_network.optimizer_selection = lambda **_: (_ for _ in ()).throw(RuntimeError("x"))
        self.assertEqual(c.inference("q"), ("q", "cost-based optimizer"))


class FreezeTest(unittest.TestCase):
    def setUp(self):
        self._stdout, sys.stdout = sys.stdout, _out

    def tearDown(self):
        sys.stdout = self._stdout

    def test_frozen_never_trains(self):
        c = controller(RANK, online=False)
        for _ in range(25):
            c.inference("q", expert_filter="join")
            c.inference("q", expert_filter="hint")
        self.assertEqual([e.trained for e in c.expert_pools.values()], [0, 0])
        self.assertEqual(c.expert_chosen_count, {"HintPlanSel": 25, "JoinOrder": 25})

    def test_unfrozen_trains_every_trigger(self):
        c = controller(RANK, online=True, trigger=10)
        for _ in range(25):
            c.inference("q")
        self.assertEqual(c.expert_pools["JoinOrder"].trained, 2)
        self.assertEqual(c.expert_pools["HintPlanSel"].trained, 0)

    def test_constructor_default_keeps_online_training(self):
        import inspect

        sig = inspect.signature(moqoe.MoQOEController.__init__)
        self.assertIs(sig.parameters["enable_online_training"].default, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
