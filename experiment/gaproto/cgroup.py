"""CPU and memory usage of the database container, read from cgroup v2.

Two ways of running are supported:

  inside the container  (container name empty, the normal case): the files of
      the container's own cgroup appear at /sys/fs/cgroup thanks to the private
      cgroup namespace docker gives a container on a cgroup v2 host.
  on the host: the container's cgroup directory is located through
      `docker inspect`; as a last resort the files are read with `docker exec`,
      which costs roughly 0.1 s per sample.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

INSIDE_DIR = "/sys/fs/cgroup"


@dataclass
class CgroupSample:
    cpu_usec: int      # cumulative CPU time of the container
    memory_bytes: int  # current memory charged to the container, page cache included


def parse_cpu_stat(text: str) -> int:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            return int(parts[1])
    raise ValueError("cpu.stat has no usage_usec line")


def parse_cpu_max(text: str) -> Optional[float]:
    """CPUs allowed by cpu.max ("quota period" or "max period"); None when unlimited."""
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    return int(parts[0]) / int(parts[1])


def parse_cpuset(text: str) -> int:
    """Number of CPUs in a cpuset list such as "0-3,8"."""
    n = 0
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            n += int(b) - int(a) + 1
        else:
            n += 1
    return n


def candidate_dirs(container_id: str) -> List[str]:
    root = "/sys/fs/cgroup"
    return [
        f"{root}/system.slice/docker-{container_id}.scope",  # systemd driver
        f"{root}/docker/{container_id}",                     # cgroupfs driver
    ]


class CgroupReader:
    def __init__(self, container: str = "", ncpus: float = 0.0, cgroup_dir: str = ""):
        """
        container: docker container name when running on the host; empty inside.
        ncpus: denominator of cpu_util; 0 derives it from cpu.max or the cpuset.
        """
        self.container = container
        if cgroup_dir:
            self.dir: Optional[str] = cgroup_dir
        elif not container:
            self.dir = INSIDE_DIR
        else:
            self.dir = self._locate()
        if self.dir and not os.access(os.path.join(self.dir, "cpu.stat"), os.R_OK):
            if not container:
                raise RuntimeError(f"{self.dir}/cpu.stat is not readable; the container must run "
                                   "on a cgroup v2 host")
            self.dir = None
        self.ncpus = float(ncpus) if ncpus and ncpus > 0 else self.detect_ncpus()

    def _locate(self) -> Optional[str]:
        try:
            cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", self.container],
                                 capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        except Exception:
            return None
        for d in candidate_dirs(cid):
            if os.path.isfile(os.path.join(d, "cpu.stat")):
                return d
        return None

    @property
    def source(self) -> str:
        return self.dir if self.dir else f"docker exec {self.container}"

    def _read(self, name: str) -> str:
        if self.dir:
            with open(os.path.join(self.dir, name), encoding="ascii") as f:
                return f.read()
        return subprocess.run(["docker", "exec", self.container, "cat", f"/sys/fs/cgroup/{name}"],
                              capture_output=True, text=True, timeout=10, check=True).stdout

    def _read_optional(self, name: str) -> Optional[str]:
        try:
            return self._read(name)
        except Exception:
            return None

    def detect_ncpus(self) -> float:
        text = self._read_optional("cpu.max")
        quota = parse_cpu_max(text) if text else None
        if quota:
            return quota
        text = self._read_optional("cpuset.cpus.effective")
        if text and text.strip():
            return float(parse_cpuset(text))
        return float(os.cpu_count() or 1)

    def limits(self) -> dict:
        mem = self._read_optional("memory.max")
        return {"ncpus": self.ncpus, "cpu_max": (self._read_optional("cpu.max") or "").strip(),
                "memory_max": None if not mem or mem.strip() == "max" else int(mem.strip()),
                "source": self.source}

    def sample(self) -> CgroupSample:
        return CgroupSample(cpu_usec=parse_cpu_stat(self._read("cpu.stat")),
                            memory_bytes=int(self._read("memory.current").strip()))

    def cpu_util(self, before: CgroupSample, after: CgroupSample, elapsed_s: float) -> float:
        """Fraction of the container's CPU allowance used between two samples."""
        if elapsed_s <= 0:
            return 0.0
        used_s = (after.cpu_usec - before.cpu_usec) / 1e6
        return max(0.0, used_s / (elapsed_s * self.ncpus))
