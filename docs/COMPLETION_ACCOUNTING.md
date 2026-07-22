# Completion Accounting 与 honest-N

本文定义 Phase 1.1B manifest 的完成量口径，并加入 Phase 1.2A
的正式 result-reuse 与 model-qualification 单位。核心原则是：每个计数都必须有单位，LLM 逻辑请求不能冒充 Provider 调用，Agent decision 或 qualification case 不能冒充独立 simulation 样本，存在部分输出也不能冒充成功 run。

## Manifest schema

`completion.schema_version` 当前为 `1.1`。它在 `1.0` 上只新增
`application_provider_attempts`；历史 `1.0` manifest 仍是有效历史证据，但不含
逐尝试数据，不得把缺失猜成 0。单次 managed simulation 的结构如下；
无法安全预知的 `planned` 使用 `null`，不得填入猜测值。

```json
{
  "schema_version": "1.1",
  "simulation_runs": {
    "unit": "simulation_runs",
    "planned": 1,
    "started": 0,
    "completed": 0,
    "failed": 0
  },
  "rounds": {
    "unit": "rounds",
    "planned": 4,
    "started": 0,
    "completed": 0,
    "failed": 0,
    "skipped": 0
  },
  "agent_decisions": {
    "unit": "agent_decisions",
    "planned": 24,
    "attempted": 0,
    "completed": 0,
    "failed": 0,
    "skipped": 0
  },
  "llm_logical_requests": {
    "unit": "llm_logical_requests",
    "planned": 24,
    "attempted": 0,
    "completed": 0,
    "failed": 0
  },
  "response_sources": {
    "unit": "final_responses",
    "provider": 0,
    "cache": 0,
    "replay": 0
  },
  "provider_calls": {
    "unit": "logical_provider_requests_after_cache_and_replay",
    "attempted": 0,
    "succeeded": 0,
    "failed": 0,
    "coverage": "provider-interface requests; SDK-internal retry attempts are not observable"
  },
  "application_provider_attempts": {
    "unit": "visible_adapter_loop_attempts",
    "attempted": 0,
    "responses_received": 0,
    "parse_failed_responses": 0,
    "provider_exceptions": 0,
    "retries_scheduled": 0,
    "logical_requests_with_retry": 0,
    "exhausted_logical_requests": 0,
    "reported_models": [],
    "reported_models_truncated": false,
    "coverage": "OpenAI/Anthropic application retry loops only; excludes SDK, transport, proxy, and server-internal retries"
  },
  "parsing": {
    "unit": "agent_decision_parse_operations",
    "attempted": 0,
    "succeeded": 0,
    "failed": 0,
    "fallbacks": 0
  }
}
```

## 字段定义

| group | 字段 | 单位与定义 |
|---|---|---|
| `simulation_runs` | `planned` | 该 context 计划管理的 simulation run 数；单次正式仿真通常为 1，driver 为子 run 总数 |
|  | `started` | 实际进入 simulation computation 或 driver 实际开始的 child run 数 |
|  | `completed` | simulation computation 已正常返回的数量；它不单独表示 export/finalization 成功 |
|  | `failed` | 已开始但 simulation computation 失败，或 driver 判定失败的 run 数 |
| `rounds` | `planned` | 最终有效 Config 的 `n_rounds`；非 simulation/provisional attempt 为 `null` |
|  | `started` | 已成功写出 `RoundStarted` 的轮数 |
|  | `completed` | 已成功写出 `RoundFinished` 的轮数 |
|  | `failed` | failed context 中已开始但未完成的当前轮；当前单线仿真至多为 1 |
|  | `skipped` | `max(0, planned - completed - failed)` |
| `agent_decisions` | `planned` | 计划轮数乘实际规划的 LLM Agent 数；无法可靠确定时为 `null` |
|  | `attempted` | 已写出 `LLMRequestRecorded`、即抽象层开始请求的 decision 数 |
|  | `completed` | 已写出 `AgentDecisionParsed` 的 decision 数，包括明确产生 fallback Decision 的情况 |
|  | `failed` | `max(0, attempted - completed)`；尚未形成 Decision 的请求 |
|  | `skipped` | `max(0, planned - completed - failed)` |
| `llm_logical_requests` | `planned` | 通常与计划 Agent decision 数一致 |
|  | `attempted` | 仿真向 LLM 抽象提出的一次逻辑请求，不论最终来源 |
|  | `completed` | 得到最终 response 的逻辑请求；由 `LLMResponseRecorded` 和 wrapper 计数共同校准 |
|  | `failed` | `max(0, attempted - completed)` |
| `response_sources` | `provider` | 最终 response 来自 Provider 接口的数量，包括 Mock Provider |
|  | `cache` | 最终 response 来自 cache、没有重复穿透 Provider 的数量 |
|  | `replay` | 最终 response 来自离线 recording 的数量 |
| `provider_calls` | `attempted` | 穿透 Replay/cache 后抵达 Provider 接口的 prompt-level 请求数 |
|  | `succeeded` | 通过 Provider 接口取得最终 response 的数量 |
|  | `failed` | `max(0, attempted - succeeded)` |
| `application_provider_attempts` | `attempted` | OpenAI/Anthropic adapter 应用层循环实际发起的逐尝试数；不是独立逻辑请求数 |
|  | `responses_received` | 成功拿到 SDK response 的尝试数，包括后续被判定为 parse failure 的 response |
|  | `parse_failed_responses` | response 已收到、但按现有 `_is_parse_failure` 判定为无可解析 JSON 的尝试数 |
|  | `provider_exceptions` | 应用可见的 Provider/SDK 调用异常数；异常正文仅进入私有事件 |
|  | `retries_scheduled` | 已记录尝试后确定继续下一次应用层尝试的数量 |
|  | `logical_requests_with_retry` | 至少进入第 2 次应用层尝试的逻辑请求数；每个请求最多计 1 |
|  | `exhausted_logical_requests` | 在最后可用尝试仍是 parse failure 或 Provider exception 的逻辑请求数 |
|  | `reported_models` | 成功 response 中 endpoint 报告的有界、公开安全 model id 去重集合；与 requested `model` 不同时必须显式标记别名歧义 |
|  | `reported_models_truncated` | 安全去重集合超过 16 个值时为 `true`，不再扩大 manifest |
| `parsing` | `attempted` | 形成 `AgentDecisionParsed` 时执行的 parser operation 数 |
|  | `succeeded` | `parse_status != error` 的 operation 数 |
|  | `failed` | `parse_status == error` 的 operation 数 |
|  | `fallbacks` | parser error 或现有 API/重试穷尽 hold fallback 的数量 |

