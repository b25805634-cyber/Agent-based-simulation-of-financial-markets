# Phase 1 本地代码基线

## 目的与边界

Phase 0.5 只把已经完成的 Phase 1 封存为一个可比较、可回滚的本地 Git 基线。它不开发新功能，也不改变科学语义。

本次明确没有：

- 开始 Phase 1.1 或 Phase 2；
- 抽取 `ManagedRunContext`；
- 修改 Replay schema/匹配契约；
- 修改解析器、Agent、Prompt、Persona、市场、社交、杠杆或默认参数；
- 添加 remote、访问代码托管服务或执行 push。

## 基线身份

| 项目 | 值 |
|---|---|
| 建立日期 | 2026-07-15（Asia/Shanghai） |
| 仓库根目录 | `/Users/aldrich/Desktop/agent模拟市场` |
| Git 版本 | `2.50.1 (Apple Git-155)` |
| 默认分支 | `main` |
| 稳定标识 | annotated tag `phase1-provenance-v1` |
| 仓库范围 | 仅本机；remote 必须为空 |
| 提交身份 | 仓库级 `Local Phase 1 Baseline <phase1-baseline@localhost>`；未修改全局配置 |

同一个 commit 不能稳定地在自身内容中记录自己的 SHA。基线 commit 的权威解析方式是：

```bash
git rev-parse 'phase1-provenance-v1^{commit}'
```

annotated tag 建立后不得移动、覆盖或复用。

## 封存的 Phase 1 合约

- 受管入口为每次运行创建唯一、不可覆盖的 `<out>/runs/<run_id>/`。
- `run_manifest.json` schema `1.0` 记录配置、Scenario、RNG、Git、Persona/人口、Prompt、输入、环境、执行批次、honest-N、状态和结果 hash。
- 公共 `events.jsonl` 与受限 `private_events.jsonl` 分离；private rationale 不进入社交 feed。
- `RecordingLLM` 保存规范化请求和原始响应；`ReplayLLM` 严格离线匹配 Prompt、Persona、round、batch、调用顺序和模型配置。
- Replay 错配明确失败，不回退到真实 Provider。
- 原有 CLI 和结果字段尽量保留；已有历史结果不覆盖。
- 真实 Provider 的 temperature 0、seed 或 cache 不被宣称为完全确定性保证。

## 纳入 Git 的代码基线

- `nmsim/`、`experiments/` 和 `tests/`；
- `docs/`、`README.md`、`AGENTS.md` 及研究说明文档；
- `nmsim/meta_feb2022_reference.csv` 和 `examples/reference_episode.csv` 等参考输入；
- `run_pipeline.sh` 和现有 Python/CLI 入口；
- `.gitignore`。

## 仅保留本机、不纳入 Git 的材料

`.gitignore` 只排除这些文件，不删除、移动或覆盖它们。Git 代码基线不是历史数据备份。

| 类别 | 审计结果 | 处理 |
|---|---:|---|
| `results*` | 355 个历史结果文件，约 8.2 MiB | 本机保留，忽略 |
| `outputs*` 目录 | 25 个生成文件，约 904 KiB | 本机保留，忽略 |
| `traces/` | 8 个含 private reasoning 的 JSON，约 592 KiB | 本机保留，忽略 |
| `logs/` | 6 个运行日志，约 28 KiB | 本机保留，忽略 |
| 根目录日志/图片 | `*.log`、`outputs_sweep_gain.png` | 本机保留，忽略 |
| 本地工具配置 | `.claude/settings.local.json`、`.DS_Store` | 忽略 |
| 内部访问材料 | `VPN接入 - Higgs Asset wiki.pdf` | 忽略，禁止 stage |
| Phase 1 私有记录 | `llm_records.jsonl`、`private_events.jsonl`、`reasoning_traces.csv` | 防御性忽略 |

历史目录集合的冻结哈希采用以下可重复命令；路径名和文件内容都进入最终 hash：

