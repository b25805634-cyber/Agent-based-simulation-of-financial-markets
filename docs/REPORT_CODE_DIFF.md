# 报告—代码差异表

> 状态说明：本表是 Phase 1 实施前的差异基线。运行 manifest、结构化 events、raw final response 和 strict replay 的当前状态见 [RUN_PROVENANCE.md](RUN_PROVENANCE.md)；表中的市场与研究效度差异仍然成立。

## 范围说明

用户指定的 `docs/handovers/导师汇报_综合版_2026-07-15.html` 当前不存在，仓库也没有其他 HTML。下表的“报告声明”来自本任务中给出的结构/数据流/结论要求，以及现有 `README.md`、`REPORT.md`、`PROJECT.md`、`BUILD_BRIEF.md`。因此：

- 能与当前源码/运行直接比对的项目给出明确状态。
- 依赖缺失 HTML 的专有文字、图注或嵌入代码，一律记为无法验证。
- 历史结论“数值可由当前 analyzer 重算”和“实验 provenance/因果识别充分”是两件事，分开判定。

状态含义：

- **一致**：核心声明与当前代码和/或本次运行一致。
- **部分一致**：能力存在，但语义、范围、记录或验证弱于声明。
- **不一致**：当前源码或运行直接反驳声明。
- **无法验证**：缺所指报告、Git、凭据、原始记录或外部服务证据。

## 结构与入口

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| `nmsim/config` 集中配置和本地随机种子 | `nmsim/config.py`, `sim.py`, `agents.py`, `contagion.py` | 部分一致 | `Config` 集中 38 个字段；本地 RNG 显式 seeded；但无校验/schema/version，真实 LLM 不用 seed | 保留当前默认；Phase 0 增 parameter-route tests 和 runtime manifest |
| `nmsim/types` 有 Order、Statement、Decision 等类型 | `nmsim/types.py` | 部分一致 | 只有 `Order`、`Statement` 和 `Side`；没有 `Decision`/Fill/Portfolio/Event | 文档改为当前事实；Phase 2 再引入类型，不做本次补写 |
| `nmsim/llm` 有 Mock、Anthropic、OpenAI-compatible/vLLM、缓存、异步批量 | `nmsim/llm.py` | 一致（能力）/部分一致（可靠性） | 三 provider 路径、cache、gather 均存在；失败可静默 hold，cache key/记录不完整 | 先记录 degraded/provenance；再单独评审 fail-closed |
| `nmsim/prompts` 有 6 类 Persona 和模板 | `nmsim/prompts.py` | 一致 | 六个 persona；real system/user builder 存在 | 给 prompt/source 加 hash 和版本 |
| `nmsim/agents` 有 Agent 状态、噪声 Agent、模型输出解析 | `nmsim/agents.py`, `llm.parse_order` | 一致 | Agent/NoiseAgent/ingest/parser 均存在 | 将 parser 责任长期移到 policy boundary；先写 characterization |
| `nmsim/market` 是净订单流压力撮合 | `nmsim/market.py` | 一致 | 公式按净流/总流，非 LOB | 报告应称 pressure clearing，不称订单撮合或真实成交 |
| `nmsim/contagion` 有社交图、种子、摘要、级联指标 | `nmsim/contagion.py`, `sim.py` | 一致（实现）/部分一致（识别） | topology/seed/digest/metrics 均有；cascade 仍混入 price channel | 指标改名/分解留 Phase 3；旧指标保留兼容 |
| `nmsim/leverage` 是 V2 冻结参照仓、纯价格保证金和强平 | `nmsim/leverage.py`, `sim.py` | 一致 | 参照多头与 voluntary portfolio 分离；price-only ratio；一次性 next-round sell | 增时序/末轮 characterization；不改当前逻辑 |
| `nmsim/sim` 是回合主循环 | `nmsim/sim.py` | 一致 | 完整入口 `run_sim` | Phase 2 才解耦，不在接管 PR 重构 |
| `nmsim/run` 是 CLI | `nmsim/run.py` | 一致但覆盖不全 | 主入口可运行；只暴露 Config 子集 | 记录公开 CLI 契约；新增参数须兼容评审 |
| `validation` 有 RMSE、DTW、崩盘指标、风格化事实 | `nmsim/validation.py` | 一致 | RMSE/DTW/reaction/kurtosis/tail/ACF 均存在 | 加样本量/不确定性说明；勿把 24-return 点估计外推 |
| 还有 `experiments/`、`repro_check.py`、多类 results | `experiments/`, `results*` | 部分一致 | `repro_check.py` 实际在 `experiments/`；大部分目录存在；没有 `results_critsweep/` | 报告列真实路径、run 数、完整/不完整状态 |
| 旧单文件已 refactor 为 package | `narrative_market_sim.py`, `nmsim/` | 一致但有重复债务 | package 是当前入口；旧文件仍可执行声明但本机写死 `/mnt` 失败，persona/RNG 已分叉 | 标记 archived/unsupported，不删除历史文件 |

