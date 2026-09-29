#!/bin/bash
# Put a patched NeurDB into NeuralDB/ next to this repository's files.
#
#   1. clone upstream NeurDB (skipped when NeuralDB/ exists)
#   2. disable pushing from the clone and from its SELIX submodule
#   3. check out the pinned commit on the local branch ga-prototype
#   4. apply the patches of patches/neurdb/
#
# Nothing is ever pushed to NeurDB. The clone is ignored by this repository.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCHES="$ROOT/patches/neurdb"
BASE="$(cat "$PATCHES/BASE_COMMIT")"
SELIX=dbengine/nr_kernel/nr_am/src/nram_storage/selix
NO_PUSH=DISABLED__do_not_push_to_upstream

shopt -s nullglob
patches=("$PATCHES"/*.patch)
if [ ${#patches[@]} -eq 0 ]; then
    echo "no patch files in $PATCHES; they are not part of the public repository," >&2
    echo "see $PATCHES/README.md" >&2
    exit 1
fi

cd "$ROOT"
[ -d NeuralDB/.git ] || git clone https://github.com/neurdb/neurdb.git NeuralDB
cd NeuralDB
git remote set-url --push origin "$NO_PUSH"

if [ -n "$(git status --porcelain)" ]; then
    echo "NeuralDB/ has uncommitted changes; refusing to touch it" >&2
    exit 1
fi
git checkout -q -B ga-prototype "$BASE"
git submodule update --init "$SELIX"
git -C "$SELIX" remote set-url --push origin "$NO_PUSH"

for p in "${patches[@]}"; do
    git apply --whitespace=nowarn "$p"
    echo "applied $(basename "$p")"
done
echo "NeuralDB/ is at $BASE plus the prototype patches (uncommitted, branch ga-prototype)"
