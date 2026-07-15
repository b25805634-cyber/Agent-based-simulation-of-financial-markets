# 当前架构与真实数据流

本文描述 2026-07-15 工作区中的实际实现，不描述理想架构。核心入口是 `python3 -m nmsim.run`；`narrative_market_sim.py` 是已经分叉的旧 Phase-1 脚本。

> Phase 1/1.1A/1.1A.1 更新：核心回合顺序和科学语义未变；managed CLI/`run_seed` 现已在 Provider 外层加入 immutable run manifest、公共/私有事件、严格 LLM Record/Replay 以及科学组件指纹。Phase 1.1A 另新增不继续市场的离线 reparse audit；Phase 1.1A.1 将最终有效 `Config` 的分类摘要与 hash 绑定到 strict replay preflight。详细 schema 与命令见 [RUN_PROVENANCE.md](RUN_PROVENANCE.md) 和 [REPLAY_COMPATIBILITY.md](REPLAY_COMPATIBILITY.md)。下文关于市场、观察和随机性的描述仍是 compatibility baseline；原“raw response 不持久化”的缺口已由私有 sidecar 补上。

## 模块责任

| 模块 | 当前责任 | 不负责的内容 |
|---|---|---|
| `nmsim/config.py` | 单一 `Config` dataclass、JSON 序列化和宽松 `from_dict` | 没有独立 Scenario、运行时校验、schema version 或 secret redaction |
| `nmsim/config_contract.py` | 对最终有效 `Config` 逐字段 fail-closed 分类，生成稳定、脱敏的 scientific/model-request/execution 摘要与 hash | 不修改 Config 值；不替代 source fingerprint 或实际 resolved model config |
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
| `nmsim/fingerprint.py` | 稳定计算 parser、Prompt、Persona、simulation core 的 schema/hash 与总科学指纹 | 不用整仓 commit 代替科学兼容性；不纳入文档、运行产物或私有日志 |
| `nmsim/recording.py` | logical LLM call Record/Replay；记录有效配置契约，并严格校验请求身份、model config、schema/hash、运行时科学配置和科学源指纹 | 不捕获 SDK 内部 retry 的中间 response；错配时不回退 Provider；不生成反事实回答 |
| `nmsim/reparse_audit.py` | 离线读取历史 raw response，用当前 `parse_order` 重新解析并逐字段对比 | 不构造 Provider、不继续仿真、不生成新价格轨迹 |
| `nmsim/provenance.py` | 唯一 run directory、原子 manifest、环境、honest-N、安全兼容链接；在 Provider 边界前计算并持久化 source/config 契约 | 无 `.git` 时不能凭空恢复 commit |
| `nmsim/run.py` | CLI、Config 覆盖、managed record/replay、六类兼容输出和终端摘要 | 不暴露全部 Config；不改变旧 market/social/risk 语义 |
| `experiments/run_seed.py` | 单个实验、Meta 对齐、health、compact orders、统一 provenance/record/replay | CSV reuse 不产生新的 LLM events；旧根 JSON只作兼容投影 |
| `experiments/*driver*.py` | 子进程/线程批量、endpoint wait、retry、部分 health gate；driver/worker 身份传入 managed manifest | retry 细节仍不进入旧的单 run 结果 JSON |
| `experiments/*analyze*.py` | 读取历史 JSON/trace 并计算表/图 | 多个 analyzer 的过滤、配对和 CI 口径不一致 |

## Scientific Component Fingerprint

`nmsim/fingerprint.py` 根据当前实际调用图，建立一个比 Git commit 更窄、但对科学行为更直接的兼容身份。`scientific_component_fingerprint` 的覆盖集是以下固定仓库相对路径：

1. `nmsim/agents.py`：observation/prompt 路由、Agent ingest 与可传播 statement。
2. `nmsim/config.py`：会影响实验的配置默认值与配置 schema。
3. `nmsim/contagion.py`：社交图、feed、attention 和级联指标。
4. `nmsim/leverage.py`：参照仓、保证金和强平逻辑。
5. `nmsim/llm.py`：Provider/Mock 请求路径与 `parse_order`。
6. `nmsim/market.py`：订单汇总、压力定价与 fills。
7. `nmsim/prompts.py`：Persona 字面量、system/user 提示词模板。
8. `nmsim/sim.py`：回合编排、事件顺序和状态更新。
9. `nmsim/types.py`：科学数据类型/schema。
10. `nmsim/validation.py`：会进入结果的验证指标。

