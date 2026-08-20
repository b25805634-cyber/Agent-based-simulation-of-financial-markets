# Config Hash Identities

“默认 Config hash”不是一个可审计的唯一概念。任何报告、测试或
manifest 说明都必须写出具体 hash 字段名、schema、输入 fixture 和
base directory；不得只写模糊的“Config hash”。

## V2 attention-distillation 的独立 identity envelope

`python3 -m experiments.v2_attention_market` 不使用 legacy `Config` 来表达 V2
科学语义。它的正式 summary/report 必须同时写出以下四个具名、
secret-free identity，不得简称为“V2 Config hash”：

| Identity | Schema | 绑定范围 |
|---|---|---|
| `v2_scientific_config_hash` | `v2_attention_market/0.1` | 固定日频 state/action/prompt 合同、状态设计、family-grouped split 的显式阈值/分配/回退规则、train-only OOD 几何与 `abs(z)>3.0` 规则、Student/baseline 架构与损失、整数清算/信用设施规则及 budget x behavior 四格参数 |
| `v2_model_request_config_hash` | `v2_teacher_request/0.1`；finish-audit v3-v7 为 `v2_teacher_request/0.2`；recommended-sampling v8 为 `v2_teacher_request/0.3`；JSON-object v9 为 `v2_teacher_request/0.4`；audited-retry v10 为 `v2_teacher_request/0.5` | Teacher provider、请求 model、temperature/top-p/top-k 采样设置、token cap、response-format 生成约束、state/replicate 请求计划与 termination provenance/验收合同；不包含 credential |
| `v2_execution_config_hash` | `v2_attention_execution/0.1`；long-timeout v5-v9 为 `v2_attention_execution/0.2`；audited-retry v10 为 `v2_attention_execution/0.3` | worker 数、dry/live 执行模式、output/run-id 执行身份与 `v2_execution_component_fingerprint/0.1`；v5-v9 显式绑定 timeout/connect/零重试；v10 还绑定 logical-vs-physical attempt 边界、应用重试次数与延时、eligible error allowlist、non-retryable 语义类和 SDK-retry-zero |
| `v2_full_effective_config_hash` | `v2_attention_full_effective_config/0.1` | 显式 envelope，绑定上述 scientific/model-request/execution 三个 identity 及各自 schema |

每次报告这些值时，还必须标出实际 managed run 目录/执行上下文与
有效 V2 计划；不得把其他 run 的值称为“默认值”。变更 Provider request
设置不应伪装成科学设计未变，变更 worker/dry-live 也不应伪装成同一
full-effective identity。真实 Provider 仍可随机；相同 hash 不是端点完全确定性
声明。

Finish-audit v3 的 model-request config 投影和 Teacher public/private row
使用 `v2_teacher_request/0.2`，明确记录 SDK `finish_reason` 且仅接受
精确的 `stop`。但 sample ID 的计划身份材料仍固定为
`v2_teacher_request/0.1` 的 `state_id + prompt_hash + replicate_index`，
以保留原计划的 sample ID、顺序和 canary。这两层 schema 不得
混称；相同 sample ID 也不是复用历史 Provider response 的授权。
具体 v3 冻结口径见 `docs/V2_TEACHER_PILOT_V3.md`。

External-network execution successor v4 继续使用
`v2_teacher_request/0.2`，并保持 v3 的 wire-level request、prompt、
4096 token cap、finish gate 和 `v2_teacher_request/0.1` sample identity
不变。但 `pilot_profile_id` 本身属于 model-request config 投影，
因此 v4 的 `v2_model_request_config_hash` 预期与 v3 不同；这是
successor-profile identity 的保守区分，不是请求载荷或科学
语义改变。v4 的 run-id/profile 也必须产生独立的
`v2_execution_config_hash` 和 `v2_full_effective_config_hash`。外部网络
权限是执行前提，不是 Provider 稳定性或模型权重身份声明。
具体 v4 证据、非复用边界和冻结命令见
`docs/V2_TEACHER_PILOT_V4.md`。

