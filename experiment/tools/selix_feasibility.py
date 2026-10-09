"""Feasibility checks F1 to F4 of GlobalAgent_hierarchical.md (section 4).

All queries are read-only JOB queries of one group (long or short). Each check
runs the group's queries under a set of variants; a variant fixes

  nqo      off / hint / auto            (enable_molqo, molqo.expert_filter)
  scheme   btree / cost / prefer        (selix.enable_index off / index_cost_scale 0.999 / 0.01)
  density  dense / mid / default        (selix.* densities; a change rebuilds SELIX on next use)

and every query is timed twice: with and without the time spent building SELIX
copies during the query (nrindex_build_time() before and after, D2). The
executed plan of every query is captured with auto_explain, so the report can
say which indexes (SELIX or btree) were used and what NQO applied.

  F1  correctness and use: btree vs cost, NQO off, one run; same results? which SELIX indexes were used?
  F2  speed: btree vs cost, NQO off, R runs
  F3  density: prefer SELIX, NQO off, the three densities, R runs
  F4  coupling: nqo x (btree | {cost, prefer} x density), R runs

Usage (inside the container, in experiment/):
  python tools/selix_feasibility.py --config config/imdb_r2.json --check f1 --group long
  python tools/selix_feasibility.py --config config/imdb_r2.json --check f4 --group short --runs 2

Output: runs/.../<check>_<group>.json (every measurement), .md (tables and the verdict);
F1 also writes <..>_used_indexes.txt, the SELIX indexes some query used.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import itertools
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto.actions import SELIX_PRESETS  # noqa: E402
from gaproto.config import load_config  # noqa: E402
from gaproto.db import connect  # noqa: E402

NQO_MODES = ("off", "hint", "auto")
SCHEMES = ("btree", "cost", "prefer")
DENSITIES = tuple(SELIX_PRESETS)            # dense, mid, default
GROUP_DIRS = {"long": "queries/job_long", "short": "queries/job_fast"}
MARGIN = 1.10                               # 10%: below this a difference is treated as noise


# --------------------------------------------------------------------------
# variants
# --------------------------------------------------------------------------
def variant_name(v: Dict[str, str]) -> str:
    if v["scheme"] == "btree":
        return f"{v['nqo']}/btree"
    return f"{v['nqo']}/{v['scheme']}/{v['density']}"


def index_setting(v: Dict[str, str]) -> str:
    return "btree" if v["scheme"] == "btree" else f"{v['scheme']}/{v['density']}"


def variants_for(check: str) -> List[Dict[str, str]]:
    if check in ("f1", "f2"):
        return [dict(nqo="off", scheme="btree", density="default"),
                dict(nqo="off", scheme="cost", density="default")]
    if check == "f3":
        return [dict(nqo="off", scheme="prefer", density=d) for d in DENSITIES]
    out = []
    for nqo in NQO_MODES:
        out.append(dict(nqo=nqo, scheme="btree", density="default"))
        for scheme, density in itertools.product(("cost", "prefer"), DENSITIES):
            out.append(dict(nqo=nqo, scheme=scheme, density=density))
    return out


def settings_sql(v: Dict[str, str]) -> List[str]:
    stmts = []
    if v["nqo"] == "off":
        stmts.append("SET enable_molqo = off")
    else:
        stmts += ["SET enable_molqo = on",
                  "SET molqo.expert_filter = '%s'" % ("hint" if v["nqo"] == "hint" else "all")]
    if v["scheme"] == "btree":
        stmts.append("SET selix.enable_index = off")
    else:
        stmts += ["SET selix.enable_index = on",
                  "SET selix.index_cost_scale = %s" % ("0.999" if v["scheme"] == "cost" else "0.01")]
    i, x, n = SELIX_PRESETS[v["density"]]
    stmts.append(f"SET selix.init_density = {i}; SET selix.max_density = {x}; SET selix.min_density = {n};")
    return stmts


def order_variants(variants: List[Dict[str, str]], rep: int) -> List[Dict[str, str]]:
    """Group by density (a density change rebuilds SELIX) and rotate the groups per repetition."""
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for v in variants:
        groups["btree" if v["scheme"] == "btree" else v["density"]].append(v)
    keys = list(groups)
    keys = keys[rep % len(keys):] + keys[:rep % len(keys)]
    return [v for k in keys for v in groups[k]]


# --------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------
def plan_from_notices(notices: List[str]) -> Optional[Dict[str, Any]]:
    """The executed plan logged by auto_explain (JSON format), if any."""
    for msg in reversed(notices):
        pos = msg.find("plan:")
        if pos < 0:
            continue
        text = msg[pos + 5:].strip()
        try:
            return json.loads(text)
        except ValueError:
            continue
    return None


def index_names(node: Any) -> List[str]:
    out: List[str] = []
    if isinstance(node, dict):
        if "Index Name" in node:
            out.append(node["Index Name"])
        for v in node.values():
            out += index_names(v)
    elif isinstance(node, list):
        for v in node:
            out += index_names(v)
    return out


def nqo_action(notices: List[str]) -> str:
    for msg in notices:
        if "optimization applied (hint)" in msg:
            return "hint"
        if "optimization applied (" in msg:
            return "settings"
    return "none"


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
class Session:
    def __init__(self, cfg, timeout_ms: int):
        self.conn = connect(cfg.db, "ga_feasibility", molqo_off=False)
        self.conn.notices = []
        self.cur = self.conn.cursor()
        self.timeout_ms = timeout_ms
        for s in ("SET client_min_messages = log", "LOAD 'auto_explain'",
                  "SET auto_explain.log_min_duration = 0", "SET auto_explain.log_format = 'json'",
                  "SET molqo.report_decisions = on", "SET selix.rebuild_on_density_change = on",
                  f"SET statement_timeout = {timeout_ms}"):
            try:
                self.cur.execute(s)
            except psycopg2.Error as e:
                raise SystemExit(f"session setup failed at {s!r}: {e}")
        self.cur.execute("SELECT 1 FROM pg_proc WHERE proname = 'nrindex_build_time'")
        if self.cur.fetchone() is None:
            raise SystemExit("nrindex_build_time() is missing: run tools/selix_indexes.py --list once")
        self.current: Optional[str] = None
        self.nqo_on = False

    def build_ms(self) -> float:
        # with NQO on, this bookkeeping query would be sent to the NQO service too
        if self.nqo_on:
            self.cur.execute("SET enable_molqo = off")
        self.cur.execute("SELECT build_ms FROM nrindex_build_time()")
        ms = float(self.cur.fetchone()[0])
        if self.nqo_on:
            self.cur.execute("SET enable_molqo = on")
        return ms

    def apply(self, v: Dict[str, str]) -> None:
        name = variant_name(v)
        if name != self.current:
            for s in settings_sql(v):
                self.cur.execute(s)
            self.current = name
            self.nqo_on = v["nqo"] != "off"

    def run(self, sql: str) -> Dict[str, Any]:
        b0 = self.build_ms()
        del self.conn.notices[:]
        t0 = time.perf_counter()
        status, fp = "ok", None
        try:
            self.cur.execute(sql)
            rows = self.cur.fetchall()
            fp = hashlib.md5(repr(rows).encode()).hexdigest()[:12]
        except psycopg2.errors.QueryCanceled:
            status = "timeout"
        except psycopg2.Error as e:
            status = "error: " + str(e).splitlines()[0]
        wall = time.perf_counter() - t0
        notices = list(self.conn.notices)
        b1 = self.build_ms()
        build = max(0.0, (b1 - b0) / 1000.0)
        plan = plan_from_notices(notices)
        return {"status": status, "wall_s": round(wall, 4), "build_s": round(build, 4),
                "net_s": round(max(0.0, wall - build), 4), "result": fp,
                "indexes": index_names(plan) if plan else [], "nqo_action": nqo_action(notices)}

    def selix_mem_mb(self) -> float:
        if self.nqo_on:
            self.cur.execute("SET enable_molqo = off")
        self.cur.execute("SELECT mem_bytes FROM nrindex_stats()")
        mb = self.cur.fetchone()[0] / 1048576.0
        if self.nqo_on:
            self.cur.execute("SET enable_molqo = on")
        return mb

    def close(self) -> None:
        self.conn.close()


def load_queries(directory: str, only: str) -> List[Tuple[str, str]]:
    files = sorted(glob.glob(os.path.join(directory, "*.sql")))
    out = [(os.path.splitext(os.path.basename(f))[0], open(f, encoding="utf-8").read().strip()) for f in files]
    if only:
        names = set(only.split(","))
        out = [q for q in out if q[0] in names]
    if not out:
        raise SystemExit(f"no queries in {directory}")
    return out


def nrindex_names(cur) -> set:
    cur.execute("SELECT c.relname FROM pg_class c JOIN pg_am a ON a.oid = c.relam WHERE a.amname = 'nrindex'")
    return {r[0] for r in cur.fetchall()}


def measure(cfg, check: str, queries, runs: int, warmup: bool) -> Dict[str, Any]:
    variants = variants_for(check)
    sess = Session(cfg, cfg.job.statement_timeout_ms)
    selix = nrindex_names(sess.cur)
    if not selix:
        raise SystemExit("no SELIX index exists: run tools/selix_indexes.py --create first")
    records: List[Dict[str, Any]] = []
    mem: Dict[str, float] = {}
    try:
        if warmup:
            v = variants[-1]
            sess.apply(v)
            print(f"warm-up pass under {variant_name(v)} (not counted)", flush=True)
            for name, sql in queries:
                r = sess.run(sql)
                print(f"  warm-up {name:5s} {r['wall_s']:8.2f} s  {r['status']}", flush=True)
        for rep in range(runs):
            for v in order_variants(variants, rep):
                sess.apply(v)
                for name, sql in queries:
                    r = sess.run(sql)
                    r.update(query=name, variant=variant_name(v), rep=rep, **v,
                             selix_used=sorted({i for i in r["indexes"] if i in selix}),
                             btree_used=sorted({i for i in r["indexes"] if i not in selix}))
                    records.append(r)
                    print(f"  run {rep} {variant_name(v):22s} {name:5s} {r['wall_s']:8.2f} s "
                          f"(build {r['build_s']:.2f})  {r['status']:8s} selix {len(r['selix_used'])} "
                          f"nqo {r['nqo_action']}", flush=True)
                if v["scheme"] != "btree":
                    mem[index_setting(v)] = round(sess.selix_mem_mb(), 1)
    finally:
        sess.close()
    return {"check": check, "variants": [variant_name(v) for v in variants], "runs": runs,
            "records": records, "selix_mem_mb": mem, "selix_indexes": sorted(selix)}


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def best_times(records, key: str = "net_s") -> Dict[Tuple[str, str], Optional[float]]:
    """(query, variant) -> min over runs of key; None when every run failed."""
    out: Dict[Tuple[str, str], Optional[float]] = {}
    for r in records:
        k = (r["query"], r["variant"])
        v = r[key] if r["status"] == "ok" else None
        if v is not None:
            out[k] = v if out.get(k) is None else min(out[k], v)
        else:
            out.setdefault(k, None)
    return out


def totals(best, queries, variant) -> Optional[float]:
    vals = [best.get((q, variant)) for q in queries]
    return None if any(v is None for v in vals) else sum(vals)


def fmt(x: Optional[float], d: int = 2) -> str:
    return "–" if x is None else f"{x:.{d}f}"


def analyze_f1(data) -> Tuple[str, bool, List[str]]:
    recs = [r for r in data["records"] if r["rep"] == 0]
    by = {(r["query"], r["variant"]): r for r in recs}
    queries = sorted({r["query"] for r in recs})
    L = ["| 查询 | 只用 btree | 按代价选择 | 结果相同 | 按代价选择时用到的 SELIX 索引 |", "|---|---|---|---|---|"]
    mismatches, used = [], set()
    for q in queries:
        a, b = by.get((q, "off/btree")), by.get((q, "off/cost/default"))
        if not a or not b:
            continue
        same = a["result"] == b["result"] and a["status"] == b["status"] == "ok"
        if a["status"] == "ok" and b["status"] == "ok" and not same:
            mismatches.append(q)
        used |= set(b["selix_used"])
        L.append(f"| {q} | {a['wall_s']:.2f} s {a['status'] if a['status'] != 'ok' else ''} | "
                 f"{b['wall_s']:.2f} s {b['status'] if b['status'] != 'ok' else ''} | "
                 f"{'是' if same else ('否' if a['status'] == b['status'] == 'ok' else '未比较')} | {', '.join(b['selix_used']) or '无'} |")
    n_using = sum(1 for q in queries if by.get((q, "off/cost/default"), {}).get("selix_used"))
    ok = not mismatches and n_using > 0
    L += ["", f"{len(queries)} 条查询，结果不同 {len(mismatches)} 条{('：' + ', '.join(mismatches)) if mismatches else ''}；"
              f"计划用到 SELIX 的 {n_using} 条；用到的 SELIX 索引 {len(used)} 个。",
          f"\n**F1 {'通过' if ok else '不通过'}**（通过的条件：全部结果相同，且至少有查询用到 SELIX）"]
    return "\n".join(L), ok, sorted(used)


def analyze_f2(data) -> Tuple[str, bool]:
    queries = sorted({r["query"] for r in data["records"]})
    L = []
    verdict = True
    for key, title in (("wall_s", "含重建"), ("net_s", "不含重建")):
        best = best_times(data["records"], key)
        ta, tb = totals(best, queries, "off/btree"), totals(best, queries, "off/cost/default")
        L.append(f"### {title}\n\n| 查询 | 只用 btree | 按代价选择 | 比值 |\n|---|---|---|---|")
        for q in queries:
            a, b = best.get((q, "off/btree")), best.get((q, "off/cost/default"))
            L.append(f"| {q} | {fmt(a)} | {fmt(b)} | {fmt(b / a if a and b else None)} |")
        ratio = tb / ta if ta and tb else None
        L.append(f"| **合计** | {fmt(ta)} | {fmt(tb)} | {fmt(ratio)} |\n")
        if ratio is None or ratio > MARGIN:
            verdict = False
    L.append(f"**F2 {'通过' if verdict else '不通过'}**（通过的条件：按代价选择的总延迟不比只用 btree 慢 10% 以上，含与不含重建都要满足）")
    return "\n".join(L), verdict


def analyze_f3(data) -> Tuple[str, bool]:
    queries = sorted({r["query"] for r in data["records"]})
    best = best_times(data["records"], "net_s")
    names = [f"off/prefer/{d}" for d in DENSITIES]
    used = {r["query"] for r in data["records"] if r["selix_used"]}
    L = ["| 查询 | " + " | ".join(DENSITIES) + " | 最慢 / 最快 | 用到 SELIX |", "|---|" + "---|" * (len(DENSITIES) + 2)]
    hits = []
    for q in queries:
        vals = [best.get((q, n)) for n in names]
        good = [v for v in vals if v]
        spread = max(good) / min(good) if len(good) == len(vals) and min(good) > 0 else None
        if spread and spread >= MARGIN and q in used:
            hits.append(q)
        L.append(f"| {q} | " + " | ".join(fmt(v) for v in vals) + f" | {fmt(spread)} | {'是' if q in used else '否'} |")
    builds = defaultdict(float)
    for r in data["records"]:
        builds[r["density"]] += r["build_s"]
    L += ["", "| 密度 | 索引内存（本连接，MB） | 重建总时间（s） |", "|---|---|---|"]
    for d in DENSITIES:
        L.append(f"| {d} | {fmt(data['selix_mem_mb'].get('prefer/' + d), 1)} | {builds[d]:.1f} |")
    ok = bool(hits)
    L.append(f"\n不含重建的延迟。密度之间相差 10% 以上、且用到 SELIX 的查询：{', '.join(hits) or '无'}。\n\n"
             f"**F3 {'通过' if ok else '不通过'}**（通过的条件：至少一条用到 SELIX 的查询在两档密度之间相差 10% 以上）")
    return "\n".join(L), ok


def flips(best, queries, settings, modes) -> Tuple[List[str], List[str]]:
    """Queries whose best NQO mode depends on the index setting, and vice versa (both with a 10% margin)."""
    nqo_flips, idx_flips = [], []
    for q in queries:
        def t(m, s):
            return best.get((q, f"{m}/btree" if s == "btree" else f"{m}/{s}"))
        found = False
        for s1, s2 in itertools.combinations(settings, 2):
            row1 = {m: t(m, s1) for m in modes}
            row2 = {m: t(m, s2) for m in modes}
            if any(v is None for v in list(row1.values()) + list(row2.values())):
                continue
            b1, b2 = min(row1, key=row1.get), min(row2, key=row2.get)
            if b1 != b2 and row2[b1] >= MARGIN * row2[b2] and row1[b2] >= MARGIN * row1[b1]:
                nqo_flips.append(f"{q}（{s1} 时最好是 {b1}，{s2} 时最好是 {b2}）")
                found = True
                break
        for m1, m2 in itertools.combinations(modes, 2):
            col1 = {s: t(m1, s) for s in settings}
            col2 = {s: t(m2, s) for s in settings}
            if any(v is None for v in list(col1.values()) + list(col2.values())):
                continue
            b1, b2 = min(col1, key=col1.get), min(col2, key=col2.get)
            if b1 != b2 and col2[b1] >= MARGIN * col2[b2] and col1[b2] >= MARGIN * col1[b1]:
                idx_flips.append(f"{q}（NQO {m1} 时最好是 {b1}，{m2} 时最好是 {b2}）")
                break
    return nqo_flips, idx_flips


def analyze_f4(data) -> Tuple[str, bool]:
    queries = sorted({r["query"] for r in data["records"]})
    settings = ["btree"] + [f"{s}/{d}" for s in ("cost", "prefer") for d in DENSITIES]
    L = []
    # Coupling is judged on the latency without rebuilds: a rebuild happens once
    # after a density change and lands on whichever query runs first, so per-query
    # comparisons of the wall time would mostly reflect the run order.
    best = best_times(data["records"], "net_s")
    L.append("### 每条查询在各组合下的最短延迟（秒，不含重建）\n")
    L.append("| 查询 | NQO | " + " | ".join(settings) + " |")
    L.append("|---|---|" + "---|" * len(settings))
    for q in queries:
        for m in NQO_MODES:
            cells = [best.get((q, f"{m}/btree" if s == "btree" else f"{m}/{s}")) for s in settings]
            L.append(f"| {q} | {m} | " + " | ".join(fmt(c) for c in cells) + " |")
    nf, xf = flips(best, queries, settings, NQO_MODES)
    L += ["", f"NQO 的最优模式随索引设置改变（相差 10% 以上）：{len(nf)} 条", *[f"- {x}" for x in nf],
          "", f"索引设置的最优选择随 NQO 模式改变（相差 10% 以上）：{len(xf)} 条", *[f"- {x}" for x in xf], ""]
    # rebuild cost per density, reported separately (D2)
    wall = best_times(data["records"], "wall_s")
    builds: Dict[str, float] = defaultdict(float)
    for r in data["records"]:
        builds["btree" if r["scheme"] == "btree" else r["density"]] += r["build_s"]
    tot_net = {s: sum(v for (q, var), v in best.items() if v is not None and var.endswith(s)) for s in settings}
    tot_wall = {s: sum(v for (q, var), v in wall.items() if v is not None and var.endswith(s)) for s in settings}
    L += ["### 重建的代价\n", "| 密度 | 这一轮里重建 SELIX 的总时间（s） |", "|---|---|"]
    L += [f"| {d} | {builds[d]:.1f} |" for d in DENSITIES]
    L += ["", "| 索引设置 | 各查询最短延迟之和（不含重建） | 同上（含重建） |", "|---|---|---|"]
    L += [f"| {s} | {tot_net[s]:.2f} | {tot_wall[s]:.2f} |" for s in settings]
    ok = bool(nf or xf)
    L.append(f"\n**F4 {'通过' if ok else '不通过'}**（通过的条件：至少一条查询，其中一个组件的最优选择随另一个组件的选择而改变，以不含重建的延迟判定，相差 10% 以上）")
    return "\n".join(L), ok


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Feasibility checks F1-F4 (GlobalAgent_hierarchical.md section 4)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", required=True, choices=("f1", "f2", "f3", "f4"))
    ap.add_argument("--group", required=True, choices=tuple(GROUP_DIRS))
    ap.add_argument("--query-dir", default="", help="default: queries/job_long or queries/job_fast")
    ap.add_argument("--only", default="", help="comma-separated query names")
    ap.add_argument("--runs", type=int, default=0, help="default: 1 for f1, 3 for f2/f3, 2 for f4")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--out", default="", help="prefix of the output files (default: <log_dir>/<check>_<group>)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    runs = args.runs or {"f1": 1, "f2": 3, "f3": 3, "f4": 2}[args.check]
    queries = load_queries(args.query_dir or GROUP_DIRS[args.group], args.only)
    out = args.out or os.path.join(cfg.log_dir, f"{args.check}_{args.group}")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    print(f"{args.check} on the {args.group} group: {len(queries)} queries, "
          f"{len(variants_for(args.check))} variants, {runs} run(s)", flush=True)

    data = measure(cfg, args.check, queries, runs, warmup=not args.no_warmup)
    data.update(group=args.group, queries=[q for q, _ in queries])
    with open(out + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

    head = f"# {args.check.upper()}（{args.group} 组）\n\n{len(queries)} 条查询，{runs} 轮，SELIX 索引 {len(data['selix_indexes'])} 个。\n\n"
    if args.check == "f1":
        text, ok, used = analyze_f1(data)
        with open(out + "_used_indexes.txt", "w", encoding="utf-8") as f:
            f.write("# SELIX indexes used by at least one query of the group (F1)\n" + "".join(u + "\n" for u in used))
    elif args.check == "f2":
        text, ok = analyze_f2(data)
    elif args.check == "f3":
        text, ok = analyze_f3(data)
    else:
        text, ok = analyze_f4(data)
    with open(out + ".md", "w", encoding="utf-8") as f:
        f.write(head + text + "\n")
    print("\n" + text.splitlines()[-1])
    print(f"written: {out}.md, {out}.json" + (f", {out}_used_indexes.txt" if args.check == "f1" else ""))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
