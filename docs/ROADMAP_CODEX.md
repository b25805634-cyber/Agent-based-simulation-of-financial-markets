# Codex 分阶段路线图

## 路线原则

1. 先冻结和记录当前语义，再解耦；不以“更优雅”为理由改变实验结果。
2. 旧 CLI、旧六类主输出、旧实验 JSON 和历史结果目录保持可读；新增信息优先 sidecar/schema version。
3. 每次只改变一个识别层：记录、架构、实验设计、Agent 生态、市场机制不可混在同一 PR。
4. 默认 pressure market、六 persona、当前 prompt、当前 metrics 和当前 leverage V2 都是 compatibility baseline，不是永恒正确模型。
5. 真实 LLM 的非确定性由 Record/Replay 和分布设计处理，不用 temperature=0 或 seed 作虚假保证。
6. 新依赖必须说明必要性、stdlib/现有依赖替代方案、锁定方式、运行和存储影响；前两个阶段原则上可只用 stdlib 和现有 SDK。

## Phase 0：接管和基线

### 目标

恢复版本/环境边界，用 characterization tests 固定“当前实际行为”，并让每次 run 至少能说明自己是谁、是否 degraded；不修正市场语义。

### 具体文件

- 恢复或重新建立 Git repository/remote；这一步需项目所有者确认正确 origin，不由代码自动猜测。
- 新增 `pyproject.toml` 或明确的最小环境声明，锁定 Python 支持范围和现有运行依赖；不趁机升级 SDK。
- 新增 `tests/test_characterization_*.py`：
  - Mock golden price/trace/propagation。
  - cross-`PYTHONHASHSEED` reproducibility。
  - real prompt observation boundary。
  - current real `social_weight` zero/positive threshold behavior。
  - current pressure `volume`/full-fill behavior。
  - current unlimited cash/share behavior。
  - next-round liquidation timing and final-round pending event。
  - public/private response field routing。
- 新增 `nmsim/provenance.py`，由 `nmsim/run.py` 只新增 `run_manifest.json` sidecar：
  - schema version、source/prompt hashes、redacted requested config。
  - resolved provider/model、Python/依赖版本。
  - actual agent count、round count、attempt/success/cache/error counts、degraded status。
  - 开始/结束时间和输出 hashes。
- `experiments/run_seed.py` 读取同一 manifest 信息，不再各自发明 provenance 字段；第一步可只增加字段，不删旧字段。
- `README.md` 增当前环境/测试命令和“真实 Provider 不保证确定”的显著说明。
- 给 `narrative_market_sim.py` 增非执行性 archive 标记/文档；暂不删除。
- 保留本次四份 `docs/` 审计文档。

### 风险

- 加 manifest 时意外把 API key 或私有 prompt/raw text写入。
- 测试把偶然路径、绝对 out dir 或 matplotlib 元数据误当科学语义。
- 依赖锁定时无意升级 provider SDK，改变 response/timeout 行为。
- 对 unknown provider 或全失败 run 立即改退出码会影响历史 driver；应拆分兼容 PR。

### 测试

- `python3 -m unittest discover -v`，首阶段使用 stdlib 避免新增 pytest 依赖。
- `PYTHONHASHSEED=0 python3 -m experiments.repro_check`。
- 相同 Mock config 双 run：对现有五类非 config 主产物逐字节比较；`config.json` 只允许 out path 差异。
- PR 前后运行已有最小 1-seed Mock 2x2，比较四个 JSON 的旧字段。
- 历史 `outputs*/results*/traces/` 做 SHA-256 manifest，PR 前后必须相同。
- manifest secret scan：key 名和值均不得包含实际 API key。

### 回滚

- provenance 先作为新增 sidecar，不改变旧文件；回滚可删除 sidecar writer 和测试辅助，不触碰核心结果。
- 依赖声明单独 commit；若本机/CI 不兼容，可回退声明而不回退科学代码。
- 不改默认 exit behavior；degraded 状态只记录。后续 fail-closed 另开 feature flag/兼容迁移。

