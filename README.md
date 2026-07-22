# Narrative Market Sim

An agent-based market sandbox for studying how **narratives / sentiment
propagate** through a population and turn into price dynamics — bubbles, panics,
herding cascades. This is a **behavioral-finance research sandbox**, NOT a price
forecaster. See `BUILD_BRIEF.md` for the full framing and guardrails.

The original single-file Phase-1 scaffold (`narrative_market_sim.py`) has been
refactored into the typed `nmsim/` package and extended through Tasks 1–3.

## Package layout

| module | role |
|---|---|
| `config.py` | central `Config` — every knob, seeded & reproducible. No secrets. |
| `types.py` | shared typed structures (`Order`, `Statement`). |
| `llm.py` | **Task 1** — provider-agnostic LLM: `MockLLM` (default) + `AnthropicLLM` (async/batched), response caching keyed on `(persona, state_hash)`, retry-on-parse-failure, cost/token tracking. |
| `prompts.py` | the dropped-in persona library (6 personas with character dials) + `build_system`/`build_user` decision-prompt templates. The strategy partner's design; the real-LLM behavior comes from here. |
| `agents.py` | wires `prompts.py` into the sim: real LLM → `build_system/build_user`; MockLLM → per-id `mock_params`. Each agent's `social_weight` ∝ its persona's `social_susceptibility`. |
| `market.py` | impact-based clearing (price moves with net order-flow imbalance — NOT a microstructure LOB). |
| `contagion.py` | **Task 2** — the social channel: `feed` / `network` topologies (fully-connected / random / scale-free), neighbor digests, propagation metrics (sentiment distribution, stance flips, cascade size). |
| `validation.py` | **Task 3** — behavioral stylized facts (kurtosis, tail ratio, vol-clustering ACF, overreaction/reversal) + real-episode comparison. |
| `events.py` | versioned public/private JSONL event streams with an enforced privacy boundary. |
| `recording.py` | provider-compatible LLM Record/Replay; strict Prompt/Persona/model/order matching and no replay network fallback. |
| `provenance.py` | immutable run directories, atomic manifests, hashes, environment and honest-N. |
| `run_context.py` | formal `ManagedRunContext` lifecycle, Record/Replay wiring, completion accounting, idempotent finalization, plus the explicit no-provenance `NullRunContext`. |
| `managed_cli.py` | two-stage CLI bootstrap and safe failed-attempt provenance. |
| `entrypoints.py` | auditable registry of official, diagnostic, library and unsupported execution surfaces. |
| `result_reuse.py` | versioned child-run identity and artifact-integrity gate for formal experiment resume. |
| `provider_capabilities.py` | conservative, secret-free capability descriptions for reviewed Provider adapters; it does not construct a Provider or score model quality. |
| `codex_exec.py` | experimental local adapter for official `codex exec`; isolated subprocess, structured output, tool-event rejection, no credential-file access. |
| `sim.py` | the round loop tying it all together. |
| `run.py` | CLI entry point → CSVs, plots, cost print. |

## Quick start

```bash
# deterministic, no API key needed (MockLLM):
python -m nmsim.run --provider mock

# real model (reads ANTHROPIC_API_KEY from the env; never stored in code):
python -m nmsim.run --provider anthropic
ANTHROPIC_API_KEY=sk-... LLM_MODEL=claude-sonnet-4-6 python -m nmsim.run

# OpenAI-compatible endpoint (e.g. a local vLLM / minimax server):
python -m nmsim.run --provider openai          # uses base_url/api_key/model from config
python -m nmsim.run --provider openai --base-url http://HOST:8000/v1 --model MiniMax-M2.7
#   defaults: base_url=http://10.214.32.152:8000/v1, api_key=EMPTY, model=MiniMax-M2.7
#   verbose/reasoning models need token headroom: --max-tokens 1024 (default)

# experiments
python -m nmsim.run --topology random --social-weight 0.8
python -m nmsim.run --no-social                       # ablation: contagion off
python -m nmsim.run --reference examples/reference_episode.csv

# exact offline replay of an existing recorded run (use the same scientific config):
python -m nmsim.run --provider mock --rounds 4 --news-round 2 \
  --replay-from /tmp/nmsim-record/runs/<run-id> --out /tmp/nmsim-replay

# Phase 1.2A model-qualification plan: 6 Personas × 8 fixtures = 48 cases;
# dry-run constructs no Provider and performs no network access:
python -m experiments.model_qualification --provider mock --dry-run \
  --out /tmp/nmsim-qualification-dry

# execute the same frozen 48-case protocol with the in-process Mock only:
python -m experiments.model_qualification --provider mock \
  --out /tmp/nmsim-qualification-mock

# Wave 0 endpoint-stochasticity plan: validate the frozen 48->6 selection and
# the 6 cases x 2 temperatures x K=30 x 3 concurrency levels = 1080-call grid;
# dry-run constructs no Provider and performs no network access:
python3 -m experiments.endpoint_stochasticity --provider openai --dry-run \
  --out /tmp/nmsim-endpoint-stochasticity-dry

# execute the full grid plus the separate two-call same-seed probe offline:
python3 -m experiments.endpoint_stochasticity --provider fake_test_provider \
  --out /tmp/nmsim-endpoint-stochasticity-fake

# a real OpenAI-compatible endpoint is reachable only behind the explicit guard:
python3 -m experiments.endpoint_stochasticity --provider openai --live \
  --out /tmp/nmsim-endpoint-stochasticity-live
```

