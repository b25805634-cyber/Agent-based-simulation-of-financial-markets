# 历史结果完整性与哈希口径

## 目的与结论

本文件说明项目交付记录中两个历史 SHA-256 的真实计算口径。核查过程只读取既有文件，没有修改、移动或规范化任何历史结果。

两个值都可以由本机保留的原始执行记录恢复，并在 2026-07-15 使用当前历史目录原样复算：

- `14be0c81d31ef01476e3ba16f86e513d0b62436ca2c4c96874b7ac0c2f9b740d`
- `db00d0d7520273c9aa2f663a1d8cfa0b7b610dab0ed8ea3ca6a25a1f124edbaf`

两者使用相同的双层 SHA-256 shell pipeline，但输入集合不同：`db00...` 比 `14be...` 多包含 `logs/` 下 6 个普通文件。因此它们不是同一 scope 在两个时点的可直接比较值，也不能用其中一个覆盖另一个。

## 聚合算法

两个命令都从项目根目录以相对路径执行，算法依次为：

1. `find ... -type f -print0` 递归枚举显式列出的目录中的普通文件；
2. `sort -z` 对 NUL 分隔的相对路径排序；
3. `shasum -a 256` 计算每个文件的内容 SHA-256，并输出“文件 hash、相对路径、换行”；
4. 最后一个 `shasum -a 256` 对步骤 3 的完整文本流再次计算 SHA-256。

因此，最终值同时取决于文件内容、相对路径、文件集合和排序。文件 mtime、权限、owner 与目录 metadata 不进入 hash。命令未使用 `find -L`，且只选择 `-type f`，所以 symlink 本身及其目标均不纳入；核查时两个 scope 内的 symlink 数均为 0。空目录和其他非普通文件也不纳入。

`sort` 的 locale 没有在历史命令中显式固定。当前复算环境为 `LC_ALL=C.UTF-8`，`shasum --version` 为 `6.02`。这足以复现本机值，但不能把该临时 pipeline 宣称为跨操作系统的规范格式。未来若建立正式 integrity 工具，应固定 locale、格式及工具版本，或使用带长度前缀的规范化记录；不得静默改变这里记录的历史口径。

## Hash 1：Phase 1 legacy collection

| 字段 | 值 |
|---|---|
| `hash_id` | `phase1_legacy_historical_collection` |
| `SHA-256` | `14be0c81d31ef01476e3ba16f86e513d0b62436ca2c4c96874b7ac0c2f9b740d` |
| `scope` | 显式列出的 4 个 `outputs` 目录、8 个 `results` 目录和 `traces/` 中的全部普通文件 |
| `included_paths` | `outputs/`, `outputs_g05/`, `outputs_g15/`, `outputs_meta/`, `results/`, `results_2x2/`, `results_2x2_mock/`, `results_2x2_v2/`, `results_sweep/`, `results_sweep_higgs/`, `results_phase2b/`, `results_levcheck/`, `traces/` |
| `excluded_paths` | `logs/`；根目录 `outputs_sweep_gain.png`；`/tmp` 中的 Phase 1 示例运行；源码、文档及所有其他未显式列出的路径；symlink 和非普通文件 |
| `file_count` | `388` |
| `ordering` | NUL 分隔相对路径经 `sort -z` 全局排序；历史命令继承 locale，当前复算为 `C.UTF-8` |
| `calculation_command` | 见下方精确命令 |
| `calculation_script_version` | 无已提交脚本；2026-07-15 本地执行记录恢复的 unversioned ad-hoc shell pipeline；`shasum 6.02` |
| `status` | `scope_recovered_and_reproduced` |

精确命令：

```bash
find outputs outputs_g05 outputs_g15 outputs_meta \
  results results_2x2 results_2x2_mock results_2x2_v2 \
  results_sweep results_sweep_higgs results_phase2b results_levcheck traces \
  -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256
```

原始证据来自 2026-07-15T02:31:43Z 的本地 Phase 1 Codex 执行记录；该记录同时保存了精确命令与返回值。当前复算仍得到同一 SHA-256。

## Hash 2：Phase 0.5 directory collection

| 字段 | 值 |
|---|---|
| `hash_id` | `phase05_historical_directory_collection` |
| `SHA-256` | `db00d0d7520273c9aa2f663a1d8cfa0b7b610dab0ed8ea3ca6a25a1f124edbaf` |
| `scope` | Hash 1 的全部普通文件，加上 `logs/` 中的全部普通文件 |
| `included_paths` | `outputs/`, `outputs_g05/`, `outputs_g15/`, `outputs_meta/`, `results/`, `results_2x2/`, `results_2x2_mock/`, `results_2x2_v2/`, `results_levcheck/`, `results_phase2b/`, `results_sweep/`, `results_sweep_higgs/`, `traces/`, `logs/` |
| `excluded_paths` | 根目录 `outputs_sweep_gain.png`；`/tmp` 中的 Phase 1 示例运行；源码、文档及所有其他未显式列出的路径；symlink 和非普通文件 |
| `file_count` | `394` |
| `ordering` | NUL 分隔相对路径经 `sort -z` 全局排序；历史命令继承 locale，当前复算为 `C.UTF-8` |
| `calculation_command` | 见下方精确命令 |
| `calculation_script_version` | 无已提交脚本；2026-07-15 本地执行记录和 `docs/BASELINE_PHASE1.md` 固化的 unversioned ad-hoc shell pipeline；`shasum 6.02` |
| `status` | `scope_recovered_and_reproduced` |

