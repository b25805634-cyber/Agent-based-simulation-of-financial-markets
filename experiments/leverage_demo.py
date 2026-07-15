"""Phase-1 mock validation of the leverage / forced-liquidation layer.

Runs the SAME seed + population with leverage OFF vs ON (mock LLM, no API), and
shows that ON produces a forced-liquidation SPIRAL: a deeper crash plus a chain
of margin calls cascading across rounds. Prints the liquidation event log so we
can see the mechanism is alive.

Usage:  python -m experiments.leverage_demo [--seed 7] [--total 30] [--rounds 24]
"""
from __future__ import annotations
import argparse
from nmsim.config import Config
from nmsim.llm import build_llm
from nmsim.run_context import NullRunContext
from nmsim.sim import run_sim


def _pop(m, total):
    from experiments.run_seed import build_population
    return build_population(m, total)


def _run(seed, m, total, rounds, news_round, leverage, L, frac):
    cfg = Config(provider="mock", seed=seed)
    cfg.population = _pop(m, total)
    cfg.seed_fraction = 2.0 / total
    cfg.n_rounds = rounds
    cfg.news_round = news_round
    cfg.leverage_enabled = leverage
    cfg.leverage_ratio = L
    cfg.leverage_fraction = frac
    llm, tracker = build_llm(cfg)
    with NullRunContext() as unmanaged:
        return unmanaged.execute_simulation(run_sim, cfg, llm, tracker)


def _summary(res):
    h = res.history
    pre = h[res.cfg.news_round - 1]
    trough = min(h[res.cfg.news_round - 1:])
    drop = trough / pre - 1
    return pre, trough, drop, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--m", type=float, default=0.5, help="fuel ratio of the population")
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--news-round", type=int, default=3,
                    help="early shock keeps positions intact when it lands")
    ap.add_argument("--leverage", type=float, default=2.5)
    ap.add_argument("--fraction", type=float, default=0.5)
    args = ap.parse_args()

    off = _run(args.seed, args.m, args.total, args.rounds, args.news_round,
               leverage=False, L=args.leverage, frac=args.fraction)
    on = _run(args.seed, args.m, args.total, args.rounds, args.news_round,
              leverage=True, L=args.leverage, frac=args.fraction)

    n_lev = sum(1 for a in on.agents if getattr(a, "is_leveraged", False))
    print(f"=== LEVERAGE MOCK VALIDATION (seed={args.seed}, {args.total} agents, "
          f"{args.rounds} rounds, news@{on.cfg.news_round}) ===")
    print(f"leveraged agents: {n_lev}/{sum(1 for a in on.agents if a.is_llm)} "
          f"(L={on.cfg.leverage_ratio}, maint={on.cfg.maintenance_margin}, "
          f"start equity ratio = {1/on.cfg.leverage_ratio:.2f})\n")

    for tag, res in (("LEVERAGE OFF", off), ("LEVERAGE ON ", on)):
        pre, trough, drop, h = _summary(res)
        nliq = len(res.liquidations)
        liq_shares = sum(e.shares for e in res.liquidations)
        print(f"{tag}: pre {pre:.2f} -> trough {trough:.2f}  drop {drop:+.1%}   "
              f"final {h[-1]:.2f}   | liquidations: {nliq} events, {liq_shares} shares")

    po, _, do, ho = _summary(off)
    pn, _, dn, hn = _summary(on)
    print(f"\nSPIRAL CHECK: leverage deepened the crash by {dn - do:+.1%} "
          f"({do:+.1%} -> {dn:+.1%})")

    # price path side-by-side around the shock
    print("\nround |  price OFF |  price ON  | liq events ON (agent:shares)")
    by_round = {}
    for e in on.liquidations:
        by_round.setdefault(e.round, []).append(f"{e.agent.split('#')[0][:6]}:{e.shares}")
    nr = on.cfg.news_round
    for r in range(max(1, nr - 1), len(ho)):
        evs = by_round.get(r, [])
        tag = "  <-- NEWS" if r == nr else ""
        evstr = (str(len(evs)) + "  " + ", ".join(evs[:6]) + ("…" if len(evs) > 6 else "")) if evs else ""
        print(f"{r:5d} | {ho[r]:9.2f} | {hn[r]:9.2f} | {evstr}{tag}")

    # the chain: rounds with cascading liquidations
    chain = sorted(by_round)
    print(f"\nliquidation rounds (the cascade): {chain}")
    if len(chain) >= 2 and (dn - do) < -0.01:
        print("=> SPIRAL CONFIRMED: leverage ON crashed deeper AND produced a "
              "multi-round liquidation chain.")
    elif chain:
        print("=> liquidations fired but spiral weak — may need a deeper shock or "
              "higher leverage to chain. Inspect above.")
    else:
        print("=> NO liquidations — shock too shallow to breach margin at this L. "
              "Try higher --leverage or a deeper-crashing seed.")


if __name__ == "__main__":
    main()
