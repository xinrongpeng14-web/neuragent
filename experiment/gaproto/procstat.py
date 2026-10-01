"""CPU time and memory of every process in the container, grouped by component.

The experiment runs inside the database container, so /proc lists exactly the
processes of the system under test: the database, the NQO service and the
experiment program itself. Each process is put into one group:

  nqo_server      worker and parent processes of the NQO service (run.py)
  pg_nqo_client   database backends serving the NQO service's own connections
                  (EXPLAIN calls of the experts); every client backend that is
                  not one of the experiment's named connections
  pg_job          backends of the JOB clients            (application_name ga_job)
  pg_ycsb         backend of the YCSB connection         (application_name ga_ycsb)
  pg_admin        control connection of the experiment   (application_name ga_admin)
  pg_background   postmaster, checkpointer, writers, autovacuum and the like
  harness         the experiment program's main process: the agent's inference,
                  metric collection, training updates
  drivers         the workload generators (YCSB and JOB driver processes, started
                  with the multiprocessing "spawn" method); they stand for the
                  clients and are not an overhead of the Global Agent
  other           everything else (shells, docker's init, ...)

CPU time comes from /proc/<pid>/stat, memory from /proc/<pid>/statm (RSS) and
/proc/<pid>/smaps_rollup (PSS, which splits pages shared between the forked NQO
workers or between backends and shared_buffers instead of counting them once
per process).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

GROUPS = ("nqo_server", "pg_nqo_client", "pg_job", "pg_ycsb", "pg_admin", "pg_background",
          "harness", "drivers", "other")

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# pid -> (backend_type, application_name), from pg_stat_activity
PgActivity = Dict[int, Tuple[str, str]]


def classify(comm: str, cmdline: str, pid: int, activity: PgActivity) -> str:
    if comm == "postgres" or cmdline.startswith("postgres"):
        if pid in activity:
            backend_type, app = activity[pid]
            if backend_type == "client backend":
                if app.startswith("ga_job"):
                    return "pg_job"
                if app == "ga_ycsb":
                    return "pg_ycsb"
                if app == "ga_admin":
                    return "pg_admin"
                return "pg_nqo_client"
        return "pg_background"
    if "python" in comm or "python" in cmdline.split(" ", 1)[0]:
        if "run.py" in cmdline:
            return "nqo_server"
        if "multiprocessing" in cmdline:
            return "drivers"
        if "gaproto" in cmdline or "check_env" in cmdline or "select_job_queries" in cmdline:
            return "harness"
    return "other"


def parse_stat(text: str) -> Tuple[str, float]:
    """(comm, cumulative CPU seconds) from the content of /proc/<pid>/stat."""
    left = text.index("(")
    right = text.rindex(")")
    comm = text[left + 1:right]
    rest = text[right + 2:].split()
    # rest[0] is the state (field 3); utime and stime are fields 14 and 15
    return comm, (int(rest[11]) + int(rest[12])) / _CLK_TCK


def parse_pss(text: str) -> Optional[int]:
    for line in text.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    return None


@dataclass
class ProcSample:
    cpu_s: Dict[int, float] = field(default_factory=dict)
    group: Dict[int, str] = field(default_factory=dict)
    rss: Dict[int, int] = field(default_factory=dict)
    pss: Dict[int, Optional[int]] = field(default_factory=dict)

    def by_group(self, values: Dict[int, Optional[int]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for pid, v in values.items():
            if v is None:
                continue
            g = self.group[pid]
            out[g] = out.get(g, 0) + int(v)
        return out

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for g in self.group.values():
            out[g] = out.get(g, 0) + 1
        return out


class ProcSampler:
    def __init__(self, pg_activity: Optional[Callable[[], Iterable[Tuple[int, str, str]]]] = None,
                 proc_root: str = "/proc", with_pss: bool = True):
        """pg_activity returns rows (pid, backend_type, application_name) of pg_stat_activity."""
        self.pg_activity = pg_activity
        self.proc_root = proc_root
        self.with_pss = with_pss

    def _activity(self) -> PgActivity:
        if self.pg_activity is None:
            return {}
        try:
            return {int(pid): (bt or "", app or "") for pid, bt, app in self.pg_activity()}
        except Exception:
            return {}

    def sample(self) -> ProcSample:
        activity = self._activity()
        s = ProcSample()
        for name in os.listdir(self.proc_root):
            if not name.isdigit():
                continue
            pid = int(name)
            base = os.path.join(self.proc_root, name)
            try:
                with open(os.path.join(base, "stat"), encoding="ascii", errors="replace") as f:
                    comm, cpu = parse_stat(f.read())
                with open(os.path.join(base, "cmdline"), "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                with open(os.path.join(base, "statm"), encoding="ascii") as f:
                    rss = int(f.read().split()[1]) * _PAGE
            except (OSError, ValueError, IndexError):
                continue        # the process ended while being read
            pss = None
            if self.with_pss:
                try:
                    with open(os.path.join(base, "smaps_rollup"), encoding="ascii") as f:
                        pss = parse_pss(f.read())
                except OSError:
                    pss = None
            s.cpu_s[pid] = cpu
            s.group[pid] = classify(comm, cmdline, pid, activity)
            s.rss[pid] = rss
            s.pss[pid] = pss
        return s


def cpu_delta(before: ProcSample, after: ProcSample) -> Dict[str, float]:
    """CPU seconds per group between two samples.

    A process that appeared between the samples contributes its whole CPU time;
    one that ended between them is lost. Both are small for the processes that
    matter here, which live for the whole step.
    """
    out: Dict[str, float] = {}
    for pid, cpu in after.cpu_s.items():
        g = after.group[pid]
        out[g] = out.get(g, 0.0) + max(0.0, cpu - before.cpu_s.get(pid, 0.0))
    return out


def summarize(before: ProcSample, after: ProcSample) -> Dict[str, Dict[str, float]]:
    """cpu_s, rss_bytes, pss_bytes and count per group, for StepMetrics."""
    return {
        "cpu_s": cpu_delta(before, after),
        "rss_bytes": after.by_group(after.rss),
        "pss_bytes": after.by_group(after.pss),
        "count": after.counts(),
    }
