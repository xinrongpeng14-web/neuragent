"""Hierarchical Global Agent of GlobalAgent_hierarchical.md v0.6.

Read-only JOB queries on IMDB; NQO and SELIX act on the same tables. One GA
step covers k queries (long group 1, short group 5); the GA issues one of nine
commands (NQO expert range x index scheme) before the step, and its reward is
the step's latency relative to the original system. Trained with PPO.
"""