Long-timeout execution successor v5 继续保持 v4 的科学设计、
model-generation payload、`v2_teacher_request/0.2` finish gate 与
`v2_teacher_request/0.1` sample identity。v5 新增每个 logical Provider
request 的 600 秒 hard wall-clock deadline，并把 HTTPX read/write/pool
各 phase-inactivity timeout 从 120 秒改为 600 秒；connect timeout 仍为
10 秒，application retry 仍为零。hard deadline 限制整个 logical request
的墙钟耗时；HTTPX phase timeout 只限制相应阶段的不活动时间，不能称为
total timeout。上述 transport policy 必须以
`hard_request_deadline_seconds=600`、
`httpx_phase_inactivity_timeout_seconds=600`、
`connect_timeout_seconds=10`、`provider_retry_count=0` 进入 v5 的
`v2_attention_execution/0.2` 投影；v1-v4 保持原有
`v2_attention_execution/0.1` 投影形状。这是 profile-specific additive
schema successor，不是对旧运行的迁移：历史 v1-v4 manifest 不回填、
不重写，也不得按 0.2 重新解释；v1-v4 没有 hard wall-clock request
deadline，不能从其 HTTPX phase timeout 反推一个。该变更使
`v2_execution_config_hash` 与 `v2_full_effective_config_hash`
形成新 identity。两个 600 秒控制都只是执行上限，不是重试、端点稳定性
声明或成功保证。由于 `pilot_profile_id` 仍被 model-request config 保守绑定，v5 的
`v2_model_request_config_hash` 也预期与 v4 不同，但不得把该差异误报为
prompt、model、temperature、token cap、state plan 或 termination contract
改变。具体 a4 失败证据、v5 非复用边界和冻结命令见
`docs/V2_TEACHER_PILOT_V5.md`。

Larger-output-cap successor v6 保持 v5 的科学设计、prompt、
state/sample plan、exact-`stop` gate、Student/market 与全部 transport
policy，但明确把唯一的 model-generation request 字段
`max_tokens` 从 4096 改为 16384。这是 request-semantic change，
不得误报为 execution-only change；它使
`v2_model_request_config_hash` 必须与 v5 不同。v6 继续使用
`v2_teacher_request/0.2` request/row schema，但 sample identity 仍使用
`v2_teacher_request/0.1` 的
`state_id + prompt_hash + replicate_index`，因此保留计划中的
sample ID、顺序和 canary 不等于允许复用历史 Provider
response。v6 仍使用 `v2_attention_execution/0.2`：600 秒 hard
wall-clock deadline、600 秒 HTTPX read/write/pool phase-inactivity
timeouts、10 秒 connect timeout 与零 retry 全部不变。新的
run/profile 身份仍要求独立的 `v2_execution_config_hash` 与
`v2_full_effective_config_hash`。a5 在第七个 response 保存的
`finish_reason=length` 与 `output_tokens=4096` 是提高 cap 的运行证据，
但不是 v6 成功、端点连续稳定或更长 response 必然有效的
声明。具体 a5 失败证据、v6 非复用边界和冻结命令见
`docs/V2_TEACHER_PILOT_V6.md`。

Documented-cap and extended-timeout successor v7 继续使用
`v2_teacher_request/0.2` request/row schema 和
`v2_teacher_request/0.1` sample identity，但把唯一的
model-generation request 字段 `max_tokens` 从 16384 改为
65536。这是明确的 request-semantic change，必须使
`v2_model_request_config_hash` 与 v6 不同；不得把它误报为仅
profile identity 或 execution change。v7 同时保持
`v2_attention_execution/0.2` schema，但把 hard wall-clock deadline
和 HTTPX read/write/pool phase-inactivity timeout 各从 600 秒改为
1800 秒；connect timeout 仍为 10 秒，retry 仍为零。因此
`v2_execution_config_hash` 与 `v2_full_effective_config_hash` 也必须
与 v6 不同。temperature 仍为 0，`top_p`/`top_k` 仍不发送；
prompt、state、sample ID/顺序、exact-`stop` gate、Student/market 与
privacy boundary 不变。官方 MiniMax 文档对非 M3 模型推荐
65536 且明确 M2.x thinking 不能关闭，以及当前部署的
vLLM/OpenAPI/tokenization 诊断，都只是新 successor 的运行依据；
它们不是端点稳定、模型行为或 v7 成功的证据。具体 a6 失败
证据、a1-a6 非复用边界、官方链接和冻结命令见
`docs/V2_TEACHER_PILOT_V7.md`。

