#!/bin/bash
# The experiment, stage by stage. Runs inside the container as user neurdb:
#
#     docker exec <container> bash /neuragent/experiment/pipeline.sh <stage> [options]
#
# Stages (round 2 order):
#   test        unit tests of the experiment program (no database needed)
#   queries     split the JOB queries by baseline latency into the fast set (<= THRESHOLD s)
#               and the long set (THRESHOLD .. LONG_MAX s)
#   gain        calibration 1: time every expert plan against the cost-based plan
#   gain_summary  sum the expert's effect per query set (printed by gain as well)
#   memcal      calibration 2: JOB throughput with a small and a large SELIX index
#   seed        create the YCSB seed table
#   baseline    original system (NQO auto, SELIX default): reference values for the reward
#   sweep_nqo   the 4 NQO modes with SELIX default, one episode each -> best static NQO mode
#   sweep_selix the 3 SELIX presets with NQO off, one episode each -> best static preset
#   train       PPO training of the Global Agent
#   evaluate    arms none / nqo / selix / static-best / ga, alternating, on the evaluation seeds
#   report      the result report (Markdown + JSON), percentages relative to static-best
#   all         everything above except the two calibrations
#
# Settings:
#   CONFIG            default config/imdb_r2.json  (config/imdb_r2_short.json for a quick check)
#   THRESHOLD         upper bound of the fast set, seconds                      default 1.0
#   LONG_MIN          shortest baseline admitted to the long set, seconds       default 10
#   LONG_MAX          longest baseline admitted to the long set, seconds        default 120
#   GAIN_RUNS         runs per variant in the gain calibration                  default 3
#   GAIN_TIMEOUT      statement timeout in the gain calibration, seconds        default 120
#   MEMCAL_KEYS       index sizes of the memory calibration                     default 1000000,20000000
#   MEMCAL_STEPS      steps per size                                            default 6
#   BASELINE_EPISODES default 3
#   TRAIN_STEPS       default 1200
#   EVAL_SEEDS        default 2001,2002,2003
#   EVAL_EPISODES     episodes per seed and arm                                 default 2
#   NQO_MODE          NQO mode of the nqo / static-best arms; default: best of sweep_nqo
#   SELIX_PRESET      preset of the selix / static-best arms; default: best of sweep_selix
#   RUN_PREFIX        prefix of the run names under <log_dir>                  default empty
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
    exec su neurdb -c "CONFIG=${CONFIG:-} THRESHOLD=${THRESHOLD:-} LONG_MIN=${LONG_MIN:-} LONG_MAX=${LONG_MAX:-} GAIN_RUNS=${GAIN_RUNS:-} \
        GAIN_TIMEOUT=${GAIN_TIMEOUT:-} MEMCAL_KEYS=${MEMCAL_KEYS:-} MEMCAL_STEPS=${MEMCAL_STEPS:-} \
        BASELINE_EPISODES=${BASELINE_EPISODES:-} TRAIN_STEPS=${TRAIN_STEPS:-} EVAL_SEEDS=${EVAL_SEEDS:-} \
        EVAL_EPISODES=${EVAL_EPISODES:-} NQO_MODE=${NQO_MODE:-} SELIX_PRESET=${SELIX_PRESET:-} RUN_PREFIX=${RUN_PREFIX:-} bash $0 $*"
fi
cd "$(dirname "$0")"
PY=${PY:-/opt/venv/bin/python}
CONFIG=${CONFIG:-config/imdb_r2.json}
THRESHOLD=${THRESHOLD:-1.0}
LONG_MIN=${LONG_MIN:-10}
LONG_MAX=${LONG_MAX:-120}
GAIN_RUNS=${GAIN_RUNS:-3}
GAIN_TIMEOUT=${GAIN_TIMEOUT:-120}
MEMCAL_KEYS=${MEMCAL_KEYS:-1000000,20000000}
MEMCAL_STEPS=${MEMCAL_STEPS:-6}
BASELINE_EPISODES=${BASELINE_EPISODES:-3}
TRAIN_STEPS=${TRAIN_STEPS:-1200}
EVAL_SEEDS=${EVAL_SEEDS:-2001,2002,2003}
EVAL_EPISODES=${EVAL_EPISODES:-2}
RUN_PREFIX=${RUN_PREFIX:-}
stage=${1:-}; shift || true
[ -n "$stage" ] || { sed -n '2,40p' "$0"; exit 1; }

