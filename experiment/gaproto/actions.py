"""The 12 joint actions: NQO mode x SELIX density preset."""
from __future__ import annotations

from typing import Dict, Tuple

NQO_MODES: Tuple[str, ...] = ("off", "auto", "hint", "join")

# (init_density, max_density, min_density); node size stays at SELIX's 16MB
SELIX_PRESETS: Dict[str, Tuple[float, float, float]] = {
    "dense": (0.85, 0.95, 0.75),
    "default": (0.70, 0.80, 0.60),
    "sparse": (0.50, 0.60, 0.40),
}
SELIX_PRESET_NAMES: Tuple[str, ...] = tuple(SELIX_PRESETS)

NUM_ACTIONS = len(NQO_MODES) * len(SELIX_PRESET_NAMES)


def encode(nqo_mode: str, selix_preset: str) -> int:
    return NQO_MODES.index(nqo_mode) * len(SELIX_PRESET_NAMES) + SELIX_PRESET_NAMES.index(selix_preset)


def decode(action: int) -> Tuple[str, str]:
    action = int(action)
    if not 0 <= action < NUM_ACTIONS:
        raise ValueError(f"action {action} outside [0, {NUM_ACTIONS})")
    mode, preset = divmod(action, len(SELIX_PRESET_NAMES))
    return NQO_MODES[mode], SELIX_PRESET_NAMES[preset]


# What NeurDB does without the Global Agent
ORIGINAL_ACTION = encode("auto", "default")


def nqo_settings(nqo_mode: str) -> Dict[str, str]:
    """Server settings that realise an NQO mode."""
    if nqo_mode == "off":
        return {"enable_molqo": "off", "molqo.expert_filter": "all"}
    filters = {"auto": "all", "hint": "hint", "join": "join"}
    return {"enable_molqo": "on", "molqo.expert_filter": filters[nqo_mode]}
