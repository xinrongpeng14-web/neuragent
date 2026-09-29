"""YCSB driver: one connection doing point lookups and inserts on a SELIX index.

The driver runs in its own process so that its tight loop does not compete with
the JOB clients for the interpreter lock. It is controlled through a pipe.

The SELIX index lives only in the backend that built it. The driver therefore
builds the index on its own connection at the start of every episode and keeps
that connection for all operations, parameter changes and statistics calls.
Only equality lookups and inserts may run on this connection: with sequential
scans disabled, any other query is answered from the index and comes back
empty.
"""
from __future__ import annotations

import multiprocessing as mp
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import psycopg2

from .config import DbConfig, YcsbConfig
from .db import connect
from .keys import MAX_KEY

STATS_COLUMNS = ("n_get", "n_put", "t_get_ns", "c_get", "t_put_ns", "c_put", "mem_bytes",
                 "n_smo", "n_keys", "n_indexes", "init_density", "max_density", "min_density")


def density_sql(init_d: float, max_d: float, min_d: float) -> str:
    """The three settings in one message, so that no index operation falls between them."""
    return (f"SET selix.init_density = {init_d:.4f}; "
            f"SET selix.max_density = {max_d:.4f}; "
            f"SET selix.min_density = {min_d:.4f};")


class TokenPacer:
    """Spreads operations evenly over time; rate 0 means no limit.

    Operations that could not be sent in time are dropped rather than sent
    later in a burst: at most burst_s seconds worth of them are kept. Otherwise
    a stall would be followed by a period above the configured rate.
    """

    def __init__(self, rate: float, now: float, burst_s: float = 0.2):
        self.rate = float(rate)
        self.start = now
        self.done = 0
        self.max_backlog = max(1, int(self.rate * burst_s))

    def allowance(self, now: float, want: int) -> int:
        if self.rate <= 0:
            return want
        due = int((now - self.start) * self.rate) - self.done
        if due > self.max_backlog:
            self.done += due - self.max_backlog
            due = self.max_backlog
        return max(0, min(want, due))

    def spent(self, n: int) -> None:
        self.done += n