cfg() { $PY -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$CONFIG" "$1"; }
LOG_DIR=$(cfg "d.get('log_dir','runs')")
REFS=$(cfg "d.get('refs_path','refs.json')")
FAST_DIR=$(cfg "d.get('job',{}).get('query_dir','queries/job_fast')")
# the long set is whatever the phases reference besides the fast set, or queries/job_long
LONG_DIR=$(cfg "next((p['query_dir'] for p in d.get('phases',[]) if p.get('query_dir') and p['query_dir']!=d.get('job',{}).get('query_dir','queries/job_fast')), 'queries/job_long')")
mkdir -p "$LOG_DIR"
log() { echo; echo "===== [$(date '+%F %T')] $* ====="; }

best_arm() {   # $1 = episodes.json of a sweep, prints the arm suffix with the highest mean reward
    $PY - "$1" "$2" <<'PY'
import json, sys, collections
path, fallback = sys.argv[1:3]
try:
    eps = json.load(open(path))
except FileNotFoundError:
    print(fallback); sys.exit()
score = collections.defaultdict(list)
for e in eps:
    score[e["arm"].split("_", 1)[1]].append(e["mean_reward"])
print(max(score, key=lambda k: sum(score[k]) / len(score[k])) if score else fallback)
PY
}

run_stage() {
    local name=$1; shift        # the remaining arguments go to the stage's program
    case $name in
    test)
        log "unit tests"
        $PY -m unittest discover -s tests -p "test_*.py" ;;
    queries)
        log "JOB queries: fast set <= $THRESHOLD s -> $FAST_DIR, long set $LONG_MIN..$LONG_MAX s -> $LONG_DIR"
        $PY tools/select_job_queries.py --config "$CONFIG" --job-dir queries/job_all --out "$FAST_DIR" \
            --threshold "$THRESHOLD" --out-long "$LONG_DIR" --long-min "$LONG_MIN" --long-max "$LONG_MAX" "$@" ;;
    gain)
        log "calibration 1: expert plans vs cost-based plans, $GAIN_RUNS runs, timeout $GAIN_TIMEOUT s"
        $PY tools/nqo_plan_gain.py --config "$CONFIG" --query-dir queries/job_all \
            --out "$LOG_DIR/${RUN_PREFIX}nqo_gain" --runs "$GAIN_RUNS" --timeout "$GAIN_TIMEOUT" "$@"
        $PY tools/nqo_gain_summary.py --gain "$LOG_DIR/${RUN_PREFIX}nqo_gain.json" --sets "$FAST_DIR,$LONG_DIR" --max-long "$LONG_MAX" ;;
    gain_summary)
        log "net effect of the expert plans per query set"
        $PY tools/nqo_gain_summary.py --gain "$LOG_DIR/${RUN_PREFIX}nqo_gain.json" --sets "$FAST_DIR,$LONG_DIR" --max-long "$LONG_MAX" "$@" ;;
    memcal)
        log "calibration 2: JOB throughput with index sizes $MEMCAL_KEYS, $MEMCAL_STEPS steps each"
        $PY tools/mem_coupling.py --config "$CONFIG" --keys "$MEMCAL_KEYS" --steps "$MEMCAL_STEPS" "$@" ;;
    seed)
        log "YCSB seed table"
        $PY -m gaproto.reset --config "$CONFIG" --prepare ;;
    baseline)
        log "original system, $BASELINE_EPISODES episodes -> $REFS"
        $PY -m gaproto.baseline --config "$CONFIG" --episodes "$BASELINE_EPISODES" --run-name "${RUN_PREFIX}baseline" "$@" ;;
    sweep_nqo)
        log "NQO modes with SELIX default, one episode each"
        $PY -m gaproto.evaluate --config "$CONFIG" --run-name "${RUN_PREFIX}sweep_nqo" \
            --arm nqo_off=fixed:off/default --arm nqo_auto=fixed:auto/default \
            --arm nqo_hint=fixed:hint/default --arm nqo_join=fixed:join/default \
            --seeds "${EVAL_SEEDS%%,*}" --episodes-per-seed 1 "$@"
        echo "best NQO mode: $(best_arm "$LOG_DIR/${RUN_PREFIX}sweep_nqo/episodes.json" auto)" ;;
    sweep_selix)
        log "SELIX presets with NQO off, one episode each"
        $PY -m gaproto.evaluate --config "$CONFIG" --run-name "${RUN_PREFIX}sweep_selix" \
            --arm selix_dense=fixed:off/dense --arm selix_mid=fixed:off/mid --arm selix_default=fixed:off/default \
            --seeds "${EVAL_SEEDS%%,*}" --episodes-per-seed 1 "$@"
        echo "best SELIX preset: $(best_arm "$LOG_DIR/${RUN_PREFIX}sweep_selix/episodes.json" default)" ;;
    train)
        log "PPO training, $TRAIN_STEPS steps"
        $PY -m gaproto.train --config "$CONFIG" --total-steps "$TRAIN_STEPS" --run-name "${RUN_PREFIX}train" "$@" ;;
    evaluate)
        local mode=${NQO_MODE:-$(best_arm "$LOG_DIR/${RUN_PREFIX}sweep_nqo/episodes.json" auto)}
        local preset=${SELIX_PRESET:-$(best_arm "$LOG_DIR/${RUN_PREFIX}sweep_selix/episodes.json" default)}
        log "evaluation: none, nqo($mode), selix($preset), static-best($mode/$preset), ga; seeds $EVAL_SEEDS x $EVAL_EPISODES"
        local model="$LOG_DIR/${RUN_PREFIX}train/model.zip"
        local ga=()
        [ -f "$model" ] && ga=(--arm "ga=ppo:$model") || echo "no trained model at $model; evaluating without the GA arm"
        $PY -m gaproto.evaluate --config "$CONFIG" --run-name "${RUN_PREFIX}eval" \
            --arm none=fixed:off/default --arm "nqo=fixed:$mode/default" --arm "selix=fixed:off/$preset" \
            --arm "static-best=fixed:$mode/$preset" "${ga[@]}" \
            --seeds "$EVAL_SEEDS" --episodes-per-seed "$EVAL_EPISODES" "$@" ;;
    report)
        log "report"
        local train=()
        [ -f "$LOG_DIR/${RUN_PREFIX}train/steps.jsonl" ] && train=(--train "$LOG_DIR/${RUN_PREFIX}train/steps.jsonl")
        $PY -m gaproto.report --config "$CONFIG" --refs "$REFS" --runs "$LOG_DIR/${RUN_PREFIX}eval/steps.jsonl" \
            "${train[@]}" --reference static-best --out "$LOG_DIR/${RUN_PREFIX}report.md" --json "$LOG_DIR/${RUN_PREFIX}summary.json" "$@" ;;
    all)
        for s in test queries seed baseline sweep_nqo sweep_selix train evaluate report; do run_stage $s; done ;;
    *)
        echo "unknown stage $name" >&2; exit 1 ;;
    esac
}
run_stage "$stage" "$@"
