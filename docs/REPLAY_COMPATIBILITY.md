# Replay 兼容性契约

本文定义 Phase 1.1A–1.1B 的本地 Replay 版本、运行时配置和配置输入边界。它的目标是让不兼容的历史响应重放明确失败，并为解析器升级提供离线审计路径；它不改变 Agent、Prompt、Persona、市场、社交、杠杆、默认参数或正常响应的解析结果。Phase 1.1B 只替换外层生命周期编排；recording schema 仍为 `1.2`，Phase 1.1A 的完整 1.2 记录在既有 strict 身份一致时继续兼容。

## 五种不同的复现与审计能力

| 能力 | 实际做什么 | 是否运行市场 | 是否调用 Provider | 可以回答的问题 | 不能据此声称什么 |
|---|---|---:|---:|---|---|
| LLM response replay | 在请求身份和兼容契约均匹配时，按原调用顺序返回记录的原始响应 | 取决于调用方 | 否 | “这次逻辑 LLM 调用当时返回了什么？” | 不代表不同 Prompt 或市场状态下模型会给出同一响应 |
| strict simulation replay | 从初始配置重新运行现有 simulation，并在每个 LLM 边界使用严格 response replay；验证记录全部消费完毕 | 是 | 否 | “在相同科学组件、请求序列和记录响应下，解析、订单、价格及指标能否重演？” | 不代表跨任意代码版本、平台或依赖都完全确定 |
| cross-commit replay with identical scientific fingerprint | Git commit 不同，但所有严格科学字段相同；继续执行 strict simulation replay，并在 manifest 标记跨 commit | 是 | 否 | “只有不影响科学行为的仓库变化时，历史记录是否仍兼容？” | 不表示两个 commit 的全部仓库内容相同 |
| reparse audit | 读取历史原始响应，用当前 `parse_order` 重新解析，并逐字段和历史决策对比 | 否 | 否 | “当前解析器会怎样解释旧响应，哪些字段发生了变化？” | 不是原实验精确 Replay，也不是新的市场反事实实验 |
| statistical reproducibility | 用明确的样本、seed、失败口径和统计方法重复多次实验并比较分布/效应 | 通常是 | 视实验设计而定 | “研究结论在样本和随机性下是否稳定？” | 单次逐字节 Replay 不能替代统计重复、对照或消融 |

这些能力不能统称为“完全可复现”。尤其是，真实 Provider 的首次采样仍可能受服务端模型版本、调度和采样实现影响；`temperature=0` 和本地 seed 都不是完全确定性的保证。

## Scientific Component Fingerprint

### 覆盖范围

当前 `scientific_component_fingerprint` 基于真实调用链覆盖下列源文件，按仓库相对路径排序：

```text
nmsim/agents.py
nmsim/config.py
nmsim/contagion.py
nmsim/leverage.py
nmsim/llm.py
nmsim/market.py
nmsim/prompts.py
nmsim/sim.py
nmsim/types.py
nmsim/validation.py
```

这组文件覆盖 Agent observation 与 Prompt 构造、Decision 解析、相关类型和配置默认值、simulation 科学步骤顺序、市场 clearing、社交传播、杠杆/强平和指标计算。`recording.py`、`provenance.py`、`reparse_audit.py` 等审计设施由独立 schema 约束；Phase 1.1B 新增/修改的 `nmsim/run_context.py`、`nmsim/managed_cli.py`、`nmsim/entrypoints.py`、`nmsim/run.py` 和 experiment driver/lifecycle 文件也不在 scientific allowlist 内。普通文档、测试、运行目录、结果、私有日志和时间戳同样不进入科学组件指纹。

因此，只修改 `README`、`docs/` 或普通工作流文档不会改变科学指纹。反之，只要上述科学组件的原始字节变化，即使 Git commit 没变而工作树为 dirty，相关 hash 也会变化，strict replay 会拒绝。

### 稳定计算规则

当前算法由 `fingerprint_schema_version=1.0` 标识：

1. `decision_parser_source_hash`：提取 `nmsim/llm.py` 中顶层 `parse_order` 函数的源码范围，统一换行为 LF，再计算 SHA-256。
2. `prompt_source_hash`：对 `nmsim/prompts.py` 原始字节计算 SHA-256；这保持 Phase 1 已建立的 Prompt source hash 口径。
3. `persona_source_hash`：以 AST 读取 `PERSONAS` 字面量，再用 UTF-8、key 排序、无额外空白且禁止 NaN 的规范 JSON 计算 SHA-256。
4. `simulation_core_source_hash`：以上述科学文件的仓库相对 POSIX 路径排序；对每项依次写入路径字节长度、路径字节、文件字节长度和原始文件字节，再计算 SHA-256。绝对项目路径、目录遍历顺序、mtime、权限和运行产物均不参与。
5. `scientific_component_fingerprint`：对包含 fingerprint/parser schema、parser hash、Prompt hash、Persona hash、simulation core hash 和排序后文件清单的规范 JSON 计算 SHA-256。

