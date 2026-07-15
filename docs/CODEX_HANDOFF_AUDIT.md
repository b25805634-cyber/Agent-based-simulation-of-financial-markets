# Codex 接管审计

审计日期：2026-07-15（Asia/Shanghai）<br>
审计工作区：`/Users/aldrich/Desktop/agent模拟市场`

> 状态说明：本文冻结 Phase 1 实施前的接管基线。其 provenance/raw response/失败删除等缺口已在随后 Phase 1 中部分关闭；当前接口与剩余边界以 [RUN_PROVENANCE.md](RUN_PROVENANCE.md) 为准。市场、Prompt、Persona、社交和杠杆审计结论未因 instrumentation 改变。

## 当前项目一句话定义

这是一个以异质 persona LLM/Mock 交易者、上一轮公开观点传播和净订单流压力定价为核心的研究型 Agent-Based 市场沙盒；它不是价格预测器，也不是限价订单簿或真实交易系统。

## 审计边界与证据等级

本审计按“当前源码与本次运行 > 当前结果文件与日志 > README/PROJECT/REPORT/BUILD_BRIEF”的顺序判断事实。

- 用户指定的 `docs/handovers/导师汇报_综合版_2026-07-15.html` 不在当前工作区；全仓库及父目录的有限深度搜索均未找到 HTML/handover 文件。因此无法逐字审计该文件，本文以任务描述中的报告声明和仓库现有四份叙事文档为报告侧证据。
- 当前目录没有 `.git/`，`git status` 和 `git log` 均失败。因此无法判断未提交修改，无法恢复 commit 历史，也无法把任何结果绑定到 Git SHA。
- 当前目录原先没有 `docs/` 和 `AGENTS.md`。本次只新增四份 `docs/` 文档，没有创建根目录 `AGENTS.md`，也没有修改核心仿真或历史结果。
- “数值可复算”只表示现有 JSON 经现有 analyzer 得到同一数字，不表示模型、prompt、代码版本或因果识别已被验证。

## 结论先行

当前真正可运行的是：无凭据 Mock 仿真；Anthropic/OpenAI-compatible 调用路径；上一轮公开 `sentiment + public_take` 的 feed/network 传播；压力定价；独立的冻结参照杠杆账本和下一轮强平卖压；CSV/JSON/PNG 输出；多组实验 driver/analyzer。Mock 在固定配置下可跨进程复现。

当前不能声称的是：真实 LLM 完全确定；`social_weight` 在真实路径上是连续剂量；`limit_price` 被执行；现金、持仓和市场库存守恒；`volume` 等于全部 fills；原始模型响应完整保存；历史结果能追溯到 commit、完整 config、prompt、模型服务版本和批次并发；已有单事件结果已经建立外部有效性。

## 当前能够真实运行的功能

| 能力 | 当前事实 | 本次验证 |
|---|---|---|
| MockLLM 仿真 | 可从仓库根目录直接运行；使用显式本地 RNG | 两次 4-round 运行均退出 0；除 `out_dir` 外全部产物逐字节一致 |
| Mock 跨进程复现 | `experiments/repro_check.py` 比较三个 `PYTHONHASHSEED` | PASS，25 个价格点，终价 83.480000 |
| Anthropic 路径 | SDK 已安装；无 key 时构造 client 仍可成功，真正请求才会降级为 `api-error` order | 只检查了配置/构造；没有凭据，未声称调用成功 |
| OpenAI-compatible 路径 | async SDK 调用、每 agent 最多 3 次尝试、失败转 hold | 对本地不可达端点做 1-agent dry-run；所有模型调用失败但 CLI 仍退出 0 |
| Persona | 六类 persona 自然语言 prompt；可按 persona 复制人群 | 源码核实 |
| 社交传播 | 上一轮公开 `sentiment + public_take`；feed 或 network；有限 attention digest | 源码核实及 Mock 运行 |
| 压力市场 | `last * (1 + kappa * net / total_order_qty)`，最低 0.01 | 源码核实及直接函数调用 |
| 杠杆 V2 | 冻结参照多头、纯价格保证金、一次性强平、卖压进下一轮 | 8-round Mock demo 退出 0，出现 1 次/50 股强平 |
| 验证指标 | log-return、总体矩 excess kurtosis、tail ratio、ACF、reaction、RMSE、DTW | 源码核实；主 CLI 和历史 analyzer 可运行 |
| 实验 | seed runner、2x2、population sweep、ablation、leverage、Phase2b、analyzers | 最小 1-seed 2x2 Mock 共 4 run 全部退出 0 |

