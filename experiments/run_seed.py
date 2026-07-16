"""Run ONE (gain, seed) config for 24 rounds vs the Meta reference, and write a
result JSON with the full post-shock path + trajectory/reaction metrics.

This is the unit of the multi-seed distribution study. It does NOT change the
market; it only measures. Two modes:

  live   : actually run the sim (real LLM via provider) -- the expensive path.
  reuse  : --price-csv / --traces-csv / --propagation-csv recompute metrics from
           an already-saved run (free; used to fold in the seed=7 runs).

Usage:
  python -m experiments.run_seed --gain 1.5 --seed 17 --provider openai --out results
  python -m experiments.run_seed --gain 1.0 --seed 7 --out results \
      --price-csv outputs_meta/price_path.csv \
      --traces-csv outputs_meta/reasoning_traces.csv \
      --propagation-csv outputs_meta/propagation.csv
"""
from __future__ import annotations
import os
import csv
import json
import math
import argparse
import sys
from pathlib import Path

from nmsim.config import Config
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.run_context import ManagedRunContext
from nmsim.result_reuse import inspect_legacy_analysis_inputs
from nmsim import validation as V

META = "nmsim/meta_feb2022_reference.csv"
_BAD_MARKERS = ("parse-failed", "api-error", "parse-retries-exhausted")

# Placebo headline: same "BREAKING:" salience/structure as the real Meta news, but
# directionally NEUTRAL — controls for the act of news arriving so the real-vs-placebo
# contrast isolates the news CONTENT, not the news event.
PLACEBO_NEWS = ("BREAKING: the company reported Q2 results in line with expectations "
                "and reaffirmed its full-year guidance.")

FUEL = ["retail_crowd", "fomo_momentum"]
DAMPENERS = ["value_institution", "contrarian_fund", "quant_arb"]


