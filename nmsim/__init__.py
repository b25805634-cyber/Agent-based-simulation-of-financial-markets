"""Narrative Market Sim — an agent-based sandbox for studying how narratives /
sentiment propagate through a market and turn into price dynamics.

This is a behavioral-finance research sandbox, NOT a price forecaster. Realism
comes from contagion mechanics + prompt-defined agent behavior. See BUILD_BRIEF.md.

Package layout:
  config.py      central Config (all knobs, seeded, reproducible)
  types.py       shared typed structures (Order, Statement)
  llm.py         Task 1: provider-agnostic LLM (mock/anthropic), caching, batching, cost
  agents.py      persona-driven LLM traders + noise agents
  market.py      impact-based clearing (NOT a microstructure LOB)
  contagion.py   Phase 3: social channel (feed/network topologies) + propagation metrics
  sim.py         the round loop tying it together
  validation.py  Phase 4: behavioral stylized facts + real-episode comparison
  run.py         CLI entry point (CSV + plots + cost print)
"""
from .config import Config

__all__ = ["Config"]
