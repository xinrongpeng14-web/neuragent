"""Train the Global Agent with PPO (stable-baselines3) on the live environment.

Hyperparameters follow GlobalAgent_prototype.md section 5.5. One PPO update is
made per episode (n_steps = steps per episode). Every environment step is
written to <log_dir>/<run_name>/steps.jsonl by the environment; this script
adds progress.json (return per episode) and saves the model after every
episode, so that an interrupted run can be resumed with --resume.

Usage:
  python -m gaproto.train --config config/imdb.json --total-steps 1200
  python -m gaproto.train --config config/imdb.json --total-steps 1200 --resume runs/imdb/train/model.zip
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

import numpy as np

from .config import load_config
from .env import make_env


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Train the Global Agent with PPO")
    ap.add_argument("--config", required=True)
    ap.add_argument("--refs", default="", help="reference values (default: refs_path of the config)")
    ap.add_argument("--run-name", default="train")
    ap.add_argument("--total-steps", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resume", default="", help="model.zip of an earlier run to continue from")
    ap.add_argument("--check-at", type=int, default=600,
                    help="step at which the mid-way check is printed (0 disables)")
    # PPO hyperparameters, defaults from the prototype document
    ap.add_argument("--n-steps", type=int, default=0, help="rollout length (default: one episode)")
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--n-epochs", type=int, default=10)
    ap.add_argument("--gamma", type=float, default=0.9)
    ap.add_argument("--gae-lambda", type=float, default=0.95)
    ap.add_argument("--ent-coef", type=float, default=0.05)
    ap.add_argument("--learning-rate", type=float, default=3e-4)
    ap.add_argument("--net", default="64,64", help="hidden layers of the policy and value networks")
    args = ap.parse_args(argv)

    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback

    cfg = load_config(args.config)
    refs_path = args.refs or cfg.refs_path
    if not os.path.isfile(refs_path):
        raise SystemExit(f"references {refs_path} not found; run gaproto.baseline first")
    run_dir = os.path.join(cfg.log_dir, args.run_name)
    os.makedirs(run_dir, exist_ok=True)
    model_path = os.path.join(run_dir, "model.zip")
    progress_path = os.path.join(run_dir, "progress.json")

    n_steps = args.n_steps or cfg.episode_steps
    batch_size = min(args.batch_size, n_steps)
    net = [int(x) for x in args.net.split(",") if x]

    env = make_env(cfg, refs_path=refs_path, run_name=args.run_name)
    env.set_tag("train")

    progress: Dict[str, Any] = {"episodes": [], "config": args.config, "refs": refs_path,
                                "hyperparameters": {
                                    "n_steps": n_steps, "batch_size": batch_size,
                                    "n_epochs": args.n_epochs, "gamma": args.gamma,
                                    "gae_lambda": args.gae_lambda, "ent_coef": args.ent_coef,
                                    "learning_rate": args.learning_rate, "net": net}}
    if args.resume and os.path.isfile(progress_path):
        with open(progress_path, encoding="utf-8") as f:
            progress["episodes"] = json.load(f).get("episodes", [])

    class EpisodeLogger(BaseCallback):
        def __init__(self):
            super().__init__()
            self.rewards: List[float] = []
            self.actions: List[int] = []
            self.t_episode = time.time()
            self.checked = False

        def _on_step(self) -> bool:
            self.rewards.append(float(self.locals["rewards"][0]))
            self.actions.append(int(self.locals["actions"][0]))
            info = self.locals["infos"][0]
            print(f"  step {self.num_timesteps:5d}  ep {info.get('episode_index', '?')} "
                  f"phase {info.get('phase', '?')}  action {info.get('nqo_mode', '?')}/"
                  f"{info.get('selix_preset', '?')}  reward {self.rewards[-1]:+.3f}", flush=True)
            if bool(self.locals["dones"][0]):
                ep = {"episode": len(progress["episodes"]), "end_step": self.num_timesteps,
                      "return": float(np.sum(self.rewards)), "mean_reward": float(np.mean(self.rewards)),
                      "steps": len(self.rewards), "wall_s": round(time.time() - self.t_episode, 1),
                      "action_counts": {str(a): int(n) for a, n in
                                        zip(*np.unique(self.actions, return_counts=True))}}
                progress["episodes"].append(ep)
                _write_json(progress_path, progress)
                self.model.save(model_path)
                recent = [e["mean_reward"] for e in progress["episodes"][-5:]]
                print(f"[episode {ep['episode']}] return {ep['return']:+.2f}  mean reward "
                      f"{ep['mean_reward']:+.3f}  (last {len(recent)} episodes: {np.mean(recent):+.3f})  "
                      f"{ep['wall_s']:.0f} s", flush=True)
                self.rewards, self.actions = [], []
                self.t_episode = time.time()
            if args.check_at and not self.checked and self.num_timesteps >= args.check_at:
                self.checked = True
                recent = [e["mean_reward"] for e in progress["episodes"][-5:]]
                if recent:
                    verdict = ("above the original system" if np.mean(recent) > 0 else
                               "still below the original system: see section 7.4 of the prototype "
                               "document before continuing")
                    print(f"[mid-way check at step {self.num_timesteps}] mean reward of the last "
                          f"{len(recent)} episodes: {np.mean(recent):+.3f}  -> {verdict}", flush=True)
            return True

    kwargs = dict(n_steps=n_steps, batch_size=batch_size, n_epochs=args.n_epochs,
                  gamma=args.gamma, gae_lambda=args.gae_lambda, ent_coef=args.ent_coef,
                  learning_rate=args.learning_rate, verbose=0, device="cpu", seed=args.seed)
    if args.resume:
        model = PPO.load(args.resume, env=env, device="cpu")
        print(f"resumed from {args.resume} at {model.num_timesteps} steps")
    else:
        model = PPO("MlpPolicy", env, policy_kwargs={"net_arch": net}, **kwargs)
    print(f"PPO: n_steps {n_steps}, batch {batch_size}, epochs {args.n_epochs}, gamma {args.gamma}, "
          f"ent_coef {args.ent_coef}, lr {args.learning_rate}, net {net}; "
          f"{args.total_steps} steps of {cfg.step_s:.0f} s", flush=True)

    t0 = time.time()
    try:
        model.learn(total_timesteps=args.total_steps, callback=EpisodeLogger(),
                    reset_num_timesteps=not args.resume)
    finally:
        model.save(model_path)
        progress["total_steps"] = int(model.num_timesteps)
        progress["wall_s"] = round(time.time() - t0, 1)
        _write_json(progress_path, progress)
        env.close()
    print(f"\nmodel saved to {model_path}; {len(progress['episodes'])} episodes, "
          f"{progress['wall_s'] / 3600:.2f} h")


if __name__ == "__main__":
    main()