### 完成标准

- 正确 Git origin/初始 baseline SHA 可用，工作树状态可检查。
- 环境可从声明安装；主 CLI、Mock smoke、repro 和最小实验在干净环境通过。
- 当前关键“缺陷语义”也有 characterization，防止重构静默修复。
- 每个新 run 有 redacted、versioned manifest，并明确 resolved provider/model/status。
- 历史文件 hash 未变。

## Phase 1：运行溯源、事件日志和 Record/Replay

**实施状态（2026-07-15）：基础里程碑已完成。** 已落地 immutable run directory、原子 manifest、公共/私有事件、final-response Record/Replay、主 CLI/`run_seed` 兼容接入、失败结果归档和 stdlib tests；legacy-only `rationale` 不再自动公开。SDK 内部 retry 的中间 response/request id 和 event-derived full replay checker 仍列为后续小型兼容工作；详见 [RUN_PROVENANCE.md](RUN_PROVENANCE.md)。Phase 2 尚未开始。

### 目标

让一次仿真可以被审计、重放和派生，而不是只留下截断 CSV；先解决真实 LLM 条件间无法共同控制的问题。

### 具体文件

- 新增 `nmsim/events.py`：versioned event envelope，至少包括：
  - `RunStarted/RunFinished`
  - `ObservationBuilt/PromptPrepared`
  - `LLMRequest/LLMResponse/LLMError/ParseResult`
  - `StatementPublished/DigestDelivered`
  - `OrderSubmitted/MarketCleared/FillApplied`
  - `MarginBreached/LiquidationScheduled/LiquidationApplied`
- 新增 `nmsim/recording.py`：append-only JSONL writer/reader；每条 event 有 run id、round、sequence、schema version、content hash。
- 新增 `nmsim/replay.py`：
  - **LLM replay**：按 prompt hash 返回已记录 raw response，不访问 provider。
  - **full replay check**：从事件验证 price/fill/state/metric 派生一致。
- 修改 `nmsim/llm.py`：显式 attempted/succeeded/failed/retried/cached；cache key 加 provider/model/temperature/max_tokens/prompt schema fingerprint；保留旧 key behavior behind compatibility version。
- 修改 `nmsim/sim.py`：在既有步骤旁发 event，不改变步骤顺序/RNG 消费。
- 修改 `nmsim/run.py`：旧 CSV/JSON 由 SimResult 继续写；可选地从 event stream 交叉验证，不替换默认格式。
- 修改 `experiments/run_seed.py` 和所有 driver：每个 attempt 独立目录/ID；失败 attempt 永不删除；accepted attempt 用 index 指向。
- 新增 `docs/EVENT_SCHEMA.md`、`docs/REPRODUCIBILITY.md`。

### 风险

- raw response 和 full prompt 可能包含敏感内容；日志体积显著增长。
- instrumentation 若在 RNG 或 batch 之间做额外迭代，可能改变顺序。
- replay key 设计错误会跨 model/config 复用。
- 历史 result 没有 raw response，不能 retroactively full replay。

### 测试

- Record 一次 Mock run，Replay 后 price、orders、statements、metrics、final state 全等。
- 用 fake real provider 记录不同 response/retry/error，再断网 replay，调用次数必须为 0。
- 改 model/temp/max_tokens 任一项，cache/replay key 必须变化。
- 同一 batch 重复 prompt 的去重/非去重策略有明确测试和 event 证据。
- standard schema 下 private reasoning 从不进入 `DigestDelivered`；legacy `rationale` 不能自动公开。
- 强平 scheduled round 与 applied round 可追踪；末轮 pending 标 `unapplied`。
- 大 run 写盘中断后 JSONL 可恢复到最后完整 event。

### 回滚

- event recording 由 `record_mode=off|metadata|full` 控制，默认先 `metadata`；关闭时必须走旧路径。
- 旧 CSV/JSON writer 不删除；若 event writer 有问题可关 flag，价格路径不受影响。
- cache key 升级带 version prefix，旧 cache/replay 只读兼容，不混用。