稳定性规则如下：

- `simulation_core_source_hash` 对上述相对路径排序，将相对路径和文件原始字节分别做长度前缀后汇总 SHA-256；绝对路径、目录遍历顺序、时间戳和权限不进入 hash。
- `decision_parser_source_hash` 是 `parse_order` 这一个顶层函数的 LF-normalized 源码 hash；`decision_parser_schema_version` 是对应的显式解析契约版本。
- `prompt_source_hash` 保持 Phase 1 口径，为 `nmsim/prompts.py` 原始字节 hash；`persona_source_hash` 是对该文件中字面量 `PERSONAS` 做排序紧凑 JSON 序列化后的 hash。`Agent.build_prompt` 另由 core 集合覆盖。
- 总指纹对 `fingerprint_schema_version`、parser schema/hash、Prompt/Persona hash、core hash 和固定文件列表的 canonical JSON 做 SHA-256。`event_schema_version` 和 `recording_schema_version` 不被混入总指纹，而是 strict replay 中的独立必匹配字段。

`README`、`docs/`、测试、实验 driver、`nmsim/run.py`、`nmsim/events.py` 及 fingerprint/provenance/recording/reparse instrumentation、run directories、results 和 private logs 均不进入科学文件集。Event/recording 变化由独立 schema 版本管理；fingerprint 算法本身变化必须同步提升 `fingerprint_schema_version`。因此单纯文档变化不应拒绝 replay。但这是保守集合：上述科学源文件按原始字节计算，即使只改其中的普通注释，也会使 core hash 改变并触发 strict replay 拒绝。`git_commit`/`git_dirty` 依然记录于 manifest 和 recording，但 commit 本身不是唯一兼容键。

## Effective Config 契约

Scientific Component Fingerprint 回答“执行的科学源码是否兼容”；`nmsim/config_contract.py` 另行回答“这次运行实际使用的配置是否兼容”。它对 CLI/实验脚本已合并到 `Config` 的最终值做稳定规范化，不改写任何配置或市场状态。`nmsim.run.run` 中原有的 `news_round > n_rounds` clamp 发生在 `RunManager.create` 之前，所以该入口 hash 的是 clamp 后的真实有效值；Phase 1.1A.1 没有为其他入口新增 clamp 或改变默认值。

38 个 `Config` 字段必须在显式 registry 中有 category 和 rationale，没有默认落入 execution 的分支。若 dataclass 新增字段而 registry 未同步，或 registry 残留已删除字段，`validate_config_classification` 立即抛出错误。当前分类边界是：

| 类别 | 典型字段/运行项 | hash 与 Strict Replay 行为 |
|---|---|---|
| scientific | seed/round/news、人口与 noise、价格与 kappa、social/network/digest、leverage/margin、`reference_path` 数据身份 | 进入 `scientific_config_hash`；任一差异在 preflight 拒绝 |
| model_request | requested provider/model/cheap-model、endpoint identity、temperature、max tokens、cache policy | 进入 `model_request_config_hash`；差异在 preflight 拒绝；resolved Provider/model 另由原有 secret-free `model_config` 逐键严格校验 |
| execution | `out_dir`、脱敏 credential 状态，以及 run ID、scenario label、output root、worker、声明的 batching 和 input locator 等运行上下文 | 进入 `execution_config_hash`；差异写入 replay manifest 但允许继续 |

每个 managed run 的 manifest 同时保存：

- `config_hash_schema_version` 和分类 mapping 的 `config_classification_hash`；
- 所有 Config 字段的 secret-free `effective_config_summary` 与 `full_effective_config_hash`；
- 三个 category 的字段列表、规范化摘要和 category hash。

