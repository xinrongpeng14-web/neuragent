"""Seed data and the reset between episodes.

Order of an episode reset:

  1. The YCSB driver closes its connection. The SELIX instance of the old
     episode disappears with that backend.
  2. The control connection drops the index, truncates the table and reloads
     it from the seed table (reset_table below).
  3. The YCSB driver opens a new connection and builds the index there.

The index has to be dropped before the reload. While it exists, every insert of
the reloading session would also go into that session's private SELIX instance,
which would grow by one copy of the table per episode.

Usage:
  python -m gaproto.reset --config cfg.json --prepare   # create the seed table once
  python -m gaproto.reset --config cfg.json             # reset the table
"""
from __future__ import annotations

import argparse
import io
import time

import numpy as np

from .config import Config, YcsbConfig, load_config
from .db import Admin
from .keys import lognormal_keys


def prepare_seed_table(admin: Admin, cfg: YcsbConfig, force: bool = False) -> int:
    """Create the seed table with initial_keys distinct keys. Returns its row count."""
    exists = admin.fetchone("SELECT to_regclass(%s) IS NOT NULL", (cfg.seed_table,))[0]
    if exists and not force:
        n = admin.fetchone(f"SELECT count(*) FROM {cfg.seed_table}")[0]
        if n == cfg.initial_keys:
            return n
    keys = lognormal_keys(cfg.initial_keys, cfg.key_seed, cfg.key_mu, cfg.key_sigma,
                          cfg.key_scale, unique=True)
    admin.execute(f"DROP TABLE IF EXISTS {cfg.seed_table}")
    admin.execute(f"CREATE TABLE {cfg.seed_table} (k int8 NOT NULL, v text)")
    buf = io.StringIO()
    for k in keys.tolist():
        buf.write(f"{k}\t{cfg.value}\n")
    buf.seek(0)
    with admin.conn.cursor() as cur:
        cur.copy_expert(f"COPY {cfg.seed_table} (k, v) FROM STDIN", buf)
    admin.execute(f"ANALYZE {cfg.seed_table}")
    return int(keys.size)


def load_lookup_keys(admin: Admin, cfg: YcsbConfig) -> np.ndarray:
    """Keys present in every episode from its first step; targets of the point lookups."""
    rows = admin.fetchall(f"SELECT k FROM {cfg.seed_table}")
    return np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))


def reset_table(admin: Admin, cfg: YcsbConfig) -> float:
    """Step 2 of the episode reset. Returns the seconds it took."""
    t0 = time.perf_counter()
    admin.execute(f"DROP INDEX IF EXISTS {cfg.index}")
    admin.execute(f"CREATE TABLE IF NOT EXISTS {cfg.table} (k int8 NOT NULL, v text)")
    admin.execute(f"TRUNCATE {cfg.table}")
    admin.execute(f"INSERT INTO {cfg.table} SELECT k, v FROM {cfg.seed_table}")
    admin.execute(f"ANALYZE {cfg.table}")
    return time.perf_counter() - t0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Prepare the YCSB seed table or reset the YCSB table")
    ap.add_argument("--config", required=True)
    ap.add_argument("--prepare", action="store_true", help="create the seed table")
    ap.add_argument("--force", action="store_true", help="recreate the seed table even if it exists")
    args = ap.parse_args(argv)

    cfg: Config = load_config(args.config)
    admin = Admin(cfg.db)
    try:
        if args.prepare:
            n = prepare_seed_table(admin, cfg.ycsb, force=args.force)
            print(f"seed table {cfg.ycsb.seed_table}: {n} keys")
        took = reset_table(admin, cfg.ycsb)
        n = admin.fetchone(f"SELECT count(*) FROM {cfg.ycsb.table}")[0]
        print(f"table {cfg.ycsb.table} reset to {n} rows in {took:.2f} s; index dropped")
    finally:
        admin.close()


if __name__ == "__main__":
    main()