## Scenario、观察与 Prompt

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| Scenario/Config 定义轮数、新闻、人群和 seed | `Config`, `run_seed.build_population` | 部分一致 | 没有 Scenario 类型；Config 定义默认，人群 sweep 在实验 helper 构造 | Phase 2 引入 Scenario 前先 frozen adapter，保持 Config/CLI |
| 新闻在可配置 round 注入 seed 子集 | `sim.py:61-95` | 部分一致 | seed subset 正确；但 seed 从 `r>=news_round` 每轮重复看到同一“fresh news” | 报告明确 persistence；未来若改单次事件必须做迁移实验 |
| 非 seed 只通过社交 + price tape 获知故事 | `sim.py:91-95`, `prompts.py` | 一致 | 非 seed `news=""`；仍看 price 和公开 feed | 保持并加 observation event test |
| Agent 能看到数值 fundamental anchor/value | `Agent.build_prompt`, `prompts.build_user` | 不一致（若报告如此表述） | real prompt 不含 `fundamental_value`；只有 persona 文字说价值锚 | 报告区分“人格叙述”与“数值 observation” |
| Agent 看到自己的 cash/shares/memory | `prompts.py:216-228` | 一致 | real/mock prompt 都有 voluntary state 和最近 3 条 memory | 明确它们不受下单约束，且无 leverage state |
| 他人 private reasoning 不传播 | `Agent.statement`, `neighbor_feed` | 部分一致 | standard schema 仅 public/sentiment；但 parser 对 legacy `rationale` 同时填 public/private | Phase 0 加 privacy regression；Phase 1 拆 public/private schema validation |
| reasoning traces 永不丢失/完整保存 | `parse_order`, `sim.traces`, `run._write_traces` | 不一致 | raw response 丢弃；public 140/90、reasoning 240 字符截断；Noise/phantom 不记录 | 报告改为“解析后摘要”；Phase 1 append-only raw events |
| System/user prompt 在模板集中构造 | `prompts.build_system/build_user`, `Agent.build_prompt` | 部分一致 | real 集中；Mock 另在 `agents.py` 构造一套 schema/prompt | 长期由 AgentPolicy 封装；保持两路径语义明确 |

## Persona 参数和 social_weight

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| 四个 0..1 persona 刻度驱动真实 LLM 行为 | `prompts.PERSONAS`, `agents.make_agents` | 不一致 | real system 只使用 `persona['persona']` 文本；三个刻度完全不读，social 刻度只参与 feed gate | 报告称其为 metadata；若将来参数化需新实验版本 |
| Mock 与 real persona 同一组数值 | `_MOCK_PARAMS_BY_ID` | 不一致 | Mock 使用独立 `w_value/news_value/w_trend/w_senti/w_social/...` | 不用 Mock 数值验证 real persona dose response |
| 有效 social coupling = 全局 gain × persona susceptibility | `sim.py:97-103` | 部分一致 | 先乘并 clip 是事实；但 real prompt 不含结果，只按是否 >0 给完整 feed | 分 provider 描述；当前 real 是 threshold，不是连续 gain |
| `social_weight` 是连续增益 | `Agent.build_prompt(mode="real")`, `prompts.build_user` | 不一致（real） | 动态核查：0.1 与 1.0 的全部 real prompts 相同；无 weight literal | 历史 gain sweep 不得作剂量解释；先加 characterization |
| `social_weight` 是连续增益 | Mock prompt/`MockLLM.complete` | 部分一致（Mock） | 数值进入 blend，但先 clip，且再乘 Mock `w_social`；quant 为 0 | 可称“截断的连续 Mock coupling” |
| `--no-social` 只关闭社交通道，price tape 保持 | `sim.py` | 一致 | social feed/graph 关闭；price 仍进入 prompt | 因真实 LLM sampling 未配对，因果措辞仍需降级 |