Near-context-cap recommended-sampling successor v8 使用新的
`v2_teacher_request/0.3` request/row schema，把 `max_tokens` 从
65536 改为 190000、temperature 从 0 改为 1，并首次显式发送
`top_p=0.95` 与 `top_k=40`。这些都是 model-generation request
semantics，必须进入 `v2_model_request_config_hash`；其中采样元组改变
Teacher 数据生成过程、replicate 离散度、soft-label 与 Student 训练分布，
不得声称为纯 execution 改变。sample identity 仍使用
`v2_teacher_request/0.1` 的
`state_id + prompt_hash + replicate_index`，因此 sample ID、顺序和
canary 不变；这不允许复用 a1-a7 的任何 response 或 honest-N。
v8 继续使用 `v2_attention_execution/0.2`，但把 hard wall-clock
deadline 和 HTTPX read/write/pool phase-inactivity timeout 各从 1800 秒改为
7200 秒；connect timeout 仍为 10 秒，retry 仍为零。因此 v8 必须同时
具有新的 `v2_model_request_config_hash`、`v2_execution_config_hash` 和
`v2_full_effective_config_hash`。由于 cap、三个采样字段与两个 timeout
同时改变，v8 与任何早期 partial run 的差异不能归因于其中单一因素。
具体 a7 失败证据、官方采样参数链接、context-margin 风险、非复用边界和
冻结命令见 `docs/V2_TEACHER_PILOT_V8.md`。

JSON-object constrained successor v9 使用新的
`v2_teacher_request/0.4` request/row schema，在完整继承 v8 的
`max_tokens=190000`、`temperature=1`、`top_p=0.95`、`top_k=40`
以及 7200/7200/10/零重试合同的同时，唯一新增标准 Chat Completions
`response_format={"type":"json_object"}`。该字段必须进入
`v2_model_request_config_hash`。即使 prompt 已要求 JSON，服务端生成约束仍可能
改变 token 路径、termination、解析率、Teacher label、replicate 离散度、
soft-label 与 Student 训练分布；不得声称这是纯 prompt 或 execution 变更。
sample identity 仍使用 `v2_teacher_request/0.1`，execution 仍使用
`v2_attention_execution/0.2`。a8 是含一个 unresolved request 的外部执行中断，
不是 response-format 对照结果；监控到的 VPN 断路与最终 KeyboardInterrupt
只能作为运行共现，不能声称因果。因此 v9 仍需 fresh 162，禁止复用
a1-a8 的 response/honest-N，也不能把 v9 与未完成 a8 的差异单因素归因于
`response_format`。具体证据、官方 vLLM/OpenAPI 依据、非复用边界、冻结命令和
风险见 `docs/V2_TEACHER_PILOT_V9.md`。

Audited transient-retry successor v10 使用
`v2_teacher_request/0.5` request/row schema 与
`v2_attention_execution/0.3` execution schema。它完全继承 v9 的
MiniMax-M2.7、190000 token cap、1/.95/40 采样元组、JSON-object
response format、7200/7200/10 秒 transport 值和 SDK retry 0；唯一新机制是
每个 logical Teacher request 最多 5 个可审计 physical Provider attempts，重试前延时
严格为 `[10,30,60,120]` 秒。只有 connection、timeout、HTTP 429 和 HTTP 5xx
可重试；收到的 content、alias、finish reason、parser 或 feasibility 失败均不得
重采样。每个 physical attempt 必须进入 Provider-attempt accounting 和 0600 private
audit，但多个 physical attempts 仍只能解析一个 logical row，最多贡献一个
honest-N。该执行/获取变更必须进入 `v2_execution_config_hash`；profile 与
request/row schema 的保守 successor identity 也使
`v2_model_request_config_hash` 与 v9 不同，但不得把该差异误报为 generation
payload 改变。sample identity 仍使用 `v2_teacher_request/0.1`，a1-a9 全部不复用。
其中 timeout allowlist 必须区分 OpenAI SDK `APITimeoutError` 与客户端
7200 秒 hard-deadline `asyncio.wait_for` 产生的 built-in `TimeoutError`；两者都可重试。
v10 `provider_calls` 的 unit 是
`physical_provider_attempts_after_cache_and_replay`，且 SDK retries disabled；
`llm_logical_requests` 仍使用 logical-request unit，两者不得混用。
具体 a9 失败证据、logical/physical accounting、retry allowlist/禁止类、冻结命令和风险
见 `docs/V2_TEACHER_PILOT_V10.md`。