`event_schema_version` 与 `recording_schema_version` 不折叠进总体科学指纹，而是作为独立 strict 字段校验。这使错误能准确指出是事件/记录协议不兼容，而不是只报一个总 hash 不同。

## 运行时 Effective Config 契约

### 六类不同的 identity

Strict Replay 不再用一个模糊的“版本 hash”代替不同责任：

| identity | 实际覆盖 | Strict Replay 作用 |
|---|---|---|
| scientific source fingerprint | 影响观察、Prompt、parser、事件顺序、市场、社交、杠杆和指标的源文件字节 | 不同即拒绝；与 Git commit identity 解耦 |
| scientific runtime config hash | 应用默认值、配置和 CLI 后的最终 `Config` 中会改变科学结果的字段 | preflight 不同即拒绝，并列出安全的字段级差异 |
| model-request config hash | 最终 `Config` 中 Provider/模型/采样/cache/endpoint 等请求字段 | preflight 不同即拒绝 |
| resolved `model_config` | 运行时 requested/resolved Provider 和模型、temperature、`max_tokens`、cache、cheap-model 开关及 endpoint hash；包括环境覆盖后的实际 identity | 在 config preflight 后仍逐字段 strict 校验，防止环境覆盖绕过 Config hash |
| execution config hash | 输出位置、run/scenario label、worker、driver batching 声明和输入/重放路径等执行上下文 | 允许不同；差异写入 manifest，不放松实际请求顺序 |
| Git commit + dirty/diff | 整个仓库快照和未提交状态 | 用于 provenance，不单独决定兼容 |

`full_effective_config_hash` 还覆盖全部 38 个 `Config` 字段的脱敏规范表示，用于审计整体身份；它包含 execution 字段，因而不作为 strict 放行/拒绝的单一开关。

文档和报告必须写具体 identity 名称，禁止用模糊的“默认 Config hash”。
历史测试值 `f0508c23…` 是 raw `asdict(Config())` 规范 JSON 的 SHA-256，
没有 config-contract schema envelope；`1a36131b…` 是固定 base_dir 下的
`full_effective_config_hash`。二者算法和身份域不同。Phase 1.1 complete tag
与 Phase 1.2A sealing 的同 fixture 对照见
[Config Hash Identities](CONFIG_HASH_IDENTITIES.md)。

### config hash schema 1.0

`config_hash_schema_version=1.0` 的计算只接收已合并完成的 `Config` dataclass，不 hash 用户原始 CLI token。每个 hash 的 payload 是
`{"config_hash_schema_version":"1.0","identity":<label>,"value":<summary>}`，以 UTF-8、key 排序、无多余空白、`allow_nan=false` 的 JSON 序列化后计算 SHA-256。因此字典/dataclass 普通字段顺序和操作系统目录遍历顺序不影响结果。

规范化细则是：有限浮点数用 `float.hex()` 标记，非有限值拒绝；Enum 保存限定类型和规范化 value；tuple/set/bytes 有独立类型标记，set 按规范 JSON 排序，bytes 只保存长度和 SHA-256；Path 只保存 resolved identity hash，不保存绝对路径明文。`reference_path` 若指向文件，科学摘要保存文件大小和内容 SHA-256；文件内容变化会改变 scientific hash。Endpoint 只保存去除 userinfo 后的 identity hash。API key/Authorization/Bearer 类秘密永不写入摘要；API key 只记录 configured 布尔值和 `<redacted>`/`<not-configured>`。所以 `full_effective_config_hash` 也是 secret-free，不是密钥的 hash。

`population` 需要一项有意的例外：`counts` 按 Persona id 排序以便审计，但 `effective_cast` 显式保留当前可执行的 mapping 插入顺序，对 `influencer_amplifier` 封顶为 1，最后按 `max_llm_agents` 截断。这个顺序影响真实 Agent 创建/请求顺序，所以即使 counts 相同，effective cast 顺序变化也必须改变 scientific hash；它不是字典顺序稳定性的缺陷。

### 字段分类（完整 38 项）

S/M/E 分别表示进入 scientific/model-request/execution hash。“拒绝”均指在第一轮之前拒绝；execution 差异允许但必须记录。