`failed` 和 `skipped` 不重叠：failed 表示已经开始/尝试但未完成，skipped 表示计划内尚未开始。Fallback 已经形成可供仿真继续的 Decision，因此计入 `agent_decisions.completed`；同时在 `parsing.fallbacks` 中披露降级，不会被当作无事发生。

Fallback 检测沿用当前事件语义：`parse_status=error`，或私有 rationale 精确为内部标记 `api-error; holding` / `parse-retries-exhausted; holding`。这些私有文本仅用于内部计数，绝不复制到公共事件、manifest 摘要或实验 summary。

`LLMProviderAttemptObserved` 的公共 payload 使用
`provider_attempt_schema=provider_attempt_v1`，包含 logical/batch/round/Agent/Persona
关联、`attempt_index`/`max_attempts`、requested `model`、nullable
`reported_model`、原始与实际尝试 prompt hash、trigger/outcome 安全代码、
nullable response hash、延迟、Provider 报告的 token 计数和 `will_retry`。
完整 system/user prompt、中间 raw response 和 exception type/detail 只进入同
event id 的 `private_events.jsonl`，文件强制 `0600`。事件写入异常必须
向上传播，不得被 Provider `except` 路径误转成 `api-error; holding`。
私有 payload 的所有字符串在写入前仍递归移除 Config/环境中已知 credential；
非 credential 的 prompt/raw/error 文本不截断、保持原始字节。

当前 async OpenAI/Anthropic 仍是最多 3 次应用层尝试；parse failure
继续累加既有 `REMINDER`，Provider exception 仍使用 0.5/1.0 秒退避。
sync `complete()` 保持一次 SDK 调用、不新增 retry。`CostTracker` 仍只统计
收到的 SDK response：parse-failed response 计入，Provider exception 不计入。

## 计数来源与收敛

Managed observer 只在底层事件成功写出后更新计数：

- `RoundStarted` / `RoundFinished` 更新轮数；
- `LLMRequestRecorded` 更新 logical request 和 decision attempted；
- `LLMProviderAttemptObserved` 在每次应用层尝试后立即更新 attempt 计数；
- `LLMResponseRecorded` 更新 logical completed 和初步 response source；
- `AgentDecisionParsed` 更新 decision 与 parsing。

在终态前，`sync_llm_accounting()` 再用 `RecordingLLM` / `ReplayLLM` 的 `request_count`、`response_count`、batch size 和 cache tracker 校准 LLM 统计。最终 source/call 口径为：

### Record 模式

```text
cache_hits = min(tracker.cache_hits, logical_requests.completed)
response_sources.cache = cache_hits
response_sources.provider = logical_requests.completed - cache_hits
provider_calls.attempted = max(0, logical_requests.attempted - cache_hits)
provider_calls.succeeded = max(0, logical_requests.completed - cache_hits)
provider_calls.failed = max(0, provider_calls.attempted - provider_calls.succeeded)
response_sources.replay = 0
```

