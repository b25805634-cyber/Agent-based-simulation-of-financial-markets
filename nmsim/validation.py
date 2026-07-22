"""Phase 4 instrumentation — Task 3.

Computes behavioral stylized facts from a simulated price path and compares the
simulated reaction shape to a real episode's news + price timeline.

Stylized facts:
  - return distribution: kurtosis (fat tails), tail ratio
  - volatility clustering: ACF of |returns|
  - overreaction / reversal: post-shock drop then partial recovery
  - narrative-contagion metric (peak cascade size from Phase 3)

Comparison:
  - drop depth, speed (rounds to trough), recovery fraction for sim vs. reference,
    plus a qualitative match score.

Pure-python (no scipy) so it runs anywhere numpy is available.
"""
from __future__ import annotations
import csv
import math
from dataclasses import dataclass, field, asdict
from typing import Optional


def log_returns(prices: list[float]) -> list[float]:
    out = []
    for a, b in zip(prices[:-1], prices[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def kurtosis(x: list[float]) -> float:
    """Excess kurtosis (0 for a normal distribution)."""
    n = len(x)
    if n < 4:
        return 0.0
    mu = _mean(x)
    m2 = sum((v - mu) ** 2 for v in x) / n
    m4 = sum((v - mu) ** 4 for v in x) / n
    return (m4 / m2 ** 2 - 3.0) if m2 > 0 else 0.0


def tail_ratio(x: list[float], q: float = 0.95) -> float:
    """Ratio of an extreme quantile to the std — large => fat tails."""
    if len(x) < 5:
        return 0.0
    s = sorted(abs(v) for v in x)
    idx = min(len(s) - 1, int(q * len(s)))
    sd = _std(x)
    return s[idx] / sd if sd > 0 else 0.0


def _std(x):
    if len(x) < 2:
        return 0.0
    mu = _mean(x)
    return math.sqrt(sum((v - mu) ** 2 for v in x) / len(x))


def acf(x: list[float], lags: int = 5) -> list[float]:
    """Autocorrelation function of a series at lags 1..lags."""
    n = len(x)
    if n < 3:
        return [0.0] * lags
    mu = _mean(x)
    denom = sum((v - mu) ** 2 for v in x)
    out = []
    for k in range(1, lags + 1):
        if k >= n or denom == 0:
            out.append(0.0)
            continue
        num = sum((x[t] - mu) * (x[t - k] - mu) for t in range(k, n))
        out.append(round(num / denom, 4))
    return out


def volatility_clustering(returns: list[float], lags: int = 5) -> list[float]:
    """ACF of |returns| — positive, slowly decaying => vol clustering."""
    return acf([abs(r) for r in returns], lags)


@dataclass
class ReactionShape:
    pre_price: float
    trough_price: float
    drop_depth: float        # (trough/pre - 1), negative for a drop
    speed: int               # steps from shock to trough
    recovery: float          # fraction of the drop recovered by the end
    final_price: float

    def as_dict(self):
        return asdict(self)


def reaction_shape(prices: list[float], shock_idx: int) -> ReactionShape:
    shock_idx = max(0, min(shock_idx, len(prices) - 1))
    pre = prices[shock_idx]
    after = prices[shock_idx:]
    trough = min(after)
    t_idx = after.index(trough)
    final = prices[-1]
    drop = trough / pre - 1 if pre else 0.0
    denom = pre - trough
    # 'recovery' = fraction of the crash clawed back, in [0,1]. Two ways it used to
    # blow up: (a) a tiny/no drop (placebo) -> denom~0 -> ratio explodes; (b) final
    # overshoots pre-shock -> ratio > 1 (the 2.49±5.14 garbage). Fix: leave it
    # UNDEFINED (None) when there was no genuine drop, and CLAMP to [0,1] otherwise.
    if denom > 1e-9 and drop <= -0.02:
        recovery = max(0.0, min(1.0, (final - trough) / denom))
    else:
        recovery = None
    return ReactionShape(round(pre, 4), round(trough, 4), round(drop, 4),
                         t_idx, (round(recovery, 4) if recovery is not None else None),
                         round(final, 4))


@dataclass
class StylizedFacts:
    n_returns: int
    kurtosis: float
    tail_ratio: float
    vol_clustering_acf: list
    return_acf: list
    reaction: dict
    peak_cascade: float

    def as_dict(self):
        return asdict(self)


def stylized_facts(prices: list[float], shock_idx: int,
                   peak_cascade: float = 0.0) -> StylizedFacts:
    rets = log_returns(prices)
    return StylizedFacts(
        n_returns=len(rets),
        kurtosis=round(kurtosis(rets), 4),
        tail_ratio=round(tail_ratio(rets), 4),
        vol_clustering_acf=volatility_clustering(rets),
        return_acf=acf(rets),
        reaction=reaction_shape(prices, shock_idx).as_dict(),
        peak_cascade=round(peak_cascade, 4),
    )


# ------------------------- real-episode comparison -------------------------
def load_reference(path: str):
    """Load a reference CSV (timestamp,price[,news]). Returns (prices, shock_idx).

    shock_idx = first row whose `news` column is non-empty, else argmax 1-step drop.
    """
    prices, news_flags = [], []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        pcol = cols.get("price") or cols.get("close") or (reader.fieldnames or ["price"])[-1]
        ncol = cols.get("news")
        for row in reader:
            prices.append(float(row[pcol]))
            news_flags.append(bool(row.get(ncol, "").strip()) if ncol else False)
    if any(news_flags):
        shock = news_flags.index(True)
    else:
        # biggest single-step drop
        drops = [(prices[i + 1] / prices[i] - 1) for i in range(len(prices) - 1)]
        shock = (drops.index(min(drops)) if drops else 0)
    return prices, shock


def _norm_path(prices: list[float], shock_idx: int):
    base = prices[shock_idx] if prices and prices[shock_idx] else 1.0
    return [p / base for p in prices]


# --------------------- trajectory-level distance metrics ---------------------
# These compare the WHOLE post-shock path (shock aligned at t=0), not just three
# summary points. Primary metric: RMSE of the normalized log-price path. DTW is
# a supplement that is robust to small time misalignment.

def norm_log_path(prices: list[float], shock_idx: int,
                  horizon: int | None = None) -> list[float]:
    """Normalized log-price path from the shock: log(p[shock+t] / p[shock]).

    t=0 is the shock (value 0.0). Optionally truncated to `horizon` steps.
    """
    base = prices[shock_idx] if prices and prices[shock_idx] > 0 else 1.0
    out = []
    for i in range(shock_idx, len(prices)):
        p = prices[i] if prices[i] > 0 else 1e-9
        out.append(math.log(p / base))
        if horizon is not None and len(out) >= horizon:
            break
    return out


def logprice_rmse(sim_prices, sim_shock, ref_prices, ref_shock):
    """RMSE between the two normalized log-price paths over the common horizon.

    Returns (rmse, horizon). Lower = closer. Scale is log-return units, so e.g.
    0.10 ~ being off by ~10% in price level on average across the path.
    """
    s = norm_log_path(sim_prices, sim_shock)
    r = norm_log_path(ref_prices, ref_shock)
    L = min(len(s), len(r))
    if L == 0:
        return float("nan"), 0
    se = sum((s[t] - r[t]) ** 2 for t in range(L))
    return math.sqrt(se / L), L


def dtw_distance(a: list[float], b: list[float]):
    """Classic DTW on two 1-D sequences with |.| local cost.

    Returns (total_cost, normalized_cost) where normalized = total / path_len.
    Robust to time warping (a drop that is a step early/late still matches).
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float("nan"), float("nan")
    INF = float("inf")
    prev = [INF] * (m + 1)
    prev[0] = 0.0
    # we also track path length for normalization via a parallel DP
    plen_prev = [0] * (m + 1)
    for i in range(1, n + 1):
        cur = [INF] * (m + 1)
        plen_cur = [0] * (m + 1)
        cur[0] = INF
        for j in range(1, m + 1):
            cost = abs(a[i - 1] - b[j - 1])
            # pick the min predecessor (diag / up / left)
            best, bplen = prev[j - 1], plen_prev[j - 1]      # diagonal
            if prev[j] < best:
                best, bplen = prev[j], plen_prev[j]          # up
            if cur[j - 1] < best:
                best, bplen = cur[j - 1], plen_cur[j - 1]    # left
            cur[j] = cost + best
            plen_cur[j] = bplen + 1
        prev, plen_prev = cur, plen_cur
    total = prev[m]
    path_len = max(1, plen_prev[m])
    return total, total / path_len


def compare_to_reference_prices(
    sim_prices: list[float],
    sim_shock: int,
    ref_prices: list[float],
    ref_shock: int,
) -> dict:
    """Compare simulated prices with an already validated reference path.

    PRIMARY: trajectory distance over the whole shock-aligned path
      - rmse_logprice : RMSE of normalized log-price (main score; lower = closer)
      - dtw / dtw_norm: DTW distance, robust to time misalignment
    SUPPLEMENTARY (kept, but no longer the whole story):
      - depth / speed / recovery match of the 3-point reaction shape.
    """
    sim_r = reaction_shape(sim_prices, sim_shock)
    ref_r = reaction_shape(ref_prices, ref_shock)

    rmse, horizon = logprice_rmse(sim_prices, sim_shock, ref_prices, ref_shock)
    s_log = norm_log_path(sim_prices, sim_shock)
    r_log = norm_log_path(ref_prices, ref_shock)
    dtw_raw, dtw_norm = dtw_distance(s_log, r_log)

    def rel(a, b):  # similarity in [0,1]; None when either side is undefined
        if a is None or b is None:
            return None
        denom = max(abs(a), abs(b), 1e-6)
        return max(0.0, 1.0 - abs(a - b) / denom)

    def _r(x):
        return round(x, 4) if x is not None else None

    return {
        "primary_metric": "rmse_logprice (lower is better)",
        "trajectory": {
            "rmse_logprice": round(rmse, 4),
            "dtw": round(dtw_raw, 4),
            "dtw_norm": round(dtw_norm, 4),
            "horizon": horizon,
        },
        "reaction_supplementary": {
            "sim_reaction": sim_r.as_dict(),
            "ref_reaction": ref_r.as_dict(),
            "depth_match": _r(rel(sim_r.drop_depth, ref_r.drop_depth)),
            "recovery_match": _r(rel(sim_r.recovery, ref_r.recovery)),
            "speed_match": _r(rel(
                sim_r.speed / max(1, len(sim_prices) - sim_shock),
                ref_r.speed / max(1, len(ref_prices) - ref_shock))),
        },
    }


def compare_to_reference(sim_prices: list[float], sim_shock: int,
                         ref_path: str) -> dict:
    """Compare a simulated path to a reference CSV.

    This compatibility wrapper retains the established CSV loading semantics;
    multi-event callers pass their explicitly transformed path to
    :func:`compare_to_reference_prices` instead.
    """

    ref_prices, ref_shock = load_reference(ref_path)
    return compare_to_reference_prices(
        sim_prices, sim_shock, ref_prices, ref_shock
    )