## 实际仓库结构

```text
.
├── README.md / BUILD_BRIEF.md / REPORT.md / PROJECT.md
├── narrative_market_sim.py          # 旧 Phase-1 单文件副本；不是当前主入口
├── nmsim/
│   ├── config.py                    # Config dataclass
│   ├── types.py                     # Order、Statement；没有 Decision 类型
│   ├── prompts.py                   # 六类 persona 与 real prompt
│   ├── agents.py                    # Agent/NoiseAgent、Mock 参数、parse 路由
│   ├── llm.py                       # Mock/Anthropic/OpenAI、parse、retry、内存 cache
│   ├── contagion.py                 # topology、digest、seed、传播指标
│   ├── market.py                    # 压力定价与 fills
│   ├── leverage.py                  # 冻结参照杠杆及强平事件
│   ├── sim.py                       # 回合主循环
│   ├── validation.py                # stylized facts、RMSE、DTW
│   ├── run.py                       # 当前公开 CLI 与六类输出
│   └── meta_feb2022_reference.csv
├── experiments/                     # 23 个 driver/analyzer 模块
├── examples/reference_episode.csv
├── outputs*/                        # 单次 CLI 历史输出，schema 年代不完全一致
├── results/                         # 早期 gain runs
├── results_2x2/                     # 60 个旧 run + 旧汇总
├── results_2x2_mock/                # 60 个 Mock run + 汇总
├── results_2x2_v2/                  # 不完整：16 个 run
├── results_sweep/                   # 134 JSON，混合 calibration/sweep/ablation/leverage
├── results_sweep_higgs/             # 16 JSON
├── results_phase2b/                 # 32 JSON
├── results_levcheck/                # 3 JSON
├── traces/                          # 8 个解析后、已截断的 trace JSON
└── logs/                            # driver 日志与夜间总结
```

`experiments/` 的实际职责：

- 单 run 与批量：`run_seed.py`、`drive.py`、`grid2x2.py`、`sweep.py`、`ablate.py`、`lev2x2.py`、`phase2b.py`、`critsweep.py`。
- 汇总与统计：`aggregate_seeds.py`、`aggregate_grid.py`、`aggregate_sweep.py`、`additive_test.py`、`ablation.py`、`lev_analyze.py`、`critsweep_analyze.py`。
- 机制/诊断：`capture_traces.py`、`analyze_traces.py`、`aggregate_mechanism.py`、`flow_decomp.py`、`calib_n.py`、`leverage_demo.py`、`bench_concurrency.py`。
- `run_pipeline.sh` 是 composition sweep 的硬编码 shell pipeline；它会写现有 `results_sweep/` 和日志，不适合当 smoke test。

## 实际入口和命令

### 主 CLI

```bash
python3 -m nmsim.run --help
python3 -m nmsim.run --provider mock
python3 -m nmsim.run --provider mock --rounds 4 --news-round 2 --seed 7 --out /tmp/nmsim-smoke
python3 -m nmsim.run --provider openai --base-url http://HOST:8000/v1 --model MODEL
```

主 CLI 暴露 provider/model/base URL/API key/max tokens、rounds/news round、LLM 数量、social topology/mode/weight、seed fraction、seed、reference、out、cache。它没有暴露 population、noise 数量、news text、fundamental、kappa、digest、temperature、broadcast ablation 或 leverage；这些只能从 Python `Config` 或实验脚本设置。

### 单实验和批量实验

```bash
python3 -m experiments.run_seed --seed 1 --provider mock --out /tmp/run-seed
python3 -m experiments.grid2x2 --seeds 1 --provider mock --temp 0 --workers 1 --out /tmp/grid
PYTHONHASHSEED=0 python3 -m experiments.repro_check
python3 -m experiments.leverage_demo --seed 7 --total 6 --rounds 8 --news-round 2
```