Mock 也实现 Provider 接口，所以无 cache 的 Mock response 记为 `response_sources.provider`，相应请求记为 Provider-interface call；`network_access` 仍为 `false`。

### Replay 模式

```text
response_sources.replay = logical_requests.completed
response_sources.provider = 0
response_sources.cache = 0
provider_calls.attempted = 0
provider_calls.succeeded = 0
provider_calls.failed = 0
network_access = false
```

Replay hit 永不记作 Provider call，`application_provider_attempts.attempted=0`，
并且 mismatch 不会 fallback 到 Provider。

### Cache 模式

每个 cache hit 只增加 `response_sources.cache`；它仍是一次 completed logical request，但不增加 Provider call，也不产生 Provider-attempt 事件。Cache miss 正常穿透 Provider。相同 prompt 连续请求、一次 miss 加一次 hit 的典型口径是：logical requests 2、Provider source 1、cache source 1、Provider attempted/succeeded 1；attempt 数取决于唯一 miss 的应用层 retry 路径。

`provider_calls` 保持历史的“穿透 cache/replay 后的逻辑 Provider
请求”单位，不因一个请求内有 2–3 次应用层尝试而膨胀。
`application_provider_attempts` 另行记录项目可见的 OpenAI/Anthropic
adapter loop。SDK 内部透明 retry、HTTP 传输层、代理或服务端重试仍不可观，
既不猜测也不计入。`network_access` 是运行路径属性，不是网络抓包结果。

## 单次运行示例

固定 6 个 LLM Agent、4 rounds、无 cache 的成功 Mock Record：

| metric | value |
|---|---:|
| `simulation_runs.completed` | 1 |
| `rounds.completed` | 4 |
| `agent_decisions.completed` | 24 |
| `llm_logical_requests.completed` | 24 |
| `response_sources.provider` | 24 |
| `response_sources.cache` | 0 |
| `response_sources.replay` | 0 |
| `provider_calls.attempted` | 24 |
| `provider_calls.succeeded` | 24 |
| `network_access` | `false` |

相同 recording 的 Strict Replay：

| metric | value |
|---|---:|
| `simulation_runs.completed` | 1 |
| `rounds.completed` | 4 |
| `agent_decisions.completed` | 24 |
| `llm_logical_requests.completed` | 24 |
| `response_sources.replay` | 24 |
| `provider_calls.attempted` | 0 |
| `provider_calls.succeeded` | 0 |
| `network_access` | `false` |

## 中途失败与 export 失败

Simulation 在第 N 轮异常时，manifest 保留此前事件已经确认的 round、decision、request 和 parse 数量。已开始但未完成的当前轮计入 `rounds.failed=1`，其余未开始计划计入 skipped。已存在 artifact 可以登记和 hash，但终态必须是：

```text
status = failed
outputs_complete = false
managed_run_completed = false
```

若 simulation 正常返回、随后 result export 失败，`simulation_runs.completed=1` 和 `simulation_computation_completed=true` 仍如实保留；managed run 仍为 failed，不能计入实验的 `completed_runs` 或 `honest_n_runs`。文件存在不是成功证据。

`KeyboardInterrupt` 和 managed `SystemExit` 同样先同步当前 completion 再失败封存。无法捕获的 `SIGKILL`/断电可能只留下 running manifest，不能推断未写出的完成量。

## `honest_n`：单次 run 的兼容字段

顶层字段保留给旧消费者，但现在明确带单位并标记弃用：

```text
honest_n = completion.agent_decisions.completed
honest_n_unit = "agent_decisions"
honest_n_deprecated = true
```

这只是一个 simulation 内形成了多少 Agent Decision 的兼容计数，不是独立随机样本量，不能用于宣称统计 N。

Manifest 还保留更早期的 `samples` 兼容对象。当前写法为：

```text
samples.expected = agent_decisions.planned
samples.completed = agent_decisions.completed
samples.failed = parsing.fallbacks
samples.honest_n = max(0, agent_decisions.completed - parsing.fallbacks)
```

因此，顶层 `honest_n` 与 `samples.honest_n` 在存在 fallback 时会有意不同：前者表示完成的 Decision，后者延续旧的“排除降级 Decision”口径。`samples.*` 不能替代带单位的 `completion`，也不能当作实验 run-level N。新消费者应直接读取 `completion`；旧字段的删除需另行兼容迁移，不能静默进行。

## `honest_n_runs`：实验汇总的样本单位

Experiment driver 的独立汇总单位是 `runs`：

