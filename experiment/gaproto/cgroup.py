"""CPU and memory usage of the database container, read from cgroup v2."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional


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


def candidate_dirs(container_id: str) -> List[str]:
    root = "/sys/fs/cgroup"
    return [
        f"{root}/system.slice/docker-{container_id}.scope",  # systemd driver
        f"{root}/docker/{container_id}",                     # cgroupfs driver
    ]


class CgroupReader:
    """Reads the container's cgroup files from the host.

    When the files cannot be read from the host, they are read inside the
    container through `docker exec`, which costs roughly 0.1 s per sample.
    """

    def __init__(self, container: str, ncpus: float, cgroup_dir: str = ""):
        self.container = container
        self.ncpus = float(ncpus)
        self.dir: Optional[str] = cgroup_dir or self._locate()
        if self.dir and not os.access(os.path.join(self.dir, "cpu.stat"), os.R_OK):
            self.dir = None

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

    def sample(self) -> CgroupSample:
        return CgroupSample(cpu_usec=parse_cpu_stat(self._read("cpu.stat")),
                            memory_bytes=int(self._read("memory.current").strip()))

    def cpu_util(self, before: CgroupSample, after: CgroupSample, elapsed_s: float) -> float:
        """Fraction of the container's CPU allowance used between two samples."""
        if elapsed_s <= 0:
            return 0.0
        used_s = (after.cpu_usec - before.cpu_usec) / 1e6
        return max(0.0, used_s / (elapsed_s * self.ncpus))
