# Run provenance、事件日志与 LLM Record/Replay

本文描述 Phase 1/1.1A/1.1A.1 的当前可执行接口。目标是审计一次已发生的运行，并在明确的源码、运行时配置和记录契约内重放；它不改变 Agent、Prompt、压力定价、社交传播、杠杆或统计公式。各种 replay 与统计复现的边界见 [REPLAY_COMPATIBILITY.md](REPLAY_COMPATIBILITY.md)，本文不把它们统称为“完全可复现”。

## 快速使用

### 主 CLI：记录

所有由主 CLI 发起的新运行默认记录 provenance、事件和逐次 LLM 响应：

```bash
python3 -m nmsim.run \
  --provider mock --rounds 4 --news-round 2 --seed 7 \
  --out /tmp/nmsim-record
```

终端会打印本次不可变目录，例如：

```text
/tmp/nmsim-record/runs/20260715T...-<uuid>/
```

### 主 CLI：离线严格重放

重放必须使用相同的 Provider 身份、模型配置和会影响 Prompt/市场状态的 Config：

```bash
python3 -m nmsim.run \
  --provider mock --rounds 4 --news-round 2 --seed 7 \
  --replay-from /tmp/nmsim-record/runs/<record-run-id> \
  --out /tmp/nmsim-replay
```

`--replay-from` 也可直接指向 `llm_records.jsonl`。Strict replay 不构造底层 Provider，没有网络 fallback；除 Prompt、Persona、round、batch、调用顺序、resolved model config 外，还会在第一轮之前校验 scientific/model-request effective Config，以及 parser、event、recording 和 scientific component 版本/hash。任一 strict 不匹配都会抛出 `ReplayMismatchError`，并把新 run 标为 `failed`。输出目录、run id 或 worker 等 execution 差异可以继续，但必须在 manifest 中记录。

Phase 1 的 recording schema `1.0` 缺少完整 strict compatibility 元数据，因此会以 `legacy_recording_missing_replay_contract` 在 preflight 明确失败。Phase 1.1A 早期生成、已有源码指纹但尚无 effective-config 字段的 schema `1.1` 也会以 `recording_missing_runtime_config_contract` 失败。两者仍可用于下文的离线 reparse audit；工具会如实标记缺失契约，不会猜测或用当前默认值回填历史身份。

### 离线重新解析审计

```bash
python3 -m nmsim.reparse_audit \
  --run /path/to/historical-run \
  --out /tmp/nmsim-reparse-audits
```

该命令只读取历史 `llm_records.jsonl` 和可用的 ParsedDecision 事件，用当前 parser 逐字段比较；不创建 Provider、不继续 simulation、不生成价格轨迹，也不修改历史目录。`--out` 是父目录，工具会创建新的不可覆盖审计目录。公共文件只保存 reasoning presence/hash，private rationale 正文只进入权限为 0600 的 `reparse_private.jsonl`。

### 单实验 runner

`experiments.run_seed` 也使用相同边界，同时保留分析器依赖的根目录旧 JSON：

```bash
python3 -m experiments.run_seed \
  --seed 1 --provider mock --rounds 4 --news-round 2 \
  --label smoke --out /tmp/nmsim-experiment

python3 -m experiments.run_seed \
  --seed 1 --provider mock --rounds 4 --news-round 2 \
  --label smoke-replay \
  --replay-from /tmp/nmsim-experiment/runs/<record-run-id> \
  --out /tmp/nmsim-experiment-replay
```

CSV reuse 模式和 Replay 互斥。批量 driver 会通过环境把 driver 名和 worker 数传入每个 manifest。

## 不可覆盖的目录布局

```text
<out>/
├── runs/
│   └── <run_id>/
│       ├── run_manifest.json
│       ├── events.jsonl
│       ├── private_events.jsonl       # 0600
│       ├── llm_records.jsonl          # 0600
│       ├── reasoning_traces.csv       # 主 CLI，0600
│       └── 原有结果文件
├── latest -> runs/<run_id>
└── 原有平铺兼容路径
```

