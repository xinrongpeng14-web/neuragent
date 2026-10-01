#!/bin/bash
# The experiment, stage by stage. Runs inside the container as user neurdb:
#
#     docker exec <container> bash /neuragent/experiment/pipeline.sh <stage> [options]
#
# Stages (in order):
#   test        unit tests of the experiment program (no database needed)
#   queries     pick the JOB queries the original system finishes within THRESHOLD seconds
#   seed        create the YCSB seed table
#   baseline    original system (NQO auto, SELIX default): reference values for the reward
#   sweep       SELIX presets with NQO off, one episode each: which preset SELIX's own
#               offline tuning would pick for this workload
#   train       PPO training of the Global Agent
#   evaluate    all arms, alternating, on the evaluation seeds
#   report      the result report (Markdown + JSON)
#   all         everything above in order
#
# Settings:
#   CONFIG            default config/imdb.json   (config/imdb_short.json for a quick check)
#   THRESHOLD         JOB latency threshold in seconds for "queries"    default 1.0
#   BASELINE_EPISODES default 3
#   TRAIN_STEPS       default 1200
#   EVAL_SEEDS        default 2001,2002,2003
#   EVAL_EPISODES     episodes per seed and arm                          default 2
#   SELIX_PRESET      preset of the "selix" and "both" arms; default: the best one of "sweep"
#   RUN_PREFIX        prefix of the run names under <log_dir>           default empty
set -euo pipefail
if [ "$(id -u)" = 0 ]; then
    exec su neurdb -c "CONFIG=${CONFIG:-} THRESHOLD=${THRESHOLD:-} BASELINE_EPISODES=${BASELINE_EPISODES:-} \
        TRAIN_STEPS=${TRAIN_STEPS:-} EVAL_SEEDS=${EVAL_SEEDS:-} EVAL_EPISODES=${EVAL_EPISODES:-} \
        SELIX_PRESET=${SELIX_PRESET:-} RUN_PREFIX=${RUN_PREFIX:-} bash $0 $*"
fi
cd "$(dirname "$0")"
PY=${PY:-/opt/venv/bin/python}
CONFIG=${CONFIG:-config/imdb.json}
THRESHOLD=${THRESHOLD:-1.0}
BASELINE_EPISODES=${BASELINE_EPISODES:-3}
TRAIN_STEPS=${TRAIN_STEPS:-1200}
EVAL_SEEDS=${EVAL_SEEDS:-2001,2002,2003}
EVAL_EPISODES=${EVAL_EPISODES:-2}
RUN_PREFIX=${RUN_PREFIX:-}
stage=${1:-}; shift || true
[ -n "$stage" ] || { sed -n '2,30p' "$0"; exit 1; }

LOG_DIR=$($PY -c "import json,sys; print(json.load(open(sys.argv[1])).get('log_dir','runs'))" "$CONFIG")
REFS=$($PY -c "import json,sys; print(json.load(open(sys.argv[1])).get('refs_path','refs.json'))" "$CONFIG")
QUERY_DIR=$($PY -c "import json,sys; print(json.load(open(sys.argv[1])).get('job',{}).get('query_dir','queries/job_fast'))" "$CONFIG")
mkdir -p "$LOG_DIR"
log() { echo; echo "===== [$(date '+%F %T')] $* ====="; }

best_preset() {   # the preset with the highest mean SELIX reward in the sweep
    $PY - "$LOG_DIR/${RUN_PREFIX}sweep/episodes.json" <<'PY'
import json, sys, collections
try:
    eps = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("default"); sys.exit()
score = collections.defaultdict(list)
for e in eps:
    score[e["arm"].split("_", 1)[1]].append(e["mean_reward"])
best = max(score, key=lambda k: sum(score[k]) / len(score[k])) if score else "default"
print(best)
PY
}

run_stage() {
    local name=$1; shift        # the remaining arguments go to the stage's program
    case $name in
    test)
        log "unit tests"
        $PY -m unittest discover -s tests -p "test_*.py" ;;
    queries)
        log "JOB queries below $THRESHOLD s (original optimizer, NQO off)"
        $PY tools/select_job_queries.py --config "$CONFIG" --job-dir queries/job_all --out "$QUERY_DIR" \
            --threshold "$THRESHOLD" "$@" ;;
    seed)
        log "YCSB seed table"
        $PY -m gaproto.reset --config "$CONFIG" --prepare ;;
    baseline)
        log "original system, $BASELINE_EPISODES episodes -> $REFS"
        $PY -m gaproto.baseline --config "$CONFIG" --episodes "$BASELINE_EPISODES" --run-name "${RUN_PREFIX}baseline" "$@" ;;
    sweep)
        log "SELIX presets with NQO off, one episode each"
        $PY -m gaproto.evaluate --config "$CONFIG" --run-name "${RUN_PREFIX}sweep" \
            --arm selix_dense=fixed:off/dense --arm selix_default=fixed:off/default --arm selix_sparse=fixed:off/sparse \
            --seeds "${EVAL_SEEDS%%,*}" --episodes-per-seed 1 "$@"
        echo "best preset: $(best_preset)" ;;
    train)
        log "PPO training, $TRAIN_STEPS steps"
        $PY -m gaproto.train --config "$CONFIG" --total-steps "$TRAIN_STEPS" --run-name "${RUN_PREFIX}train" "$@" ;;
    evaluate)
        local preset=${SELIX_PRESET:-$(best_preset)}
        log "evaluation: arms none, nqo, selix($preset), both($preset), ga; seeds $EVAL_SEEDS x $EVAL_EPISODES"
        local model="$LOG_DIR/${RUN_PREFIX}train/model.zip"
        local ga=()
        [ -f "$model" ] && ga=(--arm "ga=ppo:$model") || echo "no trained model at $model; evaluating without the GA arm"
        $PY -m gaproto.evaluate --config "$CONFIG" --run-name "${RUN_PREFIX}eval" \
            --arm none=fixed:off/default --arm nqo=original --arm "selix=fixed:off/$preset" \
            --arm "both=fixed:auto/$preset" "${ga[@]}" \
            --seeds "$EVAL_SEEDS" --episodes-per-seed "$EVAL_EPISODES" "$@" ;;
    report)
        log "report"
        local train=()
        [ -f "$LOG_DIR/${RUN_PREFIX}train/steps.jsonl" ] && train=(--train "$LOG_DIR/${RUN_PREFIX}train/steps.jsonl")
        $PY -m gaproto.report --config "$CONFIG" --refs "$REFS" --runs "$LOG_DIR/${RUN_PREFIX}eval/steps.jsonl" \
            "${train[@]}" --reference none --out "$LOG_DIR/${RUN_PREFIX}report.md" --json "$LOG_DIR/${RUN_PREFIX}summary.json" "$@" ;;
    all)
        for s in test queries seed baseline sweep train evaluate report; do run_stage $s; done ;;
    *)
        echo "unknown stage $name" >&2; exit 1 ;;
    esac
}
run_stage "$stage" "$@"
