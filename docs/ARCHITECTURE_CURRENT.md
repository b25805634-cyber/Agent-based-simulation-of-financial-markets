# 当前架构与真实数据流

本文描述 2026-07-15 工作区中的实际实现，不描述理想架构。核心入口是 `python3 -m nmsim.run`；`narrative_market_sim.py` 是已经分叉的旧 Phase-1 脚本。

> Phase 1 更新：核心回合顺序和科学语义未变；managed CLI/`run_seed` 现已在 Provider 外层加入 immutable run manifest、公共/私有事件和严格 LLM Record/Replay。详细 schema 与命令见 [RUN_PROVENANCE.md](RUN_PROVENANCE.md)。下文关于市场、观察和随机性的描述仍是 compatibility baseline；原“raw response 不持久化”的缺口已由新私有 sidecar 补上。

## 模块责任

| 模块 | 当前责任 | 不负责的内容 |
|---|---|---|
| `nmsim/config.py` | 单一 `Config` dataclass、JSON 序列化和宽松 `from_dict` | 没有独立 Scenario、运行时校验、schema version 或 secret redaction |
| `nmsim/types.py` | `Order`、`Statement` TypedDict 和 `Side` | 没有 `Decision`、fill、portfolio 或 event 类型 |
| `nmsim/prompts.py` | 六类 persona、real system/user prompt | 不执行 persona 数值 policy；不含数值 fundamental/social gain |
| `nmsim/agents.py` | Agent 状态、NoiseAgent、本地 Mock 参数、prompt 路由、response ingest、statement | 不做资金/库存风控；不保存 raw response |
| `nmsim/llm.py` | Mock/Anthropic/OpenAI-compatible、解析、async retry、内存 cache、成本估算、provider factory | 不保证真实 LLM 确定性；不把调用记录持久化；不做 main CLI health gate |
| `nmsim/contagion.py` | network、feed、news seed、attention digest、sentiment/cascade 指标 | 不传播订单；cascade 不是隔离价格通道后的纯 contagion effect |
| `nmsim/market.py` | 净订单流压力价格、matched volume、全额 signed fills | 不是 LOB；不执行 limit；无显式 market maker/约束/库存守恒 |
| `nmsim/leverage.py` | 冻结参照杠杆多头、价格保证金、一次性强平、phantom sell order | 不改 Agent voluntary cash/shares；无真实债权人/清算执行账本 |
| `nmsim/sim.py` | 建人群和社交结构、逐轮 prompt/batch/order/price/portfolio/metrics/强平 | 不写文件；不做 config 校验或 provider health 判定 |
| `nmsim/validation.py` | returns、stylized facts、reaction、reference loader、RMSE、DTW | 不提供统计置信区间、多事件设计或失败样本处理 |
| `nmsim/events.py` | versioned 公共/私有 JSONL envelope、隐私键拦截 | 不改变 simulation 或推导反事实 |
| `nmsim/recording.py` | logical LLM call Record/Replay、严格 Prompt/Persona/model/order 匹配 | 不捕获 SDK 内部 retry 的中间 response；不生成反事实回答 |
| `nmsim/provenance.py` | 唯一 run directory、原子 manifest、hash、环境、honest-N、安全兼容链接 | 无 `.git` 时不能凭空恢复 commit |
| `nmsim/run.py` | CLI、Config 覆盖、managed record/replay、六类兼容输出和终端摘要 | 不暴露全部 Config；不改变旧 market/social/risk 语义 |
| `experiments/run_seed.py` | 单个实验、Meta 对齐、health、compact orders、统一 provenance/record/replay | CSV reuse 不产生新的 LLM events；旧根 JSON只作兼容投影 |
| `experiments/*driver*.py` | 子进程/线程批量、endpoint wait、retry、部分 health gate | 被拒 run 常被删除；workers/retry 不进入单 run JSON |
| `experiments/*analyze*.py` | 读取历史 JSON/trace 并计算表/图 | 多个 analyzer 的过滤、配对和 CI 口径不一致 |