| Config field | category | rationale | S | M | E | Strict Replay mismatch |
|---|---|---|:---:|:---:|:---:|---|
| `seed` | scientific | 驱动本地 RNG 和 MockLLM | ✓ |  |  | 拒绝 |
| `n_rounds` | scientific | 决定时域和末轮强平机会 | ✓ |  |  | 拒绝 |
| `news_round` | scientific | 决定新闻到达、margin gate 和指标对齐 | ✓ |  |  | 拒绝 |
| `news_text` | scientific | 定义发送给知情 Agent 的 Scenario 事件 | ✓ |  |  | 拒绝 |
| `initial_price` | scientific | 初始化价格、组合和杠杆参照仓 | ✓ |  |  | 拒绝 |
| `fundamental_value` | scientific | 进入 Mock 观察与决策路径 | ✓ |  |  | 拒绝 |
| `recent_window` | scientific | 决定 Agent 可见价格历史窗口 | ✓ |  |  | 拒绝 |
| `kappa` | scientific | 决定净订单流价格冲击 | ✓ |  |  | 拒绝 |
| `n_llm_agents` | scientific | 设置 legacy Persona 人口 | ✓ |  |  | 拒绝 |
| `n_noise_agents` | scientific | 设置背景噪声交易者数量 | ✓ |  |  | 拒绝 |
| `max_llm_agents` | scientific | 截断有效 Persona cast 和请求 batch | ✓ |  |  | 拒绝 |
| `population` | scientific | 定义 Persona counts 和顺序敏感 `effective_cast` | ✓ |  |  | 拒绝 |
| `provider` | model_request | 选择请求的 Provider |  | ✓ |  | 拒绝 |
| `model` | model_request | 选择显式模型 identity |  | ✓ |  | 拒绝 |
| `cheap_model` | model_request | 定义 cheap-model 分支的候选模型 |  | ✓ |  | 拒绝 |
| `use_cheap_model` | model_request | 切换模型选择分支 |  | ✓ |  | 拒绝 |
| `openai_base_url` | model_request | 识别 OpenAI-compatible endpoint，不持久化 credential |  | ✓ |  | 拒绝 |
| `openai_api_key` | execution | 只用于鉴权运输，永久脱敏 |  |  | ✓ | 允许，记录 configured 状态差异 |
| `openai_model` | model_request | 设置 OpenAI-compatible 默认 served model |  | ✓ |  | 拒绝 |
| `temperature` | model_request | 改变 Provider 采样分布 |  | ✓ |  | 拒绝 |
| `max_tokens` | model_request | 改变响应 token 上限 |  | ✓ |  | 拒绝 |
| `cache_enabled` | model_request | 决定逻辑响应是否可来自 cache |  | ✓ |  | 拒绝 |
| `social_enabled` | scientific | 启用/移除社交传播通道 | ✓ |  |  | 拒绝 |
| `social_mode` | scientific | 选择 feed/network 信息路由 | ✓ |  |  | 拒绝 |
| `topology` | scientific | 选择社交图拓扑 | ✓ |  |  | 拒绝 |
| `n_neighbors` | scientific | 决定生成社交图的 peer degree | ✓ |  |  | 拒绝 |
| `social_weight` | scientific | 设置全局社交耦合增益 | ✓ |  |  | 拒绝 |
| `broadcast_mode` | scientific | 选择 influencer broadcast 消融路由 | ✓ |  |  | 拒绝 |
| `demote_influencer` | scientific | 改变 hub 和强制 seed 处理 | ✓ |  |  | 拒绝 |
| `leverage_enabled` | scientific | 启用杠杆与强制平仓层 | ✓ |  |  | 拒绝 |
| `leverage_ratio` | scientific | 设置参照仓中心杠杆 | ✓ |  |  | 拒绝 |
| `leverage_spread` | scientific | 设置杠杆异质性和 breach threshold | ✓ |  |  | 拒绝 |
| `maintenance_margin` | scientific | 设置 margin-call 阈值 | ✓ |  |  | 拒绝 |
| `leverage_fraction` | scientific | 设置持有参照杠杆仓的 LLM Agent 比例 | ✓ |  |  | 拒绝 |
| `digest_size` | scientific | 限制 Agent 可见邻居 statement 数 | ✓ |  |  | 拒绝 |
| `seed_fraction` | scientific | 设置初始知情 Agent 子集 | ✓ |  |  | 拒绝 |
| `reference_path` | scientific | 识别正式 validation 指标的科学输入内容 | ✓ |  |  | 拒绝 |
| `out_dir` | execution | 只改变输出位置 |  |  | ✓ | 允许，在 manifest 记录差异 |