## 调用、解析、缓存和并发

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| structured output 会校验、重试、降级 | `parse_order`, `acomplete` | 部分一致 | 非 JSON 重试；合法 JSON 的坏字段只 coercion；最终 hold | 记录 validation error 逐字段；保留旧 normalized output |
| API/解析失败可被 run health 捕获 | `run_seed._BAD_MARKERS` | 部分一致 | experiment 能统计最终 marker；main CLI 无 health gate；raw attempts 不保留 | 统一 run status；失败 run 不再删除 |
| cache keyed by `(persona,state_hash)` | `CachingLLM._key` | 部分一致 | 实际 key 是完整 system+user hash，persona 单独 hash 仅 Mock `PERSONA:` 可取；real persona label 为空但 system 已进 state hash | 文档直接说 full-prompt digest |
| cache key 包含模型、温度和相关配置 | `CachingLLM._key` | 不一致 | 不含 provider/model/temp/max_tokens/agent id；只含 prompt | Phase 1 key 加 runtime fingerprint；schema versioned |
| cache 带来跨 run 可复现 | `CachingLLM._cache` | 不一致 | 仅进程内 dict，不持久化；首次真实 completion 仍由 provider 决定 | 不再用 cache 声称跨 run determinism；Record/Replay 才能做到 |
| async batch 顺序正确 | `asyncio.gather`, cache miss index | 一致 | gather 和 miss index 都保持输入顺序 | 加顺序 test |
| concurrency/batch composition 有运行记录 | outputs/result JSON | 不一致 | 没有实际 batch/miss/workers/concurrency/retry 字段 | run manifest/event log 记录 |
| max LLM agents 控制成本 | `make_agents` | 一致 | cast 最多 40，默认无 population 最多 persona 数 6 | 记录 realized count；避免把配置 cap 当实际 count |
| token/cost 有输出 | `CostTracker`, `run_seed` | 部分一致 | main 只打印，experiment 写；Mock 和 API exception 不计 calls | manifest 写 requested/attempted/succeeded/cached/failed |
| Provider 不可用会 graceful fallback Mock | `build_llm`, async exception path | 部分一致 | constructor error才 fallback；请求失败变 hold，未知 provider静默 Mock | resolved provider/status 必须显式；unknown provider 报错需兼容 PR |

## 社交传播与指标

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| influencer 是最高 degree hub 且默认 seed | `build_network`, `run_sim` | 一致（默认 network） | hub 连所有节点；forced seed；fully_connected 时 degree 并列 | 报告注明 fully connected tie 和 demotion 例外 |
| digest 是注意力摘要 | `neighbor_feed` | 一致 | 可见 statements shuffle 后按绝对 sentiment 取 top-k | 记录实际被选 message id，才能 replay |
| 传播的是 sentiment + public_take，不是 order | `neighbor_feed` | 一致 | feed tuple 只含 sentiment/text | 保持 privacy test |
| cascade 是社交传播因果指标 | `PropagationMetrics.record` | 不一致/部分一致 | social-off 也可因 price 对齐产生 cascade；历史 social-off peak 常为 0.5 | 改称 alignment metric；Phase 3 做 channel-separated counterfactual |
| placebo 不会误触发 cascade | seed-sign conviction gate | 部分一致 | 当前代码有 gate；旧 `results_2x2` 是修复前并显示 placebo cascade | 新旧 schema/data 分目录并标版本；旧 summary 不覆盖 |

