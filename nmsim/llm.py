"""LLM interface — Task 1.

Contract (kept stable across providers):
    llm.complete(system: str, user: str) -> str
    llm.complete_batch([(system, user), ...]) -> [str, ...]   # async/batched
The returned string must contain a JSON object:
    {"side","quantity","limit_price","sentiment","rationale"}

Provides:
  - MockLLM        deterministic, no API key (default fallback)
  - AnthropicLLM   real provider (async client, batched), model via env/config
  - CachingLLM     response cache keyed on (persona, state_hash) + cost control
  - build_llm()    selects provider from config/env, wraps with caching
  - parse_order()  structured-output parsing with retry-on-failure

No secrets in code: ANTHROPIC_API_KEY etc. come from the environment.
"""
from __future__ import annotations
import os
import re
import json
import random
import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from .config import Config
from .decision_contract import (
    DecisionResponseValidity,
    validate_decision_response,
)
from .provider_attempts import (
    ProviderAttemptContext,
    ProviderAttemptContextCarrier,
    ProviderAttemptObservation,
    forward_provider_attempt_contexts,
    observe_provider_attempt,
)
from .types import Order

# Default models (overridable by env LLM_MODEL / config). Newest Claude as of
# 2026-06: Claude Fable 5 (claude-fable-5) is the most capable widely-released
# model (GA 2026-06-09); Opus 4.8 remains the flagship Opus tier. Set the agent
# default to Fable 5; cheap tier stays Haiku 4.5.
_DEFAULT_MODEL = "claude-fable-5"
_DEFAULT_CHEAP = "claude-haiku-4-5-20251001"