`full_effective_config_hash` 对所有规范化后的 Config 字段做整体身份，因其中也有 execution 字段，它不被当成单一的阻断键。Strict Replay 分别使用 scientific/model-request hash 阻断科学或请求差异，并单独记录 execution 差异。规范化使用排序字段/键、tagged float hex、排序 set 和显式 Enum/tuple 表示；不支持的对象拒绝使用可含内存地址的 `repr` 充数。`reference_path` 存文件字节 SHA-256 与 size，不存绝对科学输入路径；endpoint 和 output/runtime path 只存脱敏 identity hash；API key 只有“已配置/未配置”与固定 redaction sentinel。

`population` 是一个必须保留当前语义的特例：原始 counts 按 key 排序用于审计，同时显式保存按当前 dict insertion order 产生的 `effective_cast`。该 cast 会影响 Agent、batch、社交图和 leverage cohort 顺序，因此不能为追求表面上的 dict 无序性而丢掉。

## 从公开入口到价格更新

```mermaid
flowchart TD
    CLI[python -m nmsim.run / experiment driver] --> CFG[Config]
    CFG --> RM[RunManager.create]
    RM --> CONTRACT[effective Config summaries + hashes]
    CONTRACT --> MANIFEST[running manifest + RunStarted]
    CFG --> MODE{record or replay}
    MODE -->|record| FACTORY[build_llm]
    FACTORY --> MOCK[MockLLM]
    FACTORY --> ANT[AnthropicLLM]
    FACTORY --> OAI[OpenAILLM]
    MOCK --> CACHE[CachingLLM]
    ANT --> CACHE
    OAI --> CACHE
    CACHE --> REC[RecordingLLM]
    CONTRACT --> REC
    MODE -->|replay| SOURCE[llm_records.jsonl]
    SOURCE --> PREFLIGHT[ReplayLLM constructor preflight]
    CONTRACT --> PREFLIGHT
    PREFLIGHT -->|mismatch| FAILED[failed manifest + RunFailed; no RoundStarted]
    PREFLIGHT -->|pass| SIM[run_sim]
    REC --> SIM
    CFG --> SIM

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
RunManager -> source fingerprint + effective Config contract
build_llm -> existing CachingLLM -> RecordingLLM(contract metadata) -> run_sim
record file + current contract -> ReplayLLM constructor preflight (no inner provider) -> run_sim
```

Recording 位于 cache 外侧，保存每个 logical Agent response；manifest 另记真实 Provider calls/cache hits。Provider 细节：

- `provider=auto`：只有环境里存在 `ANTHROPIC_API_KEY` 才选 Anthropic，否则 Mock。
- `provider=anthropic`：constructor 错误时 factory fallback Mock；没有 key 在当前 SDK 中不一定构造失败，请求异常会在 per-prompt 层转 hold，不会换 Mock。
- `provider=openai`：constructor 错误时 fallback Mock；不可达 endpoint 同样是 per-prompt hold。
- 其他 provider 字符串：静默 Mock。
- Real provider 没有传 seed。Anthropic 的若干 model prefix 不发送 temperature。
- sync `.complete` 存在，但主循环总走 `complete_batch`。

### Strict Replay 兼容契约

Strict replay 是 managed replay 的默认且唯一成功路径。`ReplayLLM` 完全不持有 inner Provider；`RunManager` 已在它构造前创建 running manifest、标记 `network_access=false`/`provider_calls=0` 并计算当前 source/config 契约。`ReplayLLM.__init__` 随后在调用 `run_sim`、发出第一个 `RoundStarted` 或消费第一条历史 response 之前完成 preflight。实际检查层次是：

