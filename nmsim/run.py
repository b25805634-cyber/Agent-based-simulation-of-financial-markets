"""Entry point — wires config -> sim -> outputs (CSV + plots) and prints cost.

Usage:
    python -m nmsim.run                         # default run (auto provider)
    python -m nmsim.run --provider mock --rounds 30
    python -m nmsim.run --topology random --social-weight 0.8
    python -m nmsim.run --reference path/to/episode.csv
    python -m nmsim.run --no-social             # ablation: contagion off
"""
from __future__ import annotations
import os
import csv
import json
import argparse
import sys
from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path

from .config import Config
from .llm import build_llm  # compatibility import; construction lives in ManagedRunContext
from .managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from .run_context import ManagedRunContext, safe_output_root
from .sim import run_sim, SimResult
from . import validation as V


_OUTPUT_FILENAMES = (
    "price_path.csv",
    "reasoning_traces.csv",
    "propagation.csv",
    "stylized_facts.json",
    "config.json",
    "sim_overview.png",
)
_BAD_MARKERS = ("parse-failed", "api-error", "parse-retries-exhausted")


def _write_price_csv(res: SimResult, path: str):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["round", "price", "volume"])
        for r, p, v in res.rows:
            w.writerow([r, f"{p:.4f}", v])


def _write_traces(res: SimResult, path: str):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["round", "agent", "side", "quantity", "limit_price", "sentiment",
                    "public_take", "reasoning"])
        for r, items in res.traces.items():
            for agent, side, qty, limit, senti, take, why in items:
                w.writerow([r, agent, side, qty, f"{limit:.2f}", f"{senti:.3f}", take, why])


def _write_propagation_csv(res: SimResult, path: str):
    m = res.metrics
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["round", "mean_sentiment", "std_sentiment", "pos_share",
                    "neg_share", "flips", "cascade_size"])
        for i, r in enumerate(m.rounds):
            w.writerow([r, m.mean_sentiment[i], m.std_sentiment[i], m.pos_share[i],
                        m.neg_share[i], m.flips[i], m.cascade_size[i]])


def _plot(res: SimResult, facts, out_dir: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot] matplotlib not installed; skipping plots (CSV/JSON still written)")
        return

    cfg = res.cfg
    m = res.metrics
    fig, axes = plt.subplots(2, 1, figsize=(9, 8), sharex=False)

    # price path
    xs = list(range(len(res.history)))
    axes[0].plot(xs, res.history, marker="o", ms=3, color="#1D9E75")
    axes[0].axvline(cfg.news_round, color="#D85A30", ls="--", lw=1.2)
    axes[0].text(cfg.news_round + 0.2, max(res.history), "  news shock",
                 color="#D85A30", va="top")
    axes[0].set_title("Price path"); axes[0].set_ylabel("price")
    axes[0].grid(alpha=0.3)

    # sentiment spread over time + cascade size
    ax = axes[1]
    ax.plot(m.rounds, m.mean_sentiment, color="#2C6FBB", label="mean sentiment")
    lo = [mu - sd for mu, sd in zip(m.mean_sentiment, m.std_sentiment)]
    hi = [mu + sd for mu, sd in zip(m.mean_sentiment, m.std_sentiment)]
    ax.fill_between(m.rounds, lo, hi, color="#2C6FBB", alpha=0.15, label="±1 std")
    ax.axhline(0, color="#888", lw=0.8)
    ax.axvline(cfg.news_round, color="#D85A30", ls="--", lw=1.2)
    ax2 = ax.twinx()
    ax2.plot(m.rounds, m.cascade_size, color="#C04CC0", lw=1.4, ls=":",
             label="cascade size")
    ax2.set_ylabel("cascade size (frac aligned)", color="#C04CC0")
    ax2.set_ylim(0, 1.02)
    ax.set_title(f"Narrative spread — topology={cfg.topology}, "
                 f"social_weight={cfg.social_weight}, peak cascade={m.peak_cascade():.2f}")
    ax.set_xlabel("round"); ax.set_ylabel("sentiment")
    ax.grid(alpha=0.3); ax.legend(loc="upper left", fontsize=8)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "sim_overview.png"), dpi=130)
    plt.close(fig)


