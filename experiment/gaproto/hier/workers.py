"""Sessions that execute the JOB queries of one GA step.

Every worker keeps one connection for the whole run, so its copies of the SELIX
indexes (each backend loads its own, E1) are built once, during warm-up, and
never inside a measured query. A query is timed with and without the time spent
building SELIX copies (nrindex_build_time() before and after). The executed
plan is captured with auto_explain, which tells which indexes the query used and
whether NQO applied an optimization.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2

from ..db import connect
from . import commands as C

SESSION_SETUP = ("SET client_min_messages = log", "LOAD 'auto_explain'",
                 "SET auto_explain.log_min_duration = 0", "SET auto_explain.log_format = 'json'",
                 "SET molqo.report_decisions = on", "SET selix.rebuild_on_density_change = off")


def plan_from_notices(notices: Sequence[str]) -> Optional[Any]:
    for msg in reversed(notices):
        pos = msg.find("plan:")
        if pos < 0:
            continue
        try:
            return json.loads(msg[pos + 5:].strip())
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


def nqo_action(notices: Sequence[str]) -> str:
    """'hint' (pg_hint_plan comment, JoinOrder), 'settings' (SET switches, HintPlanSel) or 'none'."""
    for msg in notices:
        if "optimization applied (hint)" in msg:
            return "hint"
        if "optimization applied (" in msg:
            return "settings"
    return "none"


def result_hash(rows) -> str:
    return hashlib.md5(repr(rows).encode()).hexdigest()[:12]


class Worker:
    def __init__(self, idx: int, cfg):
        self.idx = idx
        self.cfg = cfg
        self.conn = connect(cfg.db, f"ga_hier_{idx}", molqo_off=False)
        self.conn.notices = []
        self.cur = self.conn.cursor()
        for s in SESSION_SETUP + tuple(C.density_sql(cfg.density)) + (
                f"SET statement_timeout = {int(cfg.statement_timeout_ms)}",):
            try:
                self.cur.execute(s)
            except psycopg2.Error as e:
                raise SystemExit(f"worker {idx}: session setup failed at {s!r}: {e}")
        self.command: Optional[int] = None
        self.nqo_on = False

    # bookkeeping queries must not reach the NQO service
    def _plain(self, sql: str):
        if self.nqo_on:
            self.cur.execute("SET enable_molqo = off")
        try:
            self.cur.execute(sql)
            return self.cur.fetchone()
        finally:
            if self.nqo_on:
                self.cur.execute("SET enable_molqo = on")

    def build_ms(self) -> float:
        return float(self._plain("SELECT build_ms FROM nrindex_build_time()")[0])

    def apply(self, command: int) -> None:
        if command == self.command:
            return
        for s in C.session_sql(command):
            self.cur.execute(s)
        self.command = command
        self.nqo_on = C.decode(command)[0] != "off"

    def warm_up(self, probes: Iterable[Tuple[str, str, Any]]) -> Dict[str, float]:
        """Make this backend load its copy of every SELIX index (one point lookup each)."""
        self.apply(C.parse("off/prefer"))
        b0 = self._plain("SELECT builds, build_ms FROM nrindex_build_time()")
        self.cur.execute("SET enable_seqscan = off")
        self.cur.execute("SET enable_bitmapscan = off")
        try:
            for table, column, value in probes:
                self.cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,))
                self.cur.fetchall()
        finally:
            self.cur.execute("RESET enable_seqscan")
            self.cur.execute("RESET enable_bitmapscan")
            del self.conn.notices[:]
        b1 = self._plain("SELECT builds, build_ms FROM nrindex_build_time()")
        return {"builds": int(b1[0]) - int(b0[0]), "build_s": (float(b1[1]) - float(b0[1])) / 1000.0}

    def run(self, name: str, sql: str) -> Dict[str, Any]:
        b0 = self.build_ms()
        del self.conn.notices[:]
        status, fp = "ok", None
        t0 = time.perf_counter()
        try:
            self.cur.execute(sql)
            fp = result_hash(self.cur.fetchall())
        except psycopg2.errors.QueryCanceled:
            status = "timeout"
        except psycopg2.Error as e:
            status = "error: " + str(e).strip().splitlines()[0][:200]
            self.conn.rollback()
        wall = time.perf_counter() - t0
        notices = list(self.conn.notices)
        build = max(0.0, (self.build_ms() - b0) / 1000.0)
        if status == "timeout":
            wall = max(wall, self.cfg.statement_timeout_ms / 1000.0)
        plan = plan_from_notices(notices)
        idx = index_names(plan) if plan else []
        return {"query": name, "status": status, "wall_s": round(wall, 4), "build_s": round(build, 4),
                "net_s": round(max(0.0, wall - build), 4), "result": fp, "worker": self.idx,
                "command": C.name(self.command), "nqo_action": nqo_action(notices),
                "index_scans": len(idx), "selix_scans": sum(1 for i in idx if i.startswith("nr_")),
                "indexes": sorted(set(idx))}

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


class WorkerPool:
    """Runs a batch of queries on the workers, at most one query per worker at a time."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.workers = [Worker(i, cfg) for i in range(cfg.workers)]
        self.free: "queue.Queue[Worker]" = queue.Queue()
        for w in self.workers:
            self.free.put(w)
        self.executor = ThreadPoolExecutor(max_workers=len(self.workers), thread_name_prefix="ga-hier")
        self.lock = threading.Lock()

    def warm_up(self, probes: List[Tuple[str, str, Any]]) -> List[Dict[str, float]]:
        return list(self.executor.map(lambda w: w.warm_up(probes), self.workers))

    def apply(self, command: int) -> None:
        for w in self.workers:
            w.apply(command)

    def _one(self, name: str, sql: str) -> Dict[str, Any]:
        w = self.free.get()
        try:
            return w.run(name, sql)
        finally:
            self.free.put(w)

    def run_batch(self, batch: Sequence[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], float]:
        t0 = time.perf_counter()
        futures = [self.executor.submit(self._one, n, s) for n, s in batch]
        results = [f.result() for f in futures]
        return results, time.perf_counter() - t0

    def close(self) -> None:
        self.executor.shutdown(wait=True)
        for w in self.workers:
            w.close()


def warmup_probes(admin, selix_cols) -> List[Tuple[str, str, Any]]:
    """One existing key per SELIX column, for the warm-up lookups."""
    probes = []
    for table, column in sorted(selix_cols):
        row = admin.fetchone(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 1")
        if row is not None:
            probes.append((table, column, row[0]))
    return probes
