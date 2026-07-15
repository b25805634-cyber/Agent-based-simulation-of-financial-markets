"""Simulation loop — ties together agents, market, and the contagion channel.

Each round:
  1. Build per-agent prompts, injecting the social digest (Phase 3) of what
     neighbors said last round.
  2. Batch the LLM calls (async under the hood) -> orders.
  3. Clear the market by net order-flow pressure -> new price + fills.
  4. Record reasoning traces (never dropped) and propagation metrics.

News is seeded into a *subset* of agents first so a story can originate and
spread through the social graph rather than hitting everyone at once.
"""
from __future__ import annotations
import hashlib
import random
from dataclasses import dataclass, field

from .config import Config
from .agents import make_agents, Agent, NoiseAgent
from .market import clear_by_pressure
from .types import Order, Statement
from . import contagion as C
from . import leverage as LV


@dataclass
class SimResult:
    history: list[float]
    rows: list[tuple]                 # (round, price, volume)
    traces: dict                      # round -> list of (agent, side, qty, limit, sentiment, rationale)
    agents: list
    metrics: C.PropagationMetrics
    adjacency: dict
    seed_agents: set
    hub_names: list
    cfg: Config
    tracker: object
    liquidations: list = field(default_factory=list)   # LiquidationEvent log (leverage layer)
    # Populated by managed entry points (nmsim.run / experiments.run_seed).
    # Direct run_sim callers remain fully backward compatible.
    run_id: str | None = None
    run_dir: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prompt_digest(system: str, user: str) -> str:
    payload = f"{len(system)}:{system}{len(user)}:{user}"
    return _digest(payload)


