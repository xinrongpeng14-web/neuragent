"""测试用的假控制器: 不加载模型, 不连数据库。输出格式与真实专家一致。"""
import os
import subprocess
import time

# HintPlanSel 的输出格式: 若干 "SET x TO y;\n" 加原 SQL (Bao 的臂: 先全关再开一部分)
ARM = ["enable_nestloop", "enable_hashjoin", "enable_mergejoin",
       "enable_seqscan", "enable_indexscan", "enable_indexonlyscan"]
ARM_ON = ["enable_nestloop", "enable_seqscan", "enable_indexscan"]


class FakeController:
    def __init__(self, args):
        self.freeze = args.freeze

    def inference(self, sql, expert_filter="all"):
        time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
        if "boom" in sql:
            raise RuntimeError("boom")
        if "plain" in sql:
            return sql, "cost-based optimizer"
        callback = os.environ.get("FAKE_CALLBACK")
        if callback:
            # 模拟真实专家: 优化过程中回连数据库执行 EXPLAIN
            env = dict(os.environ)
            if callback == "guarded":
                env["PGOPTIONS"] = "-c enable_molqo=off"
            subprocess.run(["psql", "-d", "neurdb", "-X", "-q", "-At", "-c", "EXPLAIN " + sql],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if expert_filter == "hint" and os.environ.get("FAKE_REAL_FORMAT"):
            sets = [f"SET {g} TO off" for g in ARM] + [f"SET {g} TO on" for g in ARM_ON]
            return ";\n".join(sets) + ";\n" + sql, "HintPlanSel"
        hint = os.environ.get("FAKE_HINT", "/*+ Leading(a b) */")
        name = {"hint": "HintPlanSel", "join": "JoinOrder"}.get(expert_filter, "JoinOrder")
        return hint + " " + sql, name


def make(args):
    return FakeController(args)