V2 的 real-provider request 明确不发送 seed：
`request_seed=null` 且
`request_seed_support=unsupported_and_not_sent`。sample/replicate 的 SHA-256
content identity 只用于请求与计数追踪，不是 Provider 随机性控制，
相同 model-request hash 也不得被解释为实际端点确定性。

OpenAI-compatible route 使用 V2-local
`v2_endpoint_identity/0.1`：host/port/path 和 query key 可以进入规范化
route identity，但 userinfo、fragment 和全部 query value 被省略，已配置
API key 若出现在 path 中也先脱敏。credential 值和 raw endpoint 文本
不进入 `v2_model_request_config_hash`。这是 V2 的局部身份边界，不修改
V1/provider-capability 既有合同。

`v2_scientific_component_fingerprint/0.1` 故意保守地绑定四个 V2
模块以及完整的 `experiments/v2_attention_market.py`。由于报告渲染也在
该 managed entrypoint 中，当前“只改报告”也可能使
`v2_scientific_config_hash` 变化。这是防止科学实现漏绑定的已知
conservative overbinding，不是报告文字必然改变数值科学语义的
声明。跨运行比较必须同时报告该 fingerprint，不得手工忽略 hash
差异。

`v2_execution_component_fingerprint/0.1` 绑定 managed entrypoint 及
Config/entrypoint registry/events/managed CLI/provider-attempt/provenance/
run-context 执行边界，并进入 `v2_execution_config_hash`。它与上述
scientific component fingerprint 分开报告，不得用任一个替代另一个。

Student/market 还有一层不同于 Config identity 的 artifact lineage：

| 字段 | 精确语义 |
|---|---|
| `model_semantic_hash` | 模型规范 JSON payload 的内容身份，绑定数值参数和结构语义 |
| `artifact_sha256` | 已写入 model JSON 文件的精确字节 SHA-256 |
| `model_envelope_hash` | `student_model_envelope` 在加入 self-hash 前的规范内容 hash，绑定 feature order、state contract、dataset/training projection、split、OOD reference 与各 model hash |
| `student_model_envelope_artifact_sha256` | 包含 `model_envelope_hash` 字段的 envelope 文件精确字节 hash |

Market `model_lineage` 同时携带 deployed model 的 semantic/artifact
hash 和 envelope 的 content/artifact hash，并进入 market index、每个完成
run ledger 和每个持久化 round row。上述 hash 类型不得互换，也不得用
Config hash 代替 artifact integrity。

V2 仍通过 `ManagedRunContext` 取得现有不可覆写生命周期、Git/文件指纹和
artifact 登记能力，因此 manifest 中仍可能出现 legacy
`scientific_config_hash`/`model_request_config_hash`/`execution_config_hash`/
`full_effective_config_hash` 和 `scientific_component_fingerprint`。这些未加 `v2_`
前缀的 identity 只描述管理生命周期所需的 legacy Config/代码基础设施；
它们不是 V2 数据、Student 或市场的 scientific identity，不得用于 V2
结果复用或跨运行等价性声明。该 managed attempt 的显式 `research_profile`
将 legacy Persona contract 标记为 `applicable=false`，并指向独立 V2 prompt
合同；这是范围隔离，不是对 V1 Persona 定义的修改。Legacy V1 `Config()`
默认值、Persona/prompt 定义、科学指纹语义和现有入口行为保持不变。

## 2026-07-15 sealing cross-version check

> 本节的 38/27/9 字段计数和精确 hash 只描述 2026-07-15 的封存 fixture，不代表当前 Wave 1 Config。当前分类为 41 字段：29 scientific、10 model-request、2 execution；报告当前运行时必须从该次 manifest 取得具名 identity，不得沿用下方历史值。

使用同一固定 fixture 分别执行 `phase1.1-complete-v1` 与 Phase 1.2A
sealing commit 的 `nmsim.config_contract`：

