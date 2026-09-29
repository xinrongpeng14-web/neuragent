"""JOB driver: concurrent clients that run analytical queries in a loop.

The clients use the simple query protocol and never set enable_molqo or
molqo.expert_filter themselves, so they follow whatever the Global Agent put
into the server configuration. A prepared statement would keep the plan of its
first execution and ignore later changes of the NQO mode.
"""
from __future__ import annotations

import glob
import multiprocessing as mp
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import psycopg2

from .config import DbConfig, JobConfig
from .db import connect

Query = Tuple[str, str]  # (template id, SQL text)


def load_queries(query_dir: str) -> List[Query]:
    """One query per *.sql file; the file name without extension is the template id."""
    queries = []
    for path in sorted(glob.glob(os.path.join(query_dir, "*.sql"))):
        with open(path, encoding="utf-8") as f:
            sql = f.read().strip()
        if sql:
            queries.append((os.path.splitext(os.path.basename(path))[0], sql))
    if not queries:
        raise ValueError(f"no *.sql files with content in {query_dir}")
    return queries


class _Client(threading.Thread):
    def __init__(self, idx: int, pool: "_Pool"):
        super().__init__(daemon=True, name=f"job-client-{idx}")
        self.idx = idx
        self.pool = pool
        self.conn = None
        self.in_flight = False

    def _connect(self):
        conn = connect(self.pool.db_cfg, "ga_job", molqo_off=False)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(self.pool.cfg.statement_timeout_ms)}")
        return conn

    def cancel(self) -> None:
        conn = self.conn
        if conn is not None and self.in_flight:
            try:
                conn.cancel()
            except Exception:
                pass

    def run(self) -> None:
        pool = self.pool
        order: Optional[np.ndarray] = None
        pos = 0
        epoch = -1
        while not pool.exiting:
            with pool.cond:
                while not pool.exiting and not (pool.running and self.idx < pool.active):
                    pool.cond.wait(0.2)
                if pool.exiting:
                    break
                if epoch != pool.epoch:
                    epoch = pool.epoch
                    rng = np.random.default_rng([pool.episode_seed, self.idx])
                    order = rng.permutation(len(pool.queries))
                    pos = 0
            template, sql = pool.queries[int(order[pos % len(order)])]
            pos += 1
            try:
                if self.conn is None or self.conn.closed:
                    self.conn = self._connect()
                t0 = time.perf_counter()
                self.in_flight = True
                with self.conn.cursor() as cur:
                    cur.execute(sql)
                    cur.fetchall()
                elapsed = time.perf_counter() - t0
                self.in_flight = False
                pool.record(epoch, template, elapsed)
            except psycopg2.Error as e:
                self.in_flight = False
                pool.record_error(epoch, template, e)
                time.sleep(0.05)
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass


class _Pool:
    def __init__(self, db_cfg: DbConfig, cfg: JobConfig, queries: List[Query]):
        self.db_cfg = db_cfg
        self.cfg = cfg
        self.queries = queries
        self.cond = threading.Condition()
        self.lock = threading.Lock()
        self.running = False
        self.exiting = False
        self.active = 0
        self.epoch = 0
        self.episode_seed = 0
        self.completed: List[Tuple[str, float]] = []
        self.n_error = 0
        self.last_error: Optional[str] = None
        self.clients = [_Client(i, self) for i in range(cfg.max_clients)]
        for c in self.clients:
            c.start()

    def record(self, epoch: int, template: str, elapsed: float) -> None:
        with self.lock:
            if epoch == self.epoch and self.running:
                self.completed.append((template, elapsed))

    def record_error(self, epoch: int, template: str, e: Exception) -> None:
        with self.lock:
            # queries cancelled by stop() are not failures of the workload
            if epoch == self.epoch and self.running:
                self.n_error += 1
                self.last_error = f"{template}: {type(e).__name__}: {e}".strip()

    def start(self, episode_seed: int, clients: int) -> None:
        with self.cond:
            self.episode_seed = int(episode_seed)
            self.epoch += 1
            self.active = clients
            self.running = True
            with self.lock:
                self.completed, self.n_error, self.last_error = [], 0, None
            self.cond.notify_all()

    def set_clients(self, n: int) -> None:
        with self.cond:
            self.active = n
            self.cond.notify_all()

    def stop(self, wait_s: float = 10.0) -> None:
        with self.cond:
            self.running = False
        deadline = time.time() + wait_s
        while time.time() < deadline and any(c.in_flight for c in self.clients):
            for c in self.clients:
                c.cancel()
            time.sleep(0.05)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            done, self.completed = self.completed, []
            n_error, self.n_error = self.n_error, 0
            last_error, self.last_error = self.last_error, None
        return {"completed": done, "n_error": n_error, "last_error": last_error,
                "in_flight": sum(1 for c in self.clients if c.in_flight),
                "active": self.active}

    def exit(self) -> None:
        self.stop()
        with self.cond:
            self.exiting = True
            self.cond.notify_all()
        for c in self.clients:
            c.join(timeout=2)


def _main(pipe, db_cfg: DbConfig, cfg: JobConfig, queries: List[Query]) -> None:
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pool = _Pool(db_cfg, cfg, queries)
    while True:
        msg = pipe.recv()
        cmd = msg["cmd"]
        try:
            if cmd == "start":
                pool.start(msg["episode_seed"], msg["clients"])
                reply: Dict[str, Any] = {"ok": True}
            elif cmd == "clients":
                pool.set_clients(msg["n"])
                reply = {"ok": True}
            elif cmd == "snapshot":
                reply = {"ok": True, **pool.snapshot()}
            elif cmd == "stop":
                pool.stop()
                reply = {"ok": True}
            elif cmd == "exit":
                pool.exit()
                pipe.send({"ok": True})
                return
            else:
                reply = {"ok": False, "error": f"unknown command {cmd}"}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}".strip()}
        pipe.send(reply)


class JobDriver:
    """Handle of the driver process, used by the environment."""

    def __init__(self, db_cfg: DbConfig, cfg: JobConfig, queries: List[Query],
                 reply_timeout_s: float = 60.0):
        ctx = mp.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.proc = ctx.Process(target=_main, args=(child, db_cfg, cfg, queries), daemon=True)
        self.proc.start()
        child.close()
        self.reply_timeout_s = reply_timeout_s

    def _call(self, **msg) -> Dict[str, Any]:
        self.pipe.send(msg)
        if not self.pipe.poll(self.reply_timeout_s):
            raise TimeoutError(f"JOB driver did not answer '{msg['cmd']}'")
        reply = self.pipe.recv()
        if not reply.get("ok"):
            raise RuntimeError(f"JOB driver: {reply.get('error')}")
        return reply

    def start(self, episode_seed: int, clients: int) -> None:
        self._call(cmd="start", episode_seed=int(episode_seed), clients=int(clients))

    def set_clients(self, n: int) -> None:
        self._call(cmd="clients", n=int(n))

    def snapshot(self) -> Dict[str, Any]:
        return self._call(cmd="snapshot")

    def stop(self) -> None:
        self._call(cmd="stop")

    def close(self) -> None:
        if self.proc.is_alive():
            try:
                self._call(cmd="exit")
            except Exception:
                pass
            self.proc.join(timeout=5)
            if self.proc.is_alive():
                self.proc.terminate()
        self.pipe.close()