def _replay_records_path(source: str) -> str:
    path = Path(source)
    return str(path / "llm_records.jsonl" if path.is_dir() else path)


def _managed_llm(cfg: Config, manager: ManagedRunContext, replay_from: str | None):
    """Backward-compatible helper delegated to the unified managed context."""
    if manager.cfg is not cfg:
        raise ValueError("managed context Config identity mismatch")
    return manager.prepare_llm(replay_from)


def _sample_counts(res: SimResult | None, cfg: Config, llm=None):
    if res is None:
        if cfg.population:
            planned = 0
            for persona_id, count in cfg.population.items():
                planned += min(int(count), 1) if persona_id == "influencer_amplifier" else int(count)
            planned = min(planned, cfg.max_llm_agents)
        else:
            planned = min(cfg.n_llm_agents, cfg.max_llm_agents, 6)
        completed = int(getattr(llm, "response_count", 0)) if llm is not None else 0
        return cfg.n_rounds * max(0, planned), completed, 0
    completed = failed = 0
    for items in res.traces.values():
        for item in items:
            completed += 1
            if any(marker in item[-1] for marker in _BAD_MARKERS):
                failed += 1
    actual_llm = sum(bool(getattr(agent, "is_llm", False)) for agent in res.agents)
    return cfg.n_rounds * actual_llm, completed, failed


def _safe_config_json(cfg: Config) -> str:
    """Preserve legacy config JSON while refusing to persist a supplied secret."""
    payload = json.loads(cfg.to_json())
    api_key = payload.get("openai_api_key")
    if api_key not in (None, "", "EMPTY", "<redacted>"):
        payload["openai_api_key"] = "<redacted>"
    return json.dumps(payload, indent=2, ensure_ascii=False)


def run(cfg: Config, *, replay_from: str | None = None,
        scenario_id: str | None = None, run_id: str | None = None,
        worker_count: int | None = None, input_paths=None,
        command_identity: str = "library:nmsim.run.run") -> SimResult:
    if cfg.news_round > cfg.n_rounds:
        print(f"[config] news_round {cfg.news_round} > n_rounds {cfg.n_rounds}; "
              f"clamping to {cfg.n_rounds}")
        cfg.news_round = cfg.n_rounds

    managed_inputs = dict(input_paths or {})
    if replay_from:
        managed_inputs["llm_replay_records"] = _replay_records_path(replay_from)
    manager = ManagedRunContext.create(
        cfg,
        out_root=cfg.out_dir,
        scenario_id=scenario_id,
        run_id=run_id,
        worker_count=worker_count,
        input_paths=managed_inputs,
        command_identity=command_identity,
    )
    with manager:
        llm, tracker = _managed_llm(cfg, manager, replay_from)
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

        # --- Task 3 validation instrumentation ---
        # M2: shock index = last pre-news price (history[news_round-1]), so t=0 is the
        # clean pre-shock baseline that aligns with the reference's pre-event close.
        shock = max(0, cfg.news_round - 1)
        facts = V.stylized_facts(res.history, shock, res.metrics.peak_cascade())
        facts_d = facts.as_dict()
        if cfg.reference_path:
            try:
                facts_d["reference_comparison"] = V.compare_to_reference(
                    res.history, shock, cfg.reference_path)
            except Exception as e:
                facts_d["reference_comparison"] = {"error": str(e)}

        # --- canonical, immutable outputs ---
        manager.set_stage("result_export")
        output_dir = str(manager.run_dir)
        _write_price_csv(res, os.path.join(output_dir, "price_path.csv"))
        traces_path = os.path.join(output_dir, "reasoning_traces.csv")
        _write_traces(res, traces_path)
        os.chmod(traces_path, 0o600)
        _write_propagation_csv(res, os.path.join(output_dir, "propagation.csv"))
        with open(os.path.join(output_dir, "stylized_facts.json"), "x") as fh:
            json.dump(facts_d, fh, indent=2)
        with open(os.path.join(output_dir, "config.json"), "x") as fh:
            fh.write(_safe_config_json(cfg))
        _plot(res, facts, output_dir)

        expected, completed, failed = _sample_counts(res, cfg)
        manager.register_llm_runtime(
            degraded=failed > 0,
            failed_or_degraded_decisions=failed,
        )
        manager.finish(legacy_filenames=_OUTPUT_FILENAMES)
        _print_summary(res, facts_d, output_dir)
        return res


