# Agent 市场：2026-09-09 实际进展

现有一万条 Teacher 数据已经接入新 Student，再接入真实记账的集合竞价市场。
这次完成了离线训练和市场闭环，尚未完成“像真人”的验证。原始 Teacher 回答
没有重新采集、合并或改写；新增 Teacher 请求数为 0。

所有下列正式运行来自干净代码提交
`68dc3c661c998f5cc75deb0457265cf30d62230e`，工作区是
`.worktrees/agent-market-loop`。后续报告文档提交不改变这些历史运行的身份。

## Student：已经用上一万条数据

- 数据源：`information-weight-scale-10k-live-20260825-a1`。
- 10,000 个逻辑请求中 9,918 条有效、82 条失败。失败没有改成 hold。
- 2,500 个底层状态按组切分，四种信息视图不会跨训练/验证/测试集。
- 有效训练/验证/测试行：6,954 / 1,476 / 1,488。
- 输入：24 个信息字段的可见值 + 24 个可见性标记 + 8 个账户字段。
  隐藏字段、标签名、prompt 和 reasoning 均不是 Student 输入。
- 固定 120 epochs，训练集拟合标准化；按验证集动作交叉熵选模，再评测试集。
- 三候选拟合与评估全流程约 5.067 秒，NumPy 后端；推理支持纯标准库。

| 模型 | 参数/拟合系数 | 验证 CE | 测试 CE | 测试 Brier | 买卖强度 MAE | 宏平均 F1 |
|---|---:|---:|---:|---:|---:|---:|
| 先验基线 | 5 个存储系数 | 0.609494 | 0.578710 | 0.300095 | 0.160904 | 0.301961 |
| 线性模型 | 285 | 0.435482 | 0.399413 | 0.211660 | 0.133603 | 0.555182 |
| MLP | 997 | 0.420333 | 0.382743 | 0.208284 | 0.138328 | 0.556351 |

CE、Brier、MAE 越低越好。MLP 在动作概率指标上最好，但线性的强度误差略低。
MLP 的动作损失相对先验降低约 33.9%，相对线性降低约 4.2%；这只是当前留出
Teacher 回答上的描述性比较，没有给两模型优劣作显著性或真人有效性声明。
测试结果已经看过；后续修改模型时不能把它继续称为未触碰的确认集。

## 市场：28 个完整市场、384,000 次决策

基本对照为 200 个代理、60 日、3 个种子，三种策略乘两种报价规则，共 18 个
市场，1,080 轮、216,000 次决策，含导入/完整账本写入耗时 26.259 秒。
另完成信息构成、事件中性和 1,000 代理运行，合计 28 个市场、1,680 轮、
384,000 次决策。每轮现金和股票守恒全部通过。不同市场/种子不是独立真人。

| 200 人，均衡信息构成 | 报价规则 | 三种子平均期末涨跌 | 平均无成交日 | 边际训练范围外比例 |
|---|---|---:|---:|---:|
| 选中 MLP | 强度独立报价 | -50.41% | 12.0 | 22.48% |
| 选中 MLP | 强度影响报价 | -69.48% | 8.7 | 18.19% |
| 训练先验 | 强度独立报价 | -60.26% | 0 | 1.31% |
| 训练先验 | 强度影响报价 | -60.83% | 0 | 6.21% |
| 随机方向 | 强度独立报价 | +23.05% | 0 | 0.03% |
| 随机方向 | 强度影响报价 | +23.05% | 0 | 0.03% |

这些涨跌幅不是真实性得分，也不是价格预测。随机策略使用固定 0.5 强度，
使强度联动报价中的额外 urgency 恰好为 0，因此两种报价下的随机对照一致。
不读取状态的先验也能出现明显下跌，说明不能把下跌或曲线形态归因于
LLM 已经复现真人心理。MLP 改用联动报价后平均多跌 19.07 个百分点，表明
报价规则对结果有很大影响；每臂只有三个种子，不作确认性结论。

补充的 MLP / 强度独立报价处理：

| 条件 | 市场数 | 平均期末涨跌 | 边际训练范围外比例 | 整个 managed run 耗时 |
|---|---:|---:|---:|---:|
| 200 人，量价偏重人数占 70% | 3 | -43.74% | 36.39% | 5.414 秒 |
| 200 人，基本面偏重人数占 70% | 3 | -45.30% | 24.82% | 5.583 秒 |
| 200 人，事件中性 | 3 | -66.01% | 100% | 5.511 秒 |
| 1,000 人，均衡信息构成 | 1 | -61.71% | 13.66% | 9.317 秒 |