## 市场、成交和杠杆

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| 价格由所有 Agent 净订单流和 market mechanism 产生 | `clear_by_pressure` | 一致 | net/total pressure 公式 | 保持为当前 coarse market baseline |
| `limit_price` 是 reservation price/可执行限价 | `market.py` | 不一致（执行） | 完全不读 limit；注释明确只记录 | 报告明确“非执行字段”；未来语义变更须新 engine |
| 买单受现金、卖单受持仓约束 | `sim.py:160-165` | 不一致 | 所有 fill 后直接变更；没有 pre-check | 当前研究结果依赖无限 balance，不可静默修复 |
| 不允许负现金、裸卖空或无限仓位 | 同上 | 不一致 | 历史 run 可重建 influencer `shares≈-4000` | 将约束作为 Market/RiskEngine 新版本实验因子 |
| 所有订单成交由显式做市商吸收 | `market.py` | 部分一致/不一致 | “吸收”只在注释；无实体、库存、现金、P&L | 报告称 implicit residual counterparty；Phase 5 才内生流动性 |
| `volume` 与 fills 是同一成交定义 | `market.py` | 不一致 | volume=min(buy,sell)，fills 包含两侧全量；买10卖3返回 volume3/fills+10,-3 | 先记录双重定义；任何修正都需结果 schema migration |
| Agent cash/shares/fills 有完整结果记录 | `run.py` outputs | 不一致 | 只在内存更新；主文件不写 fills/final portfolio | Phase 1 event log/sidecar；不改旧 CSV |
| V2 margin 是 price-only、与 voluntary book 分离 | `leverage.py` | 一致 | equity ratio 只由 frozen ref/debt/price；voluntary state不变 | 增 invariant tests |
| 强平卖压进入下一轮 | `sim.py:156-179` | 一致 | pending queue 时序明确 | 记录 event scheduled/executed；末轮未执行需标记 |
| 强平有完整市场结算账本 | `leverage.py` | 部分一致 | 记录 residual equity/P&L 和 phantom order；无 creditor/cash/inventory/fill | 报告称 synthetic pressure layer，不称完整清算 |

## 日志、输出和可复现

| 报告中的声明 | 对应代码路径 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| `config.json` 是 exact config，可完全复现 | `run.py:123-124` | 不一致/部分一致 | requested Config 被写；无 resolved provider/model/env override/source/prompt/deps/concurrency；API key 可泄漏 | 改为 redacted requested config + runtime manifest |
| 所有历史结果可追溯 Git commit | `.git`, results JSON | 不一致 | 当前无 `.git`；JSON 无 SHA | 先恢复 Git/remote；现有结果标 provenance unknown |
| 结果记录模型 | `run_seed.py:123-127` | 部分一致 | 后期 JSON 有 `model`；早期 202 个 run 多数缺；main CLI 不写 resolved model | schema version + required runtime model/service fingerprint |
| 结果记录 prompt/raw output | outputs/results/traces | 不一致 | 无 prompt hash/raw response；trace 仅解析后截断字段 | Phase 1 append-only events + content-addressed blobs |
| 失败记录不丢 | driver `os.remove` | 不一致 | bad run JSON 被删后重试；failure log 只记录最终放弃 | 从 delete 改 immutable attempt directories；兼容汇总只读 accepted attempts |
| Mock fixed seed 可复现 | `repro_check`, 本次双 run | 一致 | 跨 3 hashseed PASS；双 run 非路径产物相同 | 纳入 CI characterization |
| temp=0 使真实 LLM 完全确定 | Phase2b 文档/PROJECT 最终自检 | 不一致 | Phase2b 无强平时仍从新闻前分叉；provider无 seed | 保留“非确定”结论，不再以 temp=0 作纯配对前提 |

## 验证与统计

