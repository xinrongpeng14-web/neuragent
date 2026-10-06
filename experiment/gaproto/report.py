"""Build the result report from the step logs.

Input: one or more steps.jsonl files written by the environment (baseline,
evaluation, training). Records carry the arm name in "tag". The report answers
the two questions of the experiment:

  1. resources used and benefit obtained by every arm (NQO alone, SELIX alone,
     both without coordination, the Global Agent, neither), per workload phase;
  2. the states and actions seen at run time: what NQO received and answered
     (/stats counters), which SELIX densities were in effect, and the Global
     Agent's observations and action choices.

Rewards are recomputed from the raw metrics with the reference values, so that
the arms measured without references (the baseline run) are comparable.

Usage:
  python -m gaproto.report --refs runs/imdb/refs.json --runs runs/imdb/eval/steps.jsonl \\
      [--runs runs/imdb/baseline/steps.jsonl] [--train runs/imdb/train/steps.jsonl] \\
      --reference none --out runs/imdb/report.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import actions as A
from . import metrics as M
from .config import RewardConfig, load_config
from .procstat import GROUPS

Record = Dict[str, Any]
MB = 1048576.0

# resource groups shown in the tables, in this order
CPU_GROUPS = ("nqo_server", "pg_nqo_client", "pg_job", "pg_ycsb", "pg_background", "harness", "drivers", "other")
MEM_GROUPS = ("nqo_server", "pg_nqo_client", "pg_job", "pg_ycsb", "pg_background")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def read_log(path: str) -> List[Record]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_runs(paths: Sequence[str]) -> Tuple[Dict[str, List[Record]], Dict[str, Any]]:
    """steps grouped by arm; plus metadata (cgroup limits, seeds per episode)."""
    arms: Dict[str, List[Record]] = defaultdict(list)
    meta: Dict[str, Any] = {"cgroup": None, "seeds": {}}
    for path in paths:
        for i, rec in enumerate(read_log(path)):
            ev = rec.get("event")
            if ev == "open":
                meta["cgroup"] = rec.get("cgroup") or meta["cgroup"]
            elif ev == "reset":
                meta["seeds"][(path, rec.get("tag", ""), rec["episode"])] = rec.get("episode_seed")
            elif ev == "step":
                rec["_src"] = path
                arms[rec.get("tag") or "untagged"].append(rec)
    return dict(arms), meta


def episodes_of(records: List[Record]) -> Dict[Tuple[str, int], List[Record]]:
    eps: Dict[Tuple[str, int], List[Record]] = defaultdict(list)
    for r in records:
        eps[(r["_src"], r["episode"])].append(r)
    for v in eps.values():
        v.sort(key=lambda r: r["step"])
    return eps


# --------------------------------------------------------------------------
# recomputed reward
# --------------------------------------------------------------------------
def log_ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return math.log(a / b)


def reward_parts(rec: Record, refs: Optional[M.Refs], w: RewardConfig) -> Dict[str, Optional[float]]:
    """r_job, r_selix_cost, r_selix_mem and total without the switch penalty."""
    m = rec["metrics"]
    if refs is None:
        return {"r_job": None, "r_selix_cost": None, "r_selix_mem": None, "r_total": None, "q_j": None}
    q_j = M.standardised_job_throughput([tuple(x) for x in m["job_completed"]], refs.base_lat, m["elapsed_s"])
    q_ref = refs.q_j_ref.get(rec["phase"])
    if q_ref and q_j <= 0:
        r_job: Optional[float] = M.OBS_LOW
    else:
        r_job = log_ratio(q_j, q_ref)
    r_cost = log_ratio(refs.c_ref_ns.get(rec["phase"]), m["c_idx_ns"])
    lm = log_ratio(m["idx_mem_bytes"] or None, refs.mem_at(rec["step"]))
    r_mem = None if lm is None else -w.w_mem * lm
    parts = [w.w_job * (r_job or 0.0), w.w_selix * ((r_cost or 0.0) + (r_mem or 0.0))]
    return {"r_job": r_job, "r_selix_cost": r_cost, "r_selix_mem": r_mem, "r_total": sum(parts), "q_j": q_j}


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
def _mean_sd(values: Iterable[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    v = np.asarray([x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))],
                   dtype=np.float64)
    if v.size == 0:
        return None, None
    return float(v.mean()), (float(v.std(ddof=1)) if v.size > 1 else 0.0)


def phase_summary(steps: List[Record], refs: Optional[M.Refs], w: RewardConfig, ncpus: float) -> Dict[str, Any]:
    """Aggregate the steps of one episode-phase."""
    m = [r["metrics"] for r in steps]
    elapsed = sum(x["elapsed_s"] for x in m)
    done = [tuple(x) for y in m for x in y["job_completed"]]
    lat = np.asarray([l for _, l in done], dtype=np.float64)
    sampled = sum(x["idx_sampled"] for x in m)
    parts = [reward_parts(r, refs, w) for r in steps]
    cpu = {g: sum(x.get("proc_cpu_s", {}).get(g, 0.0) for x in m) / elapsed if elapsed else 0.0
           for g in GROUPS}
    cpu["total"] = sum(cpu.values())
    pss = {g: float(np.mean([x.get("proc_pss_bytes", {}).get(g, 0) for x in m])) / MB for g in GROUPS}
    counters: Dict[str, float] = defaultdict(float)
    for x in m:
        for k, v in (x.get("nqo_counters") or {}).items():
            counters[k] += v
    out = {
        "steps": len(steps),
        "elapsed_s": elapsed,
        "q_j": (sum(refs.base_lat.get(t, l) for t, l in done) / elapsed) if (refs and elapsed) else None,
        "job_done_per_step": len(done) / len(steps),
        "job_p50_s": float(np.percentile(lat, 50)) if lat.size else None,
        "job_p99_s": float(np.percentile(lat, 99)) if lat.size else None,
        "job_errors": sum(x["job_errors"] for x in m),
        "ycsb_ops_per_s": sum(x["ycsb_reads"] + x["ycsb_inserts"] for x in m) / elapsed if elapsed else 0.0,
        "ycsb_p99_ms": float(np.mean([x["ycsb_p99_s"] for x in m])) * 1000,
        "ycsb_errors": sum(x["ycsb_errors"] + x["ycsb_misses"] for x in m),
        "c_idx_ns": (sum(x["idx_sampled_ns"] for x in m) / sampled) if sampled else None,
        "idx_mem_end_mb": m[-1]["idx_mem_bytes"] / MB,
        "idx_smo_per_step": float(np.mean([x["idx_smo"] for x in m])),
        "idx_keys_end": m[-1]["idx_keys"],
        "densities": sorted({tuple(x["idx_density"]) for x in m}),
        "cpu_util": float(np.mean([x["cpu_util"] for x in m])),
        "container_cores": float(np.mean([x["cpu_util"] for x in m])) * ncpus,
        "container_mem_mb": float(np.mean([x["container_mem_bytes"] for x in m])) / MB,
        "cores": cpu,
        "pss_mb": pss,
        "nqo_requests_per_step": float(np.mean([x["nqo_requests"] for x in m])),
        "nqo_ms_per_request": (sum(x["nqo_opt_time_ms"] for x in m) / sum(x["nqo_requests"] for x in m))
        if sum(x["nqo_requests"] for x in m) else 0.0,
        "nqo_counters": dict(counters),
        "r_job": _mean_sd(p["r_job"] for p in parts)[0],
        "r_selix": _mean_sd(((p["r_selix_cost"] or 0.0) + (p["r_selix_mem"] or 0.0))
                            if p["r_total"] is not None else None for p in parts)[0],
        "r_total": _mean_sd(p["r_total"] for p in parts)[0],
        "switches": sum(r["switched"] for r in steps),
        "actions": {"/".join(A.decode(r["action"])): 0 for r in steps},
    }
    for r in steps:
        out["actions"]["/".join(A.decode(r["action"]))] += 1
    return out


def arm_summary(records: List[Record], refs: Optional[M.Refs], w: RewardConfig,
                ncpus: float, phases: Sequence[str]) -> Dict[str, Any]:
    """Per phase and over all phases: mean and sd across episodes of phase_summary values."""
    eps = episodes_of(records)
    per_phase: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for key, steps in eps.items():
        for ph in phases:
            sub = [r for r in steps if r["phase"] == ph]
            if sub:
                per_phase[ph].append(phase_summary(sub, refs, w, ncpus))
        per_phase["all"].append(phase_summary(steps, refs, w, ncpus))
    summary: Dict[str, Any] = {"episodes": len(eps), "phases": {}}
    scalar_keys = ("q_j", "job_done_per_step", "job_p50_s", "job_p99_s", "job_errors", "ycsb_ops_per_s",
                   "ycsb_p99_ms", "ycsb_errors", "c_idx_ns", "idx_mem_end_mb", "idx_smo_per_step",
                   "cpu_util", "container_cores", "container_mem_mb", "nqo_requests_per_step",
                   "nqo_ms_per_request", "r_job", "r_selix", "r_total", "switches")
    for ph, items in per_phase.items():
        agg: Dict[str, Any] = {"episodes": len(items)}
        for k in scalar_keys:
            agg[k] = _mean_sd(x[k] for x in items)
        agg["cores"] = {g: _mean_sd(x["cores"][g] for x in items) for g in list(GROUPS) + ["total"]}
        agg["pss_mb"] = {g: _mean_sd(x["pss_mb"][g] for x in items) for g in GROUPS}
        counters: Dict[str, float] = defaultdict(float)
        for x in items:
            for k, v in x["nqo_counters"].items():
                counters[k] += v / len(items)
        agg["nqo_counters_per_episode"] = dict(counters)
        actions: Dict[str, int] = defaultdict(int)
        for x in items:
            for k, v in x["actions"].items():
                actions[k] += v
        agg["actions"] = dict(sorted(actions.items(), key=lambda kv: -kv[1]))
        agg["densities"] = sorted({d for x in items for d in x["densities"]})
        agg["r_total_per_episode"] = [x["r_total"] for x in items]
        summary["phases"][ph] = agg
    return summary


def obs_in(r: Record) -> Optional[List[float]]:
    """The observation the agent acted on in this step (older logs only have the next one)."""
    return r.get("obs_in") or r.get("obs")


def observation_stats(records: List[Record], phases: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for ph in phases:
        obs = np.asarray([obs_in(r) for r in records if r["phase"] == ph and obs_in(r)], dtype=np.float64)
        if obs.size == 0:
            continue
        out[ph] = {name: {"mean": float(obs[:, i].mean()), "min": float(obs[:, i].min()),
                          "max": float(obs[:, i].max())} for i, name in enumerate(M.OBS_NAMES)}
    return out


def sample_decisions(records: List[Record], phases: Sequence[str], n: int = 3) -> List[Record]:
    """A few (observation -> action) pairs per phase from the first episode."""
    eps = episodes_of(records)
    if not eps:
        return []
    first = eps[sorted(eps)[0]]
    out = []
    for ph in phases:
        out.extend([r for r in first if r["phase"] == ph and obs_in(r)][:n])
    return out


def phase_action_table(records: List[Record], refs: Optional[M.Refs], w: RewardConfig,
                       phases: Sequence[str]) -> Dict[str, Dict[str, Tuple[Optional[float], int]]]:
    """Mean recomputed reward per (phase, action), with the count (section 7.4, item 3)."""
    table: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        p = reward_parts(r, refs, w)
        if p["r_total"] is not None:
            table[r["phase"]]["/".join(A.decode(r["action"]))].append(p["r_total"])
    return {ph: {a: (float(np.mean(v)), len(v)) for a, v in table[ph].items()} for ph in phases if ph in table}


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def fmt(ms: Tuple[Optional[float], Optional[float]], scale: float = 1.0, digits: int = 2,
        sd: bool = True) -> str:
    mean, s = ms
    if mean is None:
        return "–"
    txt = f"{mean * scale:.{digits}f}"
    if sd and s is not None and s > 0:
        txt += f" ± {s * scale:.{digits}f}"
    return txt


def pct(new: Tuple[Optional[float], Optional[float]], ref: Tuple[Optional[float], Optional[float]]) -> str:
    if new[0] is None or ref[0] is None or ref[0] == 0:
        return "–"
    return f"{(new[0] / ref[0] - 1) * 100:+.1f}%"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def render(arms: Dict[str, Dict[str, Any]], arm_records: Dict[str, List[Record]], reference: str,
           phases: Sequence[str], refs: Optional[M.Refs], w: RewardConfig, meta: Dict[str, Any],
           train: Optional[Dict[str, Any]], arm_order: Sequence[str]) -> str:
    ph_all = list(phases) + ["all"]
    L: List[str] = []
    L.append("# Global Agent 雏形实验报告\n")
    L.append("由 `gaproto.report` 从步日志自动生成。奖励按参考值重新计算，不含切换惩罚；"
             "`±` 后是回合之间的标准差。阶段 A 为分析为主，阶段 B 为写入为主，`all` 为整个回合。\n")
    cg = meta.get("cgroup") or {}
    L.append(f"- 对照臂：{', '.join(f'`{a}`' for a in arm_order)}；百分比变化相对于 `{reference}`")
    L.append(f"- 回合数：" + ", ".join(f"{a} {arms[a]['episodes']}" for a in arm_order))
    mem_max = cg.get("memory_max")
    L.append(f"- 容器：{cg.get('ncpus', '?')} 核（cpu.max `{cg.get('cpu_max', '?')}`），"
             f"内存上限 {'无' if not mem_max else f'{mem_max / MB:.0f} MB'}")
    if refs is not None:
        L.append(f"- 参考值：q_J_ref " + ", ".join(f"{k} {v:.3f}" for k, v in refs.q_j_ref.items())
                 + "；c_ref " + ", ".join(f"{k} {v:.0f} ns" for k, v in refs.c_ref_ns.items())
                 + f"；{len(refs.base_lat)} 个 JOB 模板有基线延迟")
    L.append("")

    # ---- 1. benefit
    L.append("## 1. 收益：每个臂在各阶段的表现\n")
    L.append("q_J 是 JOB 标准化吞吐（每秒完成的、按原版基线延迟加权的查询量）；c_idx 是 SELIX 单次操作耗时；"
             "索引内存取阶段末尾的值；综合回报 = 0.5·R_J + 0.5·R_S。\n")
    for ph in ph_all:
        rows = []
        for a in arm_order:
            s = arms[a]["phases"].get(ph)
            if not s:
                continue
            rows.append([f"`{a}`", fmt(s["q_j"], 1, 3), fmt(s["job_done_per_step"], 1, 1),
                         fmt(s["job_p50_s"], 1000, 0, sd=False), fmt(s["job_p99_s"], 1000, 0, sd=False),
                         fmt(s["ycsb_ops_per_s"], 1, 0), fmt(s["ycsb_p99_ms"], 1, 2, sd=False),
                         fmt(s["c_idx_ns"], 1, 0), fmt(s["idx_mem_end_mb"], 1, 1),
                         fmt(s["idx_smo_per_step"], 1, 1, sd=False),
                         fmt(s["r_job"], 1, 3), fmt(s["r_selix"], 1, 3), fmt(s["r_total"], 1, 3)])
        L.append(f"### 1.{ph_all.index(ph) + 1} 阶段 {ph}\n")
        L.append(table(["臂", "q_J", "JOB 完成/步", "JOB p50 ms", "JOB p99 ms", "YCSB ops/s", "YCSB p99 ms",
                        "c_idx ns", "索引内存 MB", "SMO/步", "R_J", "R_S", "综合回报"], rows))
        L.append("")
    # relative changes
    if reference in arms:
        L.append("### 1.%d 相对于 `%s` 的变化（整个回合）\n" % (len(ph_all) + 1, reference))
        ref = arms[reference]["phases"]["all"]
        rows = []
        for a in arm_order:
            if a == reference:
                continue
            s = arms[a]["phases"]["all"]
            d_reward = ("–" if s["r_total"][0] is None or ref["r_total"][0] is None
                        else f"{s['r_total'][0] - ref['r_total'][0]:+.3f}")
            rows.append([f"`{a}`", pct(s["q_j"], ref["q_j"]), pct(s["job_p99_s"], ref["job_p99_s"]),
                         pct(s["ycsb_ops_per_s"], ref["ycsb_ops_per_s"]), pct(s["c_idx_ns"], ref["c_idx_ns"]),
                         pct(s["idx_mem_end_mb"], ref["idx_mem_end_mb"]),
                         pct(s["cores"]["total"], ref["cores"]["total"]), d_reward])
        L.append(table(["臂", "q_J", "JOB p99", "YCSB ops/s", "c_idx", "索引内存", "总 CPU", "综合回报差"], rows))
        L.append(f"\n百分比相对于 `{reference}`。正方向为好的指标：q_J、YCSB ops/s、综合回报差。负方向为好的指标：JOB p99、c_idx、索引内存、总 CPU。\n")

    # ---- 2. resources
    L.append("## 2. 资源：每个臂各组件占用的 CPU 与内存\n")
    L.append("CPU 以“平均占用的核数”表示（该组进程在步内消耗的 CPU 秒 / 步长），来自容器内 `/proc`。"
             "内存是各组进程 PSS 的平均值（共享页按比例分摊），索引内存另见第 1 节。组的含义见 `gaproto/procstat.py`："
             "`nqo_server` 是 NQO 服务进程，`pg_nqo_client` 是 NQO 回连数据库的后端，`pg_job`/`pg_ycsb` 是两个负载的后端，"
             "`harness` 是实验程序主进程（含 GA 推理与采集），`drivers` 是负载发生器，代表客户端。\n")
    for ph in ph_all:
        rows = []
        for a in arm_order:
            s = arms[a]["phases"].get(ph)
            if not s:
                continue
            rows.append([f"`{a}`"] + [fmt(s["cores"][g], 1, 2, sd=False) for g in CPU_GROUPS]
                        + [fmt(s["cores"]["total"], 1, 2), fmt(s["container_cores"], 1, 2, sd=False)]
                        + [fmt(s["pss_mb"][g], 1, 0, sd=False) for g in MEM_GROUPS]
                        + [fmt(s["container_mem_mb"], 1, 0, sd=False),
                           fmt(s["nqo_requests_per_step"], 1, 1, sd=False), fmt(s["nqo_ms_per_request"], 1, 0, sd=False)])
        L.append(f"### 2.{ph_all.index(ph) + 1} 阶段 {ph}\n")
        L.append(table(["臂"] + [f"CPU {g}" for g in CPU_GROUPS] + ["CPU 合计", "CPU 容器(cgroup)"]
                       + [f"PSS {g} MB" for g in MEM_GROUPS] + ["容器内存 MB", "NQO 请求/步", "NQO ms/请求"], rows))
        L.append("")

    # ---- 3. states and actions at run time
    L.append("## 3. 运行时观测到的状态与动作\n")
    L.append("### 3.1 NQO：输入与输出的计数（每回合平均）\n")
    L.append("NQO 的输入是 JOB 客户端发来的 SQL 文本（每条查询一次请求）；输出是专家给出的动作："
             "`expert_HintPlanSel` 为一组 `SET enable_*` 开关，`expert_JoinOrder` 为 `Leading(...)` 连接顺序提示，"
             "`fallbacks` 为未改写、交给原生优化器。`filter_*` 是 GA 下发的专家过滤档位。\n")
    keys = ["requests", "optimized", "fallbacks", "errors", "expert_HintPlanSel", "expert_JoinOrder",
            "filter_all", "filter_hint", "filter_join"]
    rows = []
    for a in arm_order:
        for ph in phases:
            s = arms[a]["phases"].get(ph)
            if not s:
                continue
            c = s["nqo_counters_per_episode"]
            rows.append([f"`{a}`", ph] + [f"{c.get(k, 0):.0f}" for k in keys])
    L.append(table(["臂", "阶段"] + keys, rows))
    L.append("")
    L.append("### 3.2 SELIX：生效的密度参数与结构调整\n")
    L.append("SELIX 没有自己的在线状态输入；它的“动作”是三元密度参数 (init, max, min)，"
             "原版始终为 (0.70, 0.80, 0.60)。下表列出各臂在各阶段实际生效过的参数、"
             "每步结构调整（SMO）次数与阶段末索引内存。\n")
    rows = []
    for a in arm_order:
        for ph in phases:
            s = arms[a]["phases"].get(ph)
            if not s:
                continue
            rows.append([f"`{a}`", ph, "; ".join("/".join(f"{x:.2f}" for x in d) for d in s["densities"]),
                         fmt(s["idx_smo_per_step"], 1, 1), fmt(s["c_idx_ns"], 1, 0), fmt(s["idx_mem_end_mb"], 1, 1)])
    L.append(table(["臂", "阶段", "生效过的密度 init/max/min", "SMO/步", "c_idx ns", "索引内存 MB"], rows))
    L.append("")
    L.append("### 3.3 Global Agent：动作分布与切换\n")
    rows = []
    for a in arm_order:
        for ph in ph_all:
            s = arms[a]["phases"].get(ph)
            if not s:
                continue
            total = sum(s["actions"].values()) or 1
            acts = ", ".join(f"{k} {v / total * 100:.0f}%" for k, v in list(s["actions"].items())[:4])
            rows.append([f"`{a}`", ph, acts, fmt(s["switches"], 1, 1)])
    L.append(table(["臂", "阶段", "动作分布（NQO 模式/SELIX 档位）", "切换次数/回合"], rows))
    L.append("")
    L.append("### 3.4 Global Agent：观测到的状态（9 维）\n")
    L.append("每一维的定义见 `GlobalAgent_prototype.md` 5.2 节。下表是 GA 臂（没有则取第一个臂）在各阶段看到的取值范围。\n")
    ga_arm = next((a for a in arm_order if a.startswith("ga")), arm_order[0])
    stats = observation_stats(arm_records[ga_arm], phases)
    rows = []
    for name in M.OBS_NAMES:
        row = [name]
        for ph in phases:
            st = stats.get(ph, {}).get(name)
            row.append("–" if not st else f"{st['mean']:.3f} [{st['min']:.2f}, {st['max']:.2f}]")
        rows.append(row)
    L.append(f"臂 `{ga_arm}`：均值 [最小, 最大]\n")
    L.append(table(["状态分量"] + [f"阶段 {ph}" for ph in phases], rows))
    L.append("")
    L.append(f"示例决策（臂 `{ga_arm}`，第一个回合每个阶段的前 3 步）：\n")
    rows = []
    for r in sample_decisions(arm_records[ga_arm], phases):
        rows.append([str(r["step"]), r["phase"], "[" + ", ".join(f"{x:.2f}" for x in obs_in(r)) + "]",
                     "/".join(A.decode(r["action"])), f"{r['reward']:+.3f}"])
    L.append(table(["步", "阶段", "观测（输入状态）", "动作（输出）", "奖励"], rows))
    L.append("")

    # ---- 4. training
    if train:
        L.append("## 4. 训练过程\n")
        prog = train.get("progress") or {}
        eps = prog.get("episodes") or []
        if eps:
            rows = [[str(e["episode"]), str(e["end_step"]), f"{e['return']:+.2f}", f"{e['mean_reward']:+.3f}",
                     f"{e['wall_s'] / 60:.0f}"] for e in eps]
            L.append(f"PPO 训练 {prog.get('total_steps', '?')} 步，{len(eps)} 个回合，"
                     f"{(prog.get('wall_s') or 0) / 3600:.1f} 小时。每回合回报：\n")
            L.append(table(["回合", "累计步", "回报", "平均奖励", "分钟"], rows))
            L.append("")
        pat = train.get("phase_action")
        if pat:
            L.append("每个阶段下各动作在训练日志中的平均奖励（次数）。这张表区分“GA 学得不够”与“没有动作优于原版”，见雏形文档 7.4 节第 3 项。\n")
            all_actions = sorted({a for ph in pat for a in pat[ph]}, key=lambda s: A.encode(*s.split("/")))
            rows = []
            for act in all_actions:
                row = [act]
                for ph in phases:
                    v = pat.get(ph, {}).get(act)
                    row.append("–" if not v else f"{v[0]:+.3f} ({v[1]})")
                rows.append(row)
            L.append(table(["动作"] + [f"阶段 {ph}" for ph in phases], rows))
            L.append("")

    # ---- 5. verdict
    ga = next((a for a in arm_order if a.startswith("ga")), None)
    static = "static-best" if "static-best" in arms else None
    orig = next((a for a in ("nqo", "original") if a in arms), None)
    if ga and refs is not None and (static or orig):
        L.append("## 5. 判定\n")
        g = arms[ga]["phases"]["all"]

        def compare(ref_name, title):
            o = arms[ref_name]["phases"]["all"]
            d = (g["r_total"][0] or 0) - (o["r_total"][0] or 0)
            p99 = pct(g["job_p99_s"], o["job_p99_s"])
            ycsb = pct(g["ycsb_ops_per_s"], o["ycsb_ops_per_s"])
            harness = g["cores"]["harness"][0] or 0.0
            total = g["cores"]["total"][0] or 1.0
            per_ep_g, per_ep_o = g["r_total_per_episode"], o["r_total_per_episode"]
            consistent = (len(per_ep_g) == len(per_ep_o) and len(per_ep_g) > 0 and
                          all((x or 0) > (y or 0) for x, y in zip(per_ep_g, per_ep_o)))
            rows = [
                [f"综合回报 GA − {ref_name}（对数尺度，+0.05 ≈ 5%）", f"{d:+.3f}", "≥ +0.05", "是" if d >= 0.05 else "否"],
                ["逐回合方向一致", "是" if consistent else "否", "是", "是" if consistent else "否"],
                ["JOB p99 变化", p99, "≤ +10%", "是" if p99 != "–" and float(p99.rstrip("%")) <= 10 else "否"],
                ["YCSB 吞吐变化", ycsb, "≥ −5%", "是" if ycsb != "–" and float(ycsb.rstrip("%")) >= -5 else "否"],
                ["GA 推理与指标采集（harness 组）占总 CPU", f"{harness / total * 100:.1f}%", "< 2%",
                 "是" if harness / total < 0.02 else "否"],
            ]
            L.append(f"### {title}\n")
            L.append(table(["条件", "测得", "门槛", "满足"], rows))
            L.append("")

        if static:
            compare(static, f"5.1 协调收益：`{ga}` 相对各组件静态最优的组合 `static-best`")
            L.append("这是第二轮的主判定量。`static-best` 是两个组件各自扫描出的最优固定配置的组合；GA 只有在不同阶段选择不同动作才可能超过它。"
                     "若训练日志的阶段×动作表（第 4 节）显示某一个固定动作在所有阶段都最优，则协调没有价值，GA 至多等于 `static-best`。\n")
        if orig:
            compare(orig, f"5.{2 if static else 1} 相对原版 NeurDB：`{ga}` 相对 `{orig}`（第一轮的判定量）")
        L.append("门槛是雏形阶段的经验取值。回报不优于对照时，看第 4 节的阶段×动作表区分“学习失败”与“当前条件下不可行”。\n")
    return "\n".join(L)


# --------------------------------------------------------------------------
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Build the result report from step logs")
    ap.add_argument("--runs", action="append", required=True, help="steps.jsonl of evaluation or baseline runs")
    ap.add_argument("--train", default="", help="steps.jsonl of the training run")
    ap.add_argument("--refs", default="", help="refs.json for recomputing rewards")
    ap.add_argument("--config", default="", help="config for the reward weights (default weights otherwise)")
    ap.add_argument("--reference", default="none", help="arm the percentage changes refer to")
    ap.add_argument("--phases", default="", help="phase names (default: from the records)")
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--json", default="", help="also write the aggregated numbers to this file")
    args = ap.parse_args(argv)

    refs = M.Refs.load(args.refs) if args.refs else None
    w = load_config(args.config).reward if args.config else RewardConfig()
    arm_records, meta = load_runs(args.runs)
    if not arm_records:
        raise SystemExit("no step records in the given files")
    phases = [p for p in args.phases.split(",") if p] or sorted({r["phase"] for v in arm_records.values() for r in v})
    ncpus = float((meta.get("cgroup") or {}).get("ncpus") or 1.0)
    arm_order = list(arm_records)
    if args.reference in arm_order:
        arm_order.remove(args.reference)
        arm_order.insert(0, args.reference)
    arms = {a: arm_summary(arm_records[a], refs, w, ncpus, phases) for a in arm_order}

    train = None
    if args.train:
        t_records, _ = load_runs([args.train])
        t_steps = [r for v in t_records.values() for r in v]
        prog_path = os.path.join(os.path.dirname(args.train), "progress.json")
        progress = json.load(open(prog_path, encoding="utf-8")) if os.path.isfile(prog_path) else None
        train = {"progress": progress, "phase_action": phase_action_table(t_steps, refs, w, phases)}

    text = render(arms, arm_records, args.reference, phases, refs, w, meta, train, arm_order)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"arms": arms, "phases": phases, "reference": args.reference,
                       "train": train}, f, indent=2, default=str)
    print(f"report written to {args.out}" + (f" and {args.json}" if args.json else ""))
    for a in arm_order:
        s = arms[a]["phases"]["all"]
        print(f"  {a:12s} episodes {arms[a]['episodes']}  q_J {fmt(s['q_j'], 1, 3)}  "
              f"c_idx {fmt(s['c_idx_ns'], 1, 0)} ns  mem {fmt(s['idx_mem_end_mb'], 1, 1)} MB  "
              f"CPU {fmt(s['cores']['total'], 1, 2)} cores  reward {fmt(s['r_total'], 1, 3)}")


if __name__ == "__main__":
    main()