1. **Recording envelope 与完整性**：JSON、连续 call sequence、record type、Prompt/response hash 和单一 recording schema 先被验证。真正的 schema 1.0 recording 立即以 `legacy_recording_missing_replay_contract` 拒绝；不从当前默认值猜测或回填缺失契约。
2. **Effective-config 契约完整性**：当前 recording schema 1.1 每条记录必须有 config hash schema、classification hash、三类 hash/字段列表/规范化摘要以及 full hash。Phase 1.1A 期间、在该扩展前产生的 schema 1.1 recording 虽有 source fingerprint，但缺少运行时配置契约，因而以 `recording_missing_runtime_config_contract` fail closed。
3. **运行时科学/模型请求配置**：`config_hash_schema_version` 和 `config_classification_hash` 必须一致；`scientific_config_hash` 或 `model_request_config_hash` 不一致时，按规范化摘要列出具体差异字段，只显示该字段安全值 hash 和 category hash 缩写，随即拒绝。`execution_config_hash` 不作阻断键；其字段级差异写入 manifest 的 `replay_compatibility` 并显式标记 allowed。
4. **科学源与 schema 身份**：`fingerprint_schema_version`、`decision_parser_schema_version`、`decision_parser_source_hash`、`event_schema_version`、`recording_schema_version`、`prompt_source_hash`、`persona_source_hash`、`simulation_core_source_hash` 和 `scientific_component_fingerprint` 必须逐字段一致。同一 recording 内所有记录必须声明同一 source/config 身份。
5. **Resolved 模型逻辑配置**：Provider（requested/resolved）、最终模型名、temperature、max tokens、cache/use-cheap-model 状态和 endpoint identity hash 在原有 secret-free `model_config` 中逐键比较；credential 不进入该序列化对象。
6. **每次 logical request**：只有 constructor preflight 全部通过后，仿真才会开始。每轮的 Agent identity、Persona identity、round、全局调用序号、batch 序号/索引/大小与长度前缀的组合 Prompt hash 必须与下一条记录完全对应。整批全部匹配后 cursor 才推进，结束时还必须消费全部 recording。

`RecordingLLM` 在每条 schema 1.1 logical-call record 中保存 `RunManager.replay_compatibility`，即 source fingerprint/schema、Git identity 和全部 effective-config 契约；不仅依赖 manifest sidecar。任一阻断字段不一致均立即抛出 `ReplayMismatchError`。managed 运行会捕获该错误，在已创建的 run directory 保留 failed manifest 和 `RunFailed` 事件，且不生成看似成功的 canonical 结果。constructor preflight 错配时第一轮尚未开始、honest-N 为 0；若是 preflight 通过后的逐请求错配，manifest 则保留已完成的真实数量。错误不回显完整 Prompt、private rationale、API key 或 Authorization 内容；此路径没有网络访问或 Provider fallback。

schema 1.0 和 pre-extension schema 1.1 的上述拒绝只表示它们不满足当前 Strict Replay 证据契约，不表示历史运行无效或 raw response 损坏。两者均仍可进入不继续市场的离线 Reparse Audit。

worker/batch/cache 在这个边界中的分工是显式的：

- driver `worker_count` 只并行独立 run，归入 execution runtime summary；可与原运行不同，但差异必须记录。它可影响真实 Provider 服务端的并发采样，因此这一放宽只对已固定 raw response 的离线 replay 安全，不构成真实 Provider 统计确定性保证。
- 当前没有用户可配的 per-run batch size；simulation 固定每轮提交一个包含全部 LLM Agents 的 logical batch。manifest 中声明的 driver/batching 描述属 execution，但实际 batch sequence/index/size 仍在 logical request 层逐条严格校验；population 及其 `effective_cast` 则属 scientific preflight。
- `cache_enabled` 会改变响应是否复用及 Provider miss-batch/采样路径，归入 model_request，同时受 category hash 和 resolved `model_config` 严格约束，不允许在 Strict Replay 中静默改变。Recording 位于 cache 外层，仍只保存 logical batch，未单独保存 Provider 实际 miss-batch 的详细组成。

`git_commit` 和 `git_dirty` 用于 provenance，不代替上述精确契约。若来源 commit 和当前 commit 不同，但 source fingerprint、scientific/model-request config 与其他 strict 字段都相同，replay 可成功，manifest 明确写入 `cross_commit_same_scientific_fingerprint=true` 并保留 source/current commit。单纯 README/普通文档变化可属于这一情形。反之，即使 commit 相同，dirty worktree 中的科学源文件或运行时科学配置变化也会更改具体 hash 并被拒绝。

这一路径是“在已记录请求契约和市场状态下返回同一 LLM 字符串”，不会为已改变的市场状态生成反事实 Agent 回答，也不等同于真实 Provider 的统计可复现性。

### 离线 Reparse Audit 边界

