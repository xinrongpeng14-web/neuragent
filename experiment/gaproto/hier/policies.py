"""Arms of plan section 7 as policies over the nine commands.

  original              NQO auto, index by cost: arm O (NeurDB with SELIX beside btree)
  fixed:<nqo>/<scheme>  the same command at every step, e.g. fixed:off/btree (arm PG),
                        fixed:off/cost (arm none)
  static-best           the best fixed command of the sweep (static_best_path of the config)
  ppo:<model.zip>       trained GA, most probable command (arm G)
  ppo-sample:<model.zip>  trained GA, sampled command
  random                uniform random command (diagnostics)
"""
from __future__ import annotations

import json
from typing import Optional

import numpy as np

from . import commands as C


class Policy:
    name = "policy"

    def act(self, obs: np.ndarray) -> int:
        raise NotImplementedError


class FixedPolicy(Policy):
    def __init__(self, command: int, label: str = ""):
        self.command = int(command)
        self.name = label or f"fixed:{C.name(self.command)}"

    def act(self, obs):
        return self.command


class RandomPolicy(Policy):
    name = "random"

    def __init__(self, seed: Optional[int] = None):
        self.rng = np.random.default_rng(seed)

    def act(self, obs):
        return int(self.rng.integers(0, C.NUM_COMMANDS))


class PpoPolicy(Policy):
    def __init__(self, path: str, deterministic: bool = True):
        from stable_baselines3 import PPO

        self.model = PPO.load(path, device="cpu")
        self.deterministic = deterministic
        self.name = f"{'ppo' if deterministic else 'ppo-sample'}:{path}"

    def act(self, obs):
        a, _ = self.model.predict(np.asarray(obs, dtype=np.float32), deterministic=self.deterministic)
        return int(a)


def make_policy(spec: str, static_best_path: str = "", seed: Optional[int] = None) -> Policy:
    spec = spec.strip()
    if spec == "original":
        return FixedPolicy(C.parse(C.ORIGINAL), f"original ({C.ORIGINAL})")
    if spec == "random":
        return RandomPolicy(seed)
    if spec == "static-best":
        try:
            with open(static_best_path, encoding="utf-8") as f:
                best = json.load(f)["command"]
        except (OSError, KeyError, ValueError) as e:
            raise SystemExit(f"static-best: cannot read {static_best_path} ({e}); run the sweep first")
        return FixedPolicy(C.parse(best), f"static-best ({best})")
    kind, _, arg = spec.partition(":")
    if kind == "fixed" and arg:
        return FixedPolicy(C.parse(arg))
    if kind in ("ppo", "ppo-sample") and arg:
        return PpoPolicy(arg, deterministic=(kind == "ppo"))
    raise ValueError(f"unknown policy {spec!r}; see gaproto/hier/policies.py")
