"""Render the NQO plan-gain results as the Markdown table of GlobalAgent_round2.md section 2.3."""
import json
import sys

cost_path, gain_path, doc_path = sys.argv[1:4]
cost = {r["query"]: r for r in json.load(open(cost_path, encoding="utf-8"))}
gain = {r["query"]: r for r in json.load(open(gain_path, encoding="utf-8"))}
try:
    base = json.load(open("/home/zhanhao/neuragent/experiment/queries/job_fast/latencies.json"))["results"]
except Exception:
    base = {}

L = []
# --- planner cost estimates for all changed queries
ratios = []
for q, r in cost.items():
    c0, c1 = r.get("off_cost"), r.get("hint_cost")
    if c0 and c1:
        ratios.append((q, c1 / c0))
worse = [q for q, x in ratios if x > 1.05]
same = [q for q, x in ratios if 0.95 <= x <= 1.05]
better = [q for q, x in ratios if x < 0.95]
L.append(f"**规划器对两种计划的代价估计（全部 {len(cost)} 条）。** HintPlanSel 选出的臂把规划器认为更便宜的计划排除掉了：")
L.append("")
L.append("| 专家计划的估计代价 / 原生计划的估计代价 | 查询数 | 查询 |")
L.append("|---|---|---|")
L.append(f"| 高出 5% 以上 | {len(worse)} | {', '.join(sorted(worse))} |")
L.append(f"| 相差 5% 以内 | {len(same)} | {', '.join(sorted(same))} |")
L.append(f"| 低 5% 以上 | {len(better)} | {', '.join(sorted(better))} |")
if ratios:
    xs = sorted(x for _, x in ratios)
    L.append("")
    L.append(f"中位数 {xs[len(xs)//2]:.2f} 倍，最大 {xs[-1]:.1f} 倍。估计代价只是规划器的看法，不是实测；专家存在的意义正是纠正规划器，所以还要看执行时间。")
# --- timed subset
L.append("")
L.append(f"**实测执行时间（开发机，1.5 核、2.5 GB、数据库 9 GB 远大于内存，所以绝对值很慢；每种计划各跑 {next(iter(gain.values()))['runs'] if gain else 2} 次交错执行取最小值）。** 只测了基线 3 秒以内的 {len(gain)} 条，其余在这台机器上会超时：")
L.append("")
L.append("| 查询 | 实验机基线 | 原生计划 | 专家计划 | 专家 / 原生 | 专家选的臂 |")
L.append("|---|---|---|---|---|---|")
faster = slower = 0
for q in sorted(gain, key=lambda s: (int(''.join(c for c in s if c.isdigit())), s)):
    r = gain[q]
    t0, t1 = r.get("off_s"), r.get("hint_s")
    st = f"{t1 / t0:.2f}" if t0 and t1 else "–"
    if t0 and t1:
        faster += t1 < 0.9 * t0
        slower += t1 > 1.1 * t0
    arm = ", ".join(s.split()[1].replace("enable_", "") for s in r.get("hint_action", "").split(";") if s.strip().lower().endswith(" on"))
    b = base.get(q, {}).get("best_s")
    L.append(f"| {q} | {'–' if b is None else f'{b:.2f} s'} | {'–' if t0 is None else f'{t0:.1f} s'}{'' if r.get('off_status') == 'ok' else ' (' + r.get('off_status', '') + ')'} | "
             f"{'–' if t1 is None else f'{t1:.1f} s'}{'' if r.get('hint_status') == 'ok' else ' (' + r.get('hint_status', '') + ')'} | {st} | 保留 {arm} |")
L.append("")
L.append(f"专家计划明显更快（低于 0.9 倍）的有 {faster} 条，明显更慢（高于 1.1 倍）的有 {slower} 条。")
table = "\n".join(L)
print(table)
doc = open(doc_path, encoding="utf-8").read()
assert "<!-- NQO_GAIN_TABLE -->" in doc
open(doc_path, "w", encoding="utf-8").write(doc.replace("<!-- NQO_GAIN_TABLE -->", table))
print("\n[inserted into", doc_path + "]")
