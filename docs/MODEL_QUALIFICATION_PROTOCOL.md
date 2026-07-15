# Model Qualification Protocol

Phase 1.2A freezes an offline qualification protocol before any external model
is called. It is an engineering gate and a set of relative behavioral
diagnostics, not a test with one correct order and not evidence that one model
creates more realistic market agents.

## Frozen identity

| Component | Version / SHA-256 |
|---|---|
| Protocol | `1.0` / `f13942987b4801187a7f02533a4676d95e934690dc3a8ed170b890480a6b4388` |
| Fixture set | `95109438f101ea1251520b3deb71fed9b96b097d2c9b89c1a6b73e16294aaf34` |
| Rubric | `1.0` / `0bf96644169a67f5ac10308720670f8afefb2bd94b655550f856da62dff1ecf7` |
| Case identity | `1.0` |
| Public output | `1.0` |

The fixture-set hash sorts fixtures by `fixture_id`, so JSON list order does
not affect identity. Each fixture also carries an `input_hash` calculated from
its canonical JSON payload excluding the hash field itself. A changed fixture
payload changes its input hash and the set hash. Rubric edits require a new
version and hash; results from a future external model must not be used to
silently tune the frozen rubric.

Source files are:

- `qualification/protocol.json`
- `qualification/observations.json`
- `qualification/rubric.json`

## Case matrix

The six existing Personas are crossed with eight observations, producing 48
stable case identities:

1. `negative_news_price_unchanged`
2. `price_crash_no_news`
3. `unanimous_neighbor_panic`
4. `conflicting_neighbor_views`
5. `neutral_placebo_news`
6. `insufficient_cash`
7. `insufficient_inventory`
8. `deep_discount_to_fundamental`

Each fixture records its round, visible price state, visible news, visible
public social feed, cash, shares, memory, fundamental value, and the fields
that are intentionally invisible. It never contains future prices, another
Agent's private rationale, a private margin reference book, the expected
answer, or the rubric.

The prompt builder is not changed. Mock uses the existing structured mock
prompt; the in-process fake uses the existing real-agent prompt. A known
boundary must be resolved before Phase 1.2B: the current real `build_user`
template does not display the `fundamental_value` argument that
`Agent.build_prompt` receives, while the current Mock prompt does. The fixture
records the value, but no Phase 1.2A change silently adds it to the real User
Prompt. Therefore the real-provider fundamental-anchor diagnostic must not be
interpreted until that visibility contract is explicitly reviewed and, if
changed, versioned.

## Provider guard

Phase 1.2A accepts only:

- `mock`: the existing deterministic in-process MockLLM;
- `fake_test_provider`: a deterministic qualification-only test double that is
  not registered with the production LLM factory.

`anthropic`, `openai`, `auto`, and every unknown provider are rejected at
`provider_setup` before provider construction. Rejection produces a failed
managed manifest with zero Provider calls and `network_access=false`. There is
no override in this phase.

Provider capability snapshots are descriptive provenance, not model-quality
scores. In particular, a real provider must not be described as deterministic
merely because temperature is zero.

## Commands

Inspect the frozen plan without constructing a Provider:

```bash
python3 -m experiments.model_qualification \
  --provider mock \
  --dry-run \
  --out /tmp/nmsim-phase12a-qualification-dry
```

Run all 48 cases with MockLLM:

```bash
python3 -m experiments.model_qualification \
  --provider mock \
  --out /tmp/nmsim-phase12a-qualification-mock
```

Dry-run creates a managed run and `dry_run_summary.json`, but constructs no
Provider, attempts no logical request, performs no network access, and records
all 48 cases as planned/skipped. A real qualification writes:

- `run_manifest.json` and public/private event streams;
- `case_results.jsonl`, containing public decisions and diagnostic flags;
- `qualification_summary.json`, containing aggregate public metrics;
- `private_case_records.jsonl`, mode `0600`, containing full prompts, raw
  responses, and parsed private rationale.

No qualification run creates a price path, market chart, fill ledger, or
simulation replicate.

## Accounting

`run_kind` is `model_qualification`. For a successful 48-case Mock run:

- `qualification_cases.planned/attempted/completed = 48/48/48`;
- `llm_logical_requests.completed = 48`;
- `agent_decisions.completed = 48`;
- `response_sources.provider = 48`;
- Provider-interface calls succeeded = 48;
- `simulation_runs.planned/completed = 0/0`;
- `rounds.planned/completed = 0/0`;
- `honest_n_cases = 48` and `honest_n_runs = 0`.

Mock counts as a Provider-interface response source but has
`network_access=false`. A qualification case is never a simulation replicate.

## Evaluation

Engineering metrics report schema and parser success, validation and fallback
rates, public/private leakage, invalid actions or quantities, visible
cash/inventory constraint violations, missing `public_take`, Provider failures,
latency when measured, response source, and honest case count. Token usage is
`null` when the Provider does not expose it.

Behavioral diagnostics report action distributions globally, by Persona, and
by fixture before reporting relative indicators. These include
sentiment/action consistency, Persona tendency, social/news/price responses,
contrarian and fundamental-anchor comparisons, Persona distinctiveness, and a
same-model Persona-collapse indicator. Ambiguous comparisons may be
`not_scored`; none forces one action as the correct answer.

## Privacy

Only explicit `public_take`, sentiment, action, quantity, limit price, hashes,
and diagnostic flags enter public case output. Full prompts, raw responses,
and private rationale are written only to `0600` private files. A missing
`public_take` remains empty: reasoning is never substituted for it. Public
errors do not contain a full prompt or raw response.

Phase 1.2A performs no external model call. CodexExec remains a possible future
experimental Provider and does not replace MiniMax or any other Provider by
default.