- 默认 `run_id` 是 UTC 时间戳加随机 UUID；目录以 `exist_ok=False` 创建。
- 可用 `--run-id` 指定测试/调试 ID；若已存在会明确失败，不会重用或覆盖。
- 主 CLI 在安全时把原平铺文件做成 `latest/<file>` 的受管 symlink。已有普通文件或非受管 symlink 保持逐字节不变。
- `run_seed` 的 canonical 结果是 run 目录中的 `experiment_result.json`；根目录仍投影原来的 `<label>_s<seed>[_rN].json`。若该路径已存在，只保留新的 canonical run，不覆盖旧文件。
- health gate 拒绝的旧式根 JSON 被移到 `rejected/` 并附不可覆盖的原因 sidecar；不再删除失败证据。`runs/` 和 `rejected/` 都不会进入现有 analyzer 的根目录 glob。

## RunManifest

`run_manifest.json` 采用 schema `1.0`，创建后立即以 `status=running` 原子写出，结束时更新为 `finished` 或 `failed`。Manifest schema 与 recording/event/parser/fingerprint schema 是相互独立的版本口径。主要内容包括：

| 区域 | 内容 |
|---|---|
| run identity | `run_id`、Scenario id/hash、创建/开始/结束时间、状态、失败原因 |
| source | Git commit、dirty 状态、可行时的 diff hash；无 Git 时保留真实错误 |
| config | 全部 Config 字段、config hash schema/classification、secret-free 全量/科学/模型请求/执行 hashes 与可审计摘要；API key 脱敏但 configured 状态不丢失 |
| RNG | 本地 seed，并明确真实 Provider sampling 不受其完全控制 |
| LLM | requested/resolved Provider/模型、temperature、`max_tokens`、cache、record/replay、endpoint hash、logical request、Provider call、cache hit、degraded 数 |
| execution | driver worker 数、batch 策略、每轮实际 batch size、batch 数、已知连接上限 |
| population | Persona 完整定义、计划/实际人口、Agent id |
| Prompt | `prompts.py` 源码 hash、模板版本和六类 system prompt hash |
| inputs | reference、CSV reuse 或 replay record 的路径、存在性、大小和 SHA-256 |
| environment | Python、平台及 NumPy/Matplotlib/Anthropic/OpenAI/httpx 版本 |
| samples | expected、completed、failed/degraded 和 `honest_n` |
| results | run 内所有产物及外部兼容结果的大小和 SHA-256 |

Phase 1.1A 在 manifest 顶层和 `scientific_compatibility` 中记录：

- `fingerprint_schema_version`
- `decision_parser_schema_version` 与 `decision_parser_source_hash`
- `event_schema_version`
- `recording_schema_version`
- `prompt_source_hash` 与 `persona_source_hash`
- `simulation_core_source_hash`
- `scientific_component_fingerprint`
- 科学组件相对路径清单及逐文件 hash
- `git_commit` 与 `git_dirty`

Phase 1.1A.1 另在 manifest 顶层和 `config_contract` 中记录：

- `config_hash_schema_version=1.0` 与 `config_classification_hash`；
- `full_effective_config_hash`；
- `scientific_config_hash`、`model_request_config_hash`、`execution_config_hash`；
- 每类字段名称、secret-free 规范化 summary 和 full effective summary。