```text
Config()
base_dir=/Users/aldrich/Desktop/agent模拟市场
execution_context={}
```

Phase 1.1 代码通过 `git archive phase1.1-complete-v1` 展开到临时目录；
未切换或修改历史 ref，也未修改历史运行。两版结果逐字段相同：

| Identity | Phase 1.1 complete | Phase 1.2A sealing check |
|---|---|---|
| `config_hash_schema_version` | `1.0` | `1.0` |
| `config_classification_hash` | `299747fa4527f820ffbc9fbd13186ab26887f8f6cca4c24b0e36d1edcf7dbebd` | same |
| `full_effective_config_hash` | `1a36131b7dec0a90af315c03b1bdb748d7b90c5d6defa3cbd2b31db437af69dd` | same |
| `scientific_config_hash` | `891609d7ff29b8579fc51dd011c1ebcda9f2f8d8ef71c304a83211089fcc1b12` | same |
| `model_request_config_hash` | `161ee24c72dcf446453c588654aba1e7694c0137b3bacd5eb8f6f071e869b960` | same |
| `execution_config_hash` | `1a7038f9d4253429565c25f13118e237a71d6c50a0e128979551cf32bc4f742c` | same |

所有 effective/scientific/model-request/execution 规范化 summary 的字段和值也
完全一致，没有跨版本字段级差异。

## 两个曾被简称为“默认 Config hash”的值

`f0508c233c669749eedc1eabc93d2bea97d2438d8a1fd8b51923ff9195697c07`
是冻结测试中的 legacy raw-default identity：

```python
sha256(json.dumps(
    asdict(Config()),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8"))
```

它没有 hash schema envelope，不使用 tagged float/path/endpoint/credential
规范化，也不区分 scientific、model-request 和 execution；它只能说明
dataclass 默认值的 raw JSON 投影。两版对此值也相同。

`1a36131b…a69dd` 则是 `config_hash_schema_version=1.0` 的
`full_effective_config_hash`：对全部 38 个有效字段的 secret-free 规范化
summary 加上 `identity=full_effective_config` 和 schema envelope 后计算。它包含
execution 字段，所以本例明确绑定上述 `base_dir`；它不能代替 strict replay
分别使用的 scientific/model-request identity。

两者输入投影和算法不同，不是互相矛盾的计算结果，也不得直接比较。

## 固定 fixture 的规范化分类摘要

Scientific（27 fields）：

```text
broadcast_mode=all; demote_influencer=false; digest_size=4;
fundamental_value=float.hex(100.0); initial_price=float.hex(100.0);
kappa=float.hex(0.12); leverage_enabled=false;
leverage_fraction=float.hex(0.5); leverage_ratio=float.hex(2.5);
leverage_spread=float.hex(0.5); maintenance_margin=float.hex(0.25);
max_llm_agents=40; n_llm_agents=6; n_neighbors=2; n_noise_agents=8;
n_rounds=24; news_round=12; news_text=<default breaking-news text>;
population={mode:legacy,counts:null,effective_cast:null}; recent_window=5;
reference_path={configured:false,kind:null,size_bytes:null,sha256:null}; seed=7;
seed_fraction=float.hex(0.34); social_enabled=true; social_mode=network;
social_weight=float.hex(1.0); topology=scale_free
```

Model request（9 fields）：

```text
provider=auto; model=""; cheap_model=""; use_cheap_model=false;
openai_model=MiniMax-M2.7; temperature=float.hex(0.0); max_tokens=1024;
cache_enabled=true;
openai_base_url={configured:true,
  endpoint_identity_sha256:66e21f44b31bae951b37de32684b004d81c0821d956eb3351432770f11aad0c1,
  userinfo_redacted:false}
```

Execution（2 Config fields plus empty runtime context）：

```text
openai_api_key={configured:false,value:<not-configured>};
out_dir={kind:path_identity,
  resolved_path_sha256:61d1d081832e098bca6f1a976f83a0c4a16b822fc20a088e23bda70ac5faddc0};
runtime={}
```

秘密没有进入任何 summary。改变 execution path/base_dir 可以改变 full/execution
hash，但不得改变 scientific/model-request hash；改变科学输入文件字节则必须改变
scientific hash。
