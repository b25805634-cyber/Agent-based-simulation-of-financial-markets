"""Analyzer for Task 2 critical-point sweep. Per leverage level L: mean crash
depth (+95% CI), same-seed paired Δ vs the social-ON lev-OFF temp=0 baseline,
and liquidation-cascade scale. Plots x=leverage strength (L), y=mean crash depth
with CI, marks the largest level-to-level drop (candidate self-stabilise→self-
destruct jump).

Usage: python -m experiments.critsweep_analyze --out results_critsweep \
          --baseline-dir results_phase2b --m 0.7 --levels 2.0 2.6 3.0 3.5
"""
from __future__ import annotations
import os, re, glob, json, math, argparse
from statistics import mean, stdev

_T95 = {1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306,
        9:2.262,10:2.228,11:2.201,12:2.179,13:2.160,14:2.145,15:2.131}


def _ci(xs):
    n=len(xs)
    if n==0: return None
    mu=mean(xs)
    if n==1: return (mu,None,None,1)
    h=_T95.get(n-1,1.96)*stdev(xs)/math.sqrt(n)
    return (mu,mu-h,mu+h,n)


def _drop(d): return d["metrics"]["drop_depth"]
def _liqsh(d): return sum(e[2] for e in d.get("liquidations",[]))
def _nliq(d): return len(d.get("liquidations",[]))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out", default="results_critsweep")
    ap.add_argument("--baseline-dir", default="results_phase2b")
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--levels", type=float, nargs="+", required=True)
    ap.add_argument("--plot", default=None)
    args=ap.parse_args()

    # social-ON lev-OFF temp=0 baseline (paired anchor)
    def base(seed):
        f=os.path.join(args.baseline_dir, f"m{args.m:g}_real_on_s{seed}.json")
        return json.load(open(f)) if os.path.exists(f) else None

    print(f"===== CRITICAL-POINT SWEEP (m={args.m:g}, social ON, temp=0) =====")
    print("leverage strength = L center (higher = shallower breach trigger = more aggressive)\n")
    print(f"{'L':>5} {'n':>3} {'mean drop':>11} {'95% CI':>22} {'Δ vs lev-off':>13} {'liq ev':>7} {'liq sh':>7}")
    rows=[]
    for L in args.levels:
        rx=re.compile(rf"m{re.escape(f'{args.m:g}')}_critL{re.escape(f'{L:g}')}_s(\d+)\.json$")
        recs={}
        for f in glob.glob(os.path.join(args.out,"*.json")):
            g=rx.search(os.path.basename(f))
            if not g: continue
            d=json.load(open(f))
            if d.get("health",{}).get("bad_frac",1.0)>0.15: continue
            recs[int(g.group(1))]=d
        drops=[_drop(d) for d in recs.values()]
        c=_ci(drops)
        # paired Δ vs baseline
        diffs=[]
        for s,d in recs.items():
            b=base(s)
            if b: diffs.append(_drop(d)-_drop(b))
        dci=_ci(diffs)
        ev=mean([_nliq(d) for d in recs.values()]) if recs else 0
        sh=mean([_liqsh(d) for d in recs.values()]) if recs else 0
        if c:
            cis=f"[{c[1]:+.3f},{c[2]:+.3f}]" if c[3]>=2 else "n=1"
            dtxt=f"{dci[0]:+.3f}" if dci else "—"
            print(f"{L:>5.2g} {len(drops):>3} {c[0]:>+11.3f} {cis:>22} {dtxt:>13} {ev:>7.1f} {sh:>7.0f}")
            rows.append((L,c[0],c[1],c[2],len(drops)))
        else:
            print(f"{L:>5.2g}   (no clean runs yet)")

    # find the biggest level-to-level depth drop (candidate jump)
    if len(rows)>=2:
        jumps=[(rows[i+1][0], rows[i][1]-rows[i+1][1]) for i in range(len(rows)-1)]
        jl,jd=max(jumps,key=lambda x:x[1])
        print(f"\nbiggest depth jump: at L={jl:g} (Δdepth {jd:+.3f} vs previous level)"
              f"  {'<-- candidate self-stabilise→self-destruct point' if jd>0.05 else '(no sharp jump)'}")

    # plot
    if len(rows)>=2:
        try:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            xs=[r[0] for r in rows]; ys=[r[1] for r in rows]
            lo=[r[1]-r[2] for r in rows]; hi=[r[3]-r[1] for r in rows]
            fig,ax=plt.subplots(figsize=(7,5))
            ax.errorbar(xs,ys,yerr=[lo,hi],marker="o",capsize=5,lw=2,color="#c33")
            ax.set_xlabel("leverage strength  (L center; higher = shallower trigger)")
            ax.set_ylabel("mean crash depth (trough/pre − 1)")
            ax.set_title(f"Critical-point sweep (m={args.m:g}, social ON, temp=0)\n"
                         "self-stabilise → self-destruct?")
            if len(rows)>=2:
                jl=max([(rows[i+1][0],rows[i][1]-rows[i+1][1]) for i in range(len(rows)-1)],key=lambda x:x[1])[0]
                ax.axvline(jl,ls="--",color="gray",alpha=.6,label=f"biggest jump @L={jl:g}")
                ax.legend()
            out=args.plot or os.path.join(args.out,f"critsweep_m{args.m:g}.png")
            plt.tight_layout(); plt.savefig(out,dpi=130)
            print(f"figure -> {out}")
        except Exception as e:
            print(f"plot skipped: {e}")


if __name__=="__main__":
    main()