## 从公开入口到价格更新

```mermaid
flowchart TD
    CLI[python -m nmsim.run / experiment driver] --> CFG[Config]
    CFG --> FACTORY[build_llm]
    FACTORY --> MOCK[MockLLM]
    FACTORY --> ANT[AnthropicLLM]
    FACTORY --> OAI[OpenAILLM]
    MOCK --> CACHE[CachingLLM]
    ANT --> CACHE
    OAI --> CACHE
    CFG --> SIM[run_sim]
    CACHE --> SIM

    SIM --> CAST[make_agents + init_leverage]
    CAST --> GRAPH[network + seed subset]
    GRAPH --> LOOP{round r}

    LOOP --> OBS[price/recent + direct news + previous public digest + own state]
    OBS --> PROMPT[Agent.build_prompt]
    PROMPT --> BATCH[complete_batch]
    BATCH --> PARSE[parse_order + ingest]
    PARSE --> PUB[Statement: sentiment + public_take]
    PARSE --> ORDERS[voluntary orders]
    NOISE[NoiseAgent orders] --> ORDERS
    PENDING[previous-round liquidation sells] --> ORDERS
    ORDERS --> MARKET[clear_by_pressure]
    MARKET --> PRICE[new price]
    MARKET --> FILLS[signed full fills]
    FILLS --> PORT[Agent cash/shares mutation]
    PRICE --> MARGIN[margin_calls]
    MARGIN --> PENDING2[next-round phantom sells]
    PRICE --> METRICS[propagation + price history]
    PUB --> NEXT[becomes next round's last_statements]
    PENDING2 --> LOOP
    NEXT --> LOOP
    METRICS --> RESULT[SimResult]
    PORT --> RESULT
    RESULT --> OUTPUT[CSV / JSON / PNG / console]
```

## 一轮仿真的实际编号流程

以下编号对应 `nmsim/sim.py:40-194` 的真实顺序。

1. **运行级初始化（第一轮前）**
   - `random.Random(cfg.seed + 1)` 创建 digest RNG。
   - `make_agents(cfg)` 生成 persona Agents 和 NoiseAgents；所有 Agent 初始 `cash=10000`、`shares=50`。
   - `init_leverage` 在启用时按 fuel-first 顺序选择 cohort，建立与 voluntary portfolio 分离的冻结参照多头。
   - 根据实际 LLM agent 名称建 graph；非 demoted influencer 是全连接 hub。
   - `choose_seed_agents` 固定直接看到新闻的子集；非 demoted influencer 强制加入。
   - provider wrapper 的 `kind != mock` 决定使用 real prompt，否则用 Mock structured prompt。

2. **截取行情观察**
   - `recent = history[-cfg.recent_window:]`。
   - 当轮决策使用回合开始时的 `price`，即上一轮 clearing 后价格。

3. **过滤上一轮公开广播池**
   - `broadcast_mode=all`：全部上一轮 statements。
   - `exclude_influencer`：去掉 influencer statement。
   - `only_influencer`：只保留 influencer statement。
   - 第 1 轮没有上一轮 statement，social feed 为空。

4. **逐个 LLM Agent 决定其 observation**
   - `r >= news_round and agent in seed_agents` 时直接看到完整 `news_text`；因此 seed 在以后每轮都会重复看到同一新闻。
   - `effective_weight = clip(cfg.social_weight * persona.social_weight, 0, 1)`。
   - 只有 `effective_weight>0` 且 pool 非空才生成 digest。
   - feed 模式看所有其他 statements；network 模式只看 adjacency neighbor statements。
   - visible statements 先 shuffle 打散同 conviction tie，再按 `abs(sentiment)` 降序取 `digest_size`。