| 报告中的声明 | 对应代码/结果 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| RMSE 主指标按 shock t=0 对齐 | `validation.logprice_rmse`, `run_seed._assemble` | 一致 | sim 用 last pre-news index；Meta news row为 323，下一点为崩盘 | 给 episode schema/对齐规则加测试 |
| DTW 作为补充 | `validation.dtw_distance` | 一致 | classic 1-D absolute-cost DP | 记录 horizon/path normalization |
| recovery clamp/无崩盘 None | `reaction_shape` | 一致（当前代码） | 当前 clamp；旧结果仍有 >1 | 不改旧数据，标 schema/code era |
| 2x2 四格各 N=15，drop/d 数值 | `results_2x2`, `aggregate_grid` | 一致（历史数值） | 本次重算得到 -0.265/-0.446、d=1.716 | 只称“旧 analyzer 可复算”；补 paired/model/provenance 分析 |
| 2x2 是同 seed paired inference | `aggregate_grid.py` | 不一致 | analyzer 用独立样本 pooled d，不做同 seed paired CI；真实 LLM sample 未共同控制 | 重写 versioned analyzer，不覆盖旧 summary |
| composition sweep 8/8/16 pairs 与 CI | `results_sweep`, `aggregate_sweep` | 部分一致 | 均值可复算；N=16 时 t critical 回退 1.96，CI 略窄 | 修 analyzer 并发布 corrected supplement |
| proportional stabilizer slope 0.40 | `additive_test.py` | 一致（数值）/部分一致（识别） | 当前 OLS重算0.397；未控m、采样误差、endpoint漂移 | 降级为 exploratory association；Phase3预注册多事件设计 |
| ablation 36 runs、三条关键 CI | `results_sweep`, `ablation.py` | 一致（数值）/部分一致（因果） | 表值全部可复算；不同条件真实 LLM sampling不配对，失败重试选择不可审计 | Record/Replay/paired response design 后复验 |
| Higgs 方向复现、N=8 未显著 | `results_sweep_higgs` | 一致 | diff +0.030，CI跨0；correlation +0.780 | 报告保持“方向证据、非坐实” |
| Leverage naive effect欠功效/CI跨0 | `results_sweep`, `lev_analyze` | 一致 | 当前 analyzer 同报告 | 不作净因果结论；先解决 replay/paired noise |
| Phase2b temp=0 前提失效，critsweep未运行 | `PROJECT.md`, `results_phase2b`, 目录状态 | 一致 | 32 run存在；无 `results_critsweep/` | 保持停止状态，Phase1 Record/Replay 后再设计 |
| stylized facts 验证市场真实性 | `validation.py` | 部分一致 | 指标可算；24 returns、单事件、无不确定性/多事件 | 称 instrumentation，不称 validation complete |

## 外部与历史声明

| 报告中的声明 | 对应证据 | 当前状态 | 证据 | 建议处理 |
|---|---|---|---|---|
| 指定导师汇报 HTML 是最新综合叙事 | 文件系统 | 无法验证 | 文件缺失 | 请补回原文件并保留 hash/来源 |
| 历史 run 使用 MiniMax-M2.7/HiggsAI | 部分 JSON、REPORT/PROJECT | 部分一致/无法验证 | 后期 59 个 run有 model；大量旧 JSON 无 model；无服务端 request metadata | 无 model 的 run 标 unknown/claimed，不补写猜测值 |
| Anthropic 默认模型和价格是当前有效 | `llm.py` 注释/常量 | 无法验证 | 本次无凭据、未访问外部服务 | 使用官方 provider metadata 或把价格表标估算版本 |
| 没有未提交修改 | Git | 无法验证 | 无 `.git` | 恢复原 Git 仓库后重新审计 |

## 最需要在报告中立即更正的五句话

1. 把“real `social_weight` 连续调节社交耦合”改为“当前 real 路径只区分零/正；连续值只在 Mock 路径中进入公式”。
2. 把“完整 reasoning traces 永不丢失”改为“保存解析后且截断的 reasoning/public fields；raw response 未保存”。
3. 把“所有订单成交且 market maker 吸收”补充为“market maker 无显式账本，fills 与 matched volume 口径不同”。
4. 把“seed/temp/cache 保证真实模型复现”改为“仅本地 RNG/Mock 可复现；真实 provider 首次响应没有确定性保证”。
5. 把“历史结果可复现/可追溯”改为“数值可由当前 analyzer 从现存 JSON 重算，但 commit、完整 prompt/config/model/raw attempts 不可追溯”。
