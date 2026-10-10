"""The nine GA commands of plan v0.6 (section 5.2): NQO expert range x index scheme.

Commands are applied per session with SET, so a command takes effect on the
next query of every worker and never touches the server configuration.
"""
from __future__ import annotations

from typing import List, Tuple

from ..actions import SELIX_PRESETS

NQO_RANGES: Tuple[str, ...] = ("off", "hint", "auto")
SCHEMES: Tuple[str, ...] = ("btree", "cost", "prefer")
NUM_COMMANDS = len(NQO_RANGES) * len(SCHEMES)
# NeurDB as shipped plus SELIX by cost: NQO auto, planner chooses by cost (arm O)
ORIGINAL = "auto/cost"


def encode(nqo: str, scheme: str) -> int:
    return NQO_RANGES.index(nqo) * len(SCHEMES) + SCHEMES.index(scheme)


def decode(command: int) -> Tuple[str, str]:
    command = int(command)
    if not 0 <= command < NUM_COMMANDS:
        raise ValueError(f"command {command} outside 0..{NUM_COMMANDS - 1}")
    return NQO_RANGES[command // len(SCHEMES)], SCHEMES[command % len(SCHEMES)]


def name(command: int) -> str:
    return "/".join(decode(command))


def parse(text: str) -> int:
    """'auto/cost' -> command id."""
    try:
        nqo, scheme = text.strip().split("/")
        return encode(nqo.strip(), scheme.strip())
    except ValueError as e:
        raise ValueError(f"command {text!r} must be <nqo>/<scheme> with nqo in {NQO_RANGES} "
                         f"and scheme in {SCHEMES}") from e


def session_sql(command: int) -> List[str]:
    """SET statements that put one session under the command."""
    nqo, scheme = decode(command)
    stmts = ["SET enable_molqo = off"] if nqo == "off" else [
        "SET enable_molqo = on",
        "SET molqo.expert_filter = '%s'" % ("hint" if nqo == "hint" else "all")]
    if scheme == "btree":
        stmts.append("SET selix.enable_index = off")
    else:
        stmts += ["SET selix.enable_index = on",
                  "SET selix.index_cost_scale = %s" % ("0.999" if scheme == "cost" else "0.01")]
    return stmts


def density_sql(density: str) -> List[str]:
    i, x, n = SELIX_PRESETS[density]
    return [f"SET selix.init_density = {i}", f"SET selix.max_density = {x}", f"SET selix.min_density = {n}"]
