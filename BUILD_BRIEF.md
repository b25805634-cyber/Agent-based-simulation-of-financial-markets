# Build brief for Claude Code — Narrative Market Sim (Direction B)

> Paste this into Claude Code (or save as `BUILD_BRIEF.md` and tell it: "read BUILD_BRIEF.md and do Task 1"). Do the tasks **in order**, verify each before moving on.

## 1. What we're building (and what we are NOT)

An **agent-based market simulation** to study how **narratives / sentiment propagate** through a market and turn into price dynamics — bubbles, panics, herding cascades. This is a **research / behavioral-finance** project.

Critical framing — keep these in mind throughout:
- This is a **sandbox to understand mechanisms**, not a market price forecaster. Do not optimize anything for "predicting the next price."
- The realism is supposed to come from **(a) the contagion mechanics and (b) prompt-defined agent behavior**, NOT from clever per-agent code or from making the market microstructure fancy.
- The hard, non-negotiable part that decides whether any of this is real is **validation** (Phase 4). Build toward it.

## 2. Current state — read this first

A Phase 1 scaffold exists: **`narrative_market_sim.py`** (single file). Read it before doing anything. It already has:
- discrete rounds; **impact-based clearing** (price moves with net order-flow imbalance; `limit_price` recorded but not enforced) — this is intentional, see guardrails;
- 6 LLM-persona agents + noise agents for liquidity;
- a **provider-agnostic LLM interface** behind `build_llm()`, defaulting to a deterministic `MockLLM`;
- per-agent `memory` and full **reasoning-trace logging**;
- news injection at a configurable round; CSV + price-path plot output.

If the file is not in the repo, reconstruct an equivalent from the architecture below, then proceed.

First refactor: split the single file into a clean typed package — `llm.py`, `agents.py`, `market.py`, `contagion.py` (new), `sim.py`, `run.py`, plus a `config.py` (or YAML). No secrets in code (env vars). Seeded + reproducible everywhere.

## 3. Architecture (keep this shape)

- **Background layer** — cheap noise / simple-rule agents providing baseline liquidity and order flow.
- **Focal LLM agents** — the narrative-reactive population. The heart of the project.
- **Market** — simple impact-based clearing at a coarse timescale (minutes–days). NOT a microstructure LOB.
- **Information environment** — news injection; the main experimental knob.
- **Logging** — price, volume, positions, sentiment, AND every agent's natural-language rationale (this is our interpretability — never drop it).

## 4. Tasks (in order)

### Task 1 — Wire in a real LLM
- Replace `MockLLM` via `build_llm()` with a real provider (Anthropic by default), model + provider configurable via env vars. Keep the `.complete(system, user) -> str` contract.
- Add robust structured-output parsing with a retry-on-parse-failure, and **async/batched** calls (you call N agents per round — this is the cost/latency bottleneck).
- Cost controls: a cheaper-model option, response caching keyed on `(persona, state_hash)`, a `max_llm_agents` knob, and a printed per-run token/cost estimate.
- **Acceptance:** runs end-to-end with a real model, ~20 rounds, 8–12 LLM agents; every agent's rationale logged; cost printed; reproducible given a seed (cache + temperature handled).

### Task 2 — Phase 3: the contagion channel (THE CORE)
- Give agents a **social/narrative channel**: they see each other's recent sentiment + short rationale, not just the price. Make it a configurable structure:
  - a shared **feed** of recent agent statements, OR
  - a **network** where each agent sees a subset of neighbors — topology is a parameter (fully connected / random graph / scale-free).
- Each agent's decision prompt now includes a digest of what neighbors are "saying," plus a tunable **weight on social signal vs. own view**.
- Seed news into a **subset** of agents first (not everyone), so a story can originate and spread.
- Instrument propagation: per-round sentiment distribution, count of agents who flipped stance, and a **cascade-size** metric.
- **Acceptance:** under some parameter settings a single seeded headline **cascades** through the population and moves price; under others it **fizzles**. Output: a sentiment-spread-over-time plot + the price path.

### Task 3 — Validation instrumentation (sets up Phase 4)
- Compute and save: return distribution (kurtosis, tail), volatility clustering (ACF of |returns|), overreaction/reversal, and the narrative-contagion metric from Task 2.
- Add a path to feed a **real timestamped news timeline** and compare the simulated reaction shape (drop depth, speed, recovery) to a reference series.
- **Acceptance:** given a real episode's news + price series, the harness reports how well the sim matches its qualitative dynamics + behavioral stylized facts.

## 5. Guardrails — do NOT

- **No full microstructure LOB.** Timescale is minutes–days; impact-based clearing is intentional.
- **Do not pull in a "trading desk" framework** (e.g., TradingAgents). That's a single portfolio's decision system, not a market-emergence sim — wrong tool.
- **Do not turn agents into reward-maximizing optimizers / RL.** That's a different project (equilibrium / market impact). This one is about behavioral & narrative dynamics.
- **Keep behavior prompt-driven**, not hard-coded. Personas live in natural-language prompts; the `mock_params` are only for the mock.
- **Never drop the reasoning-trace logging.**

## 6. Prompt design (provided separately)

The **persona library** and the **agent decision prompt template** are being designed separately (that's the human + strategy partner's job, not Claude Code's). For now, keep the existing `persona` / decision-prompt structure and leave clear extension points; a refined prompt set will be dropped in. Do not invent elaborate personas yourself — wire the plumbing so prompts are easy to swap.
