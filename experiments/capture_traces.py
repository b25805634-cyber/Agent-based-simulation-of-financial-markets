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
import sys

from nmsim.config import Config
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.run_context import ManagedRunContext
from nmsim.sim import run_sim
from experiments.run_seed import META, PLACEBO_NEWS


def build_argparser():
    p = RaisingArgumentParser(allow_abbrev=False)
    p.add_argument("--version", action="version", version="capture_traces phase-1.1b")
    p.add_argument("--news", choices=["real", "placebo"], default="real")
    p.add_argument("--social", choices=["on", "off"], default="on")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--temp", type=float, default=0.3)
    p.add_argument("--provider", default="openai")
    p.add_argument("--out", default="traces")
    p.add_argument("--replay-from", default=None)
    p.add_argument("--run-id", default=None)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="traces",
            command_identity="python -m experiments.capture_traces",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        args = build_argparser().parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    cfg = Config(provider=args.provider, seed=args.seed, reference_path=META)
    cfg.out_dir = args.out
    cfg.social_enabled = (args.social == "on")
    cfg.social_weight = 1.0
    if args.news == "placebo":
        cfg.news_text = PLACEBO_NEWS
    cfg.temperature = args.temp
    if args.temp > 0:
        cfg.cache_enabled = False

    label = f"{args.news}_{args.social}"
    filename = f"{label}_s{args.seed}_traces.json"
    inputs = {"llm_replay_records": args.replay_from} if args.replay_from else None
    managed = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id=label,
        run_id=args.run_id,
        input_paths=inputs,
        command_identity="python -m experiments.capture_traces",
    )
    with managed:
        llm, tracker = managed.prepare_llm(args.replay_from)
        res = managed.execute_simulation(
            run_sim,
            cfg,
            llm,
            tracker,
            event_logger=managed.observer,
            run_id=managed.run_id,
        )
        managed.assert_replay_exhausted()
        managed.set_population(res.agents)
        managed.sync_llm_accounting(llm, tracker)

        # The detailed rationale remains a private 0600 artifact.  Public events
        # continue to contain only public_take and explicitly public sentiment.
        dump = {
            "label": label,
            "seed": args.seed,
            "news_round": cfg.news_round,
            "social_enabled": cfg.social_enabled,
            "seed_agents": sorted(res.seed_agents),
            "history": [round(price, 3) for price in res.history],
            "traces": {
                str(round_i): [
                    {
                        "agent": agent,
                        "side": side,
                        "qty": quantity,
                        "limit": limit_price,
                        "sentiment": sentiment,
                        "public_take": public_take,
                        "reasoning": reasoning,
                    }
                    for (
                        agent,
                        side,
                        quantity,
                        limit_price,
                        sentiment,
                        public_take,
                        reasoning,
                    ) in items
                ]
                for round_i, items in res.traces.items()
            },
        }
        managed.set_stage("result_export")
        private_path = managed.run_dir / filename
        with private_path.open("x", encoding="utf-8") as stream:
            json.dump(dump, stream, indent=2, ensure_ascii=False)
        os.chmod(private_path, 0o600)
        managed.finish(legacy_filenames=[filename])
        print(
            f"[{label} s{args.seed}] cost ${tracker.cost_usd:.3f} -> "
            f"{os.path.join(args.out, filename)}"
        )


if __name__ == "__main__":
    main()
