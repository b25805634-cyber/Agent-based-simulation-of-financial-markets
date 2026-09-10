# 第二步验证：真实人类数据、可见信息与闭环复验

执行代码：`14cc4ce03da5f5c9e92d3cdd763555b341adf0ab`，所有下列正式运行均记录
`git_dirty=false`。这次推进了输入语义、真实人类参考和复验工具；没有新增
真实 Teacher 请求，也没有宣称模拟市场已通过真人验证。

## 1. 样本增加对 Student 有没有用

保持原来的分组切分、120 epochs 和模型结构，只逐步增加训练 families。
验证集固定，原测试集的 1,500 个请求对应的标签/观察不进入拟合或评分。

| 使用训练比例 | 训练 families | 有效训练行 | 线性验证 CE | MLP 验证 CE |
|---|---:|---:|---:|---:|
| 12.5% | 219 | 871 | 0.457693 | 0.661683 |
| 25% | 438 | 1,743 | 0.443423 | 0.516680 |
| 50% | 875 | 3,483 | 0.438655 | 0.450558 |
| 100% | 1,750 | 6,954 | 0.435482 | 0.420333 |

样本扩大对 MLP 有明显帮助；当前最小规模下 MLP 甚至不如先验。满量点复现
原模型的训练/验证结果，没有重新选模或读取测试评分。不能凭四个开发点
推算十万条一定能达到某个准确率，下一批应优先覆盖实际闭环状态和边界。
曲线语义 hash 为 `3e6c711663a57955494fa6d0e21fd97123647a94e787577975830d143bf0f107`，
不包含计时，也不等于原始数据文件或 manifest 的 SHA。

## 2. 日内振幅缺失已改成明确“不可见”

新增 `available_only` 模式，省略没有观测到的日内高低价振幅，Student 的对应
mask 为 0。换手率的分母为零时也省略该字段。原始模式仍可复查，历史 28 个
市场不改写。新模式增加 6 个市场、360 轮、72,000 次决策，约 11.596 秒完成，
账务守恒全部通过。新旧模式在观察无关的随机策略下保持同一市场初始化和结果。

新模式 MLP 三种子平均期末涨跌：独立报价约 -50.63%，强度联动报价约 -70.21%。
修正语义没有消除下跌，也不应为了消除下跌去调规则。缺失字段组合在旧训练
集中未出现，单字段 min/max 检查不能发现全部这种联合变化，因此仍需要复验。

兼容性诊断实际加载 Git `333c300` 的旧模块，与当前默认模式比较：8 个代理、
12 轮、seed 17 的完整结果对象完全一致，规范 JSON SHA 为
`5c773927a6c581bd43403dc19476bf2beb72d7eb5999de07984bdf2e836bbaf1`。

## 3. 120 次复验计划已冻结，真实请求待连接恢复

从 72,000 个真实模拟中产生的代理轮次选择 24 个状态，覆盖四种信息视图、
两种报价规则、三个时间段。来源中没有空仓/满仓端点；实际 24 个观测分层
均为 interior，每层选一个状态。每状态重复五次，总计 120 个逻辑请求。
不是 120 个真人或 120 次市场运行。

计划 hash：`2b7d53cd54c9cc11637b5ffae682a946fba236dc47ead40db8878a24f47fb690`。
采样前不看 Teacher 答案；请求只包含实际可见值和自己的账户。除方向 CE/TV
外，还比较买卖条件强度均值、样本方差、MAE 和 N。工程 null 已完成 120/120，
明确标记 `real_endpoint=false`, `valid_endpoint_responses=0`。这不是端点结果。

既有仓库内网地址的 TCP 连通性诊断超时（3 秒），因此未启动这 120 次真实
请求。没有为了“继续运行”伪造回答或让失败变成 hold。连接恢复后使用新 run ID，
`--provider openai --live --confirm-request-count 120` 执行；原计划与历史数据不追加。

## 4. 已取得并核验真正的人类实验数据