`validate_config_classification()` 比较 `dataclasses.fields(Config)` 与上表注册表的精确集合。新字段未分类、已删字段留下 stale rule、非法 category 或空 rationale 都抛出 `UnclassifiedConfigFieldError`/`ConfigContractError`；没有自动落入 execution 的 catch-all。

### worker、batch 与 cache

- `worker_count`、driver/batching 声明不是 `Config` dataclass 字段，而是 execution runtime summary。当前 worker 只改变外层 cell/run 调度，单个 `run_sim` 仍同步执行，因此 Replay 可以使用不同 worker；manifest 保存 execution 差异。
- driver 声明的 batching 文字可不同，但实际每条逻辑请求的 call sequence、round、batch sequence/index/size、Agent/Persona 和 Prompt hash 仍在 recording 层逐条 strict 匹配。任一实际 batch 身份改变都拒绝，而且整批匹配前不推进 cursor。
- `cache_enabled` 会改变逻辑响应来源，归入 model-request；它同时出现在 `model_request_config_hash` 和 resolved `model_config`，差异不允许。
- execution summary 的 Config 字段是 `openai_api_key` 的脱敏 configured 状态和 `out_dir`；runtime 字段是 `run_id`、`scenario_id` label、`out_root`、`worker_count`、`batching` 和 `input_paths`。它们的差异会记录但不拒绝。
- 当前 simulation `Config`/CLI 没有普通日志 verbosity 或 audit-output 字段，因此不伪造它们已进入 hash。Reparse Audit 的 `--out` 属独立离线工具的执行路径，不进入 simulation Strict Replay 契约。若未来把 verbosity/audit path 增加为 `Config` 字段，显式分类表会先 fail closed，必须审阅后明确归入 execution。

### Scenario label 边界

`scenario_id` 现在只是人类可读的 label 和 execution provenance；修改 label 本身不阻止 Replay。当前实际科学 Scenario 语义由 `news_round`、`news_text`、`seed_fraction`、`reference_path` 内容身份，以及 population/信息可见性等 scientific Config 共同覆盖。Manifest 中的 `scenario.definition_sha256` 是对当前一个有限摘要的描述性 hash，不是独立、完整的 Strict Replay 兼容键；兼容判断仍以 scientific config contract 为准。

如果未来引入正式 `ScenarioSpec` / `EventStream`，Scenario payload、事件时间线、可见性策略和输入数据内容 hash 必须进入 `scientific_config_hash` 或独立且受 Strict Replay 约束的 `scenario_content_hash`；不得只依靠人类可读 label。Phase 1.1A.2 不实现 `ScenarioSpec`。

### Config mapping 输入

正常包导入（`import nmsim`、`from nmsim import Config` 或 `from nmsim.config import Config`）会先执行 `nmsim/__init__.py`，它将 `nmsim/config_ingestion.py` 的 strict contract 显式绑定到现有 `Config` class，并把 alias/异常类回填到已有 `nmsim.config` 导入表面。因此通过受支持的包路径取得的 `Config.from_dict(data, strict=True)` 对 mapping 输入默认 fail closed。它先通过集中的 `CONFIG_FIELD_ALIASES` 规范化有证据的历史名称，再校验未知字段。

本次对源码、Git 历史和现存 config artifact 的审计没有找到任何旧 **mapping** alias 证据，因此当前 `CONFIG_FIELD_ALIASES={}`。CLI 的 `--rounds` / `--out` / `--temp` 由 argparse 显式映射，不被冒充为 `from_dict` 的历史合同。未来只有在找到可审计旧 artifact 时才能向集中 map 加入精确 alias；届时规范化仍在 unknown-key validation 之前完成，alias 与 canonical 名或多来源同时出现时，即使值一样也必须拒绝。

未知名字段以稳定排序列出，安全的近似建议只用于诊断，不会自动更正或继续。输入值永不进入错误；过长、控制字符或 credential/private-shaped key 也会截断或脱敏为 hash，不回显 API key、Authorization 内容或 private rationale。

`strict=False` 没有保留为宽松迁移通道；显式传入它会抛出 `ConfigSchemaError`，不忽略任何字段。当前 `nmsim.run` 和 `experiments.run_seed` 由 `argparse` 解析已知 flag 并显式构造 `Config`；`argparse` 自身会拒绝未知 CLI flag，它的 `--rounds` 等 flag 不等于跳过 mapping alias 校验。Strict Replay 也不把 recorded summary 宽松反序列化为 `Config`；它将当前已合法构造的 effective Config 与 recording contract 比较。未来任何配置文件、manifest 恢复或研究入口都必须使用 strict mapping ingestion，不能以当前默认值伪装未知字段从未出现。

