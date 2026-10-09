"""Create or drop SELIX (nrindex) indexes beside the btree indexes of the IMDB tables.

Candidates are the join keys of JOB: the foreign-key columns of NeurDB's
fkindexes.sql and the integer primary keys. Columns with few distinct values
(type columns such as info_type_id, a few hundred keys over millions of rows)
are skipped by default: the planner never uses an index for them and a SELIX
node cannot split a run of millions of equal keys.

Each index is built in its own connection, so the building backend's copy of
SELIX is freed right after; every other session builds its own copy on first
use (E1). The btree indexes are not touched.

Usage (inside the container, in experiment/):
  python tools/selix_indexes.py --config config/imdb_r2.json --list
  python tools/selix_indexes.py --config config/imdb_r2.json --create [--columns cast_info.movie_id,title.id]
  python tools/selix_indexes.py --config config/imdb_r2.json --keep-only runs/imdb_r2/f1_long_used_indexes.txt
  python tools/selix_indexes.py --config config/imdb_r2.json --drop
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gaproto.actions import SELIX_PRESETS  # noqa: E402
from gaproto.config import load_config  # noqa: E402
from gaproto.db import connect  # noqa: E402

FKINDEXES = "/neuragent/NeuralDB/aiengine/neurqo_frame/script/load_to_db/imdb/fkindexes.sql"
PREFIX = "nr_"
MIN_DISTINCT = 1000

BUILD_TIME_FN = """
CREATE OR REPLACE FUNCTION nrindex_build_time(OUT builds bigint, OUT build_ms double precision)
RETURNS record AS 'nram', 'nrindex_build_time' LANGUAGE C STRICT VOLATILE
"""


def index_name(table: str, column: str) -> str:
    return f"{PREFIX}{table}_{column}"[:63]


def candidates(cur, fkfile: str = FKINDEXES) -> List[Tuple[str, str]]:
    cols = []
    if os.path.isfile(fkfile):
        for m in re.finditer(r"create index \w+ on (\w+)\((\w+)\)", open(fkfile, encoding="utf-8").read(), re.I):
            cols.append((m.group(1), m.group(2)))
    cur.execute("""
        SELECT c.relname, a.attname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0]
        WHERE i.indisprimary AND i.indnatts = 1 AND a.atttypid IN ('int4'::regtype, 'int8'::regtype)
          AND c.relname NOT LIKE 'ycsb%' AND c.relname NOT LIKE 'e\\_%'
        ORDER BY 1""")
    for t, c in cur.fetchall():
        if (t, c) not in cols:
            cols.append((t, c))
    return cols


def distinct_estimate(cur, table: str, column: str) -> float:
    cur.execute("SELECT n_distinct FROM pg_stats WHERE schemaname = 'public' AND tablename = %s AND attname = %s",
                (table, column))
    row = cur.fetchone()
    if not row or row[0] is None:
        return float("inf")
    nd = float(row[0])
    if nd >= 0:
        return nd
    cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
    return -nd * float(cur.fetchone()[0])


def existing(cur) -> List[Tuple[str, str, str]]:
    cur.execute("""
        SELECT ic.relname, tc.relname, a.attname
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_am am ON am.oid = ic.relam AND am.amname = 'nrindex'
        JOIN pg_class tc ON tc.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = i.indkey[0]
        WHERE ic.relname LIKE 'nr\\_%' ORDER BY 1""")
    return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create or drop SELIX indexes beside the btree indexes")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="show candidates and existing SELIX indexes")
    g.add_argument("--create", action="store_true")
    g.add_argument("--drop", action="store_true", help="drop every SELIX index created by this tool")
    g.add_argument("--keep-only", default="", help="drop the SELIX indexes not named in this file")
    ap.add_argument("--config", required=True)
    ap.add_argument("--columns", default="", help="table.column list; default: all candidates")
    ap.add_argument("--density", default="default", choices=tuple(SELIX_PRESETS))
    ap.add_argument("--min-distinct", type=float, default=MIN_DISTINCT)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    admin = connect(cfg.db, "ga_tool")
    cur = admin.cursor()
    cur.execute(BUILD_TIME_FN)

    if args.list or args.create:
        cols = candidates(cur)
        if args.columns:
            wanted = [tuple(x.strip().split(".")) for x in args.columns.split(",") if x.strip()]
            cols = [c for c in wanted]
        chosen, skipped = [], []
        for t, c in cols:
            nd = distinct_estimate(cur, t, c)
            (chosen if args.columns or nd >= args.min_distinct else skipped).append((t, c, nd))
        have = {e[0] for e in existing(cur)}
        print(f"{len(chosen)} columns chosen, {len(skipped)} skipped (fewer than {args.min_distinct:.0f} distinct values)")
        for t, c, nd in chosen:
            print(f"  {t}.{c:18s} ~{nd:12.0f} distinct  {'exists' if index_name(t, c) in have else ''}")
        for t, c, nd in skipped:
            print(f"  skip {t}.{c} (~{nd:.0f} distinct)")
        if args.list:
            return 0
        init_d, max_d, min_d = SELIX_PRESETS[args.density]
        total = time.perf_counter()
        for t, c, _ in chosen:
            name = index_name(t, c)
            if name in have:
                continue
            conn = connect(cfg.db, "ga_tool")      # own backend: its SELIX copy is freed on close
            k = conn.cursor()
            k.execute(f"SET selix.init_density = {init_d}; SET selix.max_density = {max_d}; "
                      f"SET selix.min_density = {min_d};")
            t0 = time.perf_counter()
            k.execute(f"CREATE INDEX {name} ON {t} USING nrindex ({c})")
            print(f"  created {name} in {time.perf_counter() - t0:.1f} s", flush=True)
            conn.close()
        print(f"done in {time.perf_counter() - total:.0f} s; btree indexes unchanged")
        return 0

    if args.drop or args.keep_only:
        keep = set()
        if args.keep_only:
            keep = {l.split()[0] for l in open(args.keep_only, encoding="utf-8") if l.strip() and not l.startswith("#")}
        for name, t, c in existing(cur):
            if name in keep:
                print(f"  keep {name}")
                continue
            cur.execute(f"DROP INDEX IF EXISTS {name}")
            print(f"  dropped {name}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