真实 OpenAI 实验 driver 还会先探测写死的 `10.214.32.152:8000`，即使环境里的 `OPENAI_BASE_URL` 指向其他服务；这会使自定义 endpoint 实验被错误阻塞。

### 当前可用的验证方式

仓库没有声明标准测试、lint 或 type-check 命令。当前能运行的是：

```bash
PYTHONPYCACHEPREFIX=/tmp/nmsim-pycache python3 -m compileall -q nmsim experiments narrative_market_sim.py
python3 -m unittest discover -v       # 当前是 0 tests
PYTHONHASHSEED=0 python3 -m experiments.repro_check
```

当前环境没有 `pytest`、`ruff`、`mypy` 或 `pyright`；也没有依赖清单或构建配置。

## 一轮仿真的已验证事实

完整编号流程、字段和 Mermaid 图见 [ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md)。关键事实如下：

1. 唯一 Scenario 实体是 `Config`；没有独立 `Scenario` 类型。
2. 每轮 real Agent 看到：当前/近期价格、价格趋势、是否属于直接新闻 seed、上一轮可见邻居的公开观点、自己的现金/持仓和最近三条私有 memory；看不到数值 `fundamental_value`、数值 `social_weight`、订单流、他人订单或杠杆状态。
3. seed agent 在 `r >= news_round` 的每一轮都重复看到同一条“fresh news”，而不是只在注入轮看到一次。
4. standard real schema 下只有 `public_take + sentiment` 传播，`reasoning` 不传播；但 legacy/malformed 响应若只给 `rationale`，parser 会同时把它填入 `public_take` 和私有 `rationale`，存在隐私边界退化路径。
5. 原始 response 不落盘；`public_take` 被截到 140 字符、社交 text 再截到 90，`reasoning/rationale` 被截到 240。因此“完整 reasoning 永不丢失”不成立。
6. 每轮全部 LLM agent 形成一个 batch；`asyncio.gather` 保持返回顺序，但任务同时创建且无应用层 semaphore。OpenAI 连接池上限 40；Anthropic 没有项目级并发上限。
7. 所有 buy/sell quantity 都参与压力价格并全额记为 fill；`limit_price` 不执行，现金/库存不约束订单。
8. 强平在新价格后检查并立即把参照账本标记为 liquidated，但卖压下一轮才进入市场；最后一轮触发的强平会记录事件却没有后续价格冲击。
9. 主 CLI 写价格、传播、解析后 trace、指标、config 和图；不写 raw response、fills、最终现金/持仓、做市商账本、强平事件、resolved provider/model、batch/concurrency、重试或代码/prompt 版本。

## Config 参数实效审计

“读取”不等于“按文档语义生效”。下表覆盖当前 `Config` 的全部字段。