精确命令：

```bash
find outputs outputs_g05 outputs_g15 outputs_meta \
  results results_2x2 results_2x2_mock results_2x2_v2 results_levcheck \
  results_phase2b results_sweep results_sweep_higgs traces logs \
  -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256
```

原始证据来自 2026-07-15T03:41:42Z 的本地 Phase 0.5 Codex 执行记录，并已写入 `docs/BASELINE_PHASE1.md`。当前复算仍得到同一 SHA-256。

## 文件集合核对

| 目录 | 普通文件数 | Hash 1 | Hash 2 |
|---|---:|:---:|:---:|
| `outputs/` | 6 | 是 | 是 |
| `outputs_g05/` | 6 | 是 | 是 |
| `outputs_g15/` | 6 | 是 | 是 |
| `outputs_meta/` | 7 | 是 | 是 |
| `results/` | 19 | 是 | 是 |
| `results_2x2/` | 63 | 是 | 是 |
| `results_2x2_mock/` | 63 | 是 | 是 |
| `results_2x2_v2/` | 16 | 是 | 是 |
| `results_levcheck/` | 3 | 是 | 是 |
| `results_phase2b/` | 34 | 是 | 是 |
| `results_sweep/` | 139 | 是 | 是 |
| `results_sweep_higgs/` | 18 | 是 | 是 |
| `traces/` | 8 | 是 | 是 |
| `logs/` | 6 | 否 | 是 |
| **合计** | **394** | **388** | **394** |

这些命令枚举了核查时所有根目录下名称匹配 `results*` 和 `outputs*` 的目录，但没有使用 glob，而是把目录名逐个写入命令。未来新增目录不会自动进入旧 scope。根目录普通文件 `outputs_sweep_gain.png` 虽然名称匹配 `outputs*`，却未被任何命令列出，因此未纳入。

两种聚合都远多于五个 Phase 1 Mock 核心输出。它们包含历史 JSON、CSV、图像、配置及 trace 等 scope 内全部普通文件；五个核心输出的单文件 hash 是 `docs/BASELINE_PHASE1.md` 中另一组独立基线，不能与这里的目录集合 hash 混用。

`traces/` 可能包含 private reasoning。其文件内容仅在本机参与摘要计算，本文件不复制或公开这些内容。目录集合 hash 不是历史数据备份，也不替代访问控制、加密或保留政策。

## 当前复算记录

2026-07-15 在项目根目录重新执行上述精确命令，结果为：

```text
phase1_legacy_historical_collection:
14be0c81d31ef01476e3ba16f86e513d0b62436ca2c4c96874b7ac0c2f9b740d  -
regular files: 388

phase05_historical_directory_collection:
db00d0d7520273c9aa2f663a1d8cfa0b7b610dab0ed8ea3ca6a25a1f124edbaf  -
regular files: 394
```

这说明当前保留的两个明确 scope 都仍与历史记录一致。它不意味着两个 hash 彼此相等，也不证明未纳入 scope 的文件未变化。

## `.gitignore` 与正式 fixture

当前 `.gitignore` 的以下规则用于保护本地生成结果：

```gitignore
/outputs/
/outputs_*/
/results/
/results_*/
```

这些规则锚定项目根目录，不会忽略 `tests/fixtures/results/...` 或 `tests/fixtures/public_results/...`；但任何未来位于根目录的 `results_fixture/`、`outputs_fixture/` 等目录都会被相应通配规则忽略。临时运行结果应继续使用现有 ignored 目录，不应为了提交 fixture 而放宽整个生成结果范围。

另外，下列防御性规则没有根目录锚点，因此会在 fixture 子目录中同样生效：

```gitignore
private_events.jsonl
llm_records.jsonl
reasoning_traces.csv
*_traces.json
```

正式 fixture 建议采用以下策略：

- 使用明确目录，例如 `tests/fixtures/public_results/`，不要使用根目录 `results_fixture/` 或 `outputs_fixture/`；
- 优先在测试临时目录中动态构造 Record/Replay 私有输入；
- 已提交 fixture 只保存最小、合成、已脱敏且确属公开的字段、hash 或 ParsedDecision 示例；
- 不提交完整 Prompt、原始模型响应、private rationale 或真实历史 `llm_records.jsonl`；
- 如确需 `.gitignore` 例外，只能为经过人工隐私审阅的具体公开路径增加精确 allow rule，并用 `git check-ignore -v` 验证；不得增加宽泛的私有日志例外。

`.gitignore` 只控制 Git 跟踪，不定义上述历史 hash 的 scope。两个历史命令显式读取被 Git 忽略的本地目录，因此不能用 Git tree hash 或 commit hash 替代它们。