5. **构造 prompt**
   - Real system：市场规则、persona 自然语言、JSON schema。
   - Real user：round、latest/recent price、trend、direct news、公开 feed、可选 mic note、自己的 shares/cash、最近 3 条 memory。
   - Real user **不含** `fundamental_value` 或 effective social weight 数值。
   - Mock prompt 额外含 `FUNDAMENTAL`、`SOCIAL_SENTIMENT`、`SOCIAL_WEIGHT` 和 `_MOCK_PARAMS_BY_ID`。

6. **整轮 batch 调用**
   - prompts 按 `llm_agents` 列表顺序组成一个 batch。
   - cache hit 直接填回原索引；所有 misses 传给 inner `complete_batch`。
   - Mock 依序同步完成；real provider 用 `asyncio.gather` 同时创建请求并保持返回列表顺序。
   - Anthropic/OpenAI async 单 prompt 最多尝试 3 次；parse failure 加严格 JSON reminder，异常最后转 hold JSON。

7. **解析和 Agent 私有状态更新**
   - `parse_order` 从 response 抽 JSON，规范 side/quantity/limit/sentiment/public/reasoning。
   - 原始 response 在此后丢弃；只保留已截断字段。
   - `order.agent` 被覆盖为 owner name。
   - `last_sentiment` 更新；memory 追加当轮 price/action 和 public_take（为空时 rationale）的 60 字符摘要。

8. **产生可传播 Statement**
   - statement 字段为 `agent`、`sentiment`、`side`、`text=public_take[:90]`。
   - neighbor digest 最终只取 statement 的 `sentiment` 和 `text`；`side` 不送给其他 Agent。
   - standard real schema 的 private `reasoning` 不进入 statement。

9. **计算 stance flip 和 seed sign**
   - 新旧 sentiment 乘积 `< -0.02` 计一次 flip。
   - 仅 news round 根据 seed agents 当轮 mean sentiment 决定 `seed_sign`；绝对均值不超过 0.3 时为 0。

10. **添加非 LLM 订单**
    - 每个 NoiseAgent 生成一个 1..8 股 buy/sell。
    - 上一轮 margin breach 生成的 `pending_liq` phantom sell orders 在此加入。

11. **汇总订单并压力清算**
    - `buy_q`/`sell_q` 汇总所有有效数量，`depth=buy_q+sell_q`。
    - `new_price = round(max(0.01, old_price * (1 + kappa*(buy_q-sell_q)/depth)), 2)`。
    - `volume = min(buy_q, sell_q)`。
    - 每笔 buy/sell 都按原 quantity 变成 signed fill；limit、cash、shares 均不参与资格判断。

12. **更新 voluntary portfolio**
    - 对每个真实 Agent：`shares += dq`；`cash -= dq * new_price`。
    - 买入可使 cash 为负，卖出可使 shares 为负；phantom liquidation seller 没有 Agent 实体，所以不进入这一循环。

13. **检查保证金并排队下一轮强平卖压**
    - 仅 `r >= news_round` 检查。
    - `equity_ratio=(ref_shares*price-debt)/(ref_shares*price)` 小于 maintenance 时，立即标记参照仓已清算并记录事件。
    - 生成 `ref_shares` 股 phantom sell，赋给下一轮 `pending_liq`；不是当轮重新 clearing。

14. **记录当轮**
    - `history` 和 `(round, price, volume)` 追加。
    - `traces[r]` 只保存 LLM orders 的解析后字段；不含 NoiseAgent、phantom order、raw response、fill 或 cash/shares。
    - propagation metrics 保存所有 LLM sentiment、flip、非 seed 与 seed sign 的对齐比例。
    - 本轮 statements 赋给 `last_statements`，在下一轮传播。

15. **循环结束**
    - 返回 `SimResult`。
    - `run.run` 计算 validation 并写文件；直接调用 `run_sim` 的实验自行选择写哪些字段。

## Agent 的实际观察边界

