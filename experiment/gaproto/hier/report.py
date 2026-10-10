"""Report of the hierarchical experiment for one group: baseline, sweep, training, evaluation.

Verdict (plan section 7): feasible when G's total latency is more than 10% below O's,
in the same direction for every seed, and G's p99 is not worse than O's.

Usage: python -m gaproto.hier.report --config config/hier_long.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from typing import Any, Dict, List

import numpy as np

from .config import load_hier_config


def _load(path: str):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def arm_table(episodes: List[Dict[str, Any]], ref_arm: str = "O") -> List[str]:
    by_arm: Dict[str, List[Dict[str, Any]]] = collections.OrderedDict()
    for e in episodes:
        by_arm.setdefault(e["arm"], []).append(e)
    ref = np.mean([e["total_net_s"] for e in by_arm[ref_arm]]) if ref_arm in by_arm else None
    L = ["| 组 | 回合数 | 总延迟（秒，均值 ± 标准差） | 相对 O | p50（秒） | p99（秒） | 结果错误 | 超时或出错 | 命令分布 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for arm, es in by_arm.items():
        tot = [e["total_net_s"] for e in es]
        rel = f"{(np.mean(tot) / ref - 1) * 100:+.1f}%" if ref else "—"
        cmds = collections.Counter()
        for e in es:
            cmds.update(e["commands"])
        lat = [x for e in es for v in e["per_query_s"].values() for x in v]
        L.append(f"| {arm} | {len(es)} | {np.mean(tot):.2f} ± {np.std(tot):.2f} | {rel} | "
                 f"{np.percentile(lat, 50):.3f} | {np.percentile(lat, 99):.3f} | "
                 f"{sum(e['wrong_results'] for e in es)} | {sum(e['not_ok'] for e in es)} | "
                 f"{', '.join(f'{k} {v}' for k, v in cmds.most_common())} |")
    return L


def verdict(episodes: List[Dict[str, Any]], g: str = "G", o: str = "O") -> List[str]:
    G = [e for e in episodes if e["arm"] == g]
    O = [e for e in episodes if e["arm"] == o]
    if not G or not O:
        return [f"没有 {g} 或 {o} 组的回合，不做判定。"]
    seeds = sorted({e["seed"] for e in G} & {e["seed"] for e in O})
    per_seed = []
    for s in seeds:
        gs = np.mean([e["total_net_s"] for e in G if e["seed"] == s])
        os_ = np.mean([e["total_net_s"] for e in O if e["seed"] == s])
        per_seed.append((s, gs, os_))
    g_tot, o_tot = np.mean([e["total_net_s"] for e in G]), np.mean([e["total_net_s"] for e in O])
    gain = 1 - g_tot / o_tot
    same_dir = all(gs < os_ for _, gs, os_ in per_seed)
    g_p99 = np.percentile([x for e in G for v in e["per_query_s"].values() for x in v], 99)
    o_p99 = np.percentile([x for e in O for v in e["per_query_s"].values() for x in v], 99)
    wrong = sum(e["wrong_results"] for e in G)
    L = ["| 种子 | G 总延迟（秒） | O 总延迟（秒） | G 相对 O |", "|---|---|---|---|"]
    L += [f"| {s} | {gs:.2f} | {os_:.2f} | {(gs / os_ - 1) * 100:+.1f}% |" for s, gs, os_ in per_seed]
    if wrong:
        v = f"**结果有误**：G 组有 {wrong} 次执行的结果与原版不同，先排查，不做判定"
    elif gain > 0.10 and same_dir and g_p99 <= o_p99:
        v = "**可行**：G 的总延迟比 O 低 10% 以上，各种子方向一致，p99 不劣于 O"
    elif g_tot < o_tot:
        v = "**有潜力**：G 比 O 低，但未同时满足“低 10% 以上、各种子方向一致、p99 不劣于 O”"
    else:
        v = "**不可行**：G 不优于 O（看训练日志中是否存在明显更好的命令，区分“不可行”与“学习失败”）"
    sb = [e["total_net_s"] for e in episodes if e["arm"] == "static-best"]
    sb_line = (f"参考：static-best 总延迟 {np.mean(sb):.2f} s，G 相对 static-best "
               f"{(g_tot / np.mean(sb) - 1) * 100:+.1f}%（低于 0 说明 G 的按步切换优于最好的固定命令）。") if sb else ""
    L += ["", f"G 总延迟 {g_tot:.2f} s，O {o_tot:.2f} s，G 比 O 低 {gain * 100:.1f}%；"
              f"p99：G {g_p99:.3f} s，O {o_p99:.3f} s。", "", v] + (["", sb_line] if sb_line else [])
    return L


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Report of the hierarchical GA experiment")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="", help="default: <log_dir>/report.md")
    args = ap.parse_args(argv)
    cfg = load_hier_config(args.config)
    d = cfg.log_dir
    L = [f"# 分层 GA 实验报告（{cfg.group} 组）", "",
         f"查询目录 `{cfg.query_dir}`，每步 {cfg.step_queries} 条查询，{cfg.workers} 个会话并行，"
         f"每回合每条查询 {cfg.repeats} 次，SELIX 密度 {cfg.density}。延迟均为不含 SELIX 构建的净时间。", ""]

    refs = _load(cfg.refs_path)
    if refs:
        L += ["## 1. 原版基线（参考延迟）", "", "| 查询 | 中位延迟（秒） | 次数 | 结果是否稳定 |", "|---|---|---|---|"]
        L += [f"| {q} | {v['net_s']:.3f} | {v['runs']} | {'是' if v['distinct_results'] == 1 else '否'} |"
              for q, v in refs["queries"].items()]
        L.append("")
    sb = _load(cfg.static_best_path)
    if sb:
        L += ["## 2. 九种固定命令的扫描（选出 static-best）", "", "| 命令 | 一个回合的总延迟（秒） |", "|---|---|"]
        L += [f"| {k} | {v:.2f} |" for k, v in sorted(sb["sweep"].items(), key=lambda kv: kv[1])]
        L += ["", f"static-best = **{sb['command']}**", ""]
    prog = _load(os.path.join(d, "train", "progress.json"))
    if prog and prog["episodes"]:
        eps = prog["episodes"]
        L += ["## 3. 训练", "", f"{len(eps)} 个回合，{prog.get('total_steps', '?')} 步，"
              f"用时 {prog.get('wall_s', 0) / 3600:.2f} 小时；超参数 {prog['hyperparameters']}。", "",
              "| 回合 | 平均奖励 | 查询总延迟（秒） | 命令分布 |", "|---|---|---|---|"]
        show = eps if len(eps) <= 20 else eps[:5] + eps[len(eps) // 2 - 2:len(eps) // 2 + 3] + eps[-10:]
        L += [f"| {e['episode']} | {e['mean_reward']:+.3f} | {e['total_net_s']:.1f} | "
              f"{', '.join(f'{k} {v}' for k, v in sorted(e['commands'].items(), key=lambda kv: -kv[1]))} |"
              for e in show]
        L.append("")
    ev = _load(os.path.join(d, "eval", "episodes.json"))
    if ev:
        L += ["## 4. 评估", ""] + arm_table(ev) + ["", "## 5. 判定（G 对 O）", ""] + verdict(ev) + [""]
    out = args.out or os.path.join(d, "report.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L[-6:]))
    print(f"written: {out}")


if __name__ == "__main__":
    main()
