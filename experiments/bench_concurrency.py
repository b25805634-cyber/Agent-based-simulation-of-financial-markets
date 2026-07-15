"""Safe concurrency benchmark for the MiniMax vLLM endpoint.

Finds the throughput-maximising concurrency: fires C concurrent realistic calls
from a SINGLE client (bounded connection pool — unlike the multi-subprocess
workers path that opened 281 connections and hung the server), measures
calls/s + output-tok/s, health-checks between levels and BACKS OFF on any
degradation. Read-only on the endpoint; run it with Phase 2 PAUSED.

Usage:  python -m experiments.bench_concurrency
"""
from __future__ import annotations
import os, time, asyncio
import httpx
from openai import AsyncOpenAI
from nmsim.config import Config
from nmsim import prompts as P

cfg = Config()
BASE = os.getenv("OPENAI_BASE_URL") or cfg.openai_base_url
KEY = os.getenv("OPENAI_API_KEY") or cfg.openai_api_key
MODEL = cfg.openai_model
MAXTOK = 400                       # representative of the JSON+reasoning output
LEVELS = [16, 32, 48, 64, 80, 96]  # climb; stop on plateau/degradation

# one realistic prompt (real input-token load)
_persona = P.PERSONAS_BY_ID["retail_crowd"]
SYS = P.build_system(_persona)
USR = P.build_user(round_i=13, price=82.4, recent=[100.0, 95.1, 90.2, 86.0, 82.4],
                   news="BREAKING: the company missed Q2 earnings and cut guidance sharply.",
                   social_feed=[(-0.8, "this is going to zero"), (-0.6, "guidance cut is brutal"),
                                (0.2, "overdone, a dip to buy")],
                   shares=50, cash=10000.0, memory=["r12: held, watching"])


def _client(max_conn):
    return AsyncOpenAI(base_url=BASE, api_key=KEY,
                       http_client=httpx.AsyncClient(
                           trust_env=False, timeout=httpx.Timeout(120.0),
                           limits=httpx.Limits(max_connections=max_conn + 8,
                                               max_keepalive_connections=max_conn + 8)))


async def _one(client):
    t0 = time.monotonic()
    try:
        r = await client.chat.completions.create(
            model=MODEL, max_tokens=MAXTOK, temperature=0.3,
            messages=[{"role": "system", "content": SYS}, {"role": "user", "content": USR}])
        out = getattr(getattr(r, "usage", None), "completion_tokens", None) or 0
        return (time.monotonic() - t0, out, True)
    except Exception as e:
        return (time.monotonic() - t0, 0, False)


async def _health(client) -> bool:
    try:
        await asyncio.wait_for(_one(client), timeout=30.0)
        return True
    except Exception:
        return False


async def main():
    print(f"endpoint {BASE}  model {MODEL}  max_tokens {MAXTOK}\n")
    print(f"{'conc':>5} {'calls/s':>8} {'out tok/s':>10} {'med lat':>8} {'p95 lat':>8} {'ok':>6}")
    results = []
    prev_cps = 0.0
    for C in LEVELS:
        client = _client(C)
        # warm-up so the first timed level isn't cold
        await asyncio.gather(*[_one(client) for _ in range(4)])
        t0 = time.monotonic()
        try:
            rs = await asyncio.wait_for(
                asyncio.gather(*[_one(client) for _ in range(C)]), timeout=300.0)
        except asyncio.TimeoutError:
            print(f"{C:>5}   TIMEOUT (>300s) — server degraded at this concurrency; stopping.")
            await client.close(); break
        elapsed = time.monotonic() - t0
        lats = sorted(r[0] for r in rs)
        ok = sum(1 for r in rs if r[2])
        out_tok = sum(r[1] for r in rs)
        cps = ok / elapsed
        tps = out_tok / elapsed
        med = lats[len(lats) // 2]
        p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
        print(f"{C:>5} {cps:>8.2f} {tps:>10.0f} {med:>8.1f} {p95:>8.1f} {ok:>3}/{C}")
        results.append((C, cps, tps, ok))
        # health-check before escalating
        healthy = await _health(client)
        await client.close()
        if ok < C * 0.9:
            print(f"      >10% calls FAILED at conc={C} — concurrency ceiling reached; stopping.")
            break
        if not healthy:
            print(f"      health-check after conc={C} FAILED — backing off; stopping.")
            break
        if cps < prev_cps * 1.05 and C > LEVELS[0]:
            print(f"      throughput plateaued (conc {C}: {cps:.2f} vs prev {prev_cps:.2f}); stopping.")
            break
        prev_cps = cps

    if results:
        best = max(results, key=lambda x: x[1])
        print(f"\nBEST throughput: {best[1]:.2f} calls/s at concurrency {best[0]}.")
        print(f"current Phase-2 setting (workers=2 -> ~60 concurrent) baseline is in the table above.")
        # workers ~= concurrency / 30 agents-per-round
        print(f"=> to sustain ~{best[0]} concurrent, run the driver at workers≈{max(2, round(best[0]/30))} "
              f"(each run bursts ~30 calls/round).")


if __name__ == "__main__":
    asyncio.run(main())
