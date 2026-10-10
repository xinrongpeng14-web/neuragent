"""State of the GA (plan v0.6, section 5.5).

Observation layout (OBS_DIM = 38, every entry clipped to [-3, 3]):

  0-2   NQO expert range in effect (one-hot: off, hint, auto)
  3-5   index scheme in effect (one-hot: btree, cost, prefer)
        -- upcoming batch: mean over the queries of the next step --
  6     native optimizer's estimated total cost, log10(cost) / 10
  7     joined tables / 17
  8-28  tables used: log10(rows) / 8 for each of the 21 IMDB tables, 0 if unused
  29    share of the equality join columns that have a SELIX index
  30    SELIX-indexed join columns: log10(rows) / 8           (key count)
  31    SELIX-indexed join columns: log10(rows per key) / 4  (duplication)
        -- recent load --
  32    group (0 long, 1 short)
  33    parallel sessions / 8
        -- last step --
  34    log(mean latency ratio to the original system)
  35    NQO inference time / total query time
  36    share of queries on which NQO applied an optimization
  37    share of the executed plans' index scans that used SELIX
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from . import commands as C

IMDB_TABLES: Tuple[str, ...] = (
    "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type", "company_name",
    "company_type", "complete_cast", "info_type", "keyword", "kind_type", "link_type",
    "movie_companies", "movie_info", "movie_info_idx", "movie_keyword", "movie_link", "name",
    "person_info", "role_type", "title")
OBS_DIM = 38
OBS_LOW, OBS_HIGH = -3.0, 3.0

_FROM = re.compile(r"\bFROM\b(.*?)\bWHERE\b", re.S | re.I)
_ALIAS = re.compile(r"^(\w+)(?:\s+(?:AS\s+)?(\w+))?$", re.I)
_JOIN = re.compile(r"\b(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\b")


@dataclass
class ParsedQuery:
    tables: List[str]                       # distinct tables, in order of appearance
    n_relations: int                        # relations in FROM (a table joined twice counts twice)
    join_cols: Set[Tuple[str, str]]         # (table, column) on either side of an equality join


def parse_query(sql: str) -> ParsedQuery:
    m = _FROM.search(sql)
    if not m:
        raise ValueError("query without FROM ... WHERE")
    aliases: Dict[str, str] = {}
    tables: List[str] = []
    items = [x.strip() for x in m.group(1).split(",") if x.strip()]
    for it in items:
        a = _ALIAS.match(it)
        if not a:
            raise ValueError(f"cannot parse FROM item {it!r}")
        table, alias = a.group(1), a.group(2) or a.group(1)
        aliases[alias] = table
        if table not in tables:
            tables.append(table)
    cols: Set[Tuple[str, str]] = set()
    for a1, c1, a2, c2 in _JOIN.findall(sql):
        if a1 in aliases and a2 in aliases:
            cols.add((aliases[a1], c1))
            cols.add((aliases[a2], c2))
    return ParsedQuery(tables, len(items), cols)


@dataclass
class ColumnStats:
    rows: float
    distinct: float


@dataclass
class Catalog:
    """What the GA knows about the database: table sizes, SELIX columns, column statistics."""
    table_rows: Dict[str, float]
    selix_cols: Set[Tuple[str, str]]
    col_stats: Dict[Tuple[str, str], ColumnStats] = field(default_factory=dict)

    @classmethod
    def load(cls, admin) -> "Catalog":
        rows = {t: float(n) for t, n in admin.fetchall(
            "SELECT relname, GREATEST(reltuples, 0) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'")}
        selix = {(t, c) for t, c in admin.fetchall(
            "SELECT tc.relname, a.attname FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_am am ON am.oid = ic.relam AND am.amname = 'nrindex' "
            "JOIN pg_class tc ON tc.oid = i.indrelid "
            "JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = i.indkey[0]")}
        stats: Dict[Tuple[str, str], ColumnStats] = {}
        for t, c, nd in admin.fetchall(
                "SELECT tablename, attname, n_distinct FROM pg_stats WHERE schemaname = 'public'"):
            if (t, c) not in selix:
                continue
            n = rows.get(t, 0.0)
            d = float(nd) if nd is not None else n
            d = -d * n if d < 0 else d
            stats[(t, c)] = ColumnStats(n, max(1.0, d))
        for tc in selix:
            stats.setdefault(tc, ColumnStats(rows.get(tc[0], 0.0), max(1.0, rows.get(tc[0], 1.0))))
        return cls(rows, selix, stats)


@dataclass
class QueryInfo:
    name: str
    sql: str
    parsed: ParsedQuery
    est_cost: float = 0.0


def native_cost(admin, sql: str) -> float:
    """Total cost of the native plan (NQO off, btree only), from EXPLAIN on the admin connection."""
    admin.execute("SET selix.enable_index = off")
    try:
        row = admin.fetchone("EXPLAIN (FORMAT JSON) " + sql)
    finally:
        admin.execute("RESET selix.enable_index")
    plan = row[0] if isinstance(row[0], list) else json.loads(row[0])
    return float(plan[0]["Plan"]["Total Cost"])


def query_vector(q: QueryInfo, cat: Catalog) -> np.ndarray:
    """Entries 6..31 of the observation for one query."""
    v = np.zeros(26, dtype=np.float64)
    v[0] = math.log10(max(1.0, q.est_cost)) / 10.0
    v[1] = q.parsed.n_relations / 17.0
    for t in q.parsed.tables:
        if t in IMDB_TABLES:
            v[2 + IMDB_TABLES.index(t)] = math.log10(1.0 + cat.table_rows.get(t, 0.0)) / 8.0
    cols = q.parsed.join_cols
    with_selix = [tc for tc in cols if tc in cat.selix_cols]
    v[23] = len(with_selix) / len(cols) if cols else 0.0
    if with_selix:
        st = [cat.col_stats[tc] for tc in with_selix]
        v[24] = float(np.mean([math.log10(1.0 + s.rows) for s in st])) / 8.0
        v[25] = float(np.mean([math.log10(max(1.0, s.rows / s.distinct)) for s in st])) / 4.0
    return v


@dataclass
class LastStep:
    """Measurements of the step that just finished (zeros before the first step)."""
    log_ratio: float = 0.0
    nqo_time_share: float = 0.0
    nqo_applied_share: float = 0.0
    selix_scan_share: float = 0.0


def observation(command: int, batch: Sequence[QueryInfo], cat: Catalog, group: str,
                workers: int, last: Optional[LastStep] = None) -> np.ndarray:
    nqo, scheme = C.decode(command)
    obs = np.zeros(OBS_DIM, dtype=np.float64)
    obs[C.NQO_RANGES.index(nqo)] = 1.0
    obs[3 + C.SCHEMES.index(scheme)] = 1.0
    if batch:
        obs[6:32] = np.mean([query_vector(q, cat) for q in batch], axis=0)
    obs[32] = 1.0 if group == "short" else 0.0
    obs[33] = workers / 8.0
    last = last or LastStep()
    obs[34:38] = (last.log_ratio, last.nqo_time_share, last.nqo_applied_share, last.selix_scan_share)
    obs = np.nan_to_num(obs, nan=0.0, posinf=OBS_HIGH, neginf=OBS_LOW)
    return np.clip(obs, OBS_LOW, OBS_HIGH).astype(np.float32)
