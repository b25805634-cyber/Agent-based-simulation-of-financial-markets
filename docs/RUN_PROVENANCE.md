# Run provenance、事件日志与 LLM Record/Replay

本文描述 Phase 1 的当前可执行接口。目标是精确审计和重放一次已经发生的运行；它不改变 Agent、Prompt、压力定价、社交传播、杠杆或统计公式。

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

### 主 CLI：离线重放

重放必须使用相同的 Provider 身份、模型配置和会影响 Prompt/市场状态的 Config：

```bash
python3 -m nmsim.run \
  --provider mock --rounds 4 --news-round 2 --seed 7 \
  --replay-from /tmp/nmsim-record/runs/<record-run-id> \
  --out /tmp/nmsim-replay
```

`--replay-from` 也可直接指向 `llm_records.jsonl`。Replay 不构造底层 Provider，没有网络 fallback；任何 Prompt、Persona、round、batch、调用顺序、模型、temperature、`max_tokens` 或 cache 配置不匹配都会抛出 `ReplayMismatchError`，并把新 run 标为 `failed`。

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

`run_manifest.json` 采用 schema `1.0`，创建后立即以 `status=running` 原子写出，结束时更新为 `finished` 或 `failed`。主要内容包括：

| 区域 | 内容 |
|---|---|
| run identity | `run_id`、Scenario id/hash、创建/开始/结束时间、状态、失败原因 |
| source | Git commit、dirty 状态、可行时的 diff hash；无 Git 时保留真实错误 |
| config | 全部 Config 字段和稳定 hash；API key 脱敏但字段不丢失 |
| RNG | 本地 seed，并明确真实 Provider sampling 不受其完全控制 |
| LLM | requested/resolved Provider/模型、temperature、`max_tokens`、cache、record/replay、endpoint hash、logical request、Provider call、cache hit、degraded 数 |
| execution | driver worker 数、batch 策略、每轮实际 batch size、batch 数、已知连接上限 |
| population | Persona 完整定义、计划/实际人口、Agent id |
| Prompt | `prompts.py` 源码 hash、模板版本和六类 system prompt hash |
| inputs | reference、CSV reuse 或 replay record 的路径、存在性、大小和 SHA-256 |
| environment | Python、平台及 NumPy/Matplotlib/Anthropic/OpenAI/httpx 版本 |
| samples | expected、completed、failed/degraded 和 `honest_n` |
| results | run 内所有产物及外部兼容结果的大小和 SHA-256 |

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

## LLM record schema 与匹配

`llm_records.jsonl` 对每个逻辑 Agent 调用保存：

- 全量 system/user Prompt 与各自 hash、组合 prompt hash；
- `agent_id`、`persona_id`、round；
- 全局 call sequence、batch sequence/index/size；
- secret-free 模型配置；
- 原始 response 和 response hash。

记录 wrapper 位于现有 `CachingLLM` 外侧，因此 replay 覆盖完整的逻辑调用序列，包括当时由 cache 返回的响应；manifest 另记实际 Provider calls 和 cache hits。

Replay 先验证整批请求，再一次性推进 cursor。匹配失败不会部分消费 batch，也绝不会改为真实 Provider 调用。运行结束还会用 `assert_exhausted()` 拒绝少 round/少 Agent 的提前结束。

## Replay 的正确解释

Replay 用于：

- 重现已有 run 的解析、订单、价格、账本更新、强平和指标；
- 调试 parser、事件派生和结果 writer；
- 审计一次已发生的 Provider 输出。

Replay 不回答“若市场状态、社交输入、Prompt 或机制不同，模型会怎样回答”。这些反事实需要新的 Provider sample、预先设计的 response bank 或明确识别设计，不能绕过严格匹配。

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
- 真实 Provider 首次采样仍可能受服务端模型、调度、权重版本和 sampling 实现影响；temperature 0、local seed 或 cache 不能替代 Record/Replay。
- 为满足不可破坏的隐私约束，parser 不再把只有 `rationale`、没有 `public_take` 的 legacy/降级响应提升为 public text。当前 Mock 和标准 real schema 都显式返回 `public_take`，其正常轨迹不变；只有 malformed/legacy/API-error 的公开广播由旧的 rationale 文本变为空。这是本阶段唯一有意的异常路径语义收紧，并有回归测试。
- 事件记录没有修复现有 market 的无限余额、全额 fill、隐式做市商或 volume/fill 口径；它只把真实行为显式记录下来。
- Phase 1 最初审计时工作区没有 `.git`，因此初始化前的 manifest 会如实记录 commit/dirty/diff 无法取得。Phase 0.5 建立仅本地 Git 基线后，新受管运行可以记录本地 commit、dirty 状态和可行时的 diff hash；这不会追溯改写旧 manifest。