| 信息 | Real LLM | MockLLM | 备注 |
|---|---:|---:|---|
| round、当前/近期价格、趋势 | 是 | 是 | 当前价是上一轮 clearing 后价格 |
| 完整新闻 | 仅 seed | 仅 seed | 从 news round 起每轮重复 |
| 他人上一轮 sentiment/public_take | social coupling >0 时 | social coupling >0 时 | attention 选绝对 sentiment 最大者 |
| 数值 social weight | 否 | 是 | real 正权重大小不可见 |
| 自己 cash/shares | 是 | 是 | 不含 leverage reference book |
| 最近 3 条自己的 memory | 是 | 是 | 解析后的摘要，不是 raw response |
| 数值 fundamental value | 否 | 是 | real persona 只有文字上的价值观 |
| 他人订单/position/cash/private reasoning | 否 | 否 | statement 内 side 也不进入 digest |
| market depth/net flow/volume/fills | 否 | 否 | 只能从 price tape 间接反应 |
| 自己杠杆、margin ratio、强平状态 | 否 | 否 | leverage 只通过价格回传 |

## LLM 调用边界

```text
Agent.build_prompt
  -> (system: str, user: str)
  -> CachingLLM.complete_batch(list[(system,user)])
  -> MockLLM | AnthropicLLM | OpenAILLM
  -> raw str
  -> parse_order(raw, last_price)
  -> normalized Order
```

Managed Provider 边界：

```text
build_llm -> existing CachingLLM -> RecordingLLM -> run_sim
record file -> ReplayLLM (no inner provider) -> run_sim
```

Recording 位于 cache 外侧，保存每个 logical Agent response；manifest 另记真实 Provider calls/cache hits。Provider 细节：

- `provider=auto`：只有环境里存在 `ANTHROPIC_API_KEY` 才选 Anthropic，否则 Mock。
- `provider=anthropic`：constructor 错误时 factory fallback Mock；没有 key 在当前 SDK 中不一定构造失败，请求异常会在 per-prompt 层转 hold，不会换 Mock。
- `provider=openai`：constructor 错误时 fallback Mock；不可达 endpoint 同样是 per-prompt hold。
- 其他 provider 字符串：静默 Mock。
- Real provider 没有传 seed。Anthropic 的若干 model prefix 不发送 temperature。
- sync `.complete` 存在，但主循环总走 `complete_batch`。

## 关键类型及字段

### `Config`

38 个字段，完整实效表见 [CODEX_HANDOFF_AUDIT.md](CODEX_HANDOFF_AUDIT.md)。它同时混合 timeline、population、provider、social、leverage、validation 和 output concern，没有独立 Scenario/Policy/Engine 配置。

### `Order` (`TypedDict`)

| 字段 | 类型/含义 | 去向 |
|---|---|---|
| `agent` | str | fills、trace |
| `side` | buy/sell/hold | market aggregation；statement 中存但不传播给 prompt |
| `quantity` | nonnegative int after parse | market aggregation；无上限/资金约束 |
| `limit_price` | float | trace only，不执行 |
| `sentiment` | `[-1,1]` float | statement、propagation metrics |
| `public_take` | str，parser 最多 140 字符 | statement 再截到 90、trace、memory |
| `rationale` | str，parser 最多 240 字符 | trace；standard path 不广播 |

### `Statement`

`agent`, `sentiment`, `side`, `text`。`neighbor_feed` 只输出 `(sentiment, text)`。

### `Agent`

- 身份/行为：`name`, `persona_id`, `persona_dict`, `persona`, `mock_params`, `social_weight`, `is_influencer`, `is_llm`。
- voluntary portfolio：`cash`, `shares`。
- leverage reference book：`is_leveraged`, `lev_ratio`, `lev_ref_shares`, `lev_debt`, `lev_liquidated`, `lev_pnl`。
- cognitive state：`memory`, `last_sentiment`。

### `SimResult`

`history`, `rows`, `traces`, `agents`, `metrics`, `adjacency`, `seed_agents`, `hub_names`, `cfg`, `tracker`, `liquidations`, `run_id`, `run_dir`。