未知输入 key 和“新增 `Config` dataclass 字段但没有进入分类表”是两个独立的 fail-closed 边界：前者由 `Config.from_dict` 在对象构造时拒绝，后者由 `validate_config_classification()` 在契约构建时拒绝。Phase 1.1B 的两阶段 CLI 在安全 output root/run id 已确定后，将这类 full-validation 失败封存为 `failure_stage=config_validation` 的 provisional managed attempt：有 failed manifest 和一个 `RunFailed`，无 `RoundStarted`、Provider/网络或成功 canonical projection。非法 run id/路径穿越、output root 无法安全解析/创建等极早期错误可能只有脱敏 `provenance_not_created_reason`；`--help`/`--version` 正常退出且不创建 run。详见 [MANAGED_RUN_LIFECYCLE.md](MANAGED_RUN_LIFECYCLE.md)。

## Manifest 与 recording 元数据

每个新 run 的 manifest 以及每条 `llm_records.jsonl` 记录都保存：

- `fingerprint_schema_version`
- `decision_parser_schema_version`
- `decision_parser_source_hash`
- `event_schema_version`
- `recording_schema_version`
- `prompt_source_hash`
- `persona_source_hash`
- `simulation_core_source_hash`
- `scientific_component_fingerprint`
- `scientific_component_files` 与逐文件 hash
- `git_commit`
- `git_dirty`
- `config_hash_schema_version` 与 `config_classification_hash`
- `full_effective_config_hash`、`scientific_config_hash`、`model_request_config_hash`、`execution_config_hash`
- 三类字段名清单和脱敏规范化摘要

当前版本为：Decision parser schema `1.0`、event schema `1.0`、recording schema `1.2`、fingerprint schema `1.0`、config hash schema `1.0`。Git identity 说明运行来自哪个完整快照，但不是唯一、也不是首要的兼容性判断。

### Recording schema 1.2 正式结构

新 `RecordingLLM` 只能写出 `recording_schema_version=1.2`，调用方不能要求它伪装成历史 envelope。在调用 Provider 之前，`validate_v12_metadata()` 要求完整的 source/config contract；每条响应落盘前，`validate_v12_record()` 还会验证声明版本和正式结构。Schema 1.2 至少要求：

- fingerprint、Decision parser、event 和 recording schema version；
- parser source、Prompt、Persona、simulation core 和总科学指纹 hash；
- config hash schema/classification，full/scientific/model-request/execution hash 与脱敏摘要；
- secret-free `model_config`；
- `agent_id`、`persona_id`、round、全局 call sequence、batch sequence/index/size；
- 完整 system/user Prompt 及各自和组合 Prompt hash；
- 最终 raw response 和 `response_hash`。

加载 1.2 时会根据正式 required-field/type/hash/version 规则校验，重算 Prompt/response hash，并验证全文件的 batch 顺序和 compatibility/model identity。空 recording 在 constructor preflight 直接以 `empty_recording` 拒绝，不推迟到首轮；`allow_append=True` 也被禁用，以免修改或破坏历史 recording。不会用“某些字段恰好存在”代替 schema validation，也不会把 1.0/1.1 原地改写或提升成 1.2。

### 兼容矩阵

| Recording schema | Runtime config contract | Strict Replay | Reparse Audit |
|---|---|---|---|
| `1.0` | 无 | 拒绝：`legacy_recording_missing_replay_contract` | 允许 |
| `1.1` | 无（pre-config-contract） | 拒绝：`recording_missing_runtime_config_contract` | 允许 |
| `1.1` | 有（Phase 1.1A.1 transitional） | 默认拒绝：`transitional_schema_1_1_with_config_contract` | 允许 |
| `1.2` | 完整且通过正式 schema validation | 所有 strict hash/identity 匹配时允许 | 允许 |

声明为 1.2 但缺少必需字段或没有通过正式校验的记录是 malformed 1.2，不会降级为 1.1 或根据当前默认值补齐。Strict Replay 先拒绝结构错误；只要 raw response 等审计输入仍可读，Reparse Audit 可以进行离线检查，但会将该记录标成 `invalid_schema_1_2` / `strict_replay=false`，不会修复或升级原记录。

## Strict replay 校验顺序