| 参数 | 当前真实路由 | 判定 |
|---|---|---|
| `seed` | network、seed subset、digest RNG、NoiseAgent、MockLLM | Mock/local RNG 有效；不发送给真实 provider |
| `n_rounds` | `sim.run_sim` 循环 | 有效 |
| `news_round` | news gate、seed sign、margin gate、指标对齐 | 有效；只有主 CLI 会把 `>n_rounds` clamp，实验直接调 `run_sim` 不会 |
| `news_text` | seed agents 的 prompt | 有效；从 news round 起每轮重复 |
| `initial_price` | 初价、杠杆债务/阈值 | 有效 |
| `fundamental_value` | Mock prompt 的 `FUNDAMENTAL` | **Mock-only**；real prompt 完全不含该数值 |
| `recent_window` | price history slice | 有效 |
| `kappa` | 压力价格公式 | 有效 |
| `n_llm_agents` | 无 `population` 时截取前 N 个 persona，最多 6 个 | 有效但大于 6 不增加 agent |
| `n_noise_agents` | NoiseAgent 数量 | 有效 |
| `max_llm_agents` | cast 硬上限 | 有效；population 按 dict 插入顺序截断 |
| `population` | persona copy 数量 | 有效；influencer 被静默 cap 为 1 |
| `provider` | build factory；未知字符串静默走 Mock | 有效但 typo 有静默换模型风险 |
| `model` | Anthropic/OpenAI requested model | 条件有效；主结果不记录 resolved value |
| `cheap_model`, `use_cheap_model` | Anthropic 选模 | Anthropic-only；对 OpenAI/Mock 无效 |
| `openai_base_url`, `openai_api_key`, `openai_model` | OpenAI client | OpenAI-only；API key 会原样写进主 CLI `config.json` |
| `temperature` | OpenAI 请求；部分 Anthropic model；Mock 不读取 | provider-dependent；部分 Anthropic 前缀明确忽略；不是确定性保证 |
| `max_tokens` | 真实 provider request | real-only |
| `cache_enabled` | 单次进程内 `CachingLLM` | 有效；不持久化，不跨 run |
| `social_enabled` | digest/adjacency/有效 coupling 开关 | 有效；关闭后 seed 仍收新闻、非 seed 仍看价格 |
| `social_mode` | `feed` 或 `network` 可见集合 | 有效 |
| `topology`, `n_neighbors` | 仅 network 模式建图 | 条件有效；feed 模式不生效 |
| `social_weight` | `clip(gain * persona_social, 0, 1)`；Mock prompt 包含数值，real prompt 只按 `>0` 决定是否给 feed | **Mock 为截断后的连续量；real 实际只有阈值/开关作用** |
| `broadcast_mode` | digest pool 过滤及 influencer mic note | 有效；只有实验脚本暴露 |
| `demote_influencer` | 取消 hub 和 auto-seed | 有效；influencer 仍可能被随机选为 seed |
| `leverage_enabled` | 初始化和 margin call | 有效；主 CLI 不暴露 |
| `leverage_ratio`, `leverage_spread` | staggered L 和债务 | 有效；没有合法区间校验 |
| `maintenance_margin` | breach check/threshold | 有效；没有合法区间校验 |
| `leverage_fraction` | fuel-first 选择 cohort | 有效；round 后可能为 0 或超过预期，未校验 |
| `digest_size` | attention slots | 有效；`<=0` 变为空 feed |
| `seed_fraction` | `round(fraction*N)`，至少 1，再合并 forced influencer | 有效；forced seed 可使实际比例高于目标 |
| `reference_path` | 只在 `run.run` 写 reference comparison | 有效；`run_sim` 不使用；`run_seed` 另写死 Meta path |
| `out_dir` | 主 CLI 输出目录 | 有效；实验 `run_seed` 使用自己的 `--out` 写单 JSON |

动态核查结果：instrumented-real 路径中 `social_weight=0.1` 与 `1.0` 的全部 prompt 完全相同；`fundamental_value=100` 与 `999` 也完全相同。真实 prompt 不含这两个数值。

## Persona 数值刻度审计

| 刻度 | real LLM | MockLLM | 当前结论 |
|---|---|---|---|
| `narrative_susceptibility` | 不进入 prompt 数值；人格文字另行描述 | 不读取，Mock 使用独立 `news_value/w_senti` | 文档元数据 |
| `social_susceptibility` | 只与全局 gain 相乘后决定 feed 是否出现；正数大小不呈现给模型 | 进入 `SOCIAL_WEIGHT`，再与独立 Mock `w_social` 共同作用 | 唯一有机械路由的 persona 刻度，但两路径语义不同 |
| `fundamental_anchor` | 不进入 prompt 数值；人格文字描述 | 不读取，Mock 使用独立 `w_value` | 文档元数据 |
| `emotional_reactivity` | 不进入 prompt 数值；人格文字描述 | 不读取，Mock 使用独立 `noise/w_senti/...` | 文档元数据 |

因此 persona 自然语言是 real 行为来源；四个数值不是 real policy 参数。Mock 是另一套手写行为模型，不能当作 real persona 的数值等价物。

## 市场、账本和成交定义