```json
{
  "unit": "runs",
  "planned_runs": 1,
  "started_runs": 1,
  "executed_runs": 1,
  "reused_runs": 0,
  "reuse_candidates_examined": 0,
  "reuse_candidates_rejected": 0,
  "completed_runs": 1,
  "failed_runs": 0,
  "honest_n_runs": 1,
  "reuse_rejection_codes": {}
}
```

定义如下：

- `planned_runs`：该 driver/cell 计划的 child simulation run 数；
- `started_runs`：当前 driver 实际进入 child execution 的兼容生命周期计数；
- `executed_runs`：本次调用真正启动的新 managed child attempt 数；每次实际启动 `run_seed` subprocess 才加一，合法复用和仅 endpoint-wait 都不增加。同一计划 replicate 如发生 health/child retry，该值可超过 `planned_runs`，但 `honest_n_runs` 仍只能计一；
- `completed_runs`：经 driver 验收为成功的 child run 数；
- `failed_runs`：失败 child run 数；
- `honest_n_runs`：被接受的成功 child run 数，必须等于 `completed_runs`；
- `reused_runs`：通过 result-reuse policy 1.0 全部身份和 artifact 校验的既有 child run 数；复用会增加 completed/honest/reused，但不增加 started/executed；
- `reuse_candidates_examined`：实际进入集中复用门的候选数；
- `reuse_candidates_rejected`：至少一项身份、完整性、路径安全或 health 条件不满足的候选数；
- `reuse_rejection_codes`：公共、稳定且脱敏的拒绝 reason-code 计数。

Driver 只有在 `completed_runs + failed_runs == planned_runs` 且 `honest_n_runs == completed_runs` 时才能 finish。被拒绝的候选本身不增加 completed/failed/honest；后续新启动的 child 才进入 executed/started，并根据真实终态进入 completed 或 failed。公共 `driver_summary.json` 只含受控 failure/reuse reason code 和脱敏 reuse audit；原始 child stdout/stderr 等详细失败信息写入权限为 `0600` 的 `driver_failures.private.jsonl`。

对 `grid2x2 --seeds 1`，每个 cell 的正确口径是：

```text
planned_runs = 1
completed_runs = 1
failed_runs = 0
honest_n_runs = 1
```

某个底层 child run 即使包含 144 个 completed Agent Decision，它也仍只贡献一个 `honest_n_runs`。Agent decision 行存在依赖、共享市场状态和同一 seed，不能当作 144 个独立实验样本。

同一 grid 合法重跑时，若所有 child 都通过 policy 1.0，则
`executed_runs=0`、`reused_runs=planned_runs`、`completed_runs=honest_n_runs=planned_runs`。
这不是增加新独立样本，只是对同一批已验证 replicate 的合法恢复。无 child manifest 的历史平铺文件只能作为显式标记的 `legacy_unverified_input` 进入分析，不进入上述 run-level 计数。

## Model qualification 的单位

`run_kind=model_qualification` 不是 simulation run。它复用通用
completion 中的 logical request、Agent decision、response source 和
Provider-interface call 字段，同时在 `qualification` 区域记录带单位的 `qualification_cases`。成功的 48-case Mock 运行口径为：

```text
qualification_cases.planned/attempted/completed/failed/skipped = 48/48/48/0/0
llm_logical_requests.completed = 48
agent_decisions.completed = 48
response_sources.provider = 48
provider_calls.succeeded = 48
simulation_runs.planned/completed = 0/0
rounds.planned/completed = 0/0
honest_n_cases = 48
honest_n_runs = 0
network_access = false
```

Mock 仍是 Provider 接口响应来源，但不需外部网络。Dry-run 不构造
Provider，logical request 和 Provider call 均为 0，48 个 case 记为计划但未尝试，`honest_n_cases=0`。不论 case 数多少，qualification 都不得增加 `honest_n_runs`。

## 成功判定与不变量

正式汇总应同时检查 lifecycle 和计数，而不是只检查某个输出文件：

- 单次 run 成功：`status=finished`、`managed_run_completed=true`、`outputs_complete=true`；
- driver 接受样本：child 满足正式成功条件后才增加 `completed_runs`/`honest_n_runs`；
- `response_sources.provider + cache + replay == llm_logical_requests.completed`（已同步终态）；
- Replay 的 Provider call 必须全部为 0；
- cache hit 不增加 Provider call；
- fallback Decision 计入 decision completed，同时计入 parsing fallback；
- failed 与 skipped 不重叠；
- 未知 planned 使用 `null`；
- 私有 rationale 不进入公共 completion、错误或实验 summary。

Completion 是 provenance 计数，不是因果识别设计。统计可复现性仍需独立 seeds、明确失败运行处理、对照/消融与适当的样本单位；Record/Replay 本身不会扩大独立样本量。
