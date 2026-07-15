# 仅本地 Git 工作流

## 硬约束

这个仓库的 Git 历史只存在于当前机器。除非未来获得单独、明确的授权：

- `git remote` 和 `git remote -v` 必须没有输出；
- 不得执行 `git remote add`、`git push` 或任何托管平台 CLI；
- 不得连接 GitHub、GitLab、Gitee 或其他代码托管服务；
- 不得把本地 Git 误称为备份或异机恢复方案。

## 日常起点检查

```bash
git rev-parse --show-toplevel
git status --short --branch
git remote -v
git log --oneline --decorate -n 5
git tag --list
```

预期仓库根目录为本项目目录，remote 输出为空。Git identity 优先使用已有配置；如果缺失，只能设置仓库级、明确不冒充个人的身份，不修改 global config：

```bash
git config --local user.name "Local Phase 1 Baseline"
git config --local user.email phase1-baseline@localhost
```

## 运行与 staging 纪律

仿真和测试输出优先写到 `/tmp` 或其他仓库外目录。提交前：

```bash
git status --short
git diff --stat
git diff
```

使用明确路径 staging，不使用宽泛的 `git add .` 或 `git add -A`：

```bash
git add nmsim experiments tests docs README.md AGENTS.md .gitignore
git diff --cached --name-only
git diff --cached --check
git diff --cached
```

对敏感候选使用：

```bash
git check-ignore -v .claude/settings.local.json
git check-ignore -v private_events.jsonl
git check-ignore -v llm_records.jsonl
git check-ignore -v reasoning_traces.csv
```

禁止用 `git add -f` 绕过忽略规则。以下材料不得进入 Git：

- `.env*`、credential、private key；
- `.claude/settings.local.json`；
- 完整 Prompt、原始模型响应、private rationale；
- `private_events.jsonl`、`llm_records.jsonl`、`reasoning_traces.csv`；
- 生成的 `runs/`、`results*`、`outputs*`、`traces/` 和日志。

参考输入 `nmsim/meta_feb2022_reference.csv` 和 `examples/reference_episode.csv` 是例外，应被追踪。

## Phase 1 基线与 tag

基线提交和 annotated tag 的约定是：

```bash
git commit -m "chore: seal Phase 1 provenance baseline"
git tag -a phase1-provenance-v1 -m "Phase 1 provenance and replay local baseline"
```

验证 tag 是 annotated tag，且指向当前基线 commit：

```bash
git cat-file -t phase1-provenance-v1
git rev-parse 'phase1-provenance-v1^{commit}'
git rev-parse HEAD
```

第一条应输出 `tag`，后两条必须一致。tag 建立后不得移动或复用。

## 与基线比较

```bash
git diff --stat phase1-provenance-v1..HEAD
git diff phase1-provenance-v1..HEAD -- nmsim experiments tests docs
git status --short
```

Git diff 只证明文本差异；科学语义是否变化仍必须通过 `docs/BASELINE_PHASE1.md` 和 `docs/RUN_PROVENANCE.md` 中的 Mock、Record/Replay、RNG 及实验兼容回归判断。

## 安全回滚

优先创建一个反向 commit：

```bash
git revert <commit>
```

只恢复指定文件时：

```bash
git restore --source=phase1-provenance-v1 -- path/to/file
```

只读调查旧基线可使用 detached checkout，但先确保当前工作已提交，并在调查后回到 `main`。

不得使用 `git reset --hard`。尤其不得使用 `git clean -fdx`：`-x` 会删除 `.gitignore` 保护的历史结果、私有记录和本机材料。

## Manifest 与 Git 状态

若需要 manifest 记录 `dirty=false`：

1. 先提交所有有意代码和文档变化；
2. 确认 `git status --porcelain` 为空；
3. 把输出写到仓库外；
4. 再启动仿真。

`dirty=true` 是事实，不是运行失败；不得手工改 manifest。manifest 的 `diff_hash` 覆盖 tracked diff 和 untracked 文件名，不覆盖 ignored 历史结果内容，因此历史数据仍需单独 inventory/hash。

## 本地仓库的局限

- 没有 remote，就没有异机副本。
- `.git` 目录损坏或丢失会同时失去 commit、branch 和 tag。
- `.gitignore` 只阻止未追踪文件进入新 commit，不会保护文件内容、修改权限或提供加密。
- 任何未来 remote/发布动作前，都必须重新审计凭据、private LLM 数据、内网 endpoint 和内部访问文档。
