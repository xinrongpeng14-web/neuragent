"""Diagnostic: run a fixed list of actions for one episode, twice, with a fresh
environment each time, and list the sessions on the server in between.

Usage: probe_sessions.py <config> [action,action,...]
Without actions every step uses the original action.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gaproto import actions as A  # noqa: E402
from gaproto.config import load_config  # noqa: E402
from gaproto.db import Admin  # noqa: E402
from gaproto.env import make_env  # noqa: E402


def sessions(cfg, tag):
    a = Admin(cfg.db)
    rows = a.fetchall("SELECT application_name, state, count(*) FROM pg_stat_activity "
                      "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() "
                      "GROUP BY 1, 2 ORDER BY 1, 2")
    print(f"[{tag}] sessions: {rows}", flush=True)
    a.close()


def main():
    cfg = load_config(sys.argv[1])
    actions = [int(a) for a in sys.argv[2].split(",")] if len(sys.argv) > 2 else None

    for rnd in range(2):
        sessions(cfg, f"round {rnd} before")
        env = make_env(cfg, None, run_name=f"probe{rnd}")
        env.reset(seed=50 + rnd)
        for step in range(cfg.episode_steps):
            act = A.ORIGINAL_ACTION if actions is None else actions[step % len(actions)]
            t0 = time.time()
            _, _, _, _, info = env.step(act)
            m = info["metrics"]
            print(f"  round {rnd} step {step} phase {info['phase']} "
                  f"{info['nqo_mode']}/{info['selix_preset']:<7} "
                  f"cpu {m['cpu_util']:.2f} job {len(m['job_completed']):3d} "
                  f"ycsb {m['ycsb_ops_per_s']:6.0f}/s p50 {m['ycsb_p50_s'] * 1e6:6.0f} us "
                  f"p99 {m['ycsb_p99_s'] * 1e6:7.0f} us c_idx {m['c_idx_ns'] or 0:5.0f} ns  "
                  f"wall {time.time() - t0:.1f} s", flush=True)
        sessions(cfg, f"round {rnd} during")
        env.close()
        time.sleep(1)
        sessions(cfg, f"round {rnd} after close")


if __name__ == "__main__":
    main()
