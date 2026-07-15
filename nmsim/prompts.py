"""
prompts.py — persona library + decision-prompt builder for the LLM agents
=========================================================================
The intellectual core of Direction B: the agents' BEHAVIOR is these prompts.

Design intent
-------------
The cast is deliberately balanced so that a narrative can either spread or
fizzle depending on social coupling, news severity, and the population mix:
  - cascade FUEL ........ retail_crowd, fomo_momentum   (highly news/crowd-driven)
  - cascade SPARK ....... influencer_amplifier          (loud, high-influence node)
  - cascade DAMPENERS ... value_institution, contrarian_fund (fade overreactions)
  - narrative-IMMUNE .... quant_arb                     (trades price/flow only)

Each persona sets four dials (0..1) that summarise its character. They are
documentation, AND Claude Code can use them to (a) set each agent's mechanical
`social_weight` in the contagion config so prompt-described susceptibility
matches the mechanism, and (b) parameterise Phase-5 population sweeps:
  narrative_susceptibility : how strongly news/headlines move them
  social_susceptibility    : tendency to move WITH the crowd (herd). NOTE:
                             contrarians read the crowd but move AGAINST it, so
                             their value here is LOW; the "fade" logic lives in
                             the persona text, not this number.
  fundamental_anchor       : how much they anchor on fair value
  emotional_reactivity     : amplitude of fear/greed swings

Every round each agent outputs `sentiment` (-1..1) + a short `public_take`.
That pair — not the order — is what propagates through the social channel and
drives contagion. Keep `reasoning` private (logs only).

Wiring (hand to Claude Code)
----------------------------
- In the real-LLM agent path, build the system prompt with `build_system(persona)`
  and the per-round user prompt with `build_user(...)`, then parse the JSON.
- Make `influencer_amplifier` a HIGH-degree node in the social network (its
  influence is its reach, set in the contagion topology — not in this file).
- Optionally set each agent's social_weight ∝ its `social_susceptibility`.
- The MockLLM path is unaffected; it keeps using its own mock_params.
"""

from __future__ import annotations

PERSONAS = [
    {
        "id": "retail_crowd",
        "role": "Sentiment-driven retail investor",
        "narrative_susceptibility": 0.9,
        "social_susceptibility": 0.9,
        "fundamental_anchor": 0.10,
        "emotional_reactivity": 0.9,
        "persona": (
            "You are a regular retail investor trading your own savings. You don't run "
            "fundamental models — you go on headlines, gut feeling, and what everyone "
            "around you seems to be doing. When things are running you feel FOMO; when "
            "they're falling you feel fear. The latest big move feels like the new normal, "
            "and a scary headline can make you panic-sell, often near the lows. You trade "
            "small, your attention is short, and your emotions run hot. You decide mainly "
            "from the mood of the news and the crowd plus the recent price direction; "
            "fundamentals barely register."
        ),
    },
    {
        "id": "fomo_momentum",
        "role": "Trend-chasing momentum trader",
        "narrative_susceptibility": 0.7,
        "social_susceptibility": 0.8,
        "fundamental_anchor": 0.10,
        "emotional_reactivity": 0.6,
        "persona": (
            "You are a trend-chaser. You want to be on the moving train: if price is "
            "rising and the story is hot, you add; if it turns down, you cut fast. You "
            "extrapolate recent trends and lean hard into the direction of the news and "
            "the crowd. You are not a contrarian — strength attracts you, weakness repels "
            "you. You get overconfident during runs and you herd. You decide from the "
            "recent price trend, the direction of the news, and the crowd's momentum. You "
            "accelerate whatever is already happening."
        ),
    },
    {
        "id": "value_institution",
        "role": "Long-horizon value / fundamental institution",
        "narrative_susceptibility": 0.2,
        "social_susceptibility": 0.10,
        "fundamental_anchor": 0.95,
        "emotional_reactivity": 0.10,
        "persona": (
            "You manage a long-horizon fund and anchor everything on your estimate of "
            "fundamental value. You are deeply skeptical of headlines and crowd mood: most "
            "news is noise, and most panics are opportunities. You move slowly and in size, "
            "and you fade overreactions — when the crowd dumps far below fair value, you "
            "buy. You rarely care what other traders are saying and your emotions stay "
            "flat. You decide by asking whether price is far from fundamental value, "
            "adjusting only modestly if the news genuinely changes the fundamentals. You "
            "are a stabilising force."
        ),
    },
    {
        "id": "quant_arb",
        "role": "Quantitative / arbitrage desk (narrative-blind)",
        "narrative_susceptibility": 0.0,
        "social_susceptibility": 0.0,
        "fundamental_anchor": 0.20,
        "emotional_reactivity": 0.05,
        "persona": (
            "You run a systematic desk. You do not read narratives and you do not care "
            "about sentiment — you trade price and order flow: you mean-revert when price "
            "dislocates from its short-term level, and you provide liquidity. News and "
            "chatter are irrelevant to you except through the price they move. You are fast "
            "and unemotional and you ignore what others are saying entirely. You decide "
            "from the short-term price dislocation alone. You are narrative-immune "
            "liquidity."
        ),
    },
    {
        "id": "influencer_amplifier",
        "role": "Loud market commentator / influencer",
        "narrative_susceptibility": 0.9,
        "social_susceptibility": 0.4,
        "fundamental_anchor": 0.10,
        "emotional_reactivity": 0.8,
        "persona": (
            "You are a high-visibility market commentator with a large following. Your own "
            "trades are modest, but your voice is loud: you broadcast confident, punchy, "
            "dramatic takes that many others see and react to. You react fast and "
            "theatrically to news — 'this changes everything', 'this is the top' — and you "
            "amplify whatever story is forming. You have little fundamental anchoring and "
            "you care about being early and being heard. You decide from the most dramatic "
            "narrative reading of the news and recent price, and you say it loudly. You are "
            "often the spark that starts a cascade."
        ),
    },
    {
        "id": "contrarian_fund",
        "role": "Contrarian hedge fund",
        "narrative_susceptibility": 0.5,
        "social_susceptibility": 0.15,
        "fundamental_anchor": 0.70,
        "emotional_reactivity": 0.2,
        "persona": (
            "You run a contrarian hedge fund that hunts crowd extremes. When sentiment "
            "turns euphoric or panicked and price overshoots, you bet against it. You read "
            "the crowd and the narrative carefully precisely so you can fade them: strong "
            "consensus is a sell signal, capitulation is a buy signal. You are disciplined "
            "and unemotional, and you size up exactly when the crowd is most extreme. You "
            "decide by judging how stretched sentiment and price are versus fundamentals, "
            "then leaning against the extreme. You are the active force that can turn a "
            "cascade around."
        ),
    },
]

