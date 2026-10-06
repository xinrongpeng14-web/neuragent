"""Does the plan an NQO expert proposes run faster than the cost-based plan?

For every query of a directory, ask the NQO service what each expert would do,
and for every expert that changes the plan, execute the query with the
cost-based plan and with the expert's plan (applied the way nr_molqo applies
it: SET LOCAL ... for the statement, or the hint prefix). Variants are run
interleaved, `--runs` times each, and the minimum counts. The planner's cost
estimate of each variant is recorded too.

Usage (inside the container, in experiment/):
  python tools/nqo_plan_gain.py --config config/imdb_r2.json --query-dir queries/job_all \\
      --out runs/imdb_r2/nqo_gain --runs 3 --timeout 120

Output: <out>.json (all numbers) and <out>.md (the table), and a list of the
queries whose expert plan is at least 20% faster: <out>_wins.txt
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto.config import load_config  # noqa: E402
from gaproto.db import connect  # noqa: E402

FILTERS = ("hint", "join")


def ask_nqo(url, sql, flt, timeout_s=600.0):
    """(prefix, expert, opt_ms): the expert's action, or an empty prefix when the plan is unchanged."""
    body = json.dumps({"sql": sql, "expert_filter": flt}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        d = json.loads(r.read().decode())
    opt = d.get("optimized_sql", sql)
    prefix = opt[: len(opt) - len(sql)].strip() if opt.endswith(sql) and opt != sql else ""
    sets = [x.strip() for x in prefix.split(";") if x.strip().upper().startswith("SET")]
    if sets:
        on = {x.split()[1] for x in sets if x.upper().endswith(" ON")}
        off = {x.split()[1] for x in sets if x.upper().endswith(" OFF")}
        if on == off:            # HintPlanSel arm 0: everything switched back on
            prefix = ""
    return prefix, d.get("expert_name"), d.get("opt_time_ms")


class Runner:
    def __init__(self, db_cfg, timeout_s):
        self.conn = connect(db_cfg, "ga_tool", molqo_off=True)
        self.cur = self.conn.cursor()
        self.timeout_s = timeout_s
        self.cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
        self.cur.execute("LOAD 'pg_hint_plan'")

    def _rollback(self):
        try:
            self.cur.execute("ROLLBACK")
        except psycopg2.Error:
            pass

    def timed(self, stmts):
        t0 = time.perf_counter()
        try:
            self.cur.execute("BEGIN")
            for s in stmts:
                self.cur.execute(s)
            self.cur.fetchall()
            self.cur.execute("COMMIT")
            return time.perf_counter() - t0, "ok"
        except psycopg2.errors.QueryCanceled:
            self._rollback()
            return self.timeout_s, "timeout"
        except psycopg2.Error as e:
            self._rollback()
            return None, f"error: {str(e).splitlines()[0]}"

    def plan_cost(self, stmts):
        try:
            self.cur.execute("BEGIN")
            for s in stmts[:-1]:
                self.cur.execute(s)
            self.cur.execute("EXPLAIN (FORMAT JSON) " + stmts[-1])
            cost = self.cur.fetchone()[0][0]["Plan"]["Total Cost"]
            self.cur.execute("COMMIT")
            return cost
        except psycopg2.Error:
            self._rollback()
            return None


def variant_statements(sql, flt, prefix):
    if flt == "hint":
        sets = [x.strip().replace("SET ", "SET LOCAL ", 1) for x in prefix.split(";") if x.strip().upper().startswith("SET")]
        return sets + [sql]
    return [prefix + " " + sql]


def arm_label(prefix):
    on = [x.split()[1].replace("enable_", "") for x in prefix.split(";") if x.strip().upper().endswith(" ON")]
    off = [x.split()[1].replace("enable_", "") for x in prefix.split(";") if x.strip().upper().endswith(" OFF")]
    disabled = sorted(set(off) - set(on))
    return ("off: " + ", ".join(disabled)) if disabled else prefix[:60]


def render(results, runs):
    L = ["# NQO 专家计划与原生计划的执行时间", "",
         f"每种计划交错执行 {runs} 次，取最小值。`专家 / 原生` 低于 1 表示专家计划更快。规划器代价是 EXPLAIN 的估计，不是实测。", "",
         "| 查询 | 原生计划 | 专家 | 专家计划 | 专家 / 原生 | 代价估计 专家 / 原生 | 推理 ms | 专家的动作 |", "|---|---|---|---|---|---|---|---|"]
    wins, losses, unchanged = [], [], 0
    for r in results:
        if not r["experts"]:
            unchanged += 1
            continue
        for flt, e in r["experts"].items():
            t0, t1 = r["off_s"], e["s"]
            # a timed-out variant has no real duration: no ratio, no win or loss
            ratio = (t1 / t0) if (t0 and t1 and r["off_status"] == "ok" and e["status"] == "ok") else None
            cr = (e["cost"] / r["off_cost"]) if r.get("off_cost") and e.get("cost") else None
            if ratio is not None and ratio <= 0.8:
                wins.append((r["query"], flt, ratio))
            if ratio is not None and ratio >= 1.25:
                losses.append((r["query"], flt, ratio))
            L.append(f"| {r['query']} | {'–' if t0 is None else f'{t0:.2f} s'}{'' if r['off_status'] == 'ok' else ' (' + r['off_status'] + ')'} | {flt} | "
                     f"{'–' if t1 is None else f'{t1:.2f} s'}{'' if e['status'] == 'ok' else ' (' + e['status'] + ')'} | "
                     f"{'–' if ratio is None else f'{ratio:.2f}'} | {'–' if cr is None else f'{cr:.2f}'} | {e['opt_ms'] if e['opt_ms'] is None else round(e['opt_ms'])} | {e['label']} |")
    L += ["", f"{len(results)} 条查询，其中 {unchanged} 条两位专家都不改变计划。",
          f"专家计划快 20% 以上：{len(wins)} 项（{', '.join(f'{q}/{f} {x:.2f}' for q, f, x in wins) or '无'}）。",
          f"专家计划慢 25% 以上：{len(losses)} 项（{', '.join(f'{q}/{f} {x:.2f}' for q, f, x in losses) or '无'}）。"]
    return "\n".join(L) + "\n", wins


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time the experts' plans against the cost-based plans")
    ap.add_argument("--config", required=True)
    ap.add_argument("--query-dir", default="queries/job_all")
    ap.add_argument("--out", default="runs/nqo_gain", help="prefix of the output files")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=120.0, help="statement timeout in seconds")
    ap.add_argument("--nqo-url", default="", help="default: derived from the config's stats_url")
    ap.add_argument("--filters", default=",".join(FILTERS))
    ap.add_argument("--only", default="", help="comma-separated query names to restrict to")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    url = args.nqo_url or cfg.nqo.stats_url.replace("/stats", "/optimize")
    filters = [f for f in args.filters.split(",") if f]
    only = {q for q in args.only.split(",") if q}
    files = sorted(glob.glob(os.path.join(args.query_dir, "*.sql")))
    if only:
        files = [f for f in files if os.path.splitext(os.path.basename(f))[0] in only]
    if not files:
        raise SystemExit(f"no queries in {args.query_dir}")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    runner = Runner(cfg.db, args.timeout)

    results = []
    print(f"{len(files)} queries, experts {filters}, {args.runs} interleaved runs, timeout {args.timeout:.0f} s", flush=True)
    for path in files:
        q = os.path.splitext(os.path.basename(path))[0]
        sql = open(path, encoding="utf-8").read().strip()
        rec = {"query": q, "experts": {}, "inference_ms": {}}
        for flt in filters:
            try:
                prefix, expert, opt_ms = ask_nqo(url, sql, flt)
            except Exception as e:
                print(f"  {q:5s} {flt}: NQO service error: {e}", flush=True)
                continue
            rec["inference_ms"][flt] = opt_ms      # paid whether or not the plan changes
            if prefix:
                rec["experts"][flt] = {"expert": expert, "action": prefix, "label": arm_label(prefix),
                                       "opt_ms": opt_ms, "stmts": variant_statements(sql, flt, prefix)}
        if not rec["experts"]:
            rec.update(off_s=None, off_status="not run", off_cost=None)
            results.append(rec)
            print(f"  {q:5s} plan unchanged by every expert", flush=True)
            continue
        variants = {"off": [sql], **{flt: e["stmts"] for flt, e in rec["experts"].items()}}
        rec["off_cost"] = runner.plan_cost(variants["off"])
        for flt, e in rec["experts"].items():
            e["cost"] = runner.plan_cost(e["stmts"])
        times = {name: [] for name in variants}
        status = {name: "ok" for name in variants}
        for _ in range(args.runs):
            for name, stmts in variants.items():
                t, st = runner.timed(stmts)
                if t is not None:
                    times[name].append(t)
                if st != "ok":
                    status[name] = st
        rec["off_s"] = min(times["off"]) if times["off"] else None
        rec["off_status"] = status["off"]
        for flt, e in rec["experts"].items():
            e["s"] = min(times[flt]) if times[flt] else None
            e["status"] = status[flt]
            e.pop("stmts", None)
        results.append(rec)
        line = f"  {q:5s} off {rec['off_s'] if rec['off_s'] is None else round(rec['off_s'], 2)} s"
        for flt, e in rec["experts"].items():
            line += f"  {flt} {e['s'] if e['s'] is None else round(e['s'], 2)} s ({e['label']})"
        print(line, flush=True)
        with open(args.out + ".json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)

    text, wins = render(results, args.runs)
    with open(args.out + ".md", "w", encoding="utf-8") as f:
        f.write(text)
    with open(args.out + "_wins.txt", "w", encoding="utf-8") as f:
        for q, flt, ratio in wins:
            f.write(f"{q} {flt} {ratio:.2f}\n")
    print(f"\n{text.splitlines()[-3]}\n{text.splitlines()[-2]}\n{text.splitlines()[-1]}")
    print(f"written: {args.out}.md, {args.out}.json, {args.out}_wins.txt")


if __name__ == "__main__":
    main()