Strict replay 是 `--replay-from` 的默认且唯一正式语义，没有静默忽略不兼容的开关。`ManagedRunContext` 先以最终有效 `Config` 调用 `RunManager` primitives，创建不可覆盖 run directory、running manifest 和 `RunStarted`；随后 `prepare_llm()` 构造 `ReplayLLM`，在 `run_sim` 之前完成所有配置/版本 preflight。此时尚未发生 `RoundStarted`、未消费第一条历史响应，也不存在 Provider 对象或网络 fallback。检查分为五层：

1. Schema 矩阵与正式结构：先根据 envelope 明示版本分类 1.0、两类 1.1 或 1.2；只有完整 1.2 可进入 Strict Replay。对 1.2 校验 required fields、type、hash 形式和版本一致性，不从字段存在性猜测版本。
2. 记录完整性：验证 JSON object、record type、连续 call sequence、完整 Prompt 的 `system_hash`/`user_hash`/`prompt_hash`、原始响应 hash，以及整份记录内 model/compatibility identity 一致。
3. 配置 preflight：要求 config hash schema/classification 一致，`scientific_config_hash` 和 `model_request_config_hash` 完全一致；`execution_config_hash` 可不同，但计算字段级差异供 manifest 审计。
4. 版本/源兼容：逐字段比较 fingerprint/parser/event/recording schema，parser/Prompt/Persona/simulation hashes 和总体科学指纹。
5. 运行时请求：逐项比较 Provider、模型、temperature、`max_tokens`、cache、cheap-model、endpoint hash 等 secret-free resolved model config，以及 call sequence、round、batch sequence/index/size、Agent identity、Persona identity 和完整 Prompt hash。整批请求全部匹配后才推进 cursor，结束时还必须消费全部记录。

任一 strict 字段不一致都会立即抛出 `ReplayMismatchError`。配置错误指出 category、差异字段、每个规范值的安全摘要及 expected/actual category hash 缩写；版本/hash 错误也指出具体字段。错误信息不会输出完整 Prompt、API key、endpoint credential、原始响应或 private rationale。Replay 对象不持有底层 Provider，没有网络 fallback，也不会在失败后转而创建 Provider。

由受管 CLI 发起的 mismatch 会保留 `status=failed`、`failure_stage=replay_preflight`、`outputs_complete=false` 的 manifest、实际 completion 和唯一 `RunFailed`；不会发布看起来成功的 canonical/flat 输出。`llm.runtime` 在读取 replay 元数据之前就记录 `network_access=false`、Provider call 为 0 和连接上限 0。

## Git commit、dirty 状态与跨 commit

- Git commit 相同不自动代表兼容：如果 dirty 工作树改动了科学组件，源 hash/指纹改变，strict replay 必须拒绝。
- Git commit 不同不自动代表不兼容：如果所有 strict 字段和请求身份相同，Replay 可以继续；manifest 写入 `cross_commit_same_scientific_fingerprint=true`，并同时记录 source/current commit、dirty 状态和科学指纹。
- 仅文档变化会令 commit 或 `git_dirty` 不同，但不会改变 strict 字段，所以不会单独阻止 Replay。
- `git_dirty` 是 provenance 字段，不是 blanket gate；真正的兼容判断来自相应源 hash 和 schema。

这只说明两个工作树在当前指纹覆盖边界内兼容，不说明整个仓库等同，也不免除对遗漏科学组件的持续审计。

### Phase 1.1B 生命周期跨 commit 边界

Phase 1.1B 的 `run_context.py`、`managed_cli.py`、`entrypoints.py`、`run.py` 与 experiment driver/lifecycle 变更位于当前 scientific component allowlist 之外。它们可以改变 manifest 生命周期、completion 口径和输出发布时机，但不改变 Prompt、Persona、parser、Agent observation、市场、社交、杠杆或 validation。因此，Phase 1.1A 产生的完整 schema 1.2 recording 在 scientific fingerprint、scientific/model-request Config、resolved model config 和每次 request identity 全部匹配时，可在 Phase 1.1B commit 上执行 cross-commit Strict Replay。

这项兼容不是对生命周期变更的 blanket 豁免：如果未来将影响科学行为的代码错放在 allowlist 之外，必须修正指纹覆盖集/版本并增加测试，不能借此声称兼容。Recording schema 仍为 `1.2`，Phase 1.1B 未引入新的 Replay 忽略开关或降级路径。生命周期终态与 completion 单位分别见 [MANAGED_RUN_LIFECYCLE.md](MANAGED_RUN_LIFECYCLE.md) 和 [COMPLETION_ACCOUNTING.md](COMPLETION_ACCOUNTING.md)。

### 2026-07-15 跨 commit 示例的真实 provenance

原记录与 Replay 保留在本机 `/private/tmp`，未修改历史文件。已直接读取 manifest/首条 recording 并与 Git tree 核对：

