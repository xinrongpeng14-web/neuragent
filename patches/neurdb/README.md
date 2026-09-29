# Changes to NeurDB

The prototype needs a modified NeurDB. The modifications are kept here as
patches against a pinned upstream commit, so that this repository never
contains a copy of NeurDB and nothing is ever pushed to the NeurDB repository.

| File | Content |
|---|---|
| `BASE_COMMIT` | Upstream commit of `github.com/neurdb/neurdb` the patches apply to |
| `0001-nr_am-selix-key-encoding-density-gucs-stats.patch` | SELIX bridge: key encoding fix, density settings, sampled timing, `nrindex_stats()`, `-O2` |
| `0002-nr_molqo-planner-scoped-hints-timeout-expert-filter.patch` | NQO, C side: planner-scoped hints, timeout, `molqo.expert_filter` |
| `0003-neurqo_frame-multiprocess-freeze-stats-filter.patch` | NQO, Python side: worker processes, frozen models, `/stats`, expert filter |

What each change does and how it was tested: `prototype/p2_p9/P2_P9_report.md`.

## The patch files are not in the public repository

NeurDB's `LICENSE` reads "All Rights Reserved" and restricts its files to
authorised individuals. A patch contains lines of the file it changes, so the
three patch files are excluded from git by `.gitignore` and exist only in the
working copy of whoever produced them.

If you are entitled to publish them, remove the block marked "Held back" from
`.gitignore`, then add and commit the files.

## Applying the patches

```bash
scripts/setup_neurdb.sh        # clones upstream at BASE_COMMIT into NeuralDB/ and applies the patches
```

## Regenerating the patches after changing NeurDB

```bash
cd NeuralDB
git add -N aiengine/neurqo_frame/run_moqoe_prototype.sh
git diff --binary main -- dbengine/nr_kernel/nr_am     > ../patches/neurdb/0001-nr_am-selix-key-encoding-density-gucs-stats.patch
git diff --binary main -- dbengine/nr_kernel/nr_molqo  > ../patches/neurdb/0002-nr_molqo-planner-scoped-hints-timeout-expert-filter.patch
git diff --binary main -- aiengine/neurqo_frame        > ../patches/neurdb/0003-neurqo_frame-multiprocess-freeze-stats-filter.patch
```