def build_population(m: float, total: int) -> dict:
    """Population for the composition sweep: 1 influencer (hub+seed, unique) +
    rest split fuel-vs-dampener by ratio m. Copies of a persona share its prompt."""
    rest = total - 1
    n_fuel = round(m * rest)
    n_damp = rest - n_fuel
    pop = {"influencer_amplifier": 1,
           "retail_crowd": n_fuel - n_fuel // 2,
           "fomo_momentum": n_fuel // 2}
    base, extra = divmod(n_damp, len(DAMPENERS))
    for i, pid in enumerate(DAMPENERS):
        pop[pid] = base + (1 if i < extra else 0)
    return pop


def _norm_price_path(history, shock):
    base = history[shock] if history[shock] else 1.0
    return [round(history[i] / base, 6) for i in range(shock, len(history))]


def _assemble(label, seed, cfg, history, peak_cascade, bad, total):
    # M2: t=0 is the LAST PRE-news price (history[news_round-1]), so the sim's first
    # reaction step is INCLUDED in the compared path and aligns with the reference,
    # whose t=0 is the pre-crash close (Meta 323) and t0->t1 IS the crash.
    shock = max(0, cfg.news_round - 1)
    cmp = V.compare_to_reference(history, shock, META)
    tj = cmp["trajectory"]
    sr = cmp["reaction_supplementary"]["sim_reaction"]
    return {
        "label": label, "seed": seed,
        "condition": {"social_enabled": cfg.social_enabled,
                      "social_weight": cfg.social_weight,
                      "temperature": cfg.temperature,
                      "cache_enabled": cfg.cache_enabled,
                      "news": "placebo" if cfg.news_text == PLACEBO_NEWS else "real"},
        "n_rounds": cfg.n_rounds, "news_round": cfg.news_round, "shock_idx": shock,
        "metrics": {
            "rmse_logprice": tj["rmse_logprice"],
            "dtw": tj["dtw"], "dtw_norm": tj["dtw_norm"],
            "drop_depth": sr["drop_depth"], "speed": sr["speed"],
            "recovery": sr["recovery"], "peak_cascade": round(peak_cascade, 4),
        },
        "norm_price_path": _norm_price_path(history, shock),
        "norm_log_path": [round(x, 6) for x in V.norm_log_path(history, shock)],
        "health": {"bad_orders": bad, "total_llm_orders": total,
                   "bad_frac": round(bad / total, 4) if total else 0.0},
    }


def _from_csv(args, cfg):
    history = [cfg.initial_price]
    with open(args.price_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            history.append(float(row["price"]))
    peak = 0.0
    if args.propagation_csv:
        with open(args.propagation_csv, newline="") as fh:
            peak = max((float(r["cascade_size"]) for r in csv.DictReader(fh)),
                       default=0.0)
    bad = total = 0
    if args.traces_csv:
        with open(args.traces_csv, newline="") as fh:
            for r in csv.DictReader(fh):
                total += 1
                if any(k in r.get("reasoning", "") for k in _BAD_MARKERS):
                    bad += 1
    return _assemble(args.label, args.seed, cfg, history, peak, bad, total)


def _replay_records_path(source):
    path = Path(source)
    return str(path / "llm_records.jsonl" if path.is_dir() else path)


def _live(args, cfg, manager):
    from nmsim.sim import run_sim
    llm, tracker = manager.prepare_llm(args.replay_from)
    res = manager.execute_simulation(
        run_sim,
        cfg,
        llm,
        tracker,
        event_logger=manager.observer,
        run_id=manager.run_id,
    )
    manager.assert_replay_exhausted()
    res.run_dir = str(manager.run_dir)
    manager.set_population(res.agents)
    manager.sync_llm_accounting(llm, tracker)
    bad = total = 0
    for items in res.traces.values():
        for (_a, _s, _q, _l, _se, _tk, why) in items:
            total += 1
            if any(k in why for k in _BAD_MARKERS):
                bad += 1
    out = _assemble(args.label, args.seed, cfg, res.history,
                    res.metrics.peak_cascade(), bad, total)
    # Provenance: record the SERVED model id so same-seed pairs can be checked to
    # come from the same model (the endpoint can silently swap MiniMax/HiggsAI).
    out["model"] = getattr(llm, "model", None) or cfg.provider
    out["cost"] = {"calls": tracker.calls, "in": tracker.input_tokens,
                   "out": tracker.output_tokens, "usd": round(tracker.cost_usd, 4)}
    # Mediator instrumentation: mean post-news sentiment of the fuel vs dampener
    # personas (copies like "retail_crowd#2" roll up to their base persona).
    def _grp_mean(prefixes):
        vals = []
        for r_i, snap in zip(res.metrics.rounds, res.metrics.per_agent_sentiment):
            if r_i >= cfg.news_round:
                vals += [v for k, v in snap.items() if k.split("#")[0] in prefixes]
        return round(sum(vals) / len(vals), 4) if vals else None
    out["metrics"]["fuel_sent_postnews"] = _grp_mean(set(FUEL))
    out["metrics"]["damp_sent_postnews"] = _grp_mean(set(DAMPENERS))
    # Compact order log [round, agent, side, qty] (LLM agents only, ~25KB) so
    # flow-decomposition analyses don't need a separate trace-capture run.
    out["orders"] = [[r, a, s, q]
                     for r, items in res.traces.items()
                     for (a, s, q, *_rest) in items]
    # Forced-liquidation event log [round, agent, shares, price, threshold, pnl] —
    # the leverage-layer mechanism evidence (empty when leverage is disabled).
    out["liquidations"] = [[e.round, e.agent, e.shares, e.price, e.threshold, e.pnl]
                           for e in res.liquidations]
    if cfg.leverage_enabled:
        out["leverage"] = {"ratio": cfg.leverage_ratio, "spread": cfg.leverage_spread,
                           "maint": cfg.maintenance_margin, "fraction": cfg.leverage_fraction}
    return out, res, llm


def build_argparser():
    p = RaisingArgumentParser(allow_abbrev=False)
    p.add_argument("--version", action="version", version="experiments.run_seed phase-1.1b")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--provider", default="openai")
    p.add_argument("--model", default=None, help="override served model id (e.g. HiggsAI)")
    p.add_argument(
        "--reasoning-effort",
        default=None,
        help="explicit CodexExec reasoning effort: minimal|low|medium|high|xhigh",
    )
    p.add_argument("--out", default="results")
    # ---- 2x2 condition controls (the social channel is toggled cleanly; price
    #      visibility is always on, so only the social channel differs) ----
    p.add_argument("--news", choices=["real", "placebo"], default="real")
    p.add_argument("--social", choices=["on", "off"], default="on")
    p.add_argument("--temp", type=float, default=None,
                   help="LLM temperature; >0 auto-disables cache for genuine sampling")
    p.add_argument("--label", default=None, help="cell label (filename prefix)")
    p.add_argument("--gain", type=float, default=1.0,
                   help="social_weight when --social on (kept for back-compat)")
    # ---- population-composition sweep ----
    p.add_argument("--arm", choices=["muted", "solo", "demoted"], default=None,
                   help="influencer-megaphone ablation arm (real news, social on):\n"
                        "  muted   = influencer take reaches no one (+told it's unheard)\n"
                        "  solo    = ONLY influencer take circulates (+told it has the mic)\n"
                        "  demoted = influencer is an ordinary node (no hub, no auto-seed)")
    p.add_argument("--m", type=float, default=None,
                   help="fuel ratio (retail+fomo)/rest; builds a ~--total population")
    p.add_argument("--total", type=int, default=30, help="total LLM agents when --m given")
    # ---- leverage + forced-liquidation layer (the amplifier) ----
    p.add_argument("--leverage", action="store_true",
                   help="enable the leverage/forced-liquidation layer (label gets _lev)")
    p.add_argument("--leverage-ratio", type=float, default=None, help="L center (default cfg 2.5)")
    p.add_argument("--leverage-spread", type=float, default=None,
                   help="stagger L in [center-spread, center+spread] (default 0.5)")
    p.add_argument("--maint", type=float, default=None, help="maintenance margin (default 0.25)")
    p.add_argument("--lev-fraction", type=float, default=None,
                   help="fraction of LLM agents leveraged, fuel-first (default 0.5)")
    p.add_argument("--rep", type=int, default=None,
                   help="repeat index: same seed re-sampled (temp>0); suffixes filename")
    p.add_argument("--rounds", type=int, default=None)
    p.add_argument("--news-round", type=int, default=None)
    # ---- reuse-from-csv mode ----
    p.add_argument("--price-csv", default=None)
    p.add_argument("--traces-csv", default=None)
    p.add_argument("--propagation-csv", default=None)
    p.add_argument("--replay-from", default=None,
                   help="run directory or llm_records.jsonl; never calls a provider")
    p.add_argument("--scenario-id", default=None)
    p.add_argument("--run-id", default=None,
                   help="optional explicit unique id; existing ids are refused")
    return p


def config_from_args(args):
    """Build the same effective Config as the historical CLI."""

    if args.label is None:
        lev_sfx = "_lev" if args.leverage else ""
        if args.arm is not None and args.m is not None:
            args.label = f"m{args.m:g}_real_{args.arm}{lev_sfx}"   # ablation arm
        elif args.m is not None:
            args.label = f"m{args.m:g}_{args.news}_{args.social}{lev_sfx}"
        else:
            args.label = f"{args.news}_{args.social}{lev_sfx}"

    cfg = Config(provider=args.provider, seed=args.seed, reference_path=META)
    cfg.out_dir = args.out
    if args.model:
        cfg.model = args.model
    if args.reasoning_effort is not None:
        from nmsim.codex_exec import validate_codex_reasoning_effort

        cfg.codex_reasoning_effort = validate_codex_reasoning_effort(
            args.reasoning_effort
        )
    cfg.social_enabled = (args.social == "on")
    cfg.social_weight = args.gain            # only matters when social_enabled (binary in real path)
    if args.arm is not None:                 # ablation arms: social on, real news, vary the mic
        cfg.social_enabled = True
        cfg.social_weight = args.gain
        if args.arm == "muted":
            cfg.broadcast_mode = "exclude_influencer"
        elif args.arm == "solo":
            cfg.broadcast_mode = "only_influencer"
        elif args.arm == "demoted":
            cfg.demote_influencer = True
    if args.leverage:
        cfg.leverage_enabled = True
        if args.leverage_ratio is not None:
            cfg.leverage_ratio = args.leverage_ratio
        if args.leverage_spread is not None:
            cfg.leverage_spread = args.leverage_spread
        if args.maint is not None:
            cfg.maintenance_margin = args.maint
        if args.lev_fraction is not None:
            cfg.leverage_fraction = args.lev_fraction
    if args.news == "placebo":
        cfg.news_text = PLACEBO_NEWS
    if args.temp is not None:
        cfg.temperature = args.temp
        if args.temp > 0:
            cfg.cache_enabled = False        # cache assumes determinism; off for sampling
    if args.m is not None:
        cfg.population = build_population(args.m, args.total)
        # keep the SEED STRUCTURE identical to the 6-agent baseline: exactly 2
        # directly-informed agents (influencer + 1), regardless of population size
        cfg.seed_fraction = 2.0 / args.total
    if args.rounds is not None:
        cfg.n_rounds = args.rounds
    if args.news_round is not None:
        cfg.news_round = args.news_round
    return cfg


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results",
            command_identity="python -m experiments.run_seed",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    parser = build_argparser()
    try:
        args = parser.parse_args(argv)
        if args.price_csv and args.replay_from:
            parser.error("--price-csv reuse mode and --replay-from are mutually exclusive")
        cfg = config_from_args(args)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    rep_sfx = f"_r{args.rep}" if args.rep is not None else ""
    path = os.path.join(args.out, f"{args.label}_s{args.seed}{rep_sfx}.json")
    legacy_basename = os.path.basename(path)
    input_paths = {}
    if args.price_csv:
        input_paths["price_csv"] = args.price_csv
    if args.traces_csv:
        input_paths["traces_csv"] = args.traces_csv
    if args.propagation_csv:
        input_paths["propagation_csv"] = args.propagation_csv
    if args.replay_from:
        input_paths["llm_replay_records"] = _replay_records_path(args.replay_from)
    try:
        worker_count = int(os.environ.get("NMSIM_DRIVER_WORKERS", "1"))
    except ValueError:
        worker_count = 1

    manager = ManagedRunContext.create(
        cfg,
        out_root=args.out,
        scenario_id=args.scenario_id or args.label,
        run_id=args.run_id,
        worker_count=worker_count,
        input_paths=input_paths,
        batching={
            "strategy": "one logical LLM-agent batch per simulation round",
            "driver": os.environ.get("NMSIM_DRIVER", "experiments.run_seed"),
            "driver_workers": worker_count,
        },
        command_identity="python -m experiments.run_seed",
        run_kind="analysis" if args.price_csv else "simulation",
        planned_simulation_runs=0 if args.price_csv else 1,
    )
    if args.price_csv:
        manager.manifest["analysis_input_provenance"] = (
            inspect_legacy_analysis_inputs(
                [path for path in input_paths.values()]
            ).as_manifest_payload()
        )
        for descriptor in manager.manifest.get("inputs", []):
            descriptor["provenance_class"] = "legacy_unverified_input"
        manager.manifest.write_atomic()
    with manager:
        if args.price_csv:
            manager.llm_mode = "reuse"
            manager.register_llm_runtime(
                provider="none", model="none", mode="reuse",
                cache_enabled=False, network_access=False,
            )
            result = _from_csv(args, cfg)
        else:
            result, res, llm = _live(args, cfg, manager)

        result["rep"] = args.rep
        if args.m is not None:
            rest = sum(cfg.population.values()) - 1
            fuel = sum(cfg.population.get(persona, 0) for persona in FUEL)
            result["m"] = args.m
            result["population"] = cfg.population
            result["realized_m"] = round(fuel / rest, 4) if rest else None
        result["run_id"] = manager.run_id
        result["run_manifest"] = str(manager.manifest_path)
        result["completion"] = manager.manifest["completion"]
        result["honest_n"] = manager.manifest["honest_n"]
        result["honest_n_unit"] = "agent_decisions"
        result["honest_n_deprecated"] = True

        manager.set_stage("result_export")
        canonical_path = manager.run_dir / "experiment_result.json"
        with canonical_path.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        compatibility_source = manager.run_dir / legacy_basename
        with compatibility_source.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)

        health = result["health"]
        failed = int(health["bad_orders"])
        manager.register_llm_runtime(
            degraded=failed > 0,
            failed_or_degraded_decisions=failed,
        )
        manager.finish(legacy_filenames=[legacy_basename])
        metrics = result["metrics"]
        location = path if os.path.exists(path) else str(canonical_path)
        print(f"[{args.label} s{args.seed}] rmse={metrics['rmse_logprice']} "
              f"drop={metrics['drop_depth']} recovery={metrics['recovery']} "
              f"cascade={metrics['peak_cascade']} bad={health['bad_frac']} -> {location}")


if __name__ == "__main__":
    main()
