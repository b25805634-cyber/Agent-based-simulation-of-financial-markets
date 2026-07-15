"""
Narrative Market Sim - Phase 1 scaffold
=======================================
A minimal agent-based market where heterogeneous traders react to a news item
and a price *emerges* from their collective orders.

Phase 1 deliberately stays minimal:
  - discrete rounds; simple impact-based clearing where price moves with the
    net order-flow imbalance (NOT a microstructure LOB)
  - a handful of LLM-persona agents + cheap noise agents for liquidity
  - one news item injected at a chosen round
  - per-agent memory + reasoning traces logged

It runs out of the box with a deterministic MockLLM (no API key needed).
To get *real* agents, swap MockLLM -> your provider inside build_llm().

NOT in Phase 1 yet (later phases):
  - Phase 3: a contagion channel (agents seeing each other's narratives) <- the core idea
  - Phase 4: validation against a real episode + behavioral stylized facts
"""

from __future__ import annotations
import os, json, random, re
from dataclasses import dataclass, field

# ------------------------------- Config -------------------------------
SEED              = 7
N_ROUNDS          = 24
NEWS_ROUND        = 12
NEWS_TEXT         = ("BREAKING: the company missed Q2 earnings by a wide margin "
                     "and cut full-year guidance sharply.")
INITIAL_PRICE     = 100.0
FUNDAMENTAL_VALUE = 100.0     # agents' prior anchor; news should shift perceived value
RECENT_WINDOW     = 5
KAPPA             = 0.12      # price-impact strength: max per-round move at full imbalance
OUT               = "/mnt/user-data/outputs"

# --------------------------- LLM interface ----------------------------
# Contract: an LLM is any object with .complete(system: str, user: str) -> str
# The returned string must contain a JSON object:
#   {"side":"buy"|"sell"|"hold","quantity":int,"limit_price":float,"rationale":str}