```bash
find outputs outputs_g05 outputs_g15 outputs_meta \
  results results_2x2 results_2x2_mock results_2x2_v2 results_levcheck \
  results_phase2b results_sweep results_sweep_higgs traces logs \
  -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256
```

Phase 0.5 操作前的结果为：

```text
db00d0d7520273c9aa2f663a1d8cfa0b7b610dab0ed8ea3ca6a25a1f124edbaf
```

旧 `reasoning_traces.csv` 和 `traces/*.json` 当前是本机明文历史证据，部分权限为 `0644`。本阶段不擅自改写历史文件；后续需单独制定权限、加密和保留政策。

## 敏感信息审计

- 未发现 `.env`、PEM/SSH 私钥、数据库凭据文件或高置信真实 API token。
- 源码的 `openai_api_key` 默认值为 `EMPTY`；Phase 1 writer 会对实际 supplied key 脱敏。
- 源码、README 和历史文档包含现有内网 endpoint。这是既有默认/历史事实，本阶段禁止为了清理而修改；任何未来 remote 或发布动作前必须重新审阅。
- `.claude/settings.local.json` 虽未发现 token，仍属于本机权限配置，不进入 Git。

## AGENTS.md 审阅

`AGENTS.md` 与已批准的项目目的、事实来源、科学不变量、工程规则、越界范围和完成标准逐项一致，本阶段未修改。SHA-256：

```text
9272c1928c6a014486a01408dd7f78036b563bb68412e74794f9d453ca00f037
```

## 科学实现冻结 hash

以下值在 Phase 0.5 开始时记录，并在提交前复核：

| 文件 | SHA-256 |
|---|---|
| `nmsim/config.py` | `8306141f75a9b80da3605369d64fedb30b2b4b675bddda74b2b8f9bb9bf930a5` |
| `nmsim/prompts.py` | `db9c26c22d35223ea7ee768d622c608f9ca27b4b81b58615720704c39e906171` |
| `nmsim/agents.py` | `06b32592f6f9d1539412ff8c3f21c35eacaa36b27eaacb941cc3676ec6d68e13` |
| `nmsim/market.py` | `d6c2635961891985dab473bdfa2ba40b9f8ce335dbe6503535ac2813462c5809` |
| `nmsim/contagion.py` | `fb1ee51a94f1f881ef37e2616d3ad74fc103ff65f8957aa811afcf3559e82c5f` |
| `nmsim/leverage.py` | `f1291fdc2f5de787799fb19ab4a59b700022a2d4d0ca922b957ade0ec1454fdf` |
| `nmsim/sim.py` | `665652f13cd92f40e2b8295fc7ff13ae0c05ca0ef46230f58474a41c86c45047` |
| `nmsim/llm.py` | `e59f3a5eba959f5e9181c18ffcb32076a0dcc5a183b45ac91cfa2ccce2ca8fd6` |
| `nmsim/types.py` | `7132a0b365fb9bba22bbc192510c6f0fbd51aaa7a967905224e6a9ccb8ff5b81` |

四轮 Mock 基线（seed 7、news round 2）的旧格式核心输出 hash：

| 文件 | SHA-256 |
|---|---|
| `price_path.csv` | `4ba6a5184dbea79e4e589aa4881fe593f070e51001a0f82fc438858e4b61ba91` |
| `reasoning_traces.csv` | `b2f253a093d5b69656dd22c03d2d6b6f710af999cb3e52bfdfc71146a3e0b65e` |
| `propagation.csv` | `d5d56a20dbccf65f8a803ac0af62b752d84da237b48173426462ae4e19277118` |
| `stylized_facts.json` | `8b4fc0223ac730ca5dc33e0f4915aa8dcfb51b48e53e5627d7d03bf897a8824f` |
| `sim_overview.png` | `a017433cdba78cdb0caba3f2fd161a629f1c3d0e3eb388a965ef1530ec5d85e9` |

`config.json` 含输出目录，因此不作为跨目录逐字节比较对象。

## Phase 0.5 回归记录

基线 commit 前必须重新执行：