- `limit_price` 只记录，不参与成交资格或价格。
- 没有现金约束、持仓约束、保证金约束或 quantity 上限。历史 `m0.7_real_off_s16.json` 从 compact orders 可重建出 influencer 最终约 `-4000` 股（初始 50），证明裸卖空不仅理论允许，历史结果中实际发生。
- 所有有效 orders 都全额 fill。例：买 10、卖 3 时函数返回 `volume=3`，fills 却是 `{B:+10, S:-3}`。
- 注释中的 market maker 没有实体、现金、库存、风险或 P&L；7 股净买盘由无账本的隐式对手方吸收。
- CSV `volume=min(total_buy,total_sell)` 是 matched cross volume；Agent 账本却把净失衡部分也作为成交。因此 `volume` 与 fills 的定义不一致。
- NoiseAgent 也更新 cash/shares，但其订单、fills 和最终状态不写主结果。
- 强平 phantom seller 有价格冲击但不进入普通 fill loop；参照杠杆 P&L 单独结算，不与 Agent voluntary cash/shares 合并。

## LLM、解析、缓存和并发

### 解析/重试/降级

- parser 用贪婪 `\{.*\}` 抽取 JSON；非 JSON 才算 parse failure 并触发 real async 重试。
- 合法 JSON 中字段错误会逐字段 coercion，不触发 retry；非法 side 变 hold，负 quantity 变 0，sentiment clamp 到 `[-1,1]`。
- Anthropic/OpenAI 每个 prompt 最多 3 次尝试；异常最后变成 hold。主 CLI 不设 health gate，所有调用失败仍可退出 0 并生成看似完整的市场结果。
- 本次不可达 OpenAI dry-run 的唯一 Agent trace 是 `hold, public_take="api-error; holding"`；价格仍由 noise agents 移动，CLI 退出 0。
- 未知 provider 字符串直接走 Mock，没有 warning。

### Cache

- key 包含完整 `system + NUL + user` 的 SHA-1 摘要，因而包含当轮实际 prompt。
- key **不包含** provider、resolved model、temperature、max_tokens、agent identity 或其他未体现在 prompt 的配置。
- cache 仅在 wrapper 内存中存在，正常 CLI 每 run 新建，所以不会自然跨 run/model 复用；若长生命周期中变更 inner model/config，或相同 prompt 的不同 agent/采样配置共享 wrapper，则有错误相关/复用风险。
- 同一 batch 内的重复 miss 不去重，会并发请求多次；之后同 key 最后一次 response 覆盖 cache。
- 主 CLI 没有 temperature 参数；实验脚本仅在 `temp>0` 时主动关 cache。程序化 `Config(temperature>0, cache_enabled=True)` 仍会缓存随机样本。

### 顺序/批次/并发记录

- agent 列表顺序构成 batch，`asyncio.gather` 保持 result 与输入索引一致，`zip(order_owners, completions)` 保持路由。
- 每轮创建所有 cache miss 的 coroutine，没有 semaphore；OpenAI httpx pool cap 40，Anthropic 依赖 SDK 默认。
- 实验 driver 的 `workers` 会把多个 run 的 per-round burst 相乘。
- `workers`、实际 batch size、cache miss 数、并发上限、retry 次数和响应顺序没有进入 run JSON。Cost tracker 只统计成功拿到 response 的调用；API 异常不计数，Mock 调用也显示 0。

## 随机性和可复现边界

本地确定性来源：

- `random.Random(seed)`：MockLLM，共享且按调用顺序消费。
- `random.Random(seed+1)`：digest shuffle，跨 agent 共享，顺序耦合。
- `random.Random(seed)`：network topology。
- `random.Random(seed+999)`：news seed subset。
- `random.Random(integer formula)`：每个 NoiseAgent/round。
- leverage 初始化和 margin check：无 RNG。

已验证固定 Mock config 在不同 `PYTHONHASHSEED` 下价格相同。未覆盖的边界：修改 agent 数/顺序、cache hit、digest 分支会改变共享 RNG 消费序列；真实 provider 没发送 seed；temperature 0、服务端 greedy decoding、cache 都不能保证首次真实响应完全确定。Phase2b 现有结果本身已经记录了 temp=0 在该 endpoint 上仍分叉。

## 历史结果状态重建