### 完成标准

- 每次 accepted/failed attempt 都有不可变记录，原始失败不再被删除。
- 同一 recorded response 可在无 provider 条件下重放出完全相同的 orders/price/metrics。
- run manifest 能列出 batch、concurrency、retry、cache、health 和所有 artifact hashes。
- private/public 边界由 event-level test 保证。

## Phase 2：Scenario、AgentPolicy、MarketEngine、RiskEngine 解耦

### 目标

在 characterization 和 replay 保护下拆责任，使研究者能替换信息、policy、市场或风险层而不改回合控制；默认 adapter 必须复现当前语义。

### 具体文件

- 新增 `nmsim/scenario.py`：timeline、news events、population spec、reference alignment；提供 `Config -> LegacyScenario` adapter。
- 新增 `nmsim/policy.py`：`AgentPolicy.observe/decide` 协议：
  - `PromptLLMPolicy`
  - `MockPolicy`
  - `NoisePolicy`
  - 不把 persona prompt 逻辑移成硬编码策略。
- 新增 `nmsim/market_engine.py`：`MarketEngine.clear(state, orders)` 协议和 `LegacyPressureMarket`。
- 新增 `nmsim/risk_engine.py`：`RiskEngine.after_clear` 协议和 `LegacyFrozenReferenceRisk`。
- 新增/扩展 `nmsim/types.py`：immutable Observation、Decision、Order、Fill、MarketClear、RiskEvent；旧 TypedDict adapter 保留。
- `nmsim/sim.py` 变为 thin orchestrator，严格保持当前 phase ordering。
- `nmsim/prompts.py` 保持内容和字节 hash；仅由 `PromptLLMPolicy` 调用。
- `nmsim/run.py` 和 `experiments/run_seed.py` 继续接受旧 Config/CLI，内部用 adapter。

### 风险

- 最危险的是改变 RNG 消费、agent 顺序、price rounding、news persistence、pending liquidation 时序或 trace 截断。
- dataclass/immutable type 转换可能改变 JSON 数字/字段顺序。
- 把目前的缺陷（如全 fill）“顺手修好”会使所有历史结果失去可比性。
- 接口过度泛化会形成新框架负担。

### 测试

- Legacy 和 refactored path 使用相同 recorded responses，逐 event 比较。
- 六个主输出旧字段逐字节相同；new schema 只作为附加字段/sidecar。
- 覆盖 network/feed、social off、broadcast 三臂、demoted、leverage off/on、无订单、全买/全卖、末轮强平。
- 用 golden source/prompt hash 防止 persona/prompt 漂移。
- benchmark 确认默认 30-agent batch 没有显著性能退化。

### 回滚

- 保留 `legacy_engine=true` adapter 至少一个完整研究周期。
- 每个新 interface 独立 commit；orchestrator 切换是最后一个 commit。
- 旧 result readers/writers 不删；若 mismatch，切回 legacy adapter。

### 完成标准

- `run_sim(Config,...)` 或兼容 wrapper 的公开调用仍工作。
- 默认配置在 recorded-response replay 下 event/price/state/metric 等价。
- Scenario、Policy、Market、Risk 可独立替换且边界无循环依赖。
- 没有新增大型 orchestration dependency。

## Phase 3：多事件验证、真实采样分布、动态信息到达、杠杆临界点

### 目标

把当前单 Meta 事件、单次 provider draw 和静态 news seed 扩展为可识别的事件面板和采样分布；在控制 LLM response noise 后再研究 leverage 临界点。

### 具体文件