1. 16 项 unittest；
2. `compileall`；
3. 跨 `PYTHONHASHSEED` 的 `repro_check`；
4. 四轮 Mock Record/Replay 及五个核心文件逐字节比较；
5. Replay 模型配置错配的预期失败与失败 manifest；
6. 最小 `grid2x2` 四 cell 兼容性运行；
7. 历史目录聚合 hash 和科学实现 hash 复核。

实际命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m unittest discover -s tests -v

PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m compileall -q nmsim experiments tests narrative_market_sim.py

PYTHONHASHSEED=0 PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m experiments.repro_check

MPLCONFIGDIR=/tmp/nmsim-mpl PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m nmsim.run --provider mock --rounds 4 --news-round 2 --seed 7 \
  --out /tmp/nmsim-phase05-baseline-record --run-id record

MPLCONFIGDIR=/tmp/nmsim-mpl PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m nmsim.run --provider mock --rounds 4 --news-round 2 --seed 7 \
  --out /tmp/nmsim-phase05-baseline-replay --run-id replay \
  --replay-from /tmp/nmsim-phase05-baseline-record/runs/record

PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m nmsim.run --provider mock --rounds 4 --news-round 2 --seed 7 \
  --max-tokens 777 --out /tmp/nmsim-phase05-mismatch --run-id mismatch \
  --replay-from /tmp/nmsim-phase05-baseline-record/runs/record

MPLCONFIGDIR=/tmp/nmsim-mpl PYTHONPYCACHEPREFIX=/tmp/nmsim-phase1-pycache \
  python3 -m experiments.grid2x2 --seeds 1 --provider mock --temp 0 \
  --workers 1 --out /tmp/nmsim-phase05-grid
```

结果：

| 验证 | 退出码 | 实际结果 |
|---|---:|---|
| unittest | 0 | 16/16 通过，`Ran 16 tests in 0.379s` |
| `compileall` | 0 | 无错误 |
| `repro_check` | 0 | 三个不同 `PYTHONHASHSEED` 完全一致；25 点；final `83.480000` |
| Mock Record | 0 | final `86.31`；honest-N `24/24` |
| Mock Replay | 0 | honest-N `24/24`；`network_access=false`；Provider calls `0` |
| Record/Replay `cmp` | 0 | 上述五个核心文件全部逐字节一致，hash 与表中基线一致 |
| Replay 模型配置错配 | 1（预期） | 明确 `ReplayMismatchError`；manifest `failed`、honest-N `0`；恰有一个 `RunFailed` |
| 最小 `grid2x2` | 0 | 四个 cell 4/4 完成，failures `0`；每个 managed run honest-N `144` |
| 两个 CLI `--help` | 0 | `nmsim.run` 与 `experiments.run_seed` 兼容入口可用 |
| 历史目录聚合复核 | 0 | 仍为 `db00d0d7520273c9aa2f663a1d8cfa0b7b610dab0ed8ea3ca6a25a1f124edbaf` |
| 科学实现/AGENTS hash 复核 | 0 | 与本文件记录值逐项一致 |

commit 后还必须在干净工作树、仓库外输出目录执行一次 Mock run，并确认：manifest 的 `git.commit` 等于 `git rev-parse HEAD`、`git.dirty=false`、运行状态为 `finished`。由于该检查发生在 commit 之后，其实际 SHA 和临时 manifest 路径在 Phase 0.5 完成报告中给出。

## 已知边界

- 本地 Git 不是异机备份；`.git` 丢失会同时丢失 commit 和 tag。
- 初始化前的历史结果没有 commit 身份，只能通过现有文件及上述 inventory/hash 保持连续性。
- 被忽略的 raw/private 文件不受 Git 版本保护。
- 尚未以真实凭据完成真实 Provider 网络采样验证。
- 低层直接 `run_sim` 的诊断/测试调用仍可选择非受管模式。

本地检查、比较和安全回滚方法见 [LOCAL_GIT_WORKFLOW.md](LOCAL_GIT_WORKFLOW.md)。