官方公开 CSV 是 [Liêu/Pelster v1](https://data.mendeley.com/datasets/jfg8s32xdm/1)，
许可 CC BY 4.0，SHA-256
`327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333`。
使用表格分析规则保留原始列、缺失值、单位和时序，重建结果单独存放。

- 81 名历史实验参与者，6 个 sessions，27 个三人组。
- 原始 1,377 行含 243 次练习；正式为 1,134 次六资产联合决策、6,804 个股票机会。
- 正式股票机会 buy/hold/sell = 1,465 / 4,268 / 1,071。
- 8,262 项持仓平衡、7,290 项连续性、1,377 项净现金检查通过。
- 418 次联合决策同时买卖不同股票；49 次买入由同期卖出收入共同支付。

最重要的时序发现：`result_final[t]` 含下一期价格变化。在可核验的 1,296 个
相邻记录中，均精确等于当期交易后现金加持仓按下一期价格估值。它完全不进入
重建的决策前状态；最后每人一期的后续价格未提供，因此另外 81 个期末值不
声称已核验。匿名个体重建记录写入 0600 文件，公共输出只有汇总。

该实验是六资产、外生价格、14 个实验期，并非我们的单资产、20 日观察窗口、
内生价格市场。原始完整开场行情、排行榜实际显示和采购批次约定尚未恢复，
所以它是外部人类参考，不能强行映射成单资产 intensity 或说已有 81 人回答
了本项目的 24 道题。详见 [来源与映射审计](HUMAN_REFERENCE_LIEU_PELSTER.md)。

## 5. 另一个需要继续处理的行为压缩问题

只读计数发现，历史 Teacher 的 8,056 次卖出中有 966 次 intensity=1（全部卖出），
809 次买入中有 11 次全部使用现金。当前 Student 输出的是条件平均强度。
本次六个市场中卖出强度最大约 0.81–0.88，没有一次全额卖出。
联动报价的三个市场分别有 313、323、474 次卖出意愿因只剩一股且向下取整而
不能形成订单；独立报价三个市场只有 0、0、1 次这样的情况。

这说明全额退出等强度分布特征会在均值压缩中丢失，但它不是所有停滞的解释。
下一步应检验能保留端点质量的条件交易规模分布，而非直接调一个“至少卖一股”
规则来制造想要的曲线。该诊断没有修改当前模型或市场。

## 确切命令与结果

工作区 `.worktrees/agent-market-loop`，Python 3.9.6；以下均为已执行命令。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m unittest discover -s tests -p 'test_*.py' -v
# Ran 630 tests in 163.415s — OK
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m compileall -q nmsim experiments tests
# exit 0
git diff --check
# exit 0

PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --agents 200 --rounds 60 --seeds 3 --policies selected --quote-rule both --observation-policy available_only --run-id information-market-available-20260909-a1 --out results_information_market
# finished; 6 markets / 360 rounds / 72,000 decisions; 11.596131 seconds

PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m experiments.information_diagnostics --task learning-curves --source-run ../v2-teacher-pilot/results_information_weight_scale_10k/runs/information-weight-scale-10k-live-20260825-a1 --source-manifest-sha256 3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4 --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --fractions 0.125 0.25 0.5 1.0 --epochs 120 --hidden-dim 16 --backend numpy --run-id information-learning-curve-20260909-a1
# finished; 12 model fits; no test predictions; no Teacher requests

PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m experiments.information_diagnostics --task human-reference --csv results_human_reference_inputs/lieu_pelster_v1/327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333/data_de_scopic.csv --run-id human-reference-audit-20260909-a1
# finished; historical_reference_humans=81; new_human_participants=0

PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m experiments.rollout_fidelity --task plan --market-run results_information_market/runs/information-market-available-20260909-a1 --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --max-states 24 --replicates 5 --run-id rollout-fidelity-plan-20260909-a1
# plan_only; selected_states=24; planned_logical_requests=120; new_teacher_requests=0

PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m experiments.rollout_fidelity --task acquire --plan-run results_rollout_fidelity/runs/rollout-fidelity-plan-20260909-a1 --provider fake_test_teacher --run-id rollout-fidelity-fake-20260909-a1
# finished; 120/120 fake responses; real_endpoint=false; valid_endpoint_responses=0
```

随后补充“采集阶段不得用命令行覆盖已冻结选样参数”的输入检查，执行
`PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 -m unittest tests.test_rollout_probes -v`：
11 tests / 0.389s / OK。它只拒绝此前会被忽略的选样参数，不改变已执行计划、
请求或历史结果。

| 运行 | Manifest SHA-256 | 注册 artifacts |
|---|---|---:|
| information-market-available-20260909-a1 | 4dbd81829b1c3477a2212dc499308c513724b88017c4253bc026c1c6384ce51d | 20/20 |
| information-learning-curve-20260909-a1 | 8d8b7c50585153ab144851e71535752ed04293992cafb3beeecdf7675aed26be | 6/6 |
| human-reference-audit-20260909-a1 | 7ad70c7585274618a75fc0c4fea4f1ab9227f4f600870b558d128d06e7a517d7 | 7/7 |
| rollout-fidelity-plan-20260909-a1 | 3611a9eda44418ba82becbeb6c1e8eda3f4469c6d69497b2955505cfd443f4c8 | 6/6 |
| rollout-fidelity-fake-20260909-a1 | e24e73bc2c622535d3d79c54c036868d327bf0c973813a965b646f76c5609939 | 9/9 |

每个运行的 `identities.json` 分别保存 `scientific_config_hash`、
`model_request_config_hash`、`execution_config_hash`、`full_effective_config_hash`
及具体 schema。它们不是 manifest SHA 或文件字节 SHA，不混称为默认配置 hash。

新增科学变化是明确可选的可见信息模式、新的诊断采样和参考任务适配。旧机制、
历史记录和默认行为保持可复查。代码和公开结果摘要更新到 PR #13；原始人类
数据、个体记录、模型产物和私有响应不随代码上传。
