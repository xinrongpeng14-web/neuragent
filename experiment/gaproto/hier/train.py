"""Train the hierarchical GA with PPO (stable-baselines3) on the live environment (plan v0.6, 5.1).

One PPO update every `--rollout-episodes` episodes. The model and progress.json
are written after every episode, so an interrupted run continues with --resume.

Usage (inside the container, in experiment/):
  python -m gaproto.hier.train --config config/hier_long.json --total-steps 600
  python -m gaproto.hier.train --config config/hier_long.json --total-steps 600 --resume runs/hier_long/train/model.zip
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import time
from typing import Any, Dict, List

import numpy as np

from . import commands as C
from .config import load_hier_config
from .env import make_env


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Train the hierarchical GA with PPO")
    ap.add_argument("--config", required=True)
    ap.add_argument("--clients", type=int, default=0, help="override the config's clients (keep it equal in every stage)")
    ap.add_argument("--run-name", default="train")
    ap.add_argument("--total-steps", type=int, default=600)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resume", default="")
    ap.add_argument("--rollout-episodes", type=int, default=4, help="episodes per PPO update")
    # hyperparameters of the prototype (GlobalAgent_prototype.md 5.5), n_steps aligned to episodes
    ap.add_argument("--n-epochs", type=int, default=10)
    ap.add_argument("--gamma", type=float, default=0.9)
    ap.add_argument("--gae-lambda", type=float, default=0.95)
    ap.add_argument("--ent-coef", type=float, default=0.05)
    ap.add_argument("--learning-rate", type=float, default=3e-4)
    ap.add_argument("--net", default="64,64")
    args = ap.parse_args(argv)

    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback

    cfg = load_hier_config(args.config, {"clients": args.clients} if args.clients else None)
    if not os.path.isfile(cfg.refs_path):
        raise SystemExit(f"references {cfg.refs_path} not found: run the baseline first")
    run_dir = os.path.join(cfg.log_dir, args.run_name)
    os.makedirs(run_dir, exist_ok=True)
    model_path = os.path.join(run_dir, "model.zip")
    progress_path = os.path.join(run_dir, "progress.json")

    env = make_env(cfg, cfg.refs_path, args.run_name)
    env.set_tag("train")
    n_steps = max(2, args.rollout_episodes * env.episode_steps)
    batch = max(2, n_steps // 2)
    net = [int(x) for x in args.net.split(",") if x]
    progress: Dict[str, Any] = {"config": args.config, "episode_steps": env.episode_steps,
                                "hyperparameters": {"n_steps": n_steps, "batch_size": batch,
                                                    "n_epochs": args.n_epochs, "gamma": args.gamma,
                                                    "gae_lambda": args.gae_lambda, "ent_coef": args.ent_coef,
                                                    "learning_rate": args.learning_rate, "net": net},
                                "episodes": []}
    if args.resume and os.path.isfile(progress_path):
        with open(progress_path, encoding="utf-8") as f:
            progress["episodes"] = json.load(f).get("episodes", [])

    class Logger(BaseCallback):
        def __init__(self):
            super().__init__()
            self.rewards: List[float] = []
            self.cmds: List[str] = []
            self.lat = 0.0
            self.t0 = time.time()

        def _on_step(self) -> bool:
            info = self.locals["infos"][0]
            r = float(self.locals["rewards"][0])
            self.rewards.append(r)
            self.cmds.append(info["command"])
            self.lat += sum(x["net_s"] for x in info["results"])
            print(f"  step {self.num_timesteps:5d} ep {info['episode_index']} {info['command']:<12s} "
                  f"{' '.join('%s:%.2fs' % (x['query'], x['net_s']) for x in info['results'])}  r {r:+.3f}",
                  flush=True)
            if bool(self.locals["dones"][0]):
                ep = {"episode": len(progress["episodes"]), "end_step": int(self.num_timesteps),
                      "return": float(np.sum(self.rewards)), "mean_reward": float(np.mean(self.rewards)),
                      "total_net_s": round(self.lat, 3), "wall_s": round(time.time() - self.t0, 1),
                      "commands": dict(collections.Counter(self.cmds))}
                progress["episodes"].append(ep)
                _write_json(progress_path, progress)
                self.model.save(model_path)
                recent = [e["mean_reward"] for e in progress["episodes"][-5:]]
                print(f"[episode {ep['episode']}] mean reward {ep['mean_reward']:+.3f} "
                      f"(last {len(recent)}: {np.mean(recent):+.3f}), queries {ep['total_net_s']:.1f} s, "
                      f"commands {ep['commands']}", flush=True)
                self.rewards, self.cmds, self.lat, self.t0 = [], [], 0.0, time.time()
            return True

    kwargs = dict(n_steps=n_steps, batch_size=batch, n_epochs=args.n_epochs, gamma=args.gamma,
                  gae_lambda=args.gae_lambda, ent_coef=args.ent_coef, learning_rate=args.learning_rate,
                  verbose=0, device="cpu", seed=args.seed)
    if args.resume:
        model = PPO.load(args.resume, env=env, device="cpu")
        print(f"resumed from {args.resume} at {model.num_timesteps} steps")
    else:
        model = PPO("MlpPolicy", env, policy_kwargs={"net_arch": net}, **kwargs)
    print(f"PPO: {C.NUM_COMMANDS} commands, n_steps {n_steps} ({args.rollout_episodes} episodes of "
          f"{env.episode_steps} steps), batch {batch}, epochs {args.n_epochs}, gamma {args.gamma}, "
          f"ent_coef {args.ent_coef}, lr {args.learning_rate}, net {net}; {args.total_steps} steps", flush=True)
    t0 = time.time()
    try:
        model.learn(total_timesteps=args.total_steps, callback=Logger(), reset_num_timesteps=not args.resume)
    finally:
        model.save(model_path)
        progress["total_steps"] = int(model.num_timesteps)
        progress["wall_s"] = round(time.time() - t0, 1)
        _write_json(progress_path, progress)
        env.close()
    print(f"model saved to {model_path}; {len(progress['episodes'])} episodes, {progress['wall_s'] / 3600:.2f} h")


if __name__ == "__main__":
    main()