`full_effective_config_hash` 基于应用默认值、配置文件与 CLI 后的最终 `Config`，而不是用户显式输入的 CLI token。字段排序后以 UTF-8 规范 JSON 序列化；float 使用 `float.hex()`，Enum/tuple/set/bytes 有类型标记，Path 和 endpoint 只保存 identity hash，秘密只保存脱敏 configured 状态。`population` 同时保存排序 counts 和顺序敏感的 `effective_cast`，以匹配当前 Agent 创建/请求顺序。完整 38 字段分类表和精确 hash payload 见 [Replay 兼容性契约](REPLAY_COMPATIBILITY.md#运行时-effective-config-契约)。

这些 identity 不可合并解读：

- scientific source fingerprint 说明科学源码字节；
- scientific runtime config hash 说明该源码实际使用的科学参数；
- model-request config hash 说明 `Config` 中的模型请求参数，环境覆盖后的 requested/resolved Provider/model 还要由现有 secret-free `llm.runtime.model_config` 逐字段校验；
- execution config hash 说明运行位置和调度上下文，差异允许但需记录；
- Git commit/dirty/diff 是整仓 provenance，不是上述任一 hash 的替代品。

当前 scientific fingerprint 覆盖 `agents.py`、`config.py`、`contagion.py`、`leverage.py`、`llm.py`、`market.py`、`prompts.py`、`sim.py`、`types.py` 和 `validation.py`。文件用稳定的仓库相对路径排序，并以路径和原始文件字节的长度前缀聚合；绝对路径、目录遍历顺序、时间戳、文档、运行目录和私有日志不参与。Parser 函数、Prompt source 与 Persona literal 另有独立 hash。完整算法见 [Replay 兼容性契约](REPLAY_COMPATIBILITY.md#scientific-component-fingerprint)。

Git commit 用于完整代码快照溯源，但不作为唯一兼容判断。不同 commit 而所有 strict source/config/request 字段相同时可以重放，manifest 会记录 `cross_commit_same_scientific_fingerprint=true` 及 source/current identity。仅 README 或普通文档变化不会阻止 Replay；dirty 工作树若实际改变科学组件，source hash/指纹不匹配会阻止 Replay。先前的本地跨 commit 示例实际上是在 HEAD 仍为 `e86485f…`、但工作树 `dirty=true` 且已含 schema 1.1 代码时录制，再在 clean `53a5f2d…` 上重放。它证明同一科学指纹跨 commit identity 的当时契约，不证明 clean Phase 1 schema 1.0 兼容；完整证据表见 [Replay 兼容性契约](REPLAY_COMPATIBILITY.md#2026-07-15-跨-commit-示例的真实-provenance)。

Manifest 不宣称真实 LLM 完全确定。Provider 返回合法的 `api-error`/降级 hold 时，为保持历史退出语义，run 仍可 `finished`；此时 `llm.runtime.degraded=true`、失败决策数和 honest-N 会如实记录。

## 事件流

每条 JSONL 事件都有：

```json
{
  "run_id": "...",
  "round": 1,
  "event_id": "evt-00000001",
  "timestamp": "...Z",
  "agent_id": "retail_crowd",
  "schema_version": "1.0",
  "type": "OrderSubmitted",
  "data": {}
}
```

当前公共流覆盖：

- `RunStarted`、`RunFinished`、`RunFailed`
- `RoundStarted`、`ScenarioEventDelivered`、`AgentObservationCreated`
- `LLMRequestRecorded`、`LLMResponseRecorded`
- `AgentDecisionParsed`、`AgentDecisionParseError`
- `PublicStatementPublished`、`OrderSubmitted`、`OrdersAggregated`
- `PriceCleared`、`FillApplied`
- `MarginCallTriggered`、`LiquidationOrderQueued`
- `MetricsRecorded`、`RoundFinished`

事件只插在原步骤旁，不重排原循环。`LiquidationOrderQueued` 明确记录目标 round 和末轮是否有机会进入后续 clearing。

### 公私边界

`events.jsonl` 只含操作元数据、hash、公开 statement、订单、fill、价格和指标；writer 会拒绝 `raw_response`、完整 Prompt、`reasoning`、`rationale` 等敏感键。

以下内容只写受限文件：

- 完整 system/user Prompt；
- 原始模型 response；
- 解析后的 private rationale；
- 原有 `reasoning_traces.csv`。

社交 feed 仍只由 `Statement(sentiment, public_take)` 构造。标准响应中的 private `reasoning` 不进入邻居 observation。受限文件是本地访问控制，不替代磁盘加密、用户隔离或数据保留政策。

## LLM record schema 与严格匹配

新写入的 `llm_records.jsonl` 继续使用 recording schema `1.1`，对每个逻辑 Agent 调用保存：

- 全量 system/user Prompt 与各自 hash、组合 prompt hash；
- `agent_id`、`persona_id`、round；
- 全局 call sequence、batch sequence/index/size；
- secret-free 模型配置；
- parser/event/recording/fingerprint schema、科学组件 hash、Git identity；
- config hash schema/classification、full/scientific/model-request/execution hashes 和 secret-free 规范摘要；
- 原始 response 和 response hash。

记录 wrapper 位于现有 `CachingLLM` 外侧，因此 replay 覆盖完整的逻辑调用序列，包括当时由 cache 返回的响应；manifest 另记实际 Provider calls 和 cache hits。

Replay 在 `run_sim` 和第一个 `RoundStarted` 之前构造 `ReplayLLM`：先校验 recording 完整性，再校验 config hash schema/classification、scientific/model-request hashes、source compatibility 和 resolved model config。Execution hash 差异只记录，不放松后续请求校验。进入仿真后，整批请求会逐项匹配 Agent、Persona、round、call/batch sequence/index/size、完整 Prompt hash 与模型配置，全部匹配后才一次性推进 cursor。匹配失败会指出 category/字段和安全 hash 摘要，不会输出完整 Prompt、credential 或 private rationale；失败不会部分消费 batch，也绝不会改为真实 Provider 调用。运行结束还会用 `assert_exhausted()` 拒绝少 round/少 Agent 的提前结束。

Preflight 错配时，run directory 和 `status=failed` manifest 已存在，`RunFailed` 事件保存安全诊断，samples completed/honest-N 为实际的 0，`network_access=false`、`provider_calls=0`，不生成成功 canonical outputs。`RunStarted` 是 run provenance 事件；配置错配时不会有任何 `RoundStarted`。

## Replay 与 Reparse 的正确解释

Replay 用于：

- 重现已有 run 的解析、订单、价格、账本更新、强平和指标；
- 调试 parser、事件派生和结果 writer；
- 审计一次已发生的 Provider 输出。

Replay 不回答“若市场状态、社交输入、Prompt 或机制不同，模型会怎样回答”。这些反事实需要新的 Provider sample、预先设计的 response bank 或明确识别设计，不能绕过严格匹配。

Reparse audit 则不运行市场：它用当前 parser 重新解释历史 raw response，并与历史 ParsedDecision 比较。它用于评估 parser 升级的字段差异，不是 strict simulation replay；一旦早期 Decision 改变，后续状态与 Prompt 就会分叉，因此审计不会默认产生价格轨迹。

统计可复现性是另一个层次，要求明确样本量、seed、失败运行、对齐方法、对照或消融。单次 LLM response replay 或逐字节相同输出不能替代统计识别设计。

## 验证命令

```bash
PYTHONPYCACHEPREFIX=/tmp/nmsim-pycache \
  python3 -m unittest discover -s tests -v

PYTHONHASHSEED=0 python3 -m experiments.repro_check

PYTHONPYCACHEPREFIX=/tmp/nmsim-pycache \
  python3 -m compileall -q nmsim experiments tests
```

比较两个 Mock run 的公共核心事件时，应去掉 `run_id` 和 wall-clock `timestamp`；科学 payload、类型、round、Agent 和顺序应一致。

## 当前边界与尚存风险

- 记录的是现有 Provider wrapper 最终返回给 simulation 的 response；SDK 内部每次 retry 的中间原始 response/request id 仍不可见。
- Phase 1 recording schema `1.0` 缺少 strict parser/event/fingerprint/config 字段，默认 strict replay 会 fail closed；pre-extension schema `1.1` 若缺 config contract 也同样拒绝。两者可用 reparse audit 做离线解析差异检查，但不能把它描述为原实验精确复现。
- `Config` 分类是显式 allowlist；新增 dataclass 字段必须同步增加 category、rationale 和测试，否则运行在创建 Provider/进入仿真前 fail closed。
- Scientific fingerprint 使用显式维护的源文件清单。未来新增影响 Prompt、决策、事件顺序或科学计算的模块时，必须同步纳入清单并增加兼容测试，避免错误放行。
- 真实 Provider 首次采样仍可能受服务端模型、调度、权重版本和 sampling 实现影响；temperature 0、local seed 或 cache 不能替代 Record/Replay。
- Phase 1 已为不可破坏的隐私约束收紧 legacy 异常路径：parser 不把只有 `rationale`、没有 `public_take` 的响应提升为 public text。当前 Mock 和标准 real schema 都显式返回 `public_take`，正常轨迹不受影响；Phase 1.1A 未再次改变这一行为，并用回归测试继续锁定它。
- 事件记录没有修复现有 market 的无限余额、全额 fill、隐式做市商或 volume/fill 口径；它只把真实行为显式记录下来。
- Phase 1 最初审计时工作区没有 `.git`，因此初始化前的 manifest 会如实记录 commit/dirty/diff 无法取得。Phase 0.5 建立仅本地 Git 基线后，新受管运行可以记录本地 commit、dirty 状态和可行时的 diff hash；这不会追溯改写旧 manifest。