def _field(text, label):
    m = re.search(rf"^{label}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None

class MockLLM:
    """Deterministic stand-in so the loop runs with no API key.
    Reads crude news sentiment + recent trend, then maps to an order using each
    persona's mock-parameters. A real LLM ignores the mock params and reasons
    from the persona text instead."""
    POS = ("beat","beats","surge","soar","record","jump","positive","strong",
           "raise","raised","growth","win","upgrade","rebound","rally")
    NEG = ("miss","missed","cut","crash","plunge","negative","weak","loss",
           "fraud","warning","slump","downgrade","sharply","disappoint")

    def __init__(self, seed=0):
        self.rng = random.Random(seed)

    def _sentiment(self, text):
        t = (text or "").lower()
        return sum(t.count(w) for w in self.POS) - sum(t.count(w) for w in self.NEG)

    def complete(self, system, user):
        m     = json.loads(_field(system, "MOCK_PARAMS") or "{}")
        last  = float(_field(user, "LAST_PRICE") or INITIAL_PRICE)
        trend = float(_field(user, "TREND") or 0.0)
        senti = self._sentiment(_field(user, "NEWS"))
        fair  = FUNDAMENTAL_VALUE + m.get("news_value", 0.0) * senti
        signal = (m.get("w_value", 0.0) * (fair - last) / last
                  + m.get("w_trend", 0.0) * trend
                  + m.get("w_senti", 0.0) * senti
                  + m.get("bias", 0.0)
                  + self.rng.uniform(-m.get("noise", 0.0), m.get("noise", 0.0)))
        if abs(signal) < m.get("inertia", 0.01):
            return json.dumps({"side": "hold", "quantity": 0, "limit_price": round(last, 2),
                               "rationale": "signal below my action threshold; holding."})
        side  = "buy" if signal > 0 else "sell"
        qty   = max(1, int(round(min(abs(signal), 0.6) * m.get("size", 20))))
        aggro = m.get("aggro", 0.01)
        edge  = aggro * (1 if side == "buy" else -1)
        limit = round(last * (1 + edge + 0.5 * max(-0.6, min(0.6, signal)) * aggro), 2)
        return json.dumps({"side": side, "quantity": qty, "limit_price": limit,
                           "rationale": f"sentiment={senti}, trend={trend:+.2%}, "
                                        f"signal={signal:+.3f} -> {side} {qty}@{limit}"})

def build_llm():
    """Return the LLM used by all agents. SWAP THIS for a real provider.

    Example (Anthropic):
        from anthropic import Anthropic
        client = Anthropic()
        class ClaudeLLM:
            def complete(self, system, user):
                msg = client.messages.create(
                    model="claude-sonnet-4-5", max_tokens=400,
                    system=system, messages=[{"role": "user", "content": user}])
                return msg.content[0].text
        return ClaudeLLM()
    """
    return MockLLM(seed=SEED)

# ------------------------------- Agents -------------------------------
@dataclass
class Agent:
    name: str
    persona: str          # natural-language description (used by a real LLM)
    mock_params: dict     # parameters the MockLLM uses (ignored by a real LLM)
    cash: float = 10000.0
    shares: int = 50
    memory: list = field(default_factory=list)

    def decide(self, llm, round_i, last_price, recent, news):
        trend = 0.0 if len(recent) < 2 else (recent[-1] - recent[0]) / recent[0]
        system = (
            "You are a trader in a simulated stock market.\n"
            f"PERSONA: {self.persona}\n"
            'Respond ONLY with a JSON object: '
            '{"side":"buy"|"sell"|"hold","quantity":int,"limit_price":float,"rationale":str}.\n'
            f"MOCK_PARAMS: {json.dumps(self.mock_params)}"
        )
        notes = " | ".join(self.memory[-3:]) if self.memory else "none"
        user = (
            f"ROUND: {round_i}\n"
            f"NEWS: {news or 'no fresh news'}\n"
            f"LAST_PRICE: {last_price:.2f}\n"
            f"RECENT_PRICES: {', '.join(f'{p:.2f}' for p in recent)}\n"
            f"TREND: {trend:.4f}\n"
            f"SHARES: {self.shares}\n"
            f"CASH: {self.cash:.2f}\n"
            f"YOUR_RECENT_NOTES: {notes}\n"
            "Decide your order."
        )
        order = parse_order(llm.complete(system, user), last_price)
        order["agent"] = self.name
        self.memory.append(
            f"r{round_i}: price {last_price:.2f}, did {order['side']} {order['quantity']} "
            f"- {order['rationale'][:60]}"
        )
        return order

class NoiseAgent(Agent):
    """Cheap background liquidity: random small orders around last price."""
    def decide(self, llm, round_i, last_price, recent, news):
        rng = random.Random((self.name, round_i).__hash__())
        side = rng.choice(("buy", "sell"))
        return {"agent": self.name, "side": side, "quantity": rng.randint(1, 8),
                "limit_price": round(last_price * (1 + rng.uniform(-0.03, 0.03)), 2),
                "rationale": "noise"}

def parse_order(raw, last_price):
    try:
        o = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        side = o.get("side", "hold")
        if side not in ("buy", "sell", "hold"):
            side = "hold"
        return {"side": side,
                "quantity": max(0, int(o.get("quantity", 0))),
                "limit_price": float(o.get("limit_price", last_price)),
                "rationale": str(o.get("rationale", ""))[:200]}
    except Exception:
        return {"side": "hold", "quantity": 0, "limit_price": last_price,
                "rationale": "parse-failed; holding"}

# ----------- Market: impact-based clearing (price responds to net order flow) -----------
def clear_by_pressure(orders, last_price):
    buy_q  = sum(o["quantity"] for o in orders if o["side"] == "buy")
    sell_q = sum(o["quantity"] for o in orders if o["side"] == "sell")
    depth  = buy_q + sell_q
    if depth == 0:
        return last_price, 0, {}
    net = buy_q - sell_q                      # >0 net buying, <0 net selling
    new_price = round(max(0.01, last_price * (1 + KAPPA * net / depth)), 2)
    # Phase 1: orders execute at the cleared price (a market-maker absorbs the
    # imbalance). limit_price is recorded for later LOB-based matching but is
    # not enforced here.
    fills = {}
    for o in orders:
        if o["quantity"] > 0 and o["side"] in ("buy", "sell"):
            fills[o["agent"]] = fills.get(o["agent"], 0) + (
                o["quantity"] if o["side"] == "buy" else -o["quantity"])
    return new_price, min(buy_q, sell_q), fills

# ------------------------------- Setup --------------------------------
def make_agents():
    P = [
        ("LLM-value",
         "A patient value investor. You buy when price is well below fair value and "
         "sell when far above it; you mostly ignore short-term momentum and fade "
         "overreactions to news.",
         {"w_value": 1.2, "news_value": 3, "w_trend": 0.0, "w_senti": 0.0,
          "bias": 0.0, "size": 25, "aggro": 0.008, "inertia": 0.02, "noise": 0.01}),
        ("LLM-momentum",
         "A momentum trader. You chase recent trends and lean into the direction of "
         "news; you buy strength and sell weakness.",
         {"w_value": 0.0, "news_value": 0, "w_trend": 1.8, "w_senti": 0.05,
          "bias": 0.0, "size": 25, "aggro": 0.015, "inertia": 0.012, "noise": 0.01}),
        ("LLM-contrarian",
         "A contrarian. You bet against recent moves and against the crowd's reaction "
         "to news, assuming the market overreacts.",
         {"w_value": 0.0, "news_value": 0, "w_trend": -2.0, "w_senti": -0.05,
          "bias": 0.0, "size": 20, "aggro": 0.012, "inertia": 0.015, "noise": 0.01}),
        ("LLM-panic",
         "A nervous retail trader. Bad news scares you into selling hard and fast, and "
         "you tend to follow what everyone else seems to be doing.",
         {"w_value": 0.0, "news_value": 0, "w_trend": 1.0, "w_senti": 0.12,
          "bias": 0.0, "size": 30, "aggro": 0.02, "inertia": 0.008, "noise": 0.02}),
        ("LLM-optimist",
         "A perpetual optimist. You read most news favorably and carry a standing "
         "bullish lean.",
         {"w_value": 0.3, "news_value": 1, "w_trend": 0.5, "w_senti": 0.02,
          "bias": 0.04, "size": 20, "aggro": 0.01, "inertia": 0.012, "noise": 0.01}),
        ("LLM-skeptic",
         "A cautious skeptic. You distrust hype and headlines, trade small, and often "
         "prefer to hold.",
         {"w_value": 0.5, "news_value": 3, "w_trend": 0.2, "w_senti": -0.01,
          "bias": -0.01, "size": 12, "aggro": 0.006, "inertia": 0.03, "noise": 0.01}),
    ]
    agents = [Agent(n, persona, mp) for n, persona, mp in P]
    agents += [NoiseAgent(f"noise-{i}", "random liquidity", {}) for i in range(8)]
    return agents

# ------------------------------- Run ----------------------------------
def run():
    random.seed(SEED)
    llm = build_llm()
    agents = make_agents()
    price = INITIAL_PRICE
    history = [price]
    rows, traces = [], {}
    for r in range(1, N_ROUNDS + 1):
        news = NEWS_TEXT if r == NEWS_ROUND else ""
        recent = history[-RECENT_WINDOW:]
        orders = [a.decide(llm, r, price, recent, news) for a in agents]
        new_price, vol, fills = clear_by_pressure(orders, price)
        for a in agents:
            dq = fills.get(a.name, 0)
            a.shares += dq
            a.cash   -= dq * new_price
        price = new_price
        history.append(price)
        rows.append((r, price, vol))
        traces[r] = [(o["agent"], o["side"], o["quantity"], o["limit_price"], o["rationale"])
                     for o in orders if o["agent"].startswith("LLM")]
    return history, rows, traces, agents

def main():
    os.makedirs(OUT, exist_ok=True)
    history, rows, traces, agents = run()

    with open(f"{OUT}/price_path.csv", "w") as fh:
        fh.write("round,price,volume\n")
        for r, p, v in rows:
            fh.write(f"{r},{p:.4f},{v}\n")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs = list(range(len(history)))
    plt.figure(figsize=(8, 4.5))
    plt.plot(xs, history, marker="o", ms=3, color="#1D9E75")
    plt.axvline(NEWS_ROUND, color="#D85A30", ls="--", lw=1.2)
    plt.text(NEWS_ROUND + 0.2, max(history), "  news shock", color="#D85A30", va="top")
    plt.title("Narrative market sim - Phase 1 (mock LLM): price reacting to a negative news shock")
    plt.xlabel("round"); plt.ylabel("price"); plt.grid(alpha=0.3)
    plt.tight_layout(); plt.savefig(f"{OUT}/price_path.png", dpi=130)

    pre, post = history[NEWS_ROUND], min(history[NEWS_ROUND:])
    print(f"price before news (r={NEWS_ROUND}): {pre:.2f}")
    print(f"trough after news:               {post:.2f}  ({(post/pre-1):+.1%})")
    print(f"final price (r={N_ROUNDS}):              {history[-1]:.2f}\n")
    print(f"=== heterogeneous reactions at the news round (r={NEWS_ROUND}) ===")
    for agent, side, qty, limit, why in traces[NEWS_ROUND]:
        print(f"  {agent:14s} {side:4s} {qty:3d} @ {limit:7.2f}   {why}")

if __name__ == "__main__":
    main()
