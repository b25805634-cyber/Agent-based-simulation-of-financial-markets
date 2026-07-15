"""Task 3: WHERE does the social dampening come from? Decompose ON-vs-OFF
post-news order flow by persona group, from existing logs (no new runs).

Sources:
  traces/real_{on,off}_s*_traces.json   -- 6-agent 2x2 mechanism dumps (full orders)
  <sweep_dir>/m*_real_{on,off}_s*.json  -- 30-agent sweep runs that carry the
                                           compact "orders" field (newer runs)

Channels tested:
  (a) fuel sells less when social ON   -> dSell_fuel < 0
  (b) dampeners buy more when social ON-> dBuy_damp  > 0
  (c) everyone hesitates: total volume & net sell pressure shrink

Usage: python -m experiments.flow_decomp [--sweep results_sweep]
"""
from __future__ import annotations
import os
import re
import glob
import json
import argparse
from statistics import mean

GROUPS = {"fuel": {"retail_crowd", "fomo_momentum"},
          "damp": {"value_institution", "contrarian_fund", "quant_arb"},
          "inf":  {"influencer_amplifier"}}


def _group(agent):
    base = agent.split("#")[0]
    for g, ids in GROUPS.items():
        if base in ids:
            return g
    return None


def _flows_from_orders(orders, news_round):
    """orders: iterable of (round, agent, side, qty) -> {group: {buy, sell}}"""
    acc = {g: {"buy": 0, "sell": 0} for g in GROUPS}
    for r, agent, side, qty in orders:
        if int(r) < news_round or side not in ("buy", "sell"):
            continue
        g = _group(agent)
        if g:
            acc[g][side] += int(qty)
    return acc


def _print_pairs(tag, pairs):
    """pairs: list of (label, flows_on, flows_off)"""
    if not pairs:
        return False
    print(f"\n===== {tag} (N={len(pairs)} 对) =====")
    agg = {g: {k: [] for k in ("dbuy", "dsell", "dnet", "von", "voff")} for g in GROUPS}
    tot_von, tot_voff, tot_dnet = [], [], []
    for label, on, off in pairs:
        for g in GROUPS:
            b_on, s_on = on[g]["buy"], on[g]["sell"]
            b_of, s_of = off[g]["buy"], off[g]["sell"]
            agg[g]["dbuy"].append(b_on - b_of)
            agg[g]["dsell"].append(s_on - s_of)
            agg[g]["dnet"].append((b_on - s_on) - (b_of - s_of))
            agg[g]["von"].append(b_on + s_on)
            agg[g]["voff"].append(b_of + s_of)
        v_on = sum(on[g]["buy"] + on[g]["sell"] for g in GROUPS)
        v_of = sum(off[g]["buy"] + off[g]["sell"] for g in GROUPS)
        n_on = sum(on[g]["buy"] - on[g]["sell"] for g in GROUPS)
        n_of = sum(off[g]["buy"] - off[g]["sell"] for g in GROUPS)
        tot_von.append(v_on); tot_voff.append(v_of); tot_dnet.append(n_on - n_of)
    print(f"{'组':6s} {'Δ买量(开−关)':>14s} {'Δ卖量(开−关)':>14s} {'Δ净流(开−关)':>14s}")
    for g in GROUPS:
        print(f"{g:6s} {mean(agg[g]['dbuy']):>+14.1f} {mean(agg[g]['dsell']):>+14.1f} "
              f"{mean(agg[g]['dnet']):>+14.1f}")
    print(f"总成交量: ON {mean(tot_von):.0f} vs OFF {mean(tot_voff):.0f} "
          f"(Δ {mean(tot_von)-mean(tot_voff):+.0f})")
    print(f"总净流 Δ(开−关): {mean(tot_dnet):+.1f}  (>0 = 净卖压减小 = 减震方向)")
    # channel verdicts
    ds_f = mean(agg['fuel']['dsell']); db_d = mean(agg['damp']['dbuy'])
    dn_f = mean(agg['fuel']['dnet']); dn_d = mean(agg['damp']['dnet'])
    print("通道判定:")
    print(f"  (a) 燃料卖得少了?  Δ卖_fuel={ds_f:+.1f} -> {'是' if ds_f < 0 else '否'}"
          f"   (Δ净_fuel={dn_f:+.1f})")
    print(f"  (b) 阻尼买得多了?  Δ买_damp={db_d:+.1f} -> {'是' if db_d > 0 else '否'}"
          f"   (Δ净_damp={dn_d:+.1f})")
    print(f"  (c) 整体缩量犹豫?  Δ总量={mean(tot_von)-mean(tot_voff):+.0f} -> "
          f"{'是' if mean(tot_von) < mean(tot_voff) else '否'}")
    contrib = {"fuel": dn_f, "damp": dn_d, "inf": mean(agg['inf']['dnet'])}
    tot = sum(contrib.values())
    if tot:
        print("净流改善的来源占比: " + ", ".join(
            f"{g} {v/tot*100:.0f}%" for g, v in contrib.items()))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default="traces")
    ap.add_argument("--sweep", default="results_sweep")
    args = ap.parse_args()

    # ---- 6-agent mechanism dumps ----
    pairs6 = []
    for f_on in sorted(glob.glob(os.path.join(args.traces, "real_on_s*_traces.json"))):
        f_off = f_on.replace("real_on_", "real_off_")
        if not os.path.exists(f_off):
            continue
        d_on, d_off = json.load(open(f_on)), json.load(open(f_off))
        nr = d_on["news_round"]
        def to_orders(d):
            return [(int(r), it["agent"], it["side"], it["qty"])
                    for r, items in d["traces"].items() for it in items]
        pairs6.append((os.path.basename(f_on),
                       _flows_from_orders(to_orders(d_on), nr),
                       _flows_from_orders(to_orders(d_off), nr)))
    _print_pairs("6-agent 2x2 配对 (traces/)", pairs6)

    # ---- 30-agent sweep runs that carry 'orders' ----
    pat = re.compile(r"(m[\d.]+)_real_(on|off)_s(\d+)\.json$")
    by_key = {}
    for f in glob.glob(os.path.join(args.sweep, "*.json")):
        g = pat.search(os.path.basename(f))
        if not g:
            continue
        d = json.load(open(f))
        if "orders" not in d or d["health"]["bad_frac"] > 0.15:
            continue
        by_key.setdefault((g.group(1), int(g.group(3))), {})[g.group(2)] = d
    pairs30 = []
    for (m, s), v in sorted(by_key.items()):
        if "on" in v and "off" in v:
            nr = v["on"]["news_round"]
            pairs30.append((f"{m}_s{s}",
                            _flows_from_orders(v["on"]["orders"], nr),
                            _flows_from_orders(v["off"]["orders"], nr)))
    if not _print_pairs("30-agent 扫描配对 (带 orders 字段)", pairs30):
        print("\n(30-agent 配对尚无双边 orders 数据 — 新跑的 run 会逐步填充)")


if __name__ == "__main__":
    main()