- 新增 `scenarios/` versioned episode specs 和 `data/README.md`；每个事件记录来源、时间粒度、shock 对齐、预先定义 outcome window。
- 扩展 `nmsim/scenario.py`：多条 `InformationEvent`、不同 arrival round、受众/渠道、重复/衰减规则；legacy 默认保持“从 news round 起重复”。
- 新增 `experiments/sample_policy.py`：同一 observation 做 K 次真实 provider sample，记录 response distribution，而非假设 seed 控制模型。
- 新增 `experiments/multi_event.py` 和 `experiments/analyze_multi_event.py`：事件内 replicate、事件间 hierarchical/cluster-aware summary；先用 stdlib/现有数值工具，若需 scipy/statsmodels须单独依赖提案。
- 重新设计 `experiments/critsweep.py`：baseline response replay 或 paired response bank；leverage strength/maintenance/fraction 分开扫描。
- versioned 修订 analyzer：paired/common-seed/common-model/accepted-attempt 过滤；correct t critical 或明确 bootstrap/permutation。
- 预注册 `docs/EXPERIMENT_DESIGNS/`：primary outcome、controls、exclusions、power、stopping rules。

### 风险

- 事件选择和对齐后见之明；多重比较；服务端模型漂移。
- response bank replay 增强内部控制，但可能低估干预改变 prompt 后的自然模型交互。
- 动态信息到达与社交传播同时变化会破坏识别。
- leverage critical point 可能是 market formula artifact，而非稳健相变。

### 测试

- 每个 scenario schema/引用数据完整性和 shock alignment test。
- placebo、no-social、no-news、leverage-off、broadcast ablation 保持明确 controls。
- 同 prompt response distribution 记录 request/model/service fingerprint；检测跨时间 drift。
- analyzer 对 synthetic known-effect/zero-effect 数据做 recovery test；失败/缺失样本按预注册处理。
- critical sweep 先 Mock/replay，确认单调性不能由代码错误/末轮 pending 造成。

### 回滚

- 每个新 scenario/experiment 写新目录和 schema version，不覆盖 Meta 历史结果。
- legacy single-event runner 继续保留。
- 动态 arrival 和 response replay 都是显式开关；默认仍是 LegacyScenario。

### 完成标准

- 至少多个预定义事件、每事件多个 response replicate；报告事件内和事件间不确定性。
- 任何 social/leverage 因果结论都有明确对照、消融和识别限制。
- temp=0 不再作为纯配对依据；所有 paired 设计说明如何控制 response noise。
- leverage 临界结论可在至少一种 response replay 和一种 fresh-sampling 分析中区分。

## Phase 4：更丰富的 Agent 生态和长周期运行

### 目标

在不把 Agent 变成 RL optimizer 的前提下，扩展人口异质性、记忆和入退场，并支持成本可控的长周期。

### 具体文件

- `nmsim/prompts/` 或 versioned prompt registry：现有六 persona 作为 `v1` 冻结；新 persona 是新版本，不原地编辑。
- 扩展 `nmsim/scenario.py` population cohort：资金规模、关注集合、信息时区/arrival、参与/休眠状态。
- 扩展 `nmsim/policy.py`：短/长记忆 adapter、attention budget；private memory 与 public statement 分离。
- 新增 `nmsim/checkpoint.py`：Agent/market/risk/RNG/event offset checkpoint 和 resume。
- 新增 `experiments/long_horizon.py`、`experiments/ecology_ablation.py`。
- `nmsim/validation.py` 新增长周期统计窗口、stationarity/rolling metrics，但旧函数不改语义。

### 风险

- persona 数量增加造成 prompt/成本爆炸和多重自由度。
- memory summarization 泄漏 private reasoning 或改变人格。
- 长期无资金约束会产生极端负仓/负现金，现有 market baseline 可能失去解释性。
- checkpoint/resume 改变 RNG/batch composition。

### 测试

- 现有六 persona v1 prompt hash 不变。
- resume 与不中断 run 在 Mock/replay 下 event 序列完全一致。
- private memory 永不进入他人 digest；public summary 有长度/来源标记。
- population entry/exit 前后账本和 agent identity 唯一。
- cost/batch budget 和 degraded-rate gate；长跑失败 attempt 不覆盖 checkpoint。

