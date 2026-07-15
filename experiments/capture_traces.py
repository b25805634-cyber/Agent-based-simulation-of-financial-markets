"""Run ONE (news, social, seed) config with the real LLM and dump the FULL
per-round reasoning traces to JSON — so we can inspect the MECHANISM (what each
agent does and says round by round), which run_seed does not persist.

Use a matched seed for social on vs off to isolate the social channel.

Usage:
  python -m experiments.capture_traces --news real --social on  --seed 1 --temp 0.3 --out traces
  python -m experiments.capture_traces --news real --social off --seed 1 --temp 0.3 --out traces
"""
from __future__ import annotations
import os
import json
import argparse

from nmsim.config import Config
from nmsim.llm import build_llm
from nmsim.sim import run_sim
from experiments.run_seed import META, PLACEBO_NEWS


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--news", choices=["real", "placebo"], default="real")
    p.add_argument("--social", choices=["on", "off"], default="on")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--temp", type=float, default=0.3)
    p.add_argument("--provider", default="openai")
    p.add_argument("--out", default="traces")
    args = p.parse_args()

    cfg = Config(provider=args.provider, seed=args.seed, reference_path=META)
    cfg.social_enabled = (args.social == "on")
    cfg.social_weight = 1.0
    if args.news == "placebo":
        cfg.news_text = PLACEBO_NEWS
    cfg.temperature = args.temp
    if args.temp > 0:
        cfg.cache_enabled = False

    llm, tracker = build_llm(cfg)
    res = run_sim(cfg, llm, tracker)

    # res.traces: {round: [(agent, side, qty, limit, sentiment, public_take, reasoning), ...]}
    dump = {
        "label": f"{args.news}_{args.social}", "seed": args.seed,
        "news_round": cfg.news_round, "social_enabled": cfg.social_enabled,
        "seed_agents": sorted(res.seed_agents),
        "history": [round(p, 3) for p in res.history],
        "traces": {str(r): [{"agent": a, "side": s, "qty": q, "limit": l,
                             "sentiment": se, "public_take": tk, "reasoning": why}
                            for (a, s, q, l, se, tk, why) in items]
                   for r, items in res.traces.items()},
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.news}_{args.social}_s{args.seed}_traces.json")
    with open(path, "w") as fh:
        json.dump(dump, fh, indent=2, ensure_ascii=False)
    print(f"[{args.news}_{args.social} s{args.seed}] cost ${tracker.cost_usd:.3f} -> {path}")


if __name__ == "__main__":
    main()
