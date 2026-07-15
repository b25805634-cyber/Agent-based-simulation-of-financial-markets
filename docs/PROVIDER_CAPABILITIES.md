# Provider Capability Contract

Phase 1.2A introduces a declarative description of the provider adapters that
already exist in the repository. It does not add a provider, construct a
client, read credentials, or make a network request. The implementation is
[`nmsim/provider_capabilities.py`](../nmsim/provider_capabilities.py), and the
capability schema version is `1.0`.

A capability record describes what the current nmsim adapter exposes. It is not
a claim about every feature offered by an upstream service. In particular,
Provider Capability is not a model-quality score, and a more capable language
model is not automatically a more realistic market Agent.

## Registered provider identities

| Resolved provider id | Current implementation evidence | Transport | Network expected | Determinism claim |
|---|---|---|---:|---|
| `mock` | `MockLLM.kind = "mock"` in `nmsim/llm.py` | In-process Python | No | Deterministic only for the same local seed and call order |
| `anthropic` | `AnthropicLLM.kind = "anthropic"` in `nmsim/llm.py` | Anthropic SDK over HTTPS | Yes | None |
| `openai` | `OpenAILLM.kind = "openai"` in `nmsim/llm.py` | OpenAI-compatible SDK over HTTP(S) | Yes | None |
| `fake_test_provider` | Phase 1.2 qualification protocol test double only | In-process test double | No | Defined by its fixture implementation |

`openai` is the existing OpenAI-compatible transport family. The configured
MiniMax/vLLM endpoint uses this adapter; MiniMax is not a separate provider
implementation in the current code.

`fake_test_provider` is marked `experimental=true` and
`implementation_scope=qualification_test_only`. It is not accepted by
`nmsim.llm.build_llm`, cannot be selected for a normal simulation, and exists
only so the qualification protocol can be tested without a network.

`auto` is deliberately absent. It is a selection policy: the current factory
resolves it to `anthropic` when the relevant environment credential is present
and to `mock` otherwise. Capability lookup requires the resolved provider id
and fails closed for `auto` or any unregistered id. This keeps a newly added
provider from silently inheriting another provider's claims.

## Schema fields

Every record contains:

- `provider_id`
- `transport_type`
- `external_network_expected`
- `authentication_mode`
- `supports_batch`
- `supports_async`
- `supports_temperature`
- `supports_seed`
- `supports_structured_output`
- `supports_usage_metadata`
- `supports_provider_response_id`
- `supports_record_replay`
- `supports_cache`
- `tool_access`
- `deterministic_claim`
- `recommended_concurrency`
- `experimental`
- `capability_schema_version`

The record also carries conservative detail for temperature behavior, usage
metadata behavior, and implementation scope.

## Conservative interpretation

### Mock

The Mock adapter provides synchronous batch completion and an async-compatible
single-completion method. It emits JSON directly, uses a local `random.Random`
instance, and accepts a simulator seed. Its determinism claim is limited to an
unchanged implementation with the same seed and the same call order; it is not
a general claim that every concurrent schedule is interchangeable.

### Anthropic

The Anthropic adapter constructs the synchronous and asynchronous SDK clients,
uses async gathering for batches, and consumes input/output usage metadata.
Temperature is conditional: `_sampling_kwargs` deliberately omits it for the
model prefixes that reject sampling arguments. The adapter does not send a
seed, does not use a provider-enforced structured-output API, does not expose a
provider response id, and does not enable tools. It makes no determinism claim,
including when the configured temperature is zero.

### OpenAI-compatible

The OpenAI-compatible adapter constructs synchronous and asynchronous clients
for a configured base URL and uses async gathering for batches. It sends
temperature, does not send a seed or `response_format`, and does not enable
tools. It consumes usage metadata when the endpoint supplies it and otherwise
uses the existing local token estimate. The current client pool is bounded to
40 connections per process, so the capability record uses 40 as its documented
adapter-level concurrency recommendation. It does not expose provider response
ids and makes no determinism claim.

`external_network_expected=true` means that an adapter uses a network transport;
it does not assert that a configured endpoint is on the public Internet.

Record/Replay and cache support describe the existing nmsim wrappers. They are
not assertions that the upstream provider offers these features natively.

## Safe snapshots

`provider_capability_snapshot()` produces a stable JSON-compatible snapshot and
hash. An optional endpoint is represented only by:

- whether it was configured;
- its scheme;
- a SHA-256 identity after userinfo and credential-shaped query values have
  been removed or redacted;
- flags indicating whether userinfo or sensitive query values were redacted.

The raw endpoint, hostname, userinfo, API key, Authorization value, cookie,
environment credential, and model response are not included. The capability
snapshot can therefore be recorded in a managed experiment summary without
turning the registry into a credential source.

Changing a capability declaration should produce a different snapshot hash so
that experiment summaries can report which reviewed declaration they used.
Capability changes do not enter the scientific component fingerprint and do
not, by themselves, invalidate a Phase 1.1 schema 1.2 recording. Strict Replay
continues to use its existing scientific, configuration, Prompt, Persona,
request, and schema contracts.

## Phase 1.2A boundary

This phase performs no real-provider qualification and no external request. It
does not change `build_llm`, Provider fallback behavior, defaults, Prompts,
Personas, parsing, simulation behavior, recording schema 1.2, or the scientific
fingerprint. A capability declaration only makes the existing adapter boundary
auditable; it does not authorize use of that adapter.