### 回滚

- 新 persona/memory/checkpoint 都由 scenario/policy version opt-in。
- 默认仍使用 v1 六 persona、当前三条 memory 和当前 population。
- 长期结果写新 schema/目录，不与旧 24-round JSON 混合。

### 完成标准

- 长跑可中断恢复且在 Mock/replay 下等价。
- 至少一种生态消融能区分 population composition 与 social topology 效应。
- 成本、失败率、memory privacy 和 provenance 全可审计。
- 不引入 RL、真实资金或交易台框架。

## Phase 5：内生流动性、多资产和可选订单簿

### 目标

在保留 `LegacyPressureMarket` 作为默认和比较基线的同时，加入有显式对手方/流动性状态的可选市场、多资产传播和可选简化订单簿；这不是现在的重写目标。

### 具体文件

- 新增 `nmsim/markets/pressure.py`：当前公式原样迁入 compatibility engine。
- 新增 `nmsim/markets/liquidity.py`：显式 market maker inventory/cash/risk limits、state-dependent depth/impact。
- 可选新增 `nmsim/markets/orderbook.py`：只有在研究问题需要时实现简化 LOB；不替换默认 pressure engine。
- 新增 `nmsim/assets.py`、multi-asset Position/Cash/Order/Fill 类型；保留 single-asset adapter。
- `nmsim/risk_engine.py` 支持跨资产 margin/netting，但 current frozen-reference V2 作为 legacy mode。
- 新增 `experiments/market_engine_ablation.py`、`experiments/cross_asset_contagion.py`。
- 新增 `docs/MARKET_SEMANTICS.md`：明确 volume、fill、maker inventory、wealth conservation、limit order 和 liquidation execution。

### 风险

- 这是最大科学语义变化；市场结果可能主要由 engine 而非 Agent 互动决定。
- 完整 LOB 会引入大量自由参数、性能成本和校准负担。
- 多资产 netting/mark-to-market 容易产生账本错误。
- 与历史 pressure results 直接拼接会造成错误比较。

### 测试

- 每个新 engine 做现金、资产、maker inventory、fees、PnL 的守恒/残差测试。
- limit price、partial fill、unfilled/cancel、price-time priority（若有 LOB）有单元/属性测试。
- pressure adapter 对现有 golden run 保持完全等价。
- market-engine ablation 使用相同 recorded policy decisions，分离行为与市场机制。
- 多资产 shock/position/margin 有小型手算 fixture。

### 回滚

- CLI 默认仍是 `market_engine=legacy_pressure`；新 engine 必须显式 opt-in。
- 新结果使用独立 schema/目录/engine id，不改变旧 CSV 含义。
- 任一 engine 可独立移除而不影响 Scenario/Policy/Risk adapter。

### 完成标准

- 至少一个显式流动性 engine 有闭合账本和完整事件记录。
- 可选 LOB 只在预先定义研究问题中使用；pressure baseline 始终可运行。
- 多资产结果能从 event log 重建所有 position/cash/fill。
- 报告清楚区分 Agent behavior effect 与 market-engine effect。

## 推荐的第一个实施里程碑

**Milestone M0：可追溯的、不改变语义的基线 run。**

交付一个小 PR：恢复 Git/环境声明；加入 stdlib characterization tests；新增 redacted `run_manifest.json` sidecar；把 Mock/repro/minimal-grid 变成可重复命令。先不改 provider exit、market accounting、social weight、parser 或 analyzer。

验证没有改变科学语义：

1. PR 前后同一 Mock config 的 price/trace/propagation/facts/PNG hashes 相同。
2. recorded current edge semantics（全 fill、正 social gain 同 real prompt、next-round liquidation）测试通过。
3. 现有 1-seed Mock 2x2 的旧 JSON 字段相同。
4. `repro_check` PASS。
5. 所有历史结果文件 SHA-256 不变。
6. prompt/persona/source hashes 无非预期变化。
