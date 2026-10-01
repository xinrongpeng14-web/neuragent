"""Policies evaluated against the environment.

A policy maps (observation, phase name) to one of the 12 joint actions. Besides
the trained PPO agent there are fixed policies, which realise the arms of the
comparison: NQO alone, SELIX alone, both without coordination, neither.

Specification strings, as accepted on the command line:

  original                     NQO auto, SELIX default: NeurDB as shipped
  fixed:<mode>/<preset>        the same action at every step, e.g. fixed:off/dense
  table:A=off/dense,B=off/default
                               one fixed action per phase: a static configuration
                               chosen per workload, without any agent
  ppo:<model.zip>              trained agent, most probable action
  ppo-sample:<model.zip>       trained agent, action sampled from its distribution
  random                       uniform random action (diagnostics only)
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from . import actions as A


class Policy:
    name = "policy"

    def act(self, obs: np.ndarray, phase: str) -> int:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class FixedPolicy(Policy):
    def __init__(self, action: int):
        self.action = int(action)
        mode, preset = A.decode(self.action)
        self.name = f"fixed:{mode}/{preset}"

    def act(self, obs, phase):
        return self.action


class PhaseTablePolicy(Policy):
    def __init__(self, table: Dict[str, int]):
        self.table = {str(k): int(v) for k, v in table.items()}
        parts = ",".join(f"{ph}={'/'.join(A.decode(a))}" for ph, a in self.table.items())
        self.name = f"table:{parts}"

    def act(self, obs, phase):
        if phase not in self.table:
            raise KeyError(f"no action for phase {phase!r} in {self.name}")
        return self.table[phase]


class RandomPolicy(Policy):
    name = "random"

    def __init__(self, seed: Optional[int] = None):
        self.rng = np.random.default_rng(seed)

    def act(self, obs, phase):
        return int(self.rng.integers(0, A.NUM_ACTIONS))


class PpoPolicy(Policy):
    def __init__(self, model_path: str, deterministic: bool = True):
        from stable_baselines3 import PPO   # heavy import, only when needed

        self.model = PPO.load(model_path, device="cpu")
        self.deterministic = deterministic
        self.model_path = model_path
        self.name = f"{'ppo' if deterministic else 'ppo-sample'}:{model_path}"

    def act(self, obs, phase):
        action, _ = self.model.predict(np.asarray(obs, dtype=np.float32), deterministic=self.deterministic)
        return int(action)

    def action_probabilities(self, obs) -> np.ndarray:
        """Probability of every action under the current policy, for the report."""
        import torch

        with torch.no_grad():
            t = torch.as_tensor(np.asarray(obs, dtype=np.float32)).unsqueeze(0)
            dist = self.model.policy.get_distribution(t)
            return dist.distribution.probs.squeeze(0).cpu().numpy()


def parse_action(text: str) -> int:
    """'mode/preset' -> action id."""
    try:
        mode, preset = text.strip().split("/")
        return A.encode(mode.strip(), preset.strip())
    except ValueError as e:
        raise ValueError(f"action {text!r} must be <mode>/<preset> with mode in {A.NQO_MODES} "
                         f"and preset in {A.SELIX_PRESET_NAMES}") from e


def make_policy(spec: str, seed: Optional[int] = None) -> Policy:
    spec = spec.strip()
    if spec == "original":
        return FixedPolicy(A.ORIGINAL_ACTION)
    if spec == "random":
        return RandomPolicy(seed)
    kind, _, arg = spec.partition(":")
    if kind == "fixed" and arg:
        return FixedPolicy(parse_action(arg))
    if kind == "table" and arg:
        table = {}
        for item in arg.split(","):
            phase, _, action = item.partition("=")
            if not action:
                raise ValueError(f"table entry {item!r} must be <phase>=<mode>/<preset>")
            table[phase.strip()] = parse_action(action)
        return PhaseTablePolicy(table)
    if kind in ("ppo", "ppo-sample") and arg:
        return PpoPolicy(arg, deterministic=(kind == "ppo"))
    raise ValueError(f"unknown policy specification {spec!r}; see gaproto/policies.py")
