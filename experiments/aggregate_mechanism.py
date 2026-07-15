"""Aggregate the mechanism signal across ALL captured real_on / real_off trace
pairs. For each seed we know whether ON was shallower (dampened) than OFF; we then
ask which behavioral difference tracks the dampening, testing:

  (A) coordination : dampeners (value/contrarian) buy more / earlier when social on
  (B) dilution     : fuel (retail/fomo) panic less when social on
  (C) momentum-off : the narrative-immune momentum trader (quant_arb) keeps shorting
                     all the way down when social is OFF, deepening the crash

Usage: python -m experiments.aggregate_mechanism --dir traces
"""
from __future__ import annotations
import os
import glob
import json
import argparse
from statistics import mean

DAMP = ["value_institution", "contrarian_fund"]
FUEL = ["retail_crowd", "fomo_momentum"]
MOM = "quant_arb"


def _by_agent(dump):
    out = {}
    for r, items in dump["traces"].items():
        for it in items:
            out.setdefault(it["agent"], {})[int(r)] = it
    return out


def _seed_stats(dump):
    nr = dump["news_round"]
    ba = _by_agent(dump)
    post = [r for r in sorted(int(x) for x in dump["traces"].keys()) if r >= nr]
    pre = dump["history"][nr - 1]
    drop = min(dump["history"][nr - 1:]) / pre - 1 if pre else 0.0
    def side_count(agent, side):
        return sum(1 for r in post if ba.get(agent, {}).get(r) and ba[agent][r]["side"] == side)
    damp_buys = mean(side_count(a, "buy") for a in DAMP)
    fuel_sent = mean(ba[a][r]["sentiment"] for a in FUEL for r in post if ba.get(a, {}).get(r))
    mom_sells = side_count(MOM, "sell")
    mom_buys = side_count(MOM, "buy")
    return {"drop": round(drop, 3), "damp_buys": round(damp_buys, 2),
            "fuel_sent": round(fuel_sent, 3), "mom_sells": mom_sells, "mom_buys": mom_buys,
            "n_post": len(post)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="traces")
    args = ap.parse_args()
    seeds = sorted(int(os.path.basename(f).split("_s")[1].split("_")[0])
                   for f in glob.glob(os.path.join(args.dir, "real_on_s*_traces.json")))
    rows = []
    for s in seeds:
        try:
            on = json.load(open(os.path.join(args.dir, f"real_on_s{s}_traces.json")))
            off = json.load(open(os.path.join(args.dir, f"real_off_s{s}_traces.json")))
        except FileNotFoundError:
            continue
        so, sf = _seed_stats(on), _seed_stats(off)
        rows.append((s, so, sf))

    print(f"paired seeds: {[r[0] for r in rows]}  (N={len(rows)})\n")
    hdr = ("seed  drop_ON drop_OFF  dampened?   dampBuy(ON/OFF)   fuelSent(ON/OFF)   "
           "quant_sells(ON/OFF)")
    print(hdr)
    for s, o, f in rows:
        dampened = "YES" if o["drop"] > f["drop"] else "no "   # ON less negative = shallower
        print(f"{s:>3}   {o['drop']:+.2f}   {f['drop']:+.2f}    {dampened}        "
              f"{o['damp_buys']:.1f}/{f['damp_buys']:.1f}          "
              f"{o['fuel_sent']:+.2f}/{f['fuel_sent']:+.2f}        "
              f"{o['mom_sells']}/{f['mom_sells']}")

    if rows:
        def avg(side, k):
            return round(mean((o if side == 'on' else f)[k] for _, o, f in rows), 3)
        n_damp = sum(1 for _, o, f in rows if o["drop"] > f["drop"])
        print(f"\n--- aggregate (N={len(rows)}) ---")
        print(f"  seeds where ON shallower (dampened): {n_damp}/{len(rows)}")
        print(f"  (A) dampener buys post-news   ON {avg('on','damp_buys')} vs OFF {avg('off','damp_buys')}")
        print(f"  (B) fuel sentiment post-news  ON {avg('on','fuel_sent')} vs OFF {avg('off','fuel_sent')}  (less neg = less panic)")
        print(f"  (C) quant_arb sells post-news ON {avg('on','mom_sells')} vs OFF {avg('off','mom_sells')}  (more = deeper momentum shorting)")


if __name__ == "__main__":
    main()