### 可由当前 analyzer 复算的数值

- 旧 `results_2x2/`：四格各 N=15；real_on mean drop `-0.265`，real_off `-0.446`，差 `+0.1805`，独立样本 pooled Cohen's d `1.716`。但该目录是修复前数据：placebo recovery 大于 1、placebo cascade 非零；多数 JSON 无 model 和 orders。
- `results_sweep/` population pairs：m=0.3 N=8，diff `+0.054 [0.020,0.088]`；m=0.5 N=8，`+0.060 [-0.007,0.127]`；m=0.7 N=16，`+0.113 [0.047,0.179]`。总 32 对的 regression 可复算为 `drop_on ~ drop_off` slope `0.397 [0.170,0.624]`、intercept `-0.050 [-0.108,0.008]`。
- ablation：off/on 各 16，muted/solo/demoted 各 12；现有 analyzer 可复算报告表中的全部均值和 CI。
- Higgs：m=0.5 N=8，paired diff `+0.030 [-0.079,0.140]`，fuel sentiment/drop correlation `+0.780`。
- leverage 2x2：leverage-off 各 16、leverage-on 各 12；当前 analyzer 的 naive leverage contrasts CI 跨 0。Phase2b 四格各 N=8，temp=0 仍非确定，contrast CI 跨 0。

### 结果文件实际数量和混合状态

- `results_2x2/`：60 run JSON + `grid_summary.json`；`failures.log` 0 行。
- `results_2x2_v2/`：16 run，实际是 real_on 15 + real_off 1，不是完整 2x2；没有 failures.log。
- `results_sweep/`：134 JSON，其中 population base 64、calibration repeats 10、三种 ablation 36、leverage 24；`failures.log` 有 32 条历史 VPN 放弃记录，但同名 run 后来多数已经存在，日志是 append-only 尝试历史，不是当前缺口清单。
- `results_sweep_higgs/`：16 run，failure 0。
- `results_phase2b/`：32 run，failure 0。
- 没有 `results_critsweep/`，与 PROJECT 中“关卡异常后未运行临界点扫描”一致。

### 统计和样本审计

- `aggregate_grid.py` 不按 health、model 或共同 seed 过滤；2x2 的 d 是独立样本 pooled d，不是 paired effect size，且没有 paired CI。
- `aggregate_sweep.py` 正确按同 seed 配对并过滤 `bad_frac>0.15`，但 t 临界值只列到 df=12；N=16 时回退 1.96 而不是约 2.131，CI 约窄 8%。
- `additive_test.py` 是简单 OLS，不控制 m-level、异方差、measurement error 或 endpoint 时间漂移；脚本自己只提示 diff 与 `|off|` 的机械耦合。
- 同 seed 只控制本地 graph/noise RNG；真实 LLM 未使用 seed，条件间没有 common random numbers。temp=0.3 和已证实不稳定的 temp=0 都不能把 paired difference 解释为“只差干预”。
- health gate 以最终 normalized rationale marker 计数；超过 15% 的 JSON 会被多个 driver 删除后重跑。被删除 run 和 raw responses 不保留，形成不可审计的失败选择。
- stylized facts 通常只有 24 个 returns，没有置信区间；单事件 Meta 校准不能支持跨事件外部有效性。
- cascade 在 social-off 条件仍可因价格通道对齐而非零；它测“非 seed 与 seed sign 对齐”，不是纯社会传播中介。

## 实际执行命令和结果