| evidence | 真实值 |
|---|---|
| source run | `/private/tmp/nmsim-phase11a-record-20260715/runs/record` |
| source manifest created / ended | `2026-07-15T04:19:35.446750Z` / `2026-07-15T04:19:44.810342Z` |
| first recording time | `2026-07-15T04:19:35.489831Z` |
| recording schema | `1.1` |
| source Git commit | `e86485fd0863022922d240158b4cae3eb397aa9d` |
| source Git dirty / diff hash | `true` / `eb815c112b2a16d8f73eba6f3e8ee7edfeb90a6dc3af3dfdb35532baf12649c5` |
| scientific component fingerprint | `29487b1fb3b03fcd27d70b987b80750c86d102e697140b85516d12ab679a9cd1` |
| parser schema / source hash | `1.0` / `a8198b120e332c1a1c21091cb7a2325da476a669eed7fa62d17cdd6615be4e7a` |
| Prompt source hash | `db9c26c22d35223ea7ee768d622c608f9ca27b4b81b58615720704c39e906171` |
| Persona source hash | `6e6fc8d48dbe31106b14852094a325e38958b35eb3a0552c712db4f5807cba06` |
| Phase 1.1A.1 config hashes | N/A；该记录早于 config contract 扩展，四个 hash 均不存在，不回填 |
| later replay | `/private/tmp/nmsim-phase11a-crosscommit-20260715/runs/cross-commit-replay`；current commit `53a5f2d5a4a9a797d71f6dc650dc77a219099aa3`，clean，cross-commit flag `true` |

Git 时间线为：基线 commit 于 `2026-07-15T11:52:44+08:00` 创建；source recording 于 `12:19:35+08:00` 在同一 HEAD 但 dirty 工作树中创建；Phase 1.1A commit 于 `12:27:25+08:00` 创建；clean replay 于 `12:27:39+08:00` 开始。基线 tree 的 `nmsim/recording.py` 明确写入 schema `1.0`，且不存在 `nmsim/fingerprint.py`；source 却包含 schema `1.1` 契约字段且如实标记 dirty。两 commit 间指纹 allowlist 中的十个科学文件没有 diff，契约/审计文件有 diff。

因此，这个历史示例证明的是：“由 dirty 工作树中的 schema 1.1 代码生成的记录，在该代码提交后，可以在相同 scientific source fingerprint 和当时全部 strict 字段下跨 Git commit identity Replay”。它**不证明** schema 1.0 记录与 1.1 兼容；由于该源记录缺少新配置契约，它在 Phase 1.1A.1 下也应 fail closed，不再作为新契约的成功 fixture。

真正由 clean Phase 1 schema 1.0 代码生成的本地样本为 `/private/tmp/nmsim-phase05-git-manifest/runs/phase1-git-baseline-check`：manifest created `2026-07-15T03:54:13.873367Z`，commit `e86485f…`，`dirty=false`，`diff_hash=null`，24 条 recording，首条 `schema_version=1.0`。它不含 parser/event/fingerprint/config 兼容字段，可用于 legacy strict-reject 和 Reparse Audit 测试。在已检查的本地 schema 1.1 记录中，未发现“`git.dirty=false` 但同时含基线 commit 不存在的 1.1 字段”这类 provenance 矛盾。

## Legacy、pre-contract 与 transitional recording 边界

Phase 1 的 `llm_records.jsonl` 外层 schema `1.0` 没有完整的 parser/event/fingerprint/config 契约。加载器可以识别该旧 envelope，但在构造 `ReplayLLM` 时立即抛出 `legacy_recording_missing_replay_contract`，列出缺失字段，说明原历史运行不因此无效，并指向 Reparse Audit。系统不会猜测或补造历史版本/配置身份，也不会以 Git commit 或当前默认值替代这些字段。

Phase 1.1A 早期生成的外层 schema `1.1` 可能已有 parser/event/fingerprint 字段，但还没有 `config_hash_schema_version`、classification/category hashes 和脱敏摘要。这类 pre-extension 1.1 记录也不会被当前默认 Config “升级”；Strict Replay 在第一轮前抛出 `recording_missing_runtime_config_contract`。

Phase 1.1A.1 开发期间还产生过“外层仍为 `1.1`，但已包含完整 runtime config contract”的 transitional 记录。它们不代表原运行无效，但也不是 Phase 1.1A 正式冻结后的格式；Strict Replay 默认抛出 `transitional_schema_1_1_with_config_contract`，不按当前代码重写或提升成 1.2。