`python3 -m nmsim.reparse_audit --run <历史 run directory> --out <audit 父目录>` 读取历史 `llm_records.jsonl` 的 raw response，并用当前 `parse_order` 重新解析。它将当前结果与历史 `AgentDecisionParsed` 或 recording 内显式保存的 parsed decision 按 request identity 对齐，比较 rationale 是否存在、sentiment、public take、action、quantity、limit/reservation price、parse/fallback status 和 validation errors。如无历史 parsed decision，则如实记为 `comparison_unavailable`。

它永远新建不可覆盖的 `reparse-audit-<timestamp>-<uuid>/` 目录，且 `--out` 不得位于历史 run 内，因此不修改源 run 和源 recording。输出为：

- `reparse_results.jsonl`（0644）：机器可读的 request identity、新旧公开 decision 和逐字段 diff；private rationale 只以 present/hash/changed 类元数据表示。
- `reparse_private.jsonl`（0600）：新旧 private rationale 正文和私有详细比较。
- `reparse_summary.json`（0644）：总数、重新解析成功/失败、完全一致/部分一致/差异/无法比较数、逐字段差异次数和安全版本契约。

Reparse audit 不构造或调用 Provider，不访问网络，不调用 `run_sim`，不将新 decision 变成订单，不继续社交/市场/风控状态，也不生成新价格轨迹。它是解析器升级差异的诊断工具，不是 strict simulation replay，更不应表述为原实验的“完全可复现”。

当前 `parse_order` 没有独立的 validation/fallback 结果类型，`Order` 也没有 `reservation_price` 字段；audit 对这些列使用原始 JSON 和当前 fallback 标记生成诊断值。历史事件缺字段时列为 unavailable，不把诊断值冒充为历史可执行字段。

## 关键类型及字段

### `Config`

38 个字段，完整实效表见 [CODEX_HANDOFF_AUDIT.md](CODEX_HANDOFF_AUDIT.md)。它同时混合 timeline、population、provider、social、leverage、validation 和 output concern，没有独立 Scenario/Policy/Engine 配置。Phase 1.1A.1 没有拆分或改写该 dataclass，只在边界将其 27 个 scientific、9 个 model-request 和 2 个 execution 字段逐一登记到 `CONFIG_FIELD_RULES`；任一未分类新字段都使契约构建 fail closed。

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
| Config 与兼容契约 | `RunManager` / `run.run` | 是；manifest 保存脱敏 Config、分类摘要/hash 和 resolved LLM 身份，legacy `config.json` 也会拒绝持久化显式 secret |
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
| Thread/process scheduling | driver `workers`、provider batching | manifest 记录 driver workers、声明的 batching 和实际 logical batch sizes；gather 保持返回索引，但 SDK 内部调度与服务端 batch 浮点/采样仍可能受并发影响 |

## 输出边界

主 CLI 在 `<out>/runs/<run_id>/` 固定写原六类结果：

1. `price_path.csv`
2. `reasoning_traces.csv`
3. `propagation.csv`
4. `stylized_facts.json`
5. `config.json`
6. `sim_overview.png`（matplotlib 可用时）

并新增 `run_manifest.json`、`events.jsonl`、`private_events.jsonl`、`llm_records.jsonl`。manifest 与每条 LLM recording 保存 parser/event/recording/fingerprint schema 版本、Prompt/Persona/parser/core 哈希、总科学指纹、Git 身份，以及版本化的 effective-config 分类、脱敏摘要和 full/scientific/model-request/execution hashes；replay 运行的 manifest 还保存 strict 检查结果、execution 差异与 cross-commit 标志。事件现记录 observation hash/public feed、LLM logical calls、所有提交订单、真实 Agent fill、逐轮 portfolio 变更、margin/liquidation、metrics、batch 与 run status。

Reparse audit 不写入 managed run directory，而是在单独 audit 目录写出 public results、0600 private rationale sidecar 和 summary；该目录不包含价格轨迹或仿真 canonical 输出。

仍未完整记录/实现：Provider SDK 中间 retry/request id、显式 market-maker inventory、phantom seller 账本、独立 adjacency artifact，以及没有 `.git` 时的 commit/diff。根目录旧输出只在安全时通过 `latest` symlink 保持兼容，普通历史文件永不覆盖。