两个 70% 处理的其余三种视图各占 10%。范围外比例是“至少一个输入值超出
训练集对应字段最小/最大值”的代理轮次比例，不是联合分布检验。中性臂每行
都因 `affected_revenue_fraction=0` 被标记：LHS 训练样本未覆盖这个精确端点。
这不等于每行都远离训练数据，但说明缺少无事件锚点；该臂不能直接当作已经
验证的行为消融。其他超界、显式裁剪和每日收盘变动替代日内振幅的结构性差异
都在原始公共账本中单独保留。

## 这次增加的科学机制

新增一套版本化的信息市场：公司数据前后相接，公开事件有时间戳，随市场价
重算盈利/账面价值比率；每个代理保留现金、持股、成本与交易经历；同一个
Student 按实际可见字段做决策。提供强度独立/联动报价、先验/随机策略、
事件中性、信息构成处理。以上均是新开发机制，不能冒充原 a10 的同一身份。
旧 prompt、V1/V2 数值代码、已有 CLI/default/schema 和历史目录未改写。

## 真人评价准备好了，但真人证据还没有

生成 24 个中性情景、12 对条件对比及对应 Student 预测。匿名回答接口与
JS divergence、NLL、Brier、条件强度误差、个体内成对变化的评分已实现。
本轮实际真人参与者/真人回答均为 0；合成测试数据不会计入真人 N。