上述三类历史/过渡记录仍可以用于离线 reparse audit。审计汇总会将缺失契约如实标记为 unavailable，并区分历史 ParsedDecision 存在或缺失；这不把旧记录升级为严格兼容。Reparse 仍不访问网络、不创建 Provider、不修改原目录、不生成市场轨迹，private rationale 正文仍只能进入 0600 私有输出。

## 离线 Reparse Audit

命令：

```bash
python3 -m nmsim.reparse_audit \
  --run /path/to/historical-run \
  --out /tmp/nmsim-reparse-audits
```

`--run` 必须是含 `llm_records.jsonl` 的历史 run 目录；`--out` 是父目录。工具在父目录中创建带 UTC 时间和 UUID 的新目录，使用排他创建且拒绝把输出放进历史 run，因此不会修改或覆盖输入。

输出包括：

```text
reparse-audit-<timestamp>-<uuid>/
├── reparse_summary.json        # 公共汇总，0644
├── reparse_results.jsonl       # 逐请求公共字段 diff，0644
└── reparse_private.jsonl       # rationale 正文 diff，0600
```

公共记录保存 request identity，并比较 reasoning 是否存在及其 hash、`sentiment`、`public_take`、`action`、`quantity`、`limit_price`、`reservation_price`、parse/fallback status 和 validation errors。private rationale 正文只进入 0600 私有文件；公共报告不会显示其正文，也绝不会用 rationale 补成 `public_take`。

当前 `Order`/parser API 没有独立返回 `reservation_price`、fallback status 或 validation error 对象；审计工具因此从原始 JSON 和当前 parser 的明确 fallback 标记派生这些审计字段，并把未出现在历史 ParsedDecision 中的字段标为 unavailable，而不是伪造旧值。

汇总报告提供总响应、成功重新解析、解析失败、完全一致、部分一致、有差异、无法比较以及逐字段差异计数，并明确写出 `provider_calls=0`、`network_access=false`、`simulation_continued=false` 和 `price_path_generated=false`。若没有历史 ParsedDecision，则标记 `comparison_unavailable`，仍报告当前解析结果，不伪造旧值。

Reparse 不继续 simulation，也不生成新价格轨迹：一旦较早 Decision 变化，后续市场状态和 Prompt 就会分叉，历史原始响应不再代表那个新状态。

## Strict replay 命令示例

先创建 recording：

```bash
python3 -m nmsim.run \
  --provider mock --rounds 4 --news-round 2 --seed 7 \
  --out /tmp/nmsim-record
```

再使用完全相同的运行与模型参数重放：

```bash
python3 -m nmsim.run \
  --provider mock --rounds 4 --news-round 2 --seed 7 \
  --replay-from /tmp/nmsim-record/runs/<record-run-id> \
  --out /tmp/nmsim-replay
```

`--replay-from` 也可指向 `llm_records.jsonl`。无论源路径形式如何，strict 契约相同；不存在失败后调用真实 Provider 的路径。
上述 Record 命令的新 `llm_records.jsonl` 只会写出 schema 1.2；历史 1.0/1.1 记录不会因为被读取而改写。

## 适用边界与剩余风险

- 指纹边界是显式维护的文件集合；若未来新增会影响科学行为的模块而未纳入清单，可能出现错误兼容。新增机制必须同时更新清单和测试。
- `decision_parser_schema_version` 需要在有意改变解析契约时人工升级；source hash 可捕获未升级但源码已变的情况。
- Strict replay 验证应用层保存的最终响应，不包含 Provider SDK 内部 retry 的每次中间 request/response。
- 即使科学指纹相同，Python/NumPy/Matplotlib 或平台差异仍可能影响数值边角和图像字节；manifest 中的环境版本用于解释这些差异。
- Reparse 是 parser 差异审计，不是因果识别、反事实推演或统计重复。
- Managed lifecycle 的 completion 是带单位 provenance 计数，不会让一次 Replay 或同一市场内的多条 Agent Decision 自动成为多个独立统计样本。
- `nmsim/config.py` 仍在 scientific component allowlist 中，而且 Phase 1.1A.2 保持了它的原始字节和全部默认值。Strict ingestion 位于 allowlist 外的 `nmsim/config_ingestion.py`，由包初始化边界显式安装；因此 Prompt、Persona、simulation core source hash 和总 scientific fingerprint 保持基线身份。这不是将新市场规则移出 allowlist：ingestion 只拒绝未知/含糊输入，最终 effective Config 仍由配置 hash 约束，recording 结构变化则由 schema 1.2 约束。未来修改这个绑定边界时仍必须审查 config/recording schema 并保留 fail-closed 测试。
