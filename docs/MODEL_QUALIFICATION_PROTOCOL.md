# Model Qualification Protocol

Phase 1.2A freezes an offline qualification protocol before any external model
is called. It is an engineering gate and a set of relative behavioral
diagnostics, not a test with one correct order and not evidence that one model
creates more realistic market agents.

## Frozen identity

| Component | Version / SHA-256 |
|---|---|
| Protocol | `1.1` / `2e88eca2e86b3a63cfdd6f251533aac3b90c3a27d7a9906328d83a81fc717925` |
| Observation payload protocol | `1.0` (fixture bytes unchanged) |
| Fixture set | `95109438f101ea1251520b3deb71fed9b96b097d2c9b89c1a6b73e16294aaf34` |
| Rubric | `1.1` / `a007938d7ac97465be4bd03e736843408e7bf6c1da7d7048babd9a94ffe458fc` |
| Visibility contract | `1.0` / `bfbd1c4b2fda8aa543d76038bdc8f6c17f9bcb7fd734ac665cfce292fc5a549c` |
| Case identity | `1.0` |
| Public output | `1.1` |

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
- `qualification/visibility_contract.json`

Protocol/rubric 1.0 was never used for an external Provider. The sealing check
therefore superseded it with 1.1 before real sampling. Observation JSON and all
eight fixture `input_hash` values were left byte-for-byte unchanged, so the
fixture-set hash remains the same. Case ids change because their registered
protocol version changes; the matrix remains the same six Personas by eight
source observations.

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
prompt; the in-process fake uses the existing real-agent prompt. The versioned
visibility contract records the important projection differences:

- both variants receive round, price tape, news/no-news, cash, shares and the
  bounded memory;
- Mock receives numeric `fundamental_value`; the real User Prompt does not;
- eligible real prompts receive individual neighbour sentiment and
  `public_take`, while Mock receives only aggregate social sentiment;
- Persona social gating still applies. In particular, `quant_arb` has zero
  effective social weight and receives no feed in either variant;
- fixture id, hashes, invisible-field deny-list, rubric, expected answers,
  future prices and private rationale never enter either Prompt.

This is an audit contract, not a silent Observation change. Each public case
may record only the prompt hash/variant, source fixture hash and visibility
contract hash; full Prompt remains private.

## Provider guard

The frozen Phase 1.2A protocol accepts these offline execution paths:

- `mock`: the existing deterministic in-process MockLLM;
- `fake_test_provider`: a deterministic qualification-only test double that is
  not registered with the production LLM factory.

Phase 1.2B-CX1 additionally registers experimental `codex_exec`. It remains
blocked unless an explicit model, real-usage confirmation, bounded case count
and one worker satisfy the managed guard; counts above one need matching second
confirmation. Dry-run is allowed without constructing the Provider and defaults
to one selected case. `anthropic`, `openai`, `auto`, and every unknown provider
remain rejected before construction. Rejection produces a failed managed
manifest with zero Provider calls and `network_access=false`.

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
contrarian comparisons, Persona distinctiveness, and a same-model
Persona-collapse indicator. `fundamental_anchor_score` is explicitly
`status=not_scored`, `reason=fundamental_anchor_not_visible`, and `score=null`.
Its raw action/sentiment evidence remains visible, but is not coerced to zero
and “did not buy” is not interpreted as lack of fundamental anchoring. A future
numeric fundamental signal requires a separate, versioned scientific
experiment; it is not added by this sealing patch.

Every rubric metric now declares model-input and evaluator dependencies.
Protocol loading fails closed when an active metric depends on an input that is
never/model-dependently visible, or when the visibility contract does not allow
that field for scoring. A deliberately `not_scored` metric must include a
machine-readable reason.

## Privacy

Only explicit `public_take`, sentiment, action, quantity, limit price, hashes,
and diagnostic flags enter public case output. Full prompts, raw responses,
and private rationale are written only to `0600` private files. A missing
`public_take` remains empty: reasoning is never substituted for it. Public
errors do not contain a full prompt or raw response.

Phase 1.2A performed no external model call. Phase 1.2B-CX1 implements the
experimental CodexExec adapter and explicit subset/real-use guards, but uses
fake executables only and performs no real Codex task. CodexExec does not
replace MiniMax or any other Provider by default; see
[CODEX_QUALIFICATION_RUNBOOK.md](CODEX_QUALIFICATION_RUNBOOK.md).