其中大量状态只存在内存，主 CLI 没有全部持久化。

### `PropagationMetrics`

逐轮保存 `rounds`, mean/std sentiment, positive/negative share, flips, cascade size, per-agent sentiment。`CONVICTION=0.3`；`cascaded()` 默认 peak >=0.6。

### `LiquidationEvent`

`round`, `agent`, `shares`, `price`, `threshold`, `pnl`。实验 `run_seed` 会写紧凑数组；主 CLI 不写。

## 状态变更位置

| 状态 | 写入位置 | 是否主 CLI 持久化 |
|---|---|---:|
| Agent memory/last sentiment | `Agent.ingest` | 仅最终 trace 的当前 response；memory 本身否 |
| Agent cash/shares | `sim.run_sim` fill loop | 否 |
| leverage fields | `leverage.init_leverage`, `margin_calls` | 否；实验仅写 events/config subset |
| price/history/rows | `sim.run_sim` | 是，`price_path.csv` |
| statements | `sim.run_sim` local `last_statements` | 间接，public_take 在 trace；完整 social event 否 |
| adjacency/seed agents | 初始化 | 终端打印部分；文件否 |
| 最终 raw response/full prompt | `RecordingLLM` 私有 sidecar | 是，`llm_records.jsonl`/`private_events.jsonl` 0600；SDK 中间 retry 仍否 |
| parsed traces | `sim.run_sim` | 是，LLM-only CSV |
| propagation metrics | `PropagationMetrics.record` | 聚合 CSV；per-agent snapshots 否 |
| validation facts | `run.run` | 是，JSON |
| Config | `run.run` | 是，但含 API key 且不是 resolved runtime manifest |
| tracker cost | provider/tracker | 只打印；experiment JSON 写部分 |
| liquidation events | `sim.run_sim` | 主 CLI 否；`run_seed` 是 |

## 随机性来源

| 来源 | Seed/控制 | 可复现边界 |
|---|---|---|
| Mock model noise | `random.Random(cfg.seed)` | 固定调用顺序可复现；cache/agent 顺序改变会改变消费序列 |
| Network | `random.Random(cfg.seed)` | 当前 sorted adjacency 和整数 seed 可跨 hashseed 复现 |
| News seed subset | `random.Random(cfg.seed+999)` | 固定 agent 名单/顺序可复现 |
| Digest tie shuffle | `random.Random(cfg.seed+1)` | 在所有 agent/round 间共享，分支和顺序耦合 |
| NoiseAgent | `Random((base_seed*1000003) XOR (idx*9176+round))` | 固定 seed/index/round 可复现 |
| Leverage | 无 RNG | cohort 和 ratio 取决于稳定 agent 顺序 |
| Real LLM | provider/server sampling | 不发送 seed；temperature=0 不保证位级确定 |
| Thread/process scheduling | driver `workers`、provider batching | gather 保持返回索引，但服务端 batch 浮点/采样可能受并发影响；未记录 |

## 输出边界

主 CLI 在 `<out>/runs/<run_id>/` 固定写原六类结果：

1. `price_path.csv`
2. `reasoning_traces.csv`
3. `propagation.csv`
4. `stylized_facts.json`
5. `config.json`
6. `sim_overview.png`（matplotlib 可用时）

并新增 `run_manifest.json`、`events.jsonl`、`private_events.jsonl`、`llm_records.jsonl`。事件现记录 observation hash/public feed、LLM logical calls、所有提交订单、真实 Agent fill、逐轮 portfolio 变更、margin/liquidation、metrics、batch 与 run status。

仍未完整记录/实现：Provider SDK 中间 retry/request id、显式 market-maker inventory、phantom seller 账本、独立 adjacency artifact，以及没有 `.git` 时的 commit/diff。根目录旧输出只在安全时通过 `latest` symlink 保持兼容，普通历史文件永不覆盖。