def _print_summary(res: SimResult, facts: dict, output_dir: str | None = None):
    cfg = res.cfg
    h = res.history
    shock = max(0, cfg.news_round - 1)               # M2: pre-news baseline
    pre, trough = h[shock], min(h[shock:])
    print("\n" + "=" * 64)
    print(f"NARRATIVE MARKET SIM  (seed={cfg.seed}, provider-resolved run)")
    print("=" * 64)
    print(res.tracker.report())
    print(f"\nprice before news (r={shock}): {pre:.2f}")
    print(f"trough after news:               {trough:.2f}  ({(trough/pre-1):+.1%})")
    print(f"final price (r={cfg.n_rounds}):              {h[-1]:.2f}")

    print(f"\n--- Phase 3: contagion (topology={cfg.topology}, "
          f"social_weight gain={cfg.social_weight}) ---")
    print(f"seed agents: {sorted(res.seed_agents)}")
    if res.adjacency and any(res.adjacency.values()):
        from .contagion import degrees
        deg = degrees(res.adjacency)
        top = sorted(deg.items(), key=lambda kv: kv[1], reverse=True)
        print("network degree (most-connected first): "
              + ", ".join(f"{nm}={d}" for nm, d in top))
        max_d = top[0][1]
        leaders = [nm for nm, d in top if d == max_d]
        hub = next((nm for nm in leaders if nm in res.hub_names), leaders[0])
        ok = all(deg.get(h, 0) == max_d for h in res.hub_names) and res.hub_names
        print(f"highest-degree node: {hub}  "
              f"({'influencer is most-connected ✓' if ok else 'NOT the influencer'})")
    print(f"peak cascade size: {res.metrics.peak_cascade():.2f}  -> "
          f"{'CASCADED' if res.metrics.cascaded() else 'fizzled'}")
    print(f"total stance flips: {sum(res.metrics.flips)}")

    print(f"\n--- Phase 4: stylized facts ---")
    print(f"return kurtosis (fat tails):  {facts['kurtosis']:+.2f}")
    print(f"tail ratio:                   {facts['tail_ratio']:.2f}")
    print(f"vol-clustering ACF(|r|) L1-5: {facts['vol_clustering_acf']}")
    print(f"reaction shape:               {facts['reaction']}")
    if "reference_comparison" in facts:
        rc = facts["reference_comparison"]
        if "error" in rc:
            print(f"reference comparison:         error: {rc['error']}")
        else:
            tj = rc["trajectory"]
            sup = rc["reaction_supplementary"]
            print(f"trajectory RMSE(log-price):   {tj['rmse_logprice']}  "
                  f"(primary; lower=closer, horizon={tj['horizon']})")
            print(f"trajectory DTW / DTW_norm:    {tj['dtw']} / {tj['dtw_norm']}")
            print(f"  supplementary depth/recovery/speed match: "
                  f"{sup['depth_match']} / {sup['recovery_match']} / {sup['speed_match']}")

    print(f"\n=== heterogeneous reactions at the news round (r={cfg.news_round}) ===")
    for agent, side, qty, limit, senti, take, why in res.traces[cfg.news_round]:
        print(f"  {agent:20s} {side:4s} {qty:3d} @ {limit:7.2f}  s={senti:+.2f}  "
              f"\"{take}\"")
    actual_output = output_dir or res.run_dir or cfg.out_dir
    print(f"\noutputs written to: {os.path.abspath(actual_output)}/")


