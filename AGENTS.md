# Project purpose

This repository is a research-grade agent-based market simulator.
It is not a live brokerage or real-money trading system.

# Sources of truth

- Executable code, tests, run manifests, and raw results are the primary source of truth.
- Research reports describe intent and historical interpretation; verify them against code.
- Never silently change scientific semantics to match documentation.

# Scientific invariants

- Agent private rationale must never enter the social feed.
- Only public statements and explicitly public sentiment may propagate.
- Do not claim full determinism for real LLM providers.
- Preserve raw responses, parsed decisions, failures, configs, and honest-N.
- Every new mechanism needs a null/control or ablation path.
- Every configurable parameter must have a traceable effect path and a test.
- Do not overwrite historical runs.

# Engineering rules

- Prefer small compatible changes over rewrites.
- Keep simulation orchestration separate from Agent, market, information, and risk logic.
- Do not add production dependencies without justification.
- Preserve existing CLI and result schemas or provide an explicit migration.
- Add tests for numerical and accounting invariants.
- Use the repository's existing build, test, lint, and type-check commands.

# Out of scope unless explicitly approved

- Real-money trading
- Brokerage integration
- Reinforcement learning
- Full limit-order-book implementation
- Large orchestration frameworks
- Silent prompt/persona/default-parameter changes

# Definition of done

A change is done only when:
- relevant tests pass;
- the exact commands and results are reported;
- documentation is updated;
- scientific behavior changes are stated explicitly;
- compatibility and remaining risks are documented.