# Rough $/Mtok (input, output) used only for a printed estimate. Approximate.
_PRICE_PER_MTOK = {
    "claude-fable":  (10.0, 50.0),
    "claude-mythos": (10.0, 50.0),
    "claude-opus":   (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku":  (1.0, 5.0),
}


def _price_for(model: str):
    for k, v in _PRICE_PER_MTOK.items():
        if model.startswith(k):
            return v
    return (3.0, 15.0)


def _approx_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _latency_ms(started: float) -> float:
    return round(max(0.0, (time.monotonic() - started) * 1000.0), 3)


def _optional_int(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------- parsing -------------------------------
def _field(text: str, label: str):
    m = re.search(rf"^{label}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def _num_field(text: str, label: str, default: float) -> float:
    """Parse the leading numeric token of a labeled line (tolerates trailing notes)."""
    raw = _field(text, label)
    if not raw:
        return default
    m = re.match(r"[-+]?\d*\.?\d+", raw)
    return float(m.group(0)) if m else default


def _user_last_price(user: str) -> float:
    """Current price from either prompt format — mock 'LAST_PRICE' or real
    'LATEST PRICE' (prompts.py). Used only for graceful order fallbacks."""
    m = re.search(r"^(?:LAST_PRICE|LATEST PRICE):\s*([-+]?\d*\.?\d+)", user, re.M)
    return float(m.group(1)) if m else 100.0


def _parse_order_legacy(raw: str, last_price: float) -> Order:
    """Parse the model's JSON into a normalized Order. Never raises.

    Accepts both ``action`` and legacy ``side`` for the order direction.
    ``rationale`` is always private: only an explicit ``public_take`` may be
    routed into a social Statement.
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            o = json.loads(m.group(0))
        except Exception:
            o = None
    else:
        o = None
    if not isinstance(o, dict):
        return Order(agent="", side="hold", quantity=0, limit_price=last_price,
                     sentiment=0.0, public_take="", rationale="parse-failed; holding")

    # Valid JSON: coerce EACH field defensively. A single bad/null field (the
    # model sometimes emits "sentiment": null etc.) must NOT discard the whole
    # real order and falsely stamp it 'parse-failed' (which the health gate
    # counts as endpoint corruption and deletes the run).
    def _num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    side = o.get("side") or o.get("action") or "hold"
    if side not in ("buy", "sell", "hold"):
        side = "hold"
    senti = max(-1.0, min(1.0, _num(o.get("sentiment"),
                                    {"buy": 1.0, "sell": -1.0, "hold": 0.0}[side])))
    try:
        qty = max(0, int(o.get("quantity") or 0))
    except (TypeError, ValueError):
        qty = 0
    # Privacy invariant: a legacy/private rationale is never promoted to a
    # public statement. Current Mock and real prompt schemas both provide an
    # explicit public_take, so this does not alter their normal trajectories.
    public = str(o.get("public_take") or "")[:140]
    reasoning = str(o.get("reasoning") or o.get("rationale") or "")[:240]
    return Order(agent="", side=side, quantity=qty,
                 limit_price=_num(o.get("limit_price"), last_price),
                 sentiment=senti, public_take=public, rationale=reasoning)


def parse_order_with_validity(
    raw: str,
    last_price: float,
    *,
    response_schema: str | None = None,
    direction_field: str = "action",
) -> tuple[Order, DecisionResponseValidity]:
    """Parse under legacy coercion or one explicit strict response schema."""

    if response_schema is None:
        order = _parse_order_legacy(raw, last_price)
        failed = order["rationale"] == "parse-failed; holding"
        return order, DecisionResponseValidity(
            None,
            None,
            not failed,
            "invalid_or_missing_json_object" if failed else None,
        )
    value, validity = validate_decision_response(
        raw,
        schema_version=response_schema,
        direction_field=direction_field,
    )
    if value is None:
        fallback_markers = [
            marker
            for marker in ("api-error", "parse-retries-exhausted")
            if marker in raw
        ]
        fallback_suffix = (
            "; " + ";".join(fallback_markers)
            if fallback_markers
            else ""
        )
        return (
            Order(
                agent="",
                side="hold",
                quantity=0,
                limit_price=last_price,
                sentiment=0.0,
                public_take="",
                rationale="strict-schema-failed:{}; holding{}".format(
                    validity.error_code, fallback_suffix
                ),
            ),
            validity,
        )
    action = value[direction_field]
    return (
        Order(
            agent="",
            side=action,
            quantity=value["quantity"],
            limit_price=float(value["limit_price"]),
            sentiment=float(value["sentiment"]),
            public_take=value["public_take"][:140],
            rationale=value["reasoning"][:240],
        ),
        validity,
    )


def parse_order(
    raw: str,
    last_price: float,
    *,
    response_schema: str | None = None,
    direction_field: str = "action",
) -> Order:
    """Parse one order; strict semantics are opt-in and legacy remains exact."""

    order, _validity = parse_order_with_validity(
        raw,
        last_price,
        response_schema=response_schema,
        direction_field=direction_field,
    )
    return order


def _is_parse_failure(
    raw: str,
    *,
    response_schema: str | None = None,
    direction_field: str = "action",
) -> bool:
    # Share the parser's notion of failure so the retry-on-parse-failure gate and
    # parse_order cannot diverge (only truly non-JSON output is a parse failure).
    _order, validity = parse_order_with_validity(
        raw,
        100.0,
        response_schema=response_schema,
        direction_field=direction_field,
    )
    return not validity.valid


# ------------------------------- MockLLM -------------------------------
class MockLLM:
    """Deterministic stand-in so the loop runs with no API key.

    Reads crude news sentiment + recent trend + (Phase 3) the social digest,
    then maps to an order using each persona's mock-parameters. A real LLM
    ignores the mock params and reasons from the persona text instead.
    """
    POS = ("beat", "beats", "surge", "soar", "record", "jump", "positive", "strong",
           "raise", "raised", "growth", "win", "upgrade", "rebound", "rally")
    NEG = ("miss", "missed", "cut", "crash", "plunge", "negative", "weak", "loss",
           "fraud", "warning", "slump", "downgrade", "sharply", "disappoint")
    kind = "mock"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def _sentiment(self, text):
        t = (text or "").lower()
        return sum(t.count(w) for w in self.POS) - sum(t.count(w) for w in self.NEG)

    def complete(self, system: str, user: str) -> str:
        m = json.loads(_field(system, "MOCK_PARAMS") or "{}")
        last = _num_field(user, "LAST_PRICE", 100.0)
        fundamental = _num_field(user, "FUNDAMENTAL", 100.0)
        trend = _num_field(user, "TREND", 0.0)
        senti = self._sentiment(_field(user, "NEWS"))
        social = _num_field(user, "SOCIAL_SENTIMENT", 0.0)   # Phase 3 digest mean
        w_social = _num_field(user, "SOCIAL_WEIGHT", 0.0)

        fair = fundamental + m.get("news_value", 0.0) * senti
        own = (m.get("w_value", 0.0) * (fair - last) / last
               + m.get("w_trend", 0.0) * trend
               + m.get("w_senti", 0.0) * senti
               + m.get("bias", 0.0))
        # social contagion: blend own view with what neighbors are saying
        signal = (1 - w_social) * own + w_social * social * m.get("w_social", 1.0)
        signal += self.rng.uniform(-m.get("noise", 0.0), m.get("noise", 0.0))

        stance = max(-1.0, min(1.0, signal * 5))
        if abs(signal) < m.get("inertia", 0.01):
            return json.dumps({"side": "hold", "quantity": 0, "limit_price": round(last, 2),
                               "sentiment": round(stance, 3),
                               "public_take": "Sitting tight for now.",
                               "reasoning": "signal below my action threshold; holding."})
        side = "buy" if signal > 0 else "sell"
        qty = max(1, int(round(min(abs(signal), 0.6) * m.get("size", 20))))
        aggro = m.get("aggro", 0.01)
        edge = aggro * (1 if side == "buy" else -1)
        limit = round(last * (1 + edge + 0.5 * max(-0.6, min(0.6, signal)) * aggro), 2)
        take = (("Loading up, this is going higher." if stance > 0.5 else "Adding here.")
                if side == "buy" else
                ("Get out, this is breaking down." if stance < -0.5 else "Trimming risk."))
        return json.dumps({"side": side, "quantity": qty, "limit_price": limit,
                           "sentiment": round(stance, 3), "public_take": take,
                           "reasoning": f"news={senti}, trend={trend:+.2%}, social={social:+.2f}, "
                                        f"signal={signal:+.3f} -> {side} {qty}@{limit}"})

    async def acomplete(self, system: str, user: str) -> str:
        return self.complete(system, user)

    def complete_batch(self, prompts):
        return [self.complete(s, u) for s, u in prompts]


# ------------------------------ AnthropicLLM ------------------------------
# Sampling params (temperature/top_p/top_k) are REMOVED on Claude Fable 5 and
# Opus 4.7/4.8 — sending them returns a 400. Gate by model prefix.
_NO_SAMPLING_PREFIXES = ("claude-fable", "claude-mythos",
                         "claude-opus-4-7", "claude-opus-4-8")


def _sampling_kwargs(model: str, temperature: float) -> dict:
    if any(model.startswith(p) for p in _NO_SAMPLING_PREFIXES):
        return {}   # model uses its own default sampling (naturally stochastic)
    return {"temperature": temperature}


class AnthropicLLM(ProviderAttemptContextCarrier):
    """Real provider using the async Anthropic client for batched calls."""
    kind = "anthropic"

    def __init__(self, cfg: Config, tracker: "CostTracker"):
        from anthropic import AsyncAnthropic, Anthropic  # lazy import
        self._init_provider_attempt_contexts()
        self._async = AsyncAnthropic()
        self._sync = Anthropic()
        self.model = (os.getenv("LLM_MODEL") or cfg.model
                      or (os.getenv("LLM_CHEAP_MODEL") or cfg.cheap_model or _DEFAULT_CHEAP
                          if cfg.use_cheap_model else _DEFAULT_MODEL))
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.decision_response_schema = cfg.decision_response_schema
        self.tracker = tracker

    def complete(self, system: str, user: str) -> str:
        context = self._take_provider_attempt_contexts(1)[0]
        started = time.monotonic()
        try:
            msg = self._sync.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                **_sampling_kwargs(self.model, self.temperature),
                system=system, messages=[{"role": "user", "content": user}])
            self.tracker.add(self.model, msg.usage.input_tokens, msg.usage.output_tokens)
            text = msg.content[0].text
            parse_failed = _is_parse_failure(
                text,
                response_schema=getattr(self, "decision_response_schema", None),
                direction_field="action",
            )
        except Exception as error:
            observe_provider_attempt(
                context,
                ProviderAttemptObservation(
                    attempt_index=1,
                    max_attempts=1,
                    provider=self.kind,
                    model=self.model,
                    attempted_system=system,
                    attempted_user=user,
                    trigger="initial",
                    outcome="provider_exception",
                    exception=error,
                    latency_ms=_latency_ms(started),
                    will_retry=False,
                ),
            )
            raise
        observe_provider_attempt(
            context,
            ProviderAttemptObservation(
                attempt_index=1,
                max_attempts=1,
                provider=self.kind,
                model=self.model,
                attempted_system=system,
                attempted_user=user,
                trigger="initial",
                outcome=(
                    "response_parse_failed" if parse_failed else "response_parseable"
                ),
                reported_model=getattr(msg, "model", None),
                response_text=text,
                latency_ms=_latency_ms(started),
                prompt_tokens=_optional_int(getattr(msg.usage, "input_tokens", None)),
                completion_tokens=_optional_int(
                    getattr(msg.usage, "output_tokens", None)
                ),
                will_retry=False,
            ),
        )
        return text

    async def acomplete(self, system: str, user: str, retries: int = 2) -> str:
        context = self._take_provider_attempt_contexts(1)[0]
        return await self._acomplete_with_context(
            system, user, retries=retries, context=context
        )

    async def _acomplete_with_context(
        self,
        system: str,
        user: str,
        *,
        retries: int,
        context: ProviderAttemptContext | None,
    ) -> str:
        last_price = _user_last_price(user)
        trigger = "initial"
        for attempt in range(retries + 1):
            attempted_user = user
            started = time.monotonic()
            try:
                msg = await self._async.messages.create(
                    model=self.model, max_tokens=self.max_tokens,
                    **_sampling_kwargs(self.model, self.temperature),
                    system=system, messages=[{"role": "user", "content": user}])
                self.tracker.add(self.model, msg.usage.input_tokens, msg.usage.output_tokens)
                text = msg.content[0].text
                parse_failed = _is_parse_failure(
                    text,
                    response_schema=getattr(self, "decision_response_schema", None),
                    direction_field="action",
                )
            except Exception as error:
                will_retry = attempt < retries
                observe_provider_attempt(
                    context,
                    ProviderAttemptObservation(
                        attempt_index=attempt + 1,
                        max_attempts=retries + 1,
                        provider=self.kind,
                        model=self.model,
                        attempted_system=system,
                        attempted_user=attempted_user,
                        trigger=trigger,
                        outcome="provider_exception",
                        exception=error,
                        latency_ms=_latency_ms(started),
                        will_retry=will_retry,
                    ),
                )
                if attempt == retries:
                    return json.dumps({"side": "hold", "quantity": 0,
                                       "limit_price": last_price, "sentiment": 0.0,
                                       "rationale": "api-error; holding"})
                await asyncio.sleep(0.5 * (attempt + 1))
                trigger = "provider_exception"
                continue
            will_retry = parse_failed and attempt < retries
            observe_provider_attempt(
                context,
                ProviderAttemptObservation(
                    attempt_index=attempt + 1,
                    max_attempts=retries + 1,
                    provider=self.kind,
                    model=self.model,
                    attempted_system=system,
                    attempted_user=attempted_user,
                    trigger=trigger,
                    outcome=(
                        "response_parse_failed"
                        if parse_failed
                        else "response_parseable"
                    ),
                    reported_model=getattr(msg, "model", None),
                    response_text=text,
                    latency_ms=_latency_ms(started),
                    prompt_tokens=_optional_int(
                        getattr(msg.usage, "input_tokens", None)
                    ),
                    completion_tokens=_optional_int(
                        getattr(msg.usage, "output_tokens", None)
                    ),
                    will_retry=will_retry,
                ),
            )
            if not parse_failed:
                return text
            # Preserve the existing cumulative retry nudge exactly.
            user = user + "\n\nREMINDER: reply with ONLY the JSON object, no prose."
            trigger = "parse_failure"
        return json.dumps({"side": "hold", "quantity": 0, "limit_price": last_price,
                           "sentiment": 0.0, "rationale": "parse-retries-exhausted; holding"})

    def complete_batch(self, prompts):
        prompt_list = list(prompts)
        contexts = self._take_provider_attempt_contexts(len(prompt_list))
        async def _gather():
            return await asyncio.gather(
                *[
                    self._acomplete_with_context(s, u, retries=2, context=context)
                    for (s, u), context in zip(prompt_list, contexts)
                ]
            )
        return asyncio.run(_gather())


# ------------------------------ OpenAILLM ------------------------------
class OpenAILLM(ProviderAttemptContextCarrier):
    """OpenAI-compatible provider (e.g. a local vLLM / minimax endpoint).

    Uses the `openai` SDK against a custom base_url; the async/batched path uses
    AsyncOpenAI. Keeps the same .complete(system, user) -> str contract.
    """
    kind = "openai"

    def __init__(self, cfg: Config, tracker: "CostTracker"):
        import httpx                                # openai dependency
        from openai import AsyncOpenAI, OpenAI      # lazy import
        self._init_provider_attempt_contexts()
        base_url = os.getenv("OPENAI_BASE_URL") or cfg.openai_base_url
        api_key = os.getenv("OPENAI_API_KEY") or cfg.openai_api_key
        # trust_env=False -> hit the endpoint directly, ignoring any ambient
        # HTTP(S)/SOCKS proxy (correct for a local/internal base_url).
        # Bound the connection pool per process. The default (max_connections=100)
        # let multiple worker subprocesses open ~281 sockets and hang the vLLM
        # server. A burst is ~one population (~30) of concurrent calls per run, so
        # cap at 40: enough headroom for one run, but workers*40 stays well under
        # the level that hung it (benchmark: a single client is healthy to 96+).
        _limits = httpx.Limits(max_connections=40, max_keepalive_connections=40)
        client_options = {"base_url": base_url, "api_key": api_key}
        if cfg.provider_sdk_max_retries is not None:
            client_options["max_retries"] = cfg.provider_sdk_max_retries
        self._async = AsyncOpenAI(
            **client_options,
            http_client=httpx.AsyncClient(trust_env=False, limits=_limits),
        )
        self._sync = OpenAI(
            **client_options,
            http_client=httpx.Client(trust_env=False, limits=_limits),
        )
        self.model = os.getenv("LLM_MODEL") or cfg.model or cfg.openai_model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.decision_response_schema = cfg.decision_response_schema
        self.provider_sdk_max_retries = cfg.provider_sdk_max_retries
        self.tracker = tracker

    def _track(self, resp, system: str, user: str, text: str):
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None)
        out_tok = getattr(usage, "completion_tokens", None)
        if in_tok is None:                       # server may omit usage
            in_tok = _approx_tokens(system) + _approx_tokens(user)
        if out_tok is None:
            out_tok = _approx_tokens(text)
        self.tracker.add(self.model, in_tok, out_tok)

    def complete(self, system: str, user: str) -> str:
        context = self._take_provider_attempt_contexts(1)[0]
        started = time.monotonic()
        try:
            resp = self._sync.chat.completions.create(
                model=self.model, max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            text = resp.choices[0].message.content or ""
            self._track(resp, system, user, text)
            parse_failed = _is_parse_failure(
                text,
                response_schema=getattr(self, "decision_response_schema", None),
                direction_field="action",
            )
        except Exception as error:
            observe_provider_attempt(
                context,
                ProviderAttemptObservation(
                    attempt_index=1,
                    max_attempts=1,
                    provider=self.kind,
                    model=self.model,
                    attempted_system=system,
                    attempted_user=user,
                    trigger="initial",
                    outcome="provider_exception",
                    exception=error,
                    latency_ms=_latency_ms(started),
                    will_retry=False,
                ),
            )
            raise
        usage = getattr(resp, "usage", None)
        observe_provider_attempt(
            context,
            ProviderAttemptObservation(
                attempt_index=1,
                max_attempts=1,
                provider=self.kind,
                model=self.model,
                attempted_system=system,
                attempted_user=user,
                trigger="initial",
                outcome=(
                    "response_parse_failed" if parse_failed else "response_parseable"
                ),
                reported_model=getattr(resp, "model", None),
                response_text=text,
                latency_ms=_latency_ms(started),
                prompt_tokens=_optional_int(getattr(usage, "prompt_tokens", None)),
                completion_tokens=_optional_int(
                    getattr(usage, "completion_tokens", None)
                ),
                will_retry=False,
            ),
        )
        return text

    async def acomplete(self, system: str, user: str, retries: int = 2) -> str:
        context = self._take_provider_attempt_contexts(1)[0]
        return await self._acomplete_with_context(
            system, user, retries=retries, context=context
        )

    async def _acomplete_with_context(
        self,
        system: str,
        user: str,
        *,
        retries: int,
        context: ProviderAttemptContext | None,
    ) -> str:
        last_price = _user_last_price(user)
        trigger = "initial"
        for attempt in range(retries + 1):
            attempted_user = user
            started = time.monotonic()
            try:
                resp = await self._async.chat.completions.create(
                    model=self.model, max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                text = resp.choices[0].message.content or ""
                self._track(resp, system, user, text)
                parse_failed = _is_parse_failure(
                    text,
                    response_schema=getattr(self, "decision_response_schema", None),
                    direction_field="action",
                )
            except Exception as error:
                will_retry = attempt < retries
                observe_provider_attempt(
                    context,
                    ProviderAttemptObservation(
                        attempt_index=attempt + 1,
                        max_attempts=retries + 1,
                        provider=self.kind,
                        model=self.model,
                        attempted_system=system,
                        attempted_user=attempted_user,
                        trigger=trigger,
                        outcome="provider_exception",
                        exception=error,
                        latency_ms=_latency_ms(started),
                        will_retry=will_retry,
                    ),
                )
                if attempt == retries:
                    return json.dumps({"side": "hold", "quantity": 0,
                                       "limit_price": last_price, "sentiment": 0.0,
                                       "public_take": "", "rationale": "api-error; holding"})
                await asyncio.sleep(0.5 * (attempt + 1))
                trigger = "provider_exception"
                continue
            will_retry = parse_failed and attempt < retries
            usage = getattr(resp, "usage", None)
            observe_provider_attempt(
                context,
                ProviderAttemptObservation(
                    attempt_index=attempt + 1,
                    max_attempts=retries + 1,
                    provider=self.kind,
                    model=self.model,
                    attempted_system=system,
                    attempted_user=attempted_user,
                    trigger=trigger,
                    outcome=(
                        "response_parse_failed"
                        if parse_failed
                        else "response_parseable"
                    ),
                    reported_model=getattr(resp, "model", None),
                    response_text=text,
                    latency_ms=_latency_ms(started),
                    prompt_tokens=_optional_int(
                        getattr(usage, "prompt_tokens", None)
                    ),
                    completion_tokens=_optional_int(
                        getattr(usage, "completion_tokens", None)
                    ),
                    will_retry=will_retry,
                ),
            )
            if not parse_failed:
                return text
            # Preserve the existing cumulative retry nudge exactly.
            user = user + "\n\nREMINDER: reply with ONLY the JSON object, no prose."
            trigger = "parse_failure"
        return json.dumps({"side": "hold", "quantity": 0, "limit_price": last_price,
                           "sentiment": 0.0, "public_take": "",
                           "rationale": "parse-retries-exhausted; holding"})

    def complete_batch(self, prompts):
        prompt_list = list(prompts)
        contexts = self._take_provider_attempt_contexts(len(prompt_list))
        async def _gather():
            return await asyncio.gather(
                *[
                    self._acomplete_with_context(s, u, retries=2, context=context)
                    for (s, u), context in zip(prompt_list, contexts)
                ]
            )
        return asyncio.run(_gather())


# ------------------------------ caching ------------------------------
@dataclass
class CostTracker:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0

    def add(self, model: str, in_tok: int, out_tok: int):
        pin, pout = _price_for(model)
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cost_usd += in_tok / 1e6 * pin + out_tok / 1e6 * pout
        self.calls += 1

    def report(self) -> str:
        return (f"LLM calls: {self.calls} (cache hits: {self.cache_hits}) | "
                f"tokens in/out: {self.input_tokens}/{self.output_tokens} | "
                f"est. cost: ${self.cost_usd:.4f}")


class CachingLLM(ProviderAttemptContextCarrier):
    """Wraps any LLM with a response cache keyed on (persona, state_hash).

    The cache makes runs reproducible and cheap: identical agent states in
    identical situations reuse the prior completion instead of re-calling.
    """
    def __init__(self, inner, tracker: CostTracker, enabled: bool = True):
        self._init_provider_attempt_contexts()
        self.inner = inner
        self.tracker = tracker
        self.enabled = enabled
        self.kind = getattr(inner, "kind", "mock")
        self.model = getattr(inner, "model", None)   # expose the served model id for provenance
        self._cache: dict[str, str] = {}

    @staticmethod
    def _key(system: str, user: str) -> str:
        persona = _field(system, "PERSONA") or ""
        state_hash = hashlib.sha1((system + "\x00" + user).encode()).hexdigest()[:16]
        return f"{hashlib.sha1(persona.encode()).hexdigest()[:8]}:{state_hash}"

    def complete(self, system: str, user: str) -> str:
        context = self._take_provider_attempt_contexts(1)[0]
        if not self.enabled:
            forward_provider_attempt_contexts(self.inner, [context])
            return self.inner.complete(system, user)
        k = self._key(system, user)
        if k in self._cache:
            self.tracker.cache_hits += 1
            return self._cache[k]
        forward_provider_attempt_contexts(self.inner, [context])
        out = self.inner.complete(system, user)
        self._cache[k] = out
        return out

    def complete_batch(self, prompts):
        prompt_list = list(prompts)
        contexts = self._take_provider_attempt_contexts(len(prompt_list))
        if not self.enabled:
            forward_provider_attempt_contexts(self.inner, contexts)
            return self.inner.complete_batch(prompt_list)
        results: list[str | None] = [None] * len(prompt_list)
        misses, miss_idx, miss_contexts = [], [], []
        for i, (s, u) in enumerate(prompt_list):
            k = self._key(s, u)
            if k in self._cache:
                self.tracker.cache_hits += 1
                results[i] = self._cache[k]
            else:
                misses.append((s, u))
                miss_idx.append(i)
                miss_contexts.append(contexts[i])
        if misses:
            forward_provider_attempt_contexts(self.inner, miss_contexts)
            fresh = self.inner.complete_batch(misses)
            for j, out in zip(miss_idx, fresh):
                results[j] = out
                self._cache[self._key(*prompt_list[j])] = out
        return results  # type: ignore[return-value]


# ------------------------------ factory ------------------------------
def build_llm(cfg: Config):
    """Return (llm, tracker). Selects provider from config/env.

    provider=auto -> anthropic if ANTHROPIC_API_KEY is set, else mock.
    provider=openai -> OpenAI-compatible endpoint (base_url/api_key from config/env).
    Swap providers by setting LLM_PROVIDER or cfg.provider.
    """
    tracker = CostTracker()
    provider = (os.getenv("LLM_PROVIDER") or cfg.provider).lower()
    if provider == "auto":
        provider = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "mock"

    if provider == "anthropic":
        try:
            inner = AnthropicLLM(cfg, tracker)
        except Exception as e:  # missing sdk / key -> graceful fallback
            print(f"[llm] anthropic unavailable ({e}); falling back to MockLLM")
            inner = MockLLM(seed=cfg.seed)
    elif provider == "openai":
        try:
            inner = OpenAILLM(cfg, tracker)
        except Exception as e:  # missing sdk / unreachable endpoint -> fallback
            print(f"[llm] openai-compatible endpoint unavailable ({e}); "
                  f"falling back to MockLLM")
            inner = MockLLM(seed=cfg.seed)
    else:
        inner = MockLLM(seed=cfg.seed)

    return CachingLLM(inner, tracker, enabled=cfg.cache_enabled), tracker