可优先评估已公开、CC BY 4.0 的
[Liêu 与 Pelster 实验数据 v1](https://data.mendeley.com/datasets/jfg8s32xdm/1)，
DOI `10.17632/jfg8s32xdm.1`。目前只核验了数据目录及许可，尚未下载、复现
原任务或判断能否映射为本项目的输入；不能把它已经当作真人锚点。

下一步需要针对闭环状态和无事件边界设计重复 Teacher probe，并建立同任务
人类证据。理论知识库先用于设计对照；不把预期偏差偷偷写进基线 prompt。
社交传播、真实市场统计对照和人群构成反推仍待后续独立验证。

## 运行与精确身份

命令、参数和参数含义见 [runbook](INFORMATION_MARKET_RUNBOOK.md)。全部运行的
四种配置 hash 使用 `information-market-identities/1.0`，位于对应
`identities.json` 和 manifest 的 `information_market_identities`；旧 Config
hash 只描述生命周期适配，不代表新市场科学参数。

训练运行的四种 hash：

```text
scientific_config_hash    1fcdf4780d4a91bb23321ed77f12aef5405085ecbd91aa688190a4033d73663d
model_request_config_hash b7d111998a4f732450f7690640fabffa203234d9e65f9212ef58298bd9a638b1
execution_config_hash     8490c6800c6b674450136ae31dfa43d82dd4ddc1e69a74166d1bdf972d68685d
full_effective_config_hash 02ab11fb0ce8fc92bcaadee1eedd09a9ec71735b8a7b8e3a7ad2750723ccdc8b
```

200 人主对照运行的四种 hash：

```text
scientific_config_hash    f68157cdfe4913ce286922ffbf4244ad3a78b590a13e181250dbf0084b546254
model_request_config_hash b7d111998a4f732450f7690640fabffa203234d9e65f9212ef58298bd9a638b1
execution_config_hash     cd16b6896e007235e60a8624f112868492d527afa0601b462e8eee751ff2891b
full_effective_config_hash b33b94373e41999ce2aa6bdbe98e572b9848946c734beaa9fd84d1cf4c0a8403
```

所有运行相对目录为 `results_information_market/runs/<run_id>/`。

| run_id | manifest SHA-256 | 注册 artifact 验证 |
|---|---|---:|
| information-student-10k-20260909-a1 | a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af | 10/10 |
| information-market-200-20260909-a1 | c9b7b6a9dfbabb214fe53fe5e5a882a5ff34beca1304d1e99ee8667d6bc63f78 | 44/44 |
| information-market-1000-20260909-a1 | 40dad103dd40b38d98f7f3d87eb1085ca3c8cd2c7e620bff455623ee75eadfa8 | 10/10 |
| information-market-neutral-20260909-a1 | 9b86fccbb024af1e9855e0d9ea156040038846e5daa81035ba4e82ca4802e4f9 | 14/14 |
| information-market-price-heavy-20260909-a1 | ffc3dfa6bd3c3009b49d4208eab87f2554722bcf94d39042fb4cb723fe3359c9 | 14/14 |
| information-market-fundamental-heavy-20260909-a1 | 6d085e0ead4ae104b90ce7cb6896669cf92bca81849a9b3f5bcc8d60c332b433 | 14/14 |
| information-human-tasks-20260909-a1 | a0a536d21f6f7c4961988a6cd9148447009575537471b5bab678699539a0c322 | 11/11 |

历史 a10 manifest SHA 仍为
`33605f7c50424156fa77e2576cc915fedc1d0cbe9ffc49474be8df105c00bf6f`；42/42
artifact 通过。历史 10k manifest SHA 仍为
`3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4`；7/7
artifact 通过。本地内容寻址备份与恢复演练通过；独立介质备份仍待配置。

## 确切验证命令与结果

执行上下文均为 `.worktrees/agent-market-loop`，系统 Python 3.9.6。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m unittest discover -s tests -p 'test_*.py' -v
```

```text
Ran 589 tests in 166.291s
OK
```

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m compileall -q nmsim experiments tests
PYTHONHASHSEED=0 PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.repro_check
git diff --check
```

```text
compileall: exit 0, no output
PASS — identical price history across 3 processes with different PYTHONHASHSEED
 history length: 25 points; final price: 83.480000
git diff --check: exit 0, no output
```

训练确切命令：

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task train --source-run ../v2-teacher-pilot/results_information_weight_scale_10k/runs/information-weight-scale-10k-live-20260825-a1 --source-manifest-sha256 3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4 --epochs 120 --hidden-dim 16 --backend numpy --run-id information-student-10k-20260909-a1 --out results_information_market
```

stdout JSON 中实际字段：`status=finished`, `selected_model=mlp`,
`trained_models=3`, `elapsed_seconds=5.066631875`；完整计数见上文。

主市场对照确切命令：

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 200 --rounds 60 --seeds 3 --profile-weights 1 1 1 1 --policies selected prior random --quote-rule both --news-mode eventful --run-id information-market-200-20260909-a1 --out results_information_market
```

stdout JSON 中实际字段：`status=finished`, `market_runs=18`, `rounds=1080`,
`agent_decisions=216000`, `elapsed_seconds=26.258825667`。

补充运行确切命令：

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 1000 --rounds 60 --seeds 1 --policies selected --quote-rule independent --run-id information-market-1000-20260909-a1 --out results_information_market
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 200 --rounds 60 --seeds 3 --profile-weights 1 1 1 1 --policies selected --quote-rule independent --news-mode neutral --run-id information-market-neutral-20260909-a1 --out results_information_market
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 200 --rounds 60 --seeds 3 --profile-weights 7 1 1 1 --policies selected --quote-rule independent --run-id information-market-price-heavy-20260909-a1 --out results_information_market
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 200 --rounds 60 --seeds 3 --profile-weights 1 7 1 1 --policies selected --quote-rule independent --run-id information-market-fundamental-heavy-20260909-a1 --out results_information_market
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m experiments.information_market --task benchmark --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --run-id information-human-tasks-20260909-a1 --out results_information_market
```

全部 exit 0 / `status=finished`。1,000 人运行为 1 个市场/60,000 决策；三个
补充处理各为 3 个市场/36,000 决策；benchmark 为 24 个任务和 24 个模型预测，
真人数为 0。各自耗时及 manifest SHA 见上文。

测试前新 worktree 无历史运行。两次全套测试各产生一个有意的 config_validation
失败目录，均为 `honest_n=0`, Provider calls 0, network false；保留在 ignored
`outputs/runs/20260909T083844.477999Z-de2c4ea05475/` 与
`outputs/runs/20260909T084744.268727Z-fce1d4096a41/`。它们是测试产物，未计入市场
结果，也未提交 Git。Matplotlib 的第三方弃用警告不影响测试通过。

HTML 做了语法解析与本地链接存在性检查；当前没有可用的浏览器自动化连接，
没有声称完成截图级视觉验收。