def run_sim(cfg: Config, llm, tracker, event_logger=None, run_id: str | None = None) -> SimResult:
    # NOTE: no module-global random.seed here — every RNG below is an explicit
    # seeded random.Random so a fixed cfg.seed fully determines the run (incl. the
    # noise agents, after the M1 fix). The digest-shuffle rng is shared across
    # agents within a round (order-coupled but seed-deterministic).
    rng = random.Random(cfg.seed + 1)
    agents = make_agents(cfg)
    LV.init_leverage(agents, cfg)      # mark leveraged agents + seed their debt (no-op if disabled)
    llm_agents = [a for a in agents if a.is_llm]
    llm_names = [a.name for a in llm_agents]
    inf_names = [a.name for a in llm_agents if a.is_influencer]
    mode = "real" if getattr(llm, "kind", "mock") != "mock" else "mock"

    # Arm C (demote): influencer loses its hub wiring + auto-seed -> ordinary node.
    hub_names = [] if cfg.demote_influencer else list(inf_names)
    force_seed = [] if cfg.demote_influencer else list(inf_names)

    adjacency = (C.build_network(llm_names, cfg.topology, cfg.n_neighbors, cfg.seed,
                                 hub_names=hub_names)
                 if cfg.social_enabled and cfg.social_mode == "network"
                 else {nm: [] for nm in llm_names})
    # The influencer is the natural spark: always in the seed subset (unless demoted).
    seed_agents = C.choose_seed_agents(llm_names, cfg.seed_fraction, cfg.seed,
                                       must_include=force_seed)
    inf_set = set(inf_names)

    price = cfg.initial_price
    history = [price]
    rows, traces = [], {}
    metrics = C.PropagationMetrics()
    last_statements: dict[str, Statement] = {}
    prev_sentiment = {nm: 0.0 for nm in llm_names}

    seed_sign = 0.0  # direction of the seeded story; set endogenously at the news round (M4)
    pending_liq: list[Order] = []   # forced-liquidation sells carried into the NEXT round's flow
    liquidations = []               # full LiquidationEvent log (leverage layer)
    for r in range(1, cfg.n_rounds + 1):
        recent = history[-cfg.recent_window:]
        if event_logger is not None:
            event_logger.emit(
                "RoundStarted", round_i=r,
                data={"price": price, "recent_prices": list(recent)},
            )
        # --- influencer-megaphone ablation: filter what enters the digest pool ---
        # 'all' (baseline), 'exclude_influencer' (mute: its take reaches no one),
        # 'only_influencer' (solo mic: only its take circulates). Everyone still
        # produces a public_take that is logged; this only gates DISTRIBUTION.
        if cfg.broadcast_mode == "exclude_influencer":
            pool = {k: v for k, v in last_statements.items() if k not in inf_set}
        elif cfg.broadcast_mode == "only_influencer":
            pool = {k: v for k, v in last_statements.items() if k in inf_set}
        else:
            pool = last_statements
        # --- build prompts (with social digest) ---
        prompts, order_owners = [], []
        for a in llm_agents:
            # The headline reaches ONLY the seed subset, directly. Everyone else
            # learns the story exclusively through the social channel (neighbor
            # digests) + the price tape -> contagion is the transmission path.
            sees_news = (r >= cfg.news_round) and (a.name in seed_agents)
            news = cfg.news_text if sees_news else ""
            if sees_news and event_logger is not None:
                event_logger.emit(
                    "ScenarioEventDelivered", round_i=r, agent_id=a.name,
                    data={
                        "scenario_event": "news",
                        "delivery": "direct_seed",
                        "news_sha256": _digest(cfg.news_text),
                        "news_length": len(cfg.news_text),
                    },
                )

            # per-agent coupling = global gain x persona susceptibility
            eff_w = (cfg.social_weight * a.social_weight) if cfg.social_enabled else 0.0
            eff_w = max(0.0, min(1.0, eff_w))
            if eff_w > 0 and pool:
                feed, social_mean = C.neighbor_feed(
                    a.name, pool, adjacency, cfg.social_mode,
                    cfg.digest_size, rng)
            else:
                feed, social_mean = [], 0.0

            # Tell the influencer whether its voice carries (ablation arms A/B).
            mic_note = ""
            if a.name in inf_set:
                if cfg.broadcast_mode == "exclude_influencer":
                    mic_note = ("Note: your public statements are NOT visible to anyone "
                                "in this market — no other trader can see what you say.")
                elif cfg.broadcast_mode == "only_influencer":
                    mic_note = ("Note: you are the ONLY trader whose statements others can "
                                "see — your voice reaches the whole market.")

            system, user = a.build_prompt(
                mode, r, price, recent, cfg.fundamental_value, news,
                social_feed=feed, social_mean=social_mean, effective_weight=eff_w,
                mic_note=mic_note)
            prompts.append((system, user))
            order_owners.append(a)
            if event_logger is not None:
                event_logger.emit(
                    "AgentObservationCreated", round_i=r, agent_id=a.name,
                    data={
                        "persona_id": a.persona_id,
                        "price": price,
                        "recent_prices": list(recent),
                        "direct_news": sees_news,
                        "social_feed": [
                            {"sentiment": sentiment, "public_take": public_take}
                            for sentiment, public_take in feed
                        ],
                        "social_mean": social_mean,
                        "effective_social_weight": eff_w,
                        "system_prompt_sha256": _digest(system),
                        "user_prompt_sha256": _digest(user),
                        "prompt_sha256": _prompt_digest(system, user),
                    },
                )

        # --- batched LLM call (cost/latency bottleneck) ---
        set_context = getattr(llm, "set_batch_context", None)
        if callable(set_context):
            set_context(
                r,
                [{"agent_id": a.name, "persona_id": a.persona_id}
                 for a in order_owners],
            )
        completions = llm.complete_batch(prompts)
        orders: list[Order] = []
        statements: dict[str, Statement] = {}
        sentiments: dict[str, float] = {}
        flips = 0
        for a, raw in zip(order_owners, completions):
            order = a.ingest(raw, r, price)
            orders.append(order)
            statements[a.name] = a.statement(order)
            sentiments[a.name] = order["sentiment"]
            parse_failed = order["rationale"] == "parse-failed; holding"
            if event_logger is not None:
                public_decision = {
                    "persona_id": a.persona_id,
                    "action": order["side"],
                    "quantity": order["quantity"],
                    "limit_price": order["limit_price"],
                    "sentiment": order["sentiment"],
                    "public_take": order["public_take"],
                    "parse_status": "error" if parse_failed else "parsed",
                    "raw_response_sha256": _digest(raw),
                }
                event_logger.emit(
                    "AgentDecisionParsed", round_i=r, agent_id=a.name,
                    data=public_decision,
                    private_data={
                        **public_decision,
                        "private_rationale": order["rationale"],
                    },
                )
                if parse_failed:
                    event_logger.emit(
                        "AgentDecisionParseError", round_i=r, agent_id=a.name,
                        data={
                            "error_code": "invalid_or_missing_json_object",
                            "raw_response_sha256": _digest(raw),
                        },
                    )
                statement = statements[a.name]
                event_logger.emit(
                    "PublicStatementPublished", round_i=r, agent_id=a.name,
                    data={
                        "sentiment": statement["sentiment"],
                        "side": statement["side"],
                        "public_take": statement["text"],
                    },
                )
                event_logger.emit(
                    "OrderSubmitted", round_i=r, agent_id=a.name,
                    data={
                        "source": "llm",
                        "side": order["side"],
                        "quantity": order["quantity"],
                        "limit_price": order["limit_price"],
                    },
                )
            if (order["sentiment"] * prev_sentiment.get(a.name, 0.0)) < -0.02:
                flips += 1
            prev_sentiment[a.name] = order["sentiment"]

        # M4: the cascade direction is the seed agents' OWN realized stance the round
        # the news first reaches them -- endogenous, not a 'miss'/'cut' substring guess.
        # Require genuine CONVICTION (|mean| > threshold): a neutral/placebo headline
        # leaves seed agents ~flat, so seed_sign stays 0 and cascade_size is 0 (it
        # should NOT register a directional cascade when there is no real story).
        if r == cfg.news_round:
            seed_vals = [sentiments[nm] for nm in seed_agents if nm in sentiments]
            mean_seed = sum(seed_vals) / len(seed_vals) if seed_vals else 0.0
            gate = C.PropagationMetrics.CONVICTION
            seed_sign = 1.0 if mean_seed > gate else (-1.0 if mean_seed < -gate else 0.0)

        # noise agents (cheap, no LLM, excluded from social graph)
        for a in agents:
            if isinstance(a, NoiseAgent):
                noise_order = a.noise_order(r, price)
                orders.append(noise_order)
                if event_logger is not None:
                    event_logger.emit(
                        "OrderSubmitted", round_i=r, agent_id=a.name,
                        data={
                            "source": "noise",
                            "side": noise_order["side"],
                            "quantity": noise_order["quantity"],
                            "limit_price": noise_order["limit_price"],
                        },
                    )

        # forced-liquidation sells from LAST round's margin breaches enter the
        # net flow here (phantom LIQUIDATION_AGENT -> price impact, no re-fill).
        orders.extend(pending_liq)
        if event_logger is not None:
            for liquidation_order in pending_liq:
                event_logger.emit(
                    "OrderSubmitted", round_i=r,
                    agent_id=liquidation_order["agent"],
                    data={
                        "source": "queued_liquidation",
                        "side": liquidation_order["side"],
                        "quantity": liquidation_order["quantity"],
                        "limit_price": liquidation_order["limit_price"],
                    },
                )

        # --- clear market ---
        buy_q = sum(o["quantity"] for o in orders if o["side"] == "buy")
        sell_q = sum(o["quantity"] for o in orders if o["side"] == "sell")
        if event_logger is not None:
            event_logger.emit(
                "OrdersAggregated", round_i=r,
                data={
                    "order_count": len(orders),
                    "buy_quantity": buy_q,
                    "sell_quantity": sell_q,
                    "net_quantity": buy_q - sell_q,
                    "depth": buy_q + sell_q,
                },
            )
        new_price, vol, fills = clear_by_pressure(orders, price, cfg.kappa)
        if event_logger is not None:
            event_logger.emit(
                "PriceCleared", round_i=r,
                data={
                    "previous_price": price,
                    "cleared_price": new_price,
                    "matched_volume": vol,
                    "buy_quantity": buy_q,
                    "sell_quantity": sell_q,
                    "kappa": cfg.kappa,
                },
            )
        for a in agents:
            dq = fills.get(a.name, 0)
            shares_before, cash_before = a.shares, a.cash
            a.shares += dq
            a.cash -= dq * new_price
            if dq and event_logger is not None:
                event_logger.emit(
                    "FillApplied", round_i=r, agent_id=a.name,
                    data={
                        "signed_quantity": dq,
                        "price": new_price,
                        "shares_before": shares_before,
                        "shares_after": a.shares,
                        "cash_before": cash_before,
                        "cash_after": a.cash,
                    },
                )
        price = new_price
        history.append(price)
        rows.append((r, price, vol))

        # --- leverage layer: check margins at the NEW price, settle breaches,
        # queue their forced sells for next round (the amplifying spiral). Gated
        # to r >= news_round: the amplifier acts ONLY on the news shock, never on
        # pre-news drift — this guarantees the drop_depth pre-shock baseline
        # (history[news_round-1]) is never rebased by a pre-news liquidation, and
        # the full leverage book is intact when the shock lands (review M4/M5). ---
        if r >= cfg.news_round:
            pending_liq, liq_events = LV.margin_calls(agents, price, cfg, r)
            liquidations.extend(liq_events)
            if event_logger is not None:
                for liq_event, liq_order in zip(liq_events, pending_liq):
                    event_logger.emit(
                        "MarginCallTriggered", round_i=r, agent_id=liq_event.agent,
                        data={
                            "price": liq_event.price,
                            "threshold": liq_event.threshold,
                            "reference_shares": liq_event.shares,
                            "realized_reference_pnl": liq_event.pnl,
                        },
                    )
                    event_logger.emit(
                        "LiquidationOrderQueued", round_i=r,
                        agent_id=liq_event.agent,
                        data={
                            "order_agent": liq_order["agent"],
                            "side": liq_order["side"],
                            "quantity": liq_order["quantity"],
                            "limit_price": liq_order["limit_price"],
                            "scheduled_round": r + 1,
                            "will_apply_within_run": r < cfg.n_rounds,
                        },
                    )
        else:
            pending_liq = []

        # --- record (reasoning traces NEVER dropped) ---
        llm_set = set(llm_names)
        traces[r] = [(o["agent"], o["side"], o["quantity"], o["limit_price"],
                      o["sentiment"], o["public_take"], o["rationale"])
                     for o in orders if o["agent"] in llm_set]
        non_seed = set(llm_names) - seed_agents
        metrics.record(r, sentiments, flips,
                       seed_sign if r >= cfg.news_round else 0.0,
                       non_seed=non_seed)
        if event_logger is not None:
            idx = len(metrics.rounds) - 1
            event_logger.emit(
                "MetricsRecorded", round_i=r,
                data={
                    "mean_sentiment": metrics.mean_sentiment[idx],
                    "std_sentiment": metrics.std_sentiment[idx],
                    "positive_share": metrics.pos_share[idx],
                    "negative_share": metrics.neg_share[idx],
                    "stance_flips": metrics.flips[idx],
                    "cascade_size": metrics.cascade_size[idx],
                },
            )
            event_logger.emit(
                "RoundFinished", round_i=r,
                data={
                    "price": price,
                    "matched_volume": vol,
                    "llm_order_count": len(order_owners),
                    "total_order_count": len(orders),
                    "liquidations_triggered": len(liq_events) if r >= cfg.news_round else 0,
                },
            )
        last_statements = statements

    return SimResult(history, rows, traces, agents, metrics,
                     adjacency, seed_agents, hub_names, cfg, tracker,
                     liquidations=liquidations, run_id=run_id)