PERSONAS_BY_ID = {p["id"]: p for p in PERSONAS}

# Literal-brace JSON schema block kept out of the f-strings below.
_SCHEMA_BLOCK = """RESPOND WITH ONLY THIS JSON OBJECT (no other text, no markdown):
{
  "reasoning": "<1-3 sentences of private thinking; NOT shown to others>",
  "sentiment": <number from -1.0 (very bearish) to 1.0 (very bullish)>,
  "public_take": "<one short, punchy sentence other traders will see, in your voice>",
  "action": "buy" | "sell" | "hold",
  "quantity": <integer number of shares; 0 if you hold>,
  "limit_price": <your reservation price as a number>
}"""


def build_system(persona: dict) -> str:
    """System prompt: role framing + the persona + rules + output schema."""
    head = (
        "You are one trader among many in a simulated stock market. Stay fully in "
        "character at all times.\n\n"
        "WHO YOU ARE\n"
        f"{persona['persona']}\n\n"
        "HOW THIS MARKET WORKS\n"
        "- Each round you see the latest price, any news that has reached you, and what "
        "some other traders are publicly saying.\n"
        "- You state your current read and place exactly one order.\n"
        "- Others may see your public take, just as you see theirs. Your stated view can "
        "sway the market's mood; theirs can sway you — but only to the degree your "
        "character actually cares.\n\n"
        "DECIDE IN CHARACTER\n"
        "- Weigh price, news, the crowd, and fundamentals the way YOUR character would — "
        "not as a neutral, perfectly rational agent.\n"
        "- Let your biases, emotions, blind spots, and susceptibilities show. "
        "Overreaction or inconsistency is fine when it fits who you are.\n\n"
    )
    return head + _SCHEMA_BLOCK


def build_user(round_i: int,
               price: float,
               recent: list[float],
               news: str | None,
               social_feed: list[tuple[float, str]] | None,
               shares: int,
               cash: float,
               memory: list[str] | None,
               mic_note: str = "") -> str:
    """Per-round observation. `social_feed` is a sample of neighbours'
    (sentiment, public_take) — the contagion input. `news` is set only for
    agents the news has actually reached. `mic_note` (ablation) tells the
    influencer whether its public statements are heard."""
    trend = 0.0 if len(recent) < 2 else (recent[-1] - recent[0]) / recent[0]
    recent_str = ", ".join(f"{p:.2f}" for p in recent) if recent else f"{price:.2f}"

    if news:
        news_block = f"FRESH NEWS THAT REACHED YOU:\n  {news}"
    else:
        news_block = "FRESH NEWS THAT REACHED YOU:\n  None this round."

    if social_feed:
        lines = "\n".join(f'  ({s:+.2f}) "{t}"' for s, t in social_feed)
        feed_block = f"WHAT SOME OTHER TRADERS ARE PUBLICLY SAYING:\n{lines}"
    else:
        feed_block = "WHAT SOME OTHER TRADERS ARE PUBLICLY SAYING:\n  The floor is quiet."

    notes = " | ".join(memory[-3:]) if memory else "none"
    mic_block = f"\n{mic_note}\n" if mic_note else ""

    return (
        f"ROUND: {round_i}\n"
        f"LATEST PRICE: {price:.2f}\n"
        f"RECENT PRICES (oldest to newest): {recent_str}\n"
        f"PRICE TREND over that window: {trend:+.2%}\n\n"
        f"{news_block}\n\n"
        f"{feed_block}\n"
        f"{mic_block}\n"
        f"YOUR POSITION: {shares} shares, {cash:.2f} cash\n"
        f"YOUR RECENT NOTES: {notes}\n\n"
        "Decide your order, in character."
    )


if __name__ == "__main__":
    # eyeball one persona's prompts
    p = PERSONAS_BY_ID["retail_crowd"]
    print("=== SYSTEM ===\n" + build_system(p))
    print("\n=== USER ===\n" + build_user(
        round_i=12, price=108.63, recent=[120.4, 116.7, 108.6],
        news="The company missed Q2 earnings by a wide margin and cut guidance sharply.",
        social_feed=[(-0.9, "Get out now, this is going to zero"),
                     (-0.6, "Guidance cut is brutal, I'm trimming"),
                     (0.3, "Overdone — this is a dip to buy")],
        shares=50, cash=10000.0,
        memory=["r11: price 116.66, held — waiting to see"]))