class _Worker:
    def __init__(self, pipe, db_cfg: DbConfig, cfg: YcsbConfig, lookup_keys: np.ndarray):
        self.pipe = pipe
        self.db_cfg = db_cfg
        self.cfg = cfg
        self.lookup_keys = lookup_keys
        self.conn = None
        self.cur = None
        self.running = False
        self.alive = True
        self.error: Optional[str] = None
        self.read_ratio = 0.5
        self.pacer = TokenPacer(0, time.perf_counter())
        self.rng = np.random.default_rng(0)
        self._reset_counters()

    def _reset_counters(self) -> None:
        self.n_read = 0
        self.n_insert = 0
        self.n_error = 0
        self.n_miss = 0
        self.latencies: List[float] = []

    # ---- commands -------------------------------------------------------
    def start(self, episode_seed: int, density: Tuple[float, float, float]) -> Dict[str, Any]:
        self.stop()
        c = self.cfg
        self.conn = connect(self.db_cfg, "ga_ycsb")
        self.cur = self.conn.cursor()
        self.cur.execute(f"SET synchronous_commit = {c.synchronous_commit}")
        # densities first: the bulk load of CREATE INDEX already uses them
        self.cur.execute(density_sql(*density))
        t0 = time.perf_counter()
        self.cur.execute(f"CREATE INDEX {c.index} ON {c.table} USING nrindex (k)")
        build_s = time.perf_counter() - t0
        self.cur.execute("SET enable_seqscan = off; SET enable_bitmapscan = off;")
        self.cur.execute(f"PREPARE ycsb_get(bigint) AS SELECT v FROM {c.table} WHERE k = $1")
        self.cur.execute(f"PREPARE ycsb_put(bigint, text) AS INSERT INTO {c.table} VALUES ($1, $2)")
        self.rng = np.random.default_rng(episode_seed)
        self.alive, self.error = True, None
        self._reset_counters()
        self.pacer = TokenPacer(self.pacer.rate, time.perf_counter())
        self.running = True
        return {"ok": True, "build_s": build_s, "stats": self._stats()}

    def stop(self) -> None:
        self.running = False
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn, self.cur = None, None

    def set_phase(self, read_ratio: float, rate: float) -> None:
        self.read_ratio = float(read_ratio)
        self.pacer = TokenPacer(rate, time.perf_counter())

    def set_density(self, density: Tuple[float, float, float]) -> Dict[str, Any]:
        self.cur.execute(density_sql(*density))
        return {"ok": True}

    def _stats(self) -> Dict[str, float]:
        self.cur.execute("SELECT * FROM nrindex_stats()")
        return dict(zip(STATS_COLUMNS, self.cur.fetchone()))

    def snapshot(self) -> Dict[str, Any]:
        lat = np.asarray(self.latencies, dtype=np.float64)
        out: Dict[str, Any] = {
            "alive": self.alive, "error": self.error,
            "n_read": self.n_read, "n_insert": self.n_insert,
            "n_error": self.n_error, "n_miss": self.n_miss,
            "lat_sum_s": float(lat.sum()) if lat.size else 0.0,
            "lat_p50_s": float(np.percentile(lat, 50)) if lat.size else 0.0,
            "lat_p99_s": float(np.percentile(lat, 99)) if lat.size else 0.0,
            "stats": None,
        }
        if self.alive and self.cur is not None:
            try:
                out["stats"] = self._stats()
            except psycopg2.Error as e:
                self._lost(e)
                out["alive"], out["error"] = False, self.error
        self._reset_counters()
        return out

    # ---- workload -------------------------------------------------------
    def _lost(self, e: Exception) -> None:
        self.alive, self.running = False, False
        self.error = f"{type(e).__name__}: {e}".strip()

    def run_batch(self) -> bool:
        """Run up to batch_ops operations. Returns False when there was nothing to do."""
        want = self.pacer.allowance(time.perf_counter(), self.cfg.batch_ops)
        if want == 0:
            return False
        c = self.cfg
        is_read = self.rng.random(want) < self.read_ratio
        read_keys = self.lookup_keys[self.rng.integers(0, self.lookup_keys.size, want)]
        new_keys = np.clip(np.rint(self.rng.lognormal(c.key_mu, c.key_sigma, want) * c.key_scale),
                           1, MAX_KEY).astype(np.int64)
        cur = self.cur
        clock = time.perf_counter
        done = 0
        for i in range(want):
            # a waiting command is answered without finishing the batch, so
            # that snapshots stay on time when operations are slow
            if i and i % 8 == 0 and self.pipe.poll(0):
                break
            done = i + 1
            t0 = clock()
            try:
                if is_read[i]:
                    cur.execute("EXECUTE ycsb_get(%s)", (int(read_keys[i]),))
                    if cur.fetchone() is None:
                        self.n_miss += 1
                    self.n_read += 1
                else:
                    cur.execute("EXECUTE ycsb_put(%s, %s)", (int(new_keys[i]), c.value))
                    self.n_insert += 1
            except psycopg2.Error as e:
                if self.conn is None or self.conn.closed:
                    self._lost(e)
                    return True
                self.n_error += 1
                continue
            self.latencies.append(clock() - t0)
        self.pacer.spent(done)
        return True

    # ---- main loop ------------------------------------------------------
    def serve(self) -> None:
        while True:
            busy = self.running and self.alive
            while self.pipe.poll(0 if busy else 0.05):
                msg = self.pipe.recv()
                try:
                    reply = self._handle(msg)
                except Exception as e:  # reported to the controller, not fatal
                    if self.conn is not None and self.conn.closed:
                        self._lost(e)
                    reply = {"ok": False, "error": f"{type(e).__name__}: {e}".strip()}
                if msg["cmd"] == "exit":
                    self.pipe.send({"ok": True})
                    return
                self.pipe.send(reply)
                busy = self.running and self.alive
            if busy and not self.run_batch():
                time.sleep(0.001)

    def _handle(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        cmd = msg["cmd"]
        if cmd == "start":
            return self.start(msg["episode_seed"], tuple(msg["density"]))
        if cmd == "stop":
            self.stop()
            return {"ok": True}
        if cmd == "phase":
            self.set_phase(msg["read_ratio"], msg["rate"])
            return {"ok": True}
        if cmd == "density":
            return self.set_density(tuple(msg["density"]))
        if cmd == "snapshot":
            return {"ok": True, **self.snapshot()}
        if cmd == "exit":
            self.stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd}"}


def _main(pipe, db_cfg: DbConfig, cfg: YcsbConfig, lookup_keys: np.ndarray) -> None:
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _Worker(pipe, db_cfg, cfg, lookup_keys).serve()


class YcsbDriver:
    """Handle of the driver process, used by the environment."""

    def __init__(self, db_cfg: DbConfig, cfg: YcsbConfig, lookup_keys: np.ndarray,
                 reply_timeout_s: float = 600.0):
        ctx = mp.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.proc = ctx.Process(target=_main, args=(child, db_cfg, cfg, lookup_keys), daemon=True)
        self.proc.start()
        child.close()
        self.reply_timeout_s = reply_timeout_s

    def _call(self, **msg) -> Dict[str, Any]:
        self.pipe.send(msg)
        if not self.pipe.poll(self.reply_timeout_s):
            raise TimeoutError(f"YCSB driver did not answer '{msg['cmd']}'")
        reply = self.pipe.recv()
        if not reply.get("ok"):
            raise RuntimeError(f"YCSB driver: {reply.get('error')}")
        return reply

    def start(self, episode_seed: int, density) -> Dict[str, Any]:
        return self._call(cmd="start", episode_seed=int(episode_seed), density=list(density))

    def stop(self) -> None:
        self._call(cmd="stop")

    def set_phase(self, read_ratio: float, rate: float) -> None:
        self._call(cmd="phase", read_ratio=read_ratio, rate=rate)

    def set_density(self, density) -> None:
        self._call(cmd="density", density=list(density))

    def snapshot(self) -> Dict[str, Any]:
        return self._call(cmd="snapshot")

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