def build_argparser():
    p = RaisingArgumentParser(
        description="Narrative Market Sim", allow_abbrev=False
    )
    p.add_argument("--version", action="version", version="nmsim phase-1.1b")
    p.add_argument(
        "--config-json",
        default=None,
        help="strict Config mapping JSON; CLI flags override canonical fields",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="auto|mock|anthropic|openai|codex_exec (experimental local CLI)",
    )
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None, help="OpenAI-compatible base_url")
    p.add_argument("--api-key", default=None, help="OpenAI-compatible api_key")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="response token cap (raise for verbose/reasoning models)")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--cheap", action="store_true", help="use the cheaper model")
    p.add_argument("--rounds", type=int, default=None)
    p.add_argument("--news-round", type=int, default=None)
    p.add_argument("--llm-agents", type=int, default=None)
    p.add_argument("--topology", default=None,
                   choices=["fully_connected", "random", "scale_free"])
    p.add_argument("--social-mode", default=None, choices=["feed", "network"])
    p.add_argument("--social-weight", type=float, default=None)
    p.add_argument("--seed-fraction", type=float, default=None)
    p.add_argument("--no-social", action="store_true", help="ablation: contagion off")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--reference", default=None, help="reference episode CSV")
    p.add_argument("--out", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--replay-from", default=None,
                   help="run directory or llm_records.jsonl; never calls a provider")
    p.add_argument("--scenario-id", default=None,
                   help="stable scenario identifier stored in the run manifest")
    p.add_argument("--run-id", default=None,
                   help="optional explicit unique id; an existing id is refused")
    return p


def _config_mapping(path: str) -> Mapping[str, object]:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, Mapping):
        raise ValueError("Config JSON root must be an object")
    return value


def cfg_from_args(args) -> Config:
    cfg = Config.from_dict(_config_mapping(args.config_json)) if args.config_json else Config()
    if args.provider is not None: cfg.provider = args.provider
    if args.model is not None: cfg.model = args.model
    if args.base_url is not None: cfg.openai_base_url = args.base_url
    if args.api_key is not None: cfg.openai_api_key = args.api_key
    if args.max_tokens is not None: cfg.max_tokens = args.max_tokens
    if getattr(args, "temperature", None) is not None: cfg.temperature = args.temperature
    if args.cheap: cfg.use_cheap_model = True
    if args.rounds is not None: cfg.n_rounds = args.rounds
    if args.news_round is not None: cfg.news_round = args.news_round
    if args.llm_agents is not None: cfg.n_llm_agents = args.llm_agents
    if args.topology is not None: cfg.topology = args.topology
    if args.social_mode is not None: cfg.social_mode = args.social_mode
    if args.social_weight is not None: cfg.social_weight = args.social_weight
    if args.seed_fraction is not None: cfg.seed_fraction = args.seed_fraction
    if args.no_social: cfg.social_enabled = False
    if args.seed is not None: cfg.seed = args.seed
    if args.reference is not None: cfg.reference_path = args.reference
    if args.out is not None: cfg.out_dir = args.out
    if args.no_cache: cfg.cache_enabled = False
    return cfg


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out=Config().out_dir,
            command_identity="python -m nmsim.run",
            allow_config_json=True,
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
        # If a strict Config file supplies out_dir, use that safe location for
        # a possible validation-failure attempt unless --out explicitly wins.
        if bootstrap is not None and args.config_json and args.out is None:
            try:
                raw = _config_mapping(args.config_json)
                candidate = raw.get("out_dir")
                if isinstance(candidate, str):
                    bootstrap = replace(
                        bootstrap,
                        out_root=safe_output_root(candidate, Config().out_dir),
                    )
            except Exception:
                # Full validation below records the actual error using the
                # already-safe default output root.
                pass
        cfg = cfg_from_args(args)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    inputs = {"config_json": args.config_json} if args.config_json else None
    run(
        cfg,
        replay_from=args.replay_from,
        scenario_id=args.scenario_id,
        run_id=args.run_id,
        input_paths=inputs,
        command_identity="python -m nmsim.run",
    )


if __name__ == "__main__":
    main()