`provider=auto` (the default) uses Anthropic when `ANTHROPIC_API_KEY` is set and
falls back to the deterministic mock otherwise.

### Outputs

Every formal research run crosses `ManagedRunContext` and is written to a
unique, non-overwriting `<out>/runs/<run_id>/` directory. Only a successfully
finalized run publishes `latest` and old flat compatibility links; each flat
link points directly at the immutable run that produced it, and an existing
regular historical file is never replaced. A partial output file is not a
successful sample: check `status=finished`, `managed_run_completed=true` and
`outputs_complete=true` in the manifest.

- `price_path.csv`, `propagation.csv`, `reasoning_traces.csv`
- `stylized_facts.json` (+ `reference_comparison` when `--reference` is given)
- `sim_overview.png` (price path + sentiment-spread-over-time + cascade size)
- `config.json` (requested Config; a supplied API key is redacted)
- `run_manifest.json`, `events.jsonl`
- `private_events.jsonl`, `llm_records.jsonl` (mode `0600`; full prompts,
  raw responses and private reasoning)

The manifest also carries unitized completion for runs, rounds, Agent
decisions, logical requests, response sources, Provider-interface calls and
OpenAI/Anthropic application-level retry attempts, plus parsing. Attempt events
keep hashes and safe codes public while secret-redacted prompts/raw/error detail
remain in the `0600` private stream. Legacy top-level `honest_n` means completed
Agent decisions and is deprecated; experiment summaries use run-level
`honest_n_runs`.

Formal experiment resume is stricter than file existence. Result-reuse policy
`1.0` accepts an old child only when its managed lifecycle, scientific source
and runtime Config, Provider/model request identity, Scenario/input, seed,
population, safe paths, and every registered artifact hash all match. Driver
summaries distinguish `executed_runs`, validated `reused_runs`, candidates
examined/rejected, completed/failed runs, and `honest_n_runs`. A rejected or
manifest-free flat result is preserved and does not count as completion.
Historical flat files may still be used by an explicitly managed analysis as
hashed `legacy_unverified_input`; that is analysis input, not child-run reuse.

`experiments.aggregate_multi_event` is the Provider-free managed analyzer for
the Wave 1 N=8/K=3 variance-components pilot. Its trust anchor is one finished
`experiments.multi_event` parent manifest; it derives and rehashes that
parent's registered selection, plan, attempt ledger, and driver summary. The
selection's accepted children and missing/rejected slots exactly partition
the declared execution plan: all 144 cells for protocol-adherent live work, or
an explicitly labeled smaller mock-only engineering grid. It revalidates
managed identities and artifacts, and emits event-complete and
cross-event-intersection honest N. Mock/non-adherent output is never eligible
for a preregistered realism claim. The pilot's fixed
qualitative matching criterion is distinct from its paired social estimand and
is not confirmatory significance evidence. See
[`docs/MULTI_EVENT_PROTOCOL.md`](docs/MULTI_EVENT_PROTOCOL.md).

`experiments.model_qualification` is a managed non-market entrypoint with
`run_kind=model_qualification`. It freezes engineering checks and relative
behavioral diagnostics before any real-model sampling; it is not a
single-correct-action test, creates no price trajectory, and contributes zero
simulation `honest_n_runs`. Phase 1.2A permits only Mock and the internal Fake
test double by default. Phase 1.2B-CX1 adds experimental `codex_exec`, but a
future live qualification requires an explicit model, real-usage confirmation,
an explicit bounded case count, and one worker; dry-run constructs no Provider.
Provider capability snapshots describe adapter behavior and network/auth
boundaries; they do not establish model quality, realism, or deterministic
responses.

