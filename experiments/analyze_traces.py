"""Read the paired real_on / real_off trace dumps and lay out the MECHANISM:
how each agent behaves round-by-round, ON vs OFF, focused on the dampeners
(value/contrarian/quant) and the fuel (retail/fomo) around the news round.

Tests two competing explanations of the dampening:
  (A) coordination: dampeners buy the dip EARLIER and more TOGETHER when social is on
  (B) dilution: fuel agents PANIC LESS when social is on (mixed signals damp fear)

Usage: python -m experiments.analyze_traces --dir traces --seed 1
"""
from __future__ import annotations
import os
import json
import argparse

DAMPENERS = ["value_institution", "contrarian_fund", "quant_arb"]
FUEL = ["retail_crowd", "fomo_momentum"]
ALL = ["retail_crowd", "fomo_momentum", "value_institution", "quant_arb",
       "influencer_amplifier", "contrarian_fund"]
SYM = {"buy": "B", "sell": "S", "hold": "H"}


def _load(d, label, seed):
    with open(os.path.join(d, f"{label}_s{seed}_traces.json")) as fh:
        return json.load(fh)


def _by_agent(dump):
    """agent -> {round_int: {side,sentiment,public_take,reasoning}}"""
    out = {a: {} for a in ALL}
    for r, items in dump["traces"].items():
        for it in items:
            if it["agent"] in out:
                out[it["agent"]][int(r)] = it
    return out


def _grid(dump, nr):
    ba = _by_agent(dump)
    rounds = sorted(int(r) for r in dump["traces"].keys())
    print("    round:", " ".join(f"{r:>2}" for r in rounds))
    for a in ALL:
        cells = []
        for r in rounds:
            it = ba[a].get(r)
            cells.append(SYM.get(it["side"], "?") if it else ".")
        marker = "  <-dampener" if a in DAMPENERS else ("  <-fuel" if a in FUEL else "")
        print(f"  {a:20s} " + " ".join(f"{c:>2}" for c in cells) + marker)
    print(f"    (news_round = {nr}; B=buy S=sell H=hold)")


def _first_buy_after(ba, agent, nr):
    rs = sorted(r for r in ba[agent] if r >= nr and ba[agent][r]["side"] == "buy")
    return rs[0] if rs else None


def _metrics(dump, nr):
    ba = _by_agent(dump)
    rounds = [r for r in sorted(int(x) for x in dump["traces"].keys()) if r >= nr]
    # (A) dampeners: first buy round + how many buying per post-news round
    fb = {a: _first_buy_after(ba, a, nr) for a in DAMPENERS}
    per_round_dampbuy = {r: sum(1 for a in DAMPENERS
                               if ba[a].get(r) and ba[a][r]["side"] == "buy")
                         for r in rounds}
    # (B) fuel: avg sentiment (panic) + sell count post-news
    fuel_sent = []
    for a in FUEL:
        vs = [ba[a][r]["sentiment"] for r in rounds if ba[a].get(r)]
        fuel_sent.append(sum(vs) / len(vs) if vs else 0.0)
    return fb, per_round_dampbuy, fuel_sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="traces")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    on = _load(args.dir, "real_on", args.seed)
    off = _load(args.dir, "real_off", args.seed)
    nr = on["news_round"]

    for tag, dump in [("SOCIAL ON", on), ("SOCIAL OFF", off)]:
        print(f"\n===== {tag} (seed {args.seed}) — price {dump['history'][nr-1]:.1f} -> "
              f"{min(dump['history'][nr-1:]):.1f} =====")
        print(f"seed agents: {dump['seed_agents']}")
        _grid(dump, nr)

    print("\n===== MECHANISM CHECK =====")
    for tag, dump in [("ON", on), ("OFF", off)]:
        fb, dbuy, fuel = _metrics(dump, nr)
        print(f"[{tag}] dampener first-buy round after news: {fb}")
        print(f"[{tag}] # dampeners buying per post-news round: {dbuy}")
        print(f"[{tag}] fuel ({FUEL}) avg post-news sentiment: "
              f"{[round(x,3) for x in fuel]}")

    # public_takes of dampeners around the news round (read these)
    print("\n===== DAMPENER public_take around news (read for coordination vs dilution) =====")
    for a in DAMPENERS:
        for tag, dump in [("ON", on), ("OFF", off)]:
            ba = _by_agent(dump)
            print(f"\n  [{a} | {tag}]")
            for r in range(nr, min(nr + 4, max(ba[a]) + 1)):
                it = ba[a].get(r)
                if it:
                    print(f"    r{r} {it['side']:4s} s={it['sentiment']:+.2f}: {it['public_take'][:120]}")


if __name__ == "__main__":
    main()