| 命令 | 退出码 | 结果摘要 |
|---|---:|---|
| `git status --short --branch` | 128 | `not a git repository` |
| `git log -8 ...` | 128 | `not a git repository` |
| `find ... '*导师汇报*'/'*handover*'/'*.html'` | 0 | 无匹配 |
| `python3 -m nmsim.run --help` | 0 | CLI 可解析 |
| `python3 -m pytest` | 1 | `No module named pytest` |
| `python3 -m unittest discover -v` | 0 | Ran 0 tests |
| `python3 -m compileall -q ...` | 1 | 系统 Python 尝试写工作区外缓存，PermissionError |
| `PYTHONPYCACHEPREFIX=/tmp/... python3 -m compileall -q ...` | 0 | 全部源码通过语法编译 |
| 两次 `python3 -m nmsim.run --provider mock --rounds 4 ...` | 0/0 | 两次终价 86.31；非 config 路径字段的产物 SHA-256 全相同 |
| 五类产物 `diff` + PNG `diff -q` | 0 | 全同；两个 config 仅 `out_dir` 不同 |
| `PYTHONHASHSEED=0 python3 -m experiments.repro_check` | 0 | PASS，跨 3 进程相同 |
| `python3 -m experiments.grid2x2 --seeds 1 --provider mock ...` | 0 | 4/4 runs，failure 0 |
| 不可达 endpoint 的 1-agent OpenAI dry-run | 0 | Provider 未成功；trace 为 `api-error; holding`，CLI 仍成功退出 |
| `python3 narrative_market_sim.py` | 1 | 旧脚本写死 `/mnt/user-data/outputs`，本机 PermissionError |
| `python3 -m experiments.leverage_demo ...` | 0 | 1 次强平，ON drop -19.5%，OFF -16.3% |
| `aggregate_grid/aggregate_sweep/ablation/additive_test/lev_analyze/flow_decomp` | 0 | 当前 analyzer 均可运行，报告主要数值可复算 |

环境：Python 3.9.6；matplotlib 3.9.4；numpy 2.0.2；anthropic 0.105.2；openai 2.40.0；httpx 0.28.1。版本来自未声明、未锁定的本机环境。

## 已知失败与未验证声明

### 已知失败

1. 指定 handover HTML 缺失。
2. Git 元数据缺失，无法检查 dirty tree/历史/commit provenance。
3. 无测试和项目环境声明；pytest/lint/type checker 不可用。
4. 旧 `narrative_market_sim.py` 在本机不能运行，且其 persona、news 语义和 RNG 都已与 package 分叉。
5. 无真实 Provider 凭据；OpenAI-compatible 内网 endpoint 未连接。本次只验证了构造、调用、重试和失败降级路径。
6. 主 CLI 在 100% provider 失败时仍退出 0；这不是成功的真实模型 run。

### 无法验证

- 任何历史结果对应的 Git commit、工作树状态、完整依赖、完整 prompt SHA、服务端镜像/权重版本、request ID、batch composition 和原始 response。
- `results_2x2/`、早期 `results_sweep/` 没有 model 字段时“实际一定是 MiniMax-M2.7”的声明。
- Claude/Fable 默认 model 名、价格表和 sampling 限制是否与真实外部服务一致。
- 报告指定 HTML 中超出任务描述和现有 REPORT/PROJECT 的任何表格、图注或代码片段。
- temp=0 的真实 endpoint 完全确定性；现有证据反而否定该前提。

## 最大五个工程风险

1. **无版本与环境基线**：没有 Git、依赖锁、测试或 CI，任何结果都不能可靠重建代码状态。
2. **Provider 软失败伪装成功**：全 API 失败会转 hold，主 CLI 退出 0；未知 provider 又静默换 Mock。
3. **运行溯源和原始证据不足**：不保存 raw response/request/retry/prompt hash/resolved model/concurrency/final portfolio；解析还截断文本。
4. **账本语义不守恒**：无资金/持仓约束、无显式做市商、fills 与 volume 不一致、无限仓位和裸卖空实际发生。
5. **重复和硬编码运行路径**：旧单文件分叉；driver 写死 endpoint；失败 run 被删除；主 CLI/experiment schema 分裂。

## 最大五个研究效度风险

1. **被解释为剂量的参数实际不是剂量**：real `social_weight>0` prompt 相同，早期 gain sweep 差异只能来自未控采样/服务变化。
2. **真实 LLM 条件间随机性未配对**：同 seed 不控制模型采样；temp=0 也已观察到分叉，削弱现有 paired causal language。
3. **成交与流动性机制不闭合**：无约束全 fill 和无账本 market maker 会改变可实现订单流、财富和冲击解释。
4. **指标与识别混淆**：cascade 同时受价格通道影响；health 删除重试造成选择；单事件、短路径和简化 CI 支持不了强外推。
5. **结果谱系断裂**：旧/新修复数据混存，缺 commit/prompt/model/raw response；数值复算不能证明实验条件一致。