`experiments.endpoint_stochasticity` is a managed non-market Wave 0
diagnostic. It measures pairwise within-case raw-byte agreement and pooled
within-case sample sigma for sentiment and signed order (`+quantity` buy, `0`
hold, `-quantity` sell) across temperatures `0`/`0.3` and concurrency
`1`/`8`/`32`. The 1080 main-grid samples and separate two-call seed probe are
distinct honest-N units and contribute zero simulation `honest_n_runs`.
Prompts, raw responses and private reasoning remain in the mode-`0600`
`private_endpoint_records.jsonl`; public output contains only hashes, explicit
public parsed fields, accounting, and aggregates. Temperature zero or equal
seed-probe responses are not a determinism claim.

See [`docs/RUN_PROVENANCE.md`](docs/RUN_PROVENANCE.md) for schemas and replay,
[`docs/MANAGED_RUN_LIFECYCLE.md`](docs/MANAGED_RUN_LIFECYCLE.md) for startup and
failure handling, [`docs/ENTRYPOINTS.md`](docs/ENTRYPOINTS.md) for the supported
execution surfaces, and [`docs/COMPLETION_ACCOUNTING.md`](docs/COMPLETION_ACCOUNTING.md)
for units and honest-N. Phase 1.2A's exact boundaries are in
[`docs/RESULT_REUSE_POLICY.md`](docs/RESULT_REUSE_POLICY.md),
[`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md), and
[`docs/MODEL_QUALIFICATION_PROTOCOL.md`](docs/MODEL_QUALIFICATION_PROTOCOL.md).
Wave 0's frozen grid, artifact schemas, privacy boundary, sigma estimators, and
N/K interpretation are in
[`docs/ENDPOINT_STOCHASTICITY.md`](docs/ENDPOINT_STOCHASTICITY.md).
The experimental Codex CLI boundary and future quota-guarded pilot procedure
are documented in [`docs/CODEX_EXEC_PROVIDER.md`](docs/CODEX_EXEC_PROVIDER.md)
and [`docs/CODEX_QUALIFICATION_RUNBOOK.md`](docs/CODEX_QUALIFICATION_RUNBOOK.md).
Phase 1.2B-CX1 uses fake executables only and runs no real Codex model task.

`nmsim.sim.run_sim` remains a low-level in-memory library API. Tests and
diagnostics may explicitly use `NullRunContext`, but those results are not
provenance-complete research outputs. `--help` and `--version` do not create a
run; once a safe output root is known, later configuration, Provider setup,
Replay preflight, simulation or export failures are retained as failed managed
attempts.

## How the tasks map to acceptance

- **Task 1 — real LLM.** Swap via `build_llm()`; `AnthropicLLM` keeps the
  `.complete(system, user) -> str` contract and adds `complete_batch` (async
  `asyncio.gather`). Cost controls: `--cheap` model, on/off cache keyed on
  `(persona, state_hash)`, `max_llm_agents`, and a printed token/cost estimate.
  Fixed local seeds make the Mock path reproducible. A real provider's first
  response is not guaranteed deterministic by `temperature=0`, cache or local
  seeds; use Record/Replay to reproduce an already observed run.
- **Task 2 — contagion (the core).** Agents see neighbors' `(sentiment,
  public_take)` via a configurable `feed`/`network` structure. The headline
  reaches only a **seed subset**; non-seed agents learn it **only through the
  social channel + price tape**. The **`influencer_amplifier` is the strictly
  highest-degree node** (wired to every peer — degree n−1) and is always in the
  seed set, so it acts as the spark. Cascade size counts conviction-backed,
  non-seed alignment. Verified: strong social gain → *CASCADED* (cascade ≈
  0.75–1.0, deep crash); low gain / `--no-social` → *fizzled* (cascade ≈ 0.5,
  price barely moves).
- **Task 3 — validation.** Computes return kurtosis/tail, vol-clustering ACF of
  `|returns|`, overreaction/reversal reaction shape, and the Task-2 cascade
  metric; `--reference CSV` reports drop-depth / speed / recovery match vs a real
  episode (`timestamp,price[,news]`). See `examples/reference_episode.csv`.
  Versioned Meta/SPY multi-event inputs and the explicit opt-in public-news
  timeline loader are documented in `docs/MULTI_EVENT_PROTOCOL.md`; timeline
  injection is not enabled by the existing CLI.

## Env vars
`LLM_PROVIDER` (`mock`/`anthropic`/`openai`) · `LLM_MODEL` · `LLM_CHEAP_MODEL` ·
`ANTHROPIC_API_KEY` · `OPENAI_BASE_URL` · `OPENAI_API_KEY`
(override config; secrets only ever come from the environment).

## Local verification

The repository uses the standard library test runner and does not require a
new test framework:

```bash
PYTHONPYCACHEPREFIX=/tmp/nmsim-pycache python3 -m unittest discover -s tests -v
PYTHONHASHSEED=0 python3 -m experiments.repro_check
PYTHONPYCACHEPREFIX=/tmp/nmsim-pycache python3 -m compileall -q nmsim experiments tests
```
