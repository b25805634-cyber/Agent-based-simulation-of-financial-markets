"""Phase 3 — the contagion channel (THE CORE).

Agents see each other's recent sentiment + short rationale, not just price.
The social structure is configurable:
  - feed     : every agent sees the same shared feed of recent statements
  - network  : each agent sees a subset of neighbors; topology is a parameter
               (fully_connected / random / scale_free)

Also instruments propagation: per-round sentiment distribution, stance-flip
counts, and a cascade-size metric tracking how a seeded story spreads.
"""
from __future__ import annotations
import random
from dataclasses import dataclass, field
from statistics import mean, pstdev

from .types import Statement


# ------------------------------ topology ------------------------------
def _wire_base(adj, base: list[str], topology: str, n_neighbors: int,
               rng: random.Random):
    """Wire a base topology among the given peer nodes (in place)."""
    nb = len(base)
    if nb <= 1:
        return
    if topology == "fully_connected":
        for a in base:
            for b in base:
                if a != b:
                    adj[a].add(b)
    elif topology == "random":
        p = min(1.0, n_neighbors / max(1, nb - 1))
        for i in range(nb):
            for j in range(i + 1, nb):
                if rng.random() < p:
                    adj[base[i]].add(base[j])
                    adj[base[j]].add(base[i])
    elif topology == "scale_free":
        # Barabasi-Albert-style preferential attachment among peers.
        m = max(1, min(n_neighbors, nb - 1))
        repeated = list(base[:m])
        for nm in base[m:]:
            chosen: list[str] = []          # ordered, not a set: set iteration of
            while len(chosen) < m and repeated:   # strings is PYTHONHASHSEED-salted,
                pick = rng.choice(repeated)       # which broke cross-process repro.
                if pick not in chosen:
                    chosen.append(pick)
            for t in chosen:
                adj[nm].add(t)
                adj[t].add(nm)
            repeated.extend(chosen)
            repeated.extend([nm] * m)
    else:
        raise ValueError(f"unknown topology: {topology}")


def build_network(names: list[str], topology: str, n_neighbors: int,
                  seed: int, hub_names: list[str] | None = None) -> dict[str, list[str]]:
    """Return adjacency: agent -> list of neighbor names it can observe.

    The peer topology is built among NON-hub nodes; each hub (e.g. the
    influencer) is then wired to every other agent, so the hub is strictly the
    highest-degree node — its influence is its reach. (In fully_connected the
    graph is complete and degrees tie by definition.)
    """
    rng = random.Random(seed)
    hubs = [h for h in (hub_names or []) if h in names]
    nonhubs = [nm for nm in names if nm not in hubs]
    n = len(names)
    adj: dict[str, set[str]] = {nm: set() for nm in names}
    if n <= 1:
        return {nm: [] for nm in names}

    _wire_base(adj, nonhubs if hubs else names, topology, n_neighbors, rng)

    # Each hub reaches everyone -> maximal degree (n-1).
    for h in hubs:
        for b in names:
            if b != h:
                adj[h].add(b)
                adj[b].add(h)

    return {nm: sorted(adj[nm]) for nm in names}


def degrees(adj: dict[str, list[str]]) -> dict[str, int]:
    return {nm: len(neigh) for nm, neigh in adj.items()}


# ------------------------------ digests ------------------------------
def neighbor_feed(agent_name: str, statements: dict[str, Statement],
                  adj: dict[str, list[str]], social_mode: str,
                  digest_size: int, rng: random.Random):
    """Return (feed, mean_sentiment) of what this agent can see.

    feed is a list of (sentiment, public_take) tuples — the contagion input the
    decision prompt consumes. The loudest (highest |sentiment|) neighbors win the
    limited attention slots.
    """
    if social_mode == "feed":
        visible = [s for nm, s in statements.items() if nm != agent_name]
    else:
        visible = [statements[nm] for nm in adj.get(agent_name, []) if nm in statements]

    k = max(0, digest_size)                       # guard: digest_size<=0 -> no feed
    if not visible or k == 0:                      # (a negative slice silently took
        return [], 0.0                             #  the WRONG neighbors before)
    rng.shuffle(visible)
    visible = sorted(visible, key=lambda s: abs(s["sentiment"]), reverse=True)[:k]
    feed = [(s["sentiment"], s["text"]) for s in visible]
    return feed, mean(s["sentiment"] for s in visible)


# ------------------------------ seeding ------------------------------
def choose_seed_agents(llm_names: list[str], fraction: float, seed: int,
                       must_include: list[str] | None = None) -> set[str]:
    """The subset a headline reaches first, so a story can originate + spread.

    `must_include` (e.g. the influencer — the natural spark) is always seeded.
    """
    rng = random.Random(seed + 999)
    forced = {nm for nm in (must_include or []) if nm in llm_names}
    k = max(1, round(fraction * len(llm_names)))
    pool = [nm for nm in llm_names if nm not in forced]
    extra = rng.sample(pool, min(max(0, k - len(forced)), len(pool)))
    return forced | set(extra)


# ------------------------------ metrics ------------------------------
@dataclass
class PropagationMetrics:
    rounds: list[int] = field(default_factory=list)
    mean_sentiment: list[float] = field(default_factory=list)
    std_sentiment: list[float] = field(default_factory=list)
    pos_share: list[float] = field(default_factory=list)
    neg_share: list[float] = field(default_factory=list)
    flips: list[int] = field(default_factory=list)        # stance sign-changes this round
    cascade_size: list[float] = field(default_factory=list)  # frac aligned w/ seed sign
    per_agent_sentiment: list[dict] = field(default_factory=list)

    # min |sentiment| to count as a genuine (conviction-backed) stance, so weak
    # price-noise drift isn't mistaken for narrative spread.
    CONVICTION = 0.3

    def record(self, round_i: int, sentiments: dict[str, float],
               flips: int, seed_sign: float, non_seed: set | None = None):
        vals = list(sentiments.values())
        self.rounds.append(round_i)
        self.mean_sentiment.append(round(mean(vals), 4) if vals else 0.0)
        self.std_sentiment.append(round(pstdev(vals), 4) if len(vals) > 1 else 0.0)
        self.pos_share.append(round(sum(v > 0.05 for v in vals) / len(vals), 4) if vals else 0.0)
        self.neg_share.append(round(sum(v < -0.05 for v in vals) / len(vals), 4) if vals else 0.0)
        self.flips.append(flips)
        # Cascade size = fraction of NON-SEED agents who took a conviction-backed
        # stance aligned with the seeded story. Isolates genuine spread beyond the
        # initially-informed subset (price-noise drift doesn't count).
        pool = {k: v for k, v in sentiments.items()
                if non_seed is None or k in non_seed}
        if seed_sign == 0 or not pool:
            self.cascade_size.append(0.0)
        else:
            aligned = sum((v * seed_sign) > self.CONVICTION for v in pool.values())
            self.cascade_size.append(round(aligned / len(pool), 4))
        self.per_agent_sentiment.append({k: round(v, 4) for k, v in sentiments.items()})

    def peak_cascade(self) -> float:
        return max(self.cascade_size, default=0.0)

    def cascaded(self, threshold: float = 0.6) -> bool:
        """Did the seeded story spread to a majority of agents at some point?"""
        return self.peak_cascade() >= threshold
