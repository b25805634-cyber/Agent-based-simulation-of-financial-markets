"""Reproducibility regression test (guards the M1 noise-RNG fix).

Runs the SAME mock-LLM config in two separate subprocesses with DIFFERENT
PYTHONHASHSEED values. After the M1 fix (noise RNG derived from cfg.seed via
integer-only inputs, not Python's salted hash()), the emergent price history
must be byte-identical across processes. Before the fix, the noise agents
diverged per process and this failed.

Usage:  python -m experiments.repro_check
Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations
import os
import sys
import subprocess

# Print the full price history (high precision) for a fixed mock config.
_SNIPPET = (
    "from nmsim.config import Config;"
    "from nmsim.llm import build_llm;"
    "from nmsim.run_context import NullRunContext;"
    "from nmsim.sim import run_sim;"
    "cfg=Config(provider='mock', seed=7, n_rounds=24, news_round=12);"
    "llm,t=build_llm(cfg);"
    "ctx=NullRunContext();res=ctx.execute_simulation(run_sim,cfg,llm,t);"
    "print(';'.join(f'{p:.6f}' for p in res.history))"
)


def _run(hashseed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    p = subprocess.run([sys.executable, "-c", _SNIPPET],
                       capture_output=True, text=True, env=env, cwd=os.getcwd())
    if p.returncode != 0:
        print(p.stderr)
        raise SystemExit(f"subprocess failed (PYTHONHASHSEED={hashseed})")
    return p.stdout.strip()


def main():
    a = _run("1")
    b = _run("2")
    c = _run("99")
    ok = (a == b == c)
    print("PASS — identical price history across 3 processes with different PYTHONHASHSEED"
          if ok else "FAIL — histories differ across processes (noise RNG not deterministic)")
    if not ok:
        print(" proc1:", a[:80])
        print(" proc2:", b[:80])
    print(f" history length: {len(a.split(';'))} points; final price: {a.split(';')[-1]}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