## 技术债

- 建立 Git origin/commit 基线、依赖声明、Python 支持版本和最小 CI。
- 给 Config 做合法区间与互斥校验；明确每个字段的 provider/CLI 适用范围。
- 消除 unknown-provider 静默 Mock；run manifest 必须写 resolved provider/model 和 degraded/failed 状态。
- 保存不可变 raw event/response，派生 CSV/JSON 可重建；不再删除失败 run。
- 统一主 CLI 与 experiment 的 provenance/schema；标注 schema version。
- 为当前科学语义写 characterization tests，再讨论任何解耦。
- 把 legacy 单文件明确标为 archived，禁止作为入口；不要直接删除历史文件。
- 修正统计器的 paired sample、health/model/common-seed 过滤和 t critical；保留旧 analyzer 输出作为历史版本。
- 明确 `volume`、fill、inventory、market maker 和 leverage settlement 的定义；这是未来设计决策，不在本次修改。

## 推荐的第一个最小、安全、高价值 PR

在先恢复 Git 仓库/remote 后，做一个 **Phase 0 characterization + provenance sidecar PR**，不改价格、prompt、persona、订单或统计公式：

1. 新增 stdlib `unittest` characterization tests，固定当前 Mock price path、公开/私有字段路由、social real-path 阈值语义、压力 market 的现有 fill/volume 行为、下一轮强平时序和跨 hashseed reproducibility。
2. 新增 versioned `run_manifest.json` sidecar：resolved provider/model、完整无 secret config、source/prompt hashes、Python/依赖版本、开始/结束时间、seed、实际 agent/batch 数、cache/retry/health、run status；不改变现有六个文件内容。
3. Provider 全失败暂不改变退出语义，只在 manifest 明确 `degraded=true` 并加测试；fail-closed 可作为单独兼容 PR 评审。
4. 历史结果目录只读，PR 前后生成 SHA-256 manifest 比较；所有 smoke 输出写 `/tmp`。

完成标准：现有 Mock 轨迹与五个既有派生产物（排除新增 sidecar 和路径字段）逐字节不变；`repro_check` 继续 PASS；所有 characterization tests 通过；核心 scientific constants/prompt hashes 无意外变化；历史结果 hash 不变。

## AGENTS.md 建议草案

根目录当前没有 `AGENTS.md`。以下仅为待审草案，未写入根目录：

```markdown
# AGENTS.md

## Scope and evidence
- Treat source code, tests, immutable run records, and actual command results as authoritative over reports.
- Before work, record Git status/commit and do not overwrite a dirty user worktree.
- Never claim a historical result is reproduced unless code, config, prompt, model, raw response, and sample selection are traceable.

## Scientific compatibility
- Do not silently change defaults, personas, prompts, market formulas, metrics, RNG order, CLI flags, or result schemas.
- Add characterization tests before refactoring. Any intended semantic change needs an explicit migration, old/new comparison, and rollback.
- Do not describe temperature=0, a local seed, or caching as full determinism for a real LLM.
- Any causal claim needs a control/ablation and an explicit identification argument.

## Privacy and records
- Only public_take and sentiment may enter another agent's social input. Private reasoning/rationale must never be broadcast.
- Preserve raw responses, failed attempts, historical results, and failure records. Never delete or overwrite them during retries.
- Keep API keys out of configs, manifests, logs, and committed outputs.

## Execution
- Use the repository's declared environment and commands; do not add dependencies without necessity, alternatives, and impact analysis.
- Write smoke/temporary outputs outside historical result directories, preferably under /tmp.
- Record exact commands, exit codes, resolved provider/model, concurrency, retries, and degraded runs.

## Research guardrails
- This is an agent-based market-emergence sandbox, not a forecaster or real trading system.
- Do not add RL, broker connectivity, real-money trading, a full LOB, or a large orchestration framework without a separately approved design.
- Maintain the public CLI and result format unless a backward-compatible migration is specified.
```
