# Replay 兼容性契约

本文定义 Phase 1.1A 的本地 Replay 版本边界。它的目标是让不兼容的历史响应重放明确失败，并为解析器升级提供离线审计路径；它不改变 Agent、Prompt、Persona、市场、社交、杠杆、默认参数或正常响应的解析结果。

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

这组文件覆盖 Agent observation 与 Prompt 构造、Decision 解析、相关类型和配置默认值、simulation 科学步骤顺序、市场 clearing、社交传播、杠杆/强平和指标计算。`recording.py`、`provenance.py`、`reparse_audit.py` 等审计设施由独立 schema 约束；普通文档、测试、实验 driver、运行目录、结果、私有日志和时间戳不进入科学组件指纹。

因此，只修改 `README`、`docs/` 或普通工作流文档不会改变科学指纹。反之，只要上述科学组件的原始字节变化，即使 Git commit 没变而工作树为 dirty，相关 hash 也会变化，strict replay 会拒绝。

### 稳定计算规则

当前算法由 `fingerprint_schema_version=1.0` 标识：

1. `decision_parser_source_hash`：提取 `nmsim/llm.py` 中顶层 `parse_order` 函数的源码范围，统一换行为 LF，再计算 SHA-256。
2. `prompt_source_hash`：对 `nmsim/prompts.py` 原始字节计算 SHA-256；这保持 Phase 1 已建立的 Prompt source hash 口径。
3. `persona_source_hash`：以 AST 读取 `PERSONAS` 字面量，再用 UTF-8、key 排序、无额外空白且禁止 NaN 的规范 JSON 计算 SHA-256。
4. `simulation_core_source_hash`：以上述科学文件的仓库相对 POSIX 路径排序；对每项依次写入路径字节长度、路径字节、文件字节长度和原始文件字节，再计算 SHA-256。绝对项目路径、目录遍历顺序、mtime、权限和运行产物均不参与。
5. `scientific_component_fingerprint`：对包含 fingerprint/parser schema、parser hash、Prompt hash、Persona hash、simulation core hash 和排序后文件清单的规范 JSON 计算 SHA-256。

`event_schema_version` 与 `recording_schema_version` 不折叠进总体科学指纹，而是作为独立 strict 字段校验。这使错误能准确指出是事件/记录协议不兼容，而不是只报一个总 hash 不同。

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

当前版本为：Decision parser schema `1.0`、event schema `1.0`、recording schema `1.1`、fingerprint schema `1.0`。Git identity 说明运行来自哪个完整快照，但不是唯一、也不是首要的兼容性判断。

## Strict replay 校验顺序

Strict replay 是 `--replay-from` 的默认且唯一正式语义，没有静默忽略不兼容的开关。检查分为三层：

1. 记录完整性：JSON object、record type、支持的外层 schema、连续 call sequence、完整 Prompt 的 `system_hash`/`user_hash`/`prompt_hash`、原始响应 hash，以及整份记录内 model/compatibility identity 一致。
2. 版本兼容：逐字段比较 fingerprint/parser/event/recording schema，parser/Prompt/Persona/simulation hashes 和总体科学指纹。
3. 运行时请求：逐项比较 Provider、模型、temperature、`max_tokens`、cache、cheap-model、endpoint hash 等 secret-free model config，以及 call sequence、round、batch sequence/index/size、Agent identity、Persona identity 和完整 Prompt hash。整批请求全部匹配后才推进 cursor，结束时还必须消费全部记录。

任一字段不一致都会立即抛出 `ReplayMismatchError`。版本/hash 错误会指出具体字段；hash 只显示缩写，结构值以摘要 hash 表示，错误信息不会输出完整 Prompt、原始响应或 private rationale。Replay 对象不持有底层 Provider，没有网络 fallback，也不会在失败后转而创建 Provider。

由受管 CLI 发起的 mismatch 会保留 `status=failed` 的 manifest、失败原因、实际完成数和 `RunFailed` 事件；不会发布看起来成功的 canonical 输出。`llm.runtime` 在读取 replay 元数据之前就记录 `network_access=false`、`provider_calls=0` 和连接上限 0。

## Git commit、dirty 状态与跨 commit

- Git commit 相同不自动代表兼容：如果 dirty 工作树改动了科学组件，源 hash/指纹改变，strict replay 必须拒绝。
- Git commit 不同不自动代表不兼容：如果所有 strict 字段和请求身份相同，Replay 可以继续；manifest 写入 `cross_commit_same_scientific_fingerprint=true`，并同时记录 source/current commit、dirty 状态和科学指纹。
- 仅文档变化会令 commit 或 `git_dirty` 不同，但不会改变 strict 字段，所以不会单独阻止 Replay。
- `git_dirty` 是 provenance 字段，不是 blanket gate；真正的兼容判断来自相应源 hash 和 schema。

这只说明两个工作树在当前指纹覆盖边界内兼容，不说明整个仓库等同，也不免除对遗漏科学组件的持续审计。

## 旧 recording schema 1.0

Phase 1 的 `llm_records.jsonl` 外层 schema `1.0` 没有完整的 parser/event/fingerprint 契约。加载器可以识别该旧 envelope，以便给出具体诊断，但它缺少 strict 字段，不能通过 Phase 1.1A 的 strict replay。系统不会猜测或补造历史版本身份，也不会以 Git commit 替代这些字段。

旧记录仍可以用于离线 reparse audit。审计汇总会将其标为 `legacy_contract_unavailable`，列出缺失字段，并如实区分历史 ParsedDecision 存在或缺失；这不把旧记录升级为严格兼容。

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

## 适用边界与剩余风险

- 指纹边界是显式维护的文件集合；若未来新增会影响科学行为的模块而未纳入清单，可能出现错误兼容。新增机制必须同时更新清单和测试。
- `decision_parser_schema_version` 需要在有意改变解析契约时人工升级；source hash 可捕获未升级但源码已变的情况。
- Strict replay 验证应用层保存的最终响应，不包含 Provider SDK 内部 retry 的每次中间 request/response。
- 即使科学指纹相同，Python/NumPy/Matplotlib 或平台差异仍可能影响数值边角和图像字节；manifest 中的环境版本用于解释这些差异。
- Reparse 是 parser 差异审计，不是因果识别、反事实推演或统计重复。
