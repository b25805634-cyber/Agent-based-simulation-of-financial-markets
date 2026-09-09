# 买卖量分布、配对市场与真人任务：2026-09-09

## 这一步实际完成了什么

小模型现在可以保留“卖一点”和“全部卖出”之间的区别，而不只是把它们
平均成一个比例。原来的买/停/卖模型没有重训，整数股数和撮合规则没有改。
新机制只在显式选择 `distribution_mean` / `distribution_sampled` 时启用。

已完成一个分布拟合、四组各三次的配对市场，以及公开人类研究的早期任务
重建。拟合和这些市场、任务导出全部为离线计算，新增 Teacher 请求为零。
正在另外执行的 120 请求 Teacher 对照不是这里的离线结果，也尚未用于本次选模。

执行位置：`/Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop`。
分支 `feat/agent-market-loop`；六个新正式运行均从干净提交
`a204422d3d1bd13dac64d86353087ff6e358c88e` 创建，经过中央 registry 和
`ManagedRunContext`。原 a10、10k、Student 和正在采集的独立 worktree 均未改写。

## 1. 小模型的验证结果

原 997 参数 MLP 的表示层、动作预测和旧均值头全部冻结。新增买入 323、
卖出 867 个分布头参数，合计 1,190；共用原 MLP 后共 2,187 个独立存储系数。
不能将同一个 frozen action/representation MLP 重复算两遍。

精确强度支持来自原训练集，买入 19 个值、卖出 51 个值；不造满仓/清仓标签、
不分箱、不注入噪声。训练 6,954 条，验证 1,476 条；原 test 的 1,500 个请求
按身份排除，未计算任何新 test 预测。四种信息视图仍按原潜在状态分组。

| 验证分数，越低越好 | 买入，109 条 | 卖出，1,199 条 |
| --- | ---: | ---: |
| 原均值头，作为点质量诊断 | 0.093187 | 0.147344 |
| 不看状态的经验分布 | 0.067469 | 0.122036 |
| 冻结 MLP 表示上的条件分布 | 0.063865 | 0.093280 |

上表是 CRPS，不是分类准确率。两动作均按预先冻结的最低验证 CRPS 规则
选中条件分布。验证集用于选择，因此这些不是独立确认集的成绩。

全卖标签为 142/1,199＝11.843%，条件分布平均预测 11.807%；全卖事件的
Brier 为 0.080377，经验分布为 0.104422。仍有条件校准偏差：一个 41 条的
分箱平均预测约 44.6%，实际约 26.8%。买入端的负结果也保留：109 条验证
买入没有全买，条件分布仍分配平均 1.403% 全买概率；其全买 Brier 0.002999
差于经验分布 0.000237 和原均值头的 0。

这支持“在这些 Teacher 标签上改善分布拟合”，不支持“已经校准所有端点”
或“模型已像人”。原数据每状态/视图 K=1，跨状态拟合的分布不等于已识别
的同状态 Teacher 随机分布，更不是人的异质性。

## 2. 接回市场之后

每组均为 200 agents × 60 日 × 3 seeds，四组共 12 个 market runs、
720 rounds、144,000 agent-decisions；人类参与者为 0。
所有组使用 `available_only`、独立报价、同一个冻结动作 Student；在每个
seed 内初始账户和外生世界一致。动作/报价/买卖量抽样使用分开的随机命名空间。
后续内生状态会分岔，不能写成“全程动作序列相同”。

| 三次市场合计，收益率取均值 | 条件均值 | 条件抽样 | 经验均值控制 | 经验抽样控制 |
| --- | ---: | ---: | ---: | ---: |
| 全卖订单意愿 | 0 | 7,956 | 0 | 3,475 |
| 实际清仓事件 | 0 | 63 | 0 | 62 |
| 至少清仓过的 agent-market 对 | 0 | 61 | 0 | 57 |
| 成交股数 | 120,320 | 125,336 | 144,257 | 150,451 |
| 平均价格变化 | −50.020% | −49.317% | −49.703% | −49.770% |
| 1 股正卖意愿但 floor 后无订单 | 30 | 36 | 0 | 129 |
| 超原训练边际范围的 agent-rounds / 36,000 | 8,314 | 8,101 | 7,673 | 7,616 |

现金和股票总数在 12/12 场、720/720 轮守恒。抽样组确实出现了清仓，但
“想全部卖”通常没有全部成交，不能把意愿当成交。抽样也没有消灭所有
一股残仓：不是每次卖出都会抽到强度 1，而且没有对手方时仍不能成交。

这个结果**没有解决市场整体持续下跌的问题**。每组仅三 seeds，不据此做
显著性结论，不把更高或更低的收益率当“更像真人”。新的缺失信息模式、
跨状态拟合、资源分布和报价制度仍可能影响闭环结果。`legacy_intensity_linked`
没有用于这个主比较，因为它会同时改变数量和报价，不是纯买卖量处理。

## 3. 真人比较任务

已从经过 SHA 核验的 Liêu–Pelster 公开数据构建 `spt2` 前五轮任务：
39 人、13 同伴组、195 个六资产联合决策、1,170 个股票行动机会。
原研究总计 81 人；此处 39 人是其子集，不能相加为新招募的 120 人。

价格序列恢复和终值算术检查已通过，但原 UI 的历史信息保留方式仍未
独立证实。使用明确的新协议
`spt2-main-1-through-5-published-clock-net-budget/0.1`，不是完整原 UI 复现。
净预算允许同期卖出为买入提供资金；没有通过删掉相应真人样本来回避问题。

固定“不交易”控制匹配 25/195 个完整决策，股票行动匹配 666/1,170
（56.923%），有符号股数 MAE 2.296581。它只是 synthetic hold null，
**不是 Teacher 得分，也不是通过阈值**。尚未在这些任务上调用 Teacher。

审查发现跨题泄漏：下一轮自己的交易历史可还原上一轮答案，195 题中有
156 题会这样暴露。因此完整 labelled bank 和 prompt-only bank 均为 0600；
公开文件只有无轨迹的目录。后续每次只把当前题交给独立请求上下文，不能
给模型整套任务。详情和原始来源见 [HUMAN_TASK_ALIGNMENT.md](HUMAN_TASK_ALIGNMENT.md)。

## 4. 正式产物与身份

拟合目录 `results_intensity_distribution/runs/intensity-distribution-20260909-a1`：

```text
manifest SHA-256: a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9
registered artifacts: 6/6 verified
study_semantic_hash: 20833feb8b8e4378a1a99fc155c4097bd4a33c3c200863a4ef670553992ea888
identity schema: intensity-distribution-experiment/1.0
scientific_config_hash: f0f2cb0b2c746f049a172bea65362ae6d036abbb0b1bcbfeef2ed4199ffde69f
model_request_config_hash: a7e6c639f240bab38d8f72855bb262104a48dcf8de3668cdf211b4c34157fde9
execution_config_hash: 441e1ce34f4affdc07c4af5cfaa173f4ecabc30378ce9b376eae034c2dce84b1
full_effective_config_hash: c0648d77ac14152a51b7012f73fbc43d5d5baf0516e5247ede3521ff9f575e71
```

以下四个目录均位于 `results_information_market/runs/`，每个 15/15 artifacts
已验证；结果 schema 为 `information-market/1.2.0`，身份 envelope 仍为
`information-market-identities/1.0`。原 CLI 默认及旧结果 schema 未改变。

```text
information-market-sizing-mean-20260909-a1
manifest SHA-256: 1775309fbc02fdc0b6cf5e65d904c8566403828264cc7125a10446bcdfa678db
scientific_config_hash: aec83637a66718413118090ec7c731abc3905636c2125e9e5c9b33ff85ca5b5f
full_effective_config_hash: 854dc9e1cc557a71af87247e29abe80814bf7b0384accd660593aa6469961de6

information-market-sizing-sampled-20260909-a1
manifest SHA-256: 83619446c8102e553ebd6ca92c8daae3f9651076b085c103287fb296eee6c9e8
scientific_config_hash: a834dbbd9e6b0d847721f87a8bbf974516c7407b0447d30a6c724172614ed57d
full_effective_config_hash: 25c2db704fa6212ff1caa395d03f038e16825df765db9b0c781a87ccd39d8e57

information-market-sizing-empirical-mean-20260909-a1
manifest SHA-256: b28049eab8ab9fb368e81e14beaf4cf78398ed7db1718b6d8906df25f3179d31
scientific_config_hash: 30301b9e42edf51369c18f565988b077afb6d8857ba1e6e129a29a8dbc0513b1
full_effective_config_hash: 7131fb022c5ef38340bae1603856c98651fb6d24431d9b11c70c54a4bfc0f008

information-market-sizing-empirical-sampled-20260909-a1
manifest SHA-256: 610736a62e1ea2e81a876b65d151165efa2f0bdb6ee998dd9596b553d5aa38fe
scientific_config_hash: fbfa5f03343684b02cdadf2af6fd84503004dffd42f84509fc223c2803ddf82e
full_effective_config_hash: db8df3a313495a5d3085f72923f2bf0316ea2c7d3f9eb1e8c388df8f164132fe

Four markets share:
model_request_config_hash: b7d111998a4f732450f7690640fabffa203234d9e65f9212ef58298bd9a638b1
execution_config_hash: 8ac76ca7ad0195771d36ea071f06ba5626529b5c1979e85376135e0b0fcb83e9
```

真人任务目录 `results_information_diagnostics/runs/human-early-tasks-20260909-a1`：

```text
manifest SHA-256: 5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286
registered artifacts: 10/10 verified
identity schema: information-diagnostics/1.0
scientific_config_hash: e98c199ce640c838925fe767ebd663a3d3dbc9174ca3a39efba06407df2c685f
model_request_config_hash: a7e6c639f240bab38d8f72855bb262104a48dcf8de3668cdf211b4c34157fde9
execution_config_hash: 4df8f55060f36d72369ad96257ce88a454b8716d2d6e7a9a401e77f611f239a7
full_effective_config_hash: e9fa516915630e86e57de41dc52777d7226264344c02be72202985e28cd5eca8
```

以上 config hash、manifest 的 byte SHA 和 study semantic hash 不互换。
历史来源仅为已验证的 analysis input，不因此成为可 resume child。没有新
production dependency；NumPy 是原有可选加速器，序列化推理仍用标准库。

## 5. 确切执行命令及结果

所有命令从前述正式 worktree 执行。重复执行须使用新的 run ID，不覆盖这里的目录。
市场表由验证过的 `summary.json` 中三个 cell 的计数求和、收益率取算术均值；
这是报告中的描述性汇总，不声称另有一个正式推断分析 run。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.intensity_distribution --source-run ../v2-teacher-pilot/results_information_weight_scale_10k/runs/information-weight-scale-10k-live-20260825-a1 --source-manifest-sha256 3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4 --student-run results_information_market/runs/information-student-10k-20260909-a1 --student-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --epochs 120 --backend numpy --run-id intensity-distribution-20260909-a1
```

结果：exit 0，real 3.92s；训练 6,954、验证 1,476、test predictions 0、
action model fits 0；买卖均选择 conditional_softmax。完整分数及身份见上文和原 summary。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --sizing-policy distribution_mean --sizing-candidate selected --observation-policy available_only --policies selected --quote-rule independent --agents 200 --rounds 60 --seeds 3 --run-id information-market-sizing-mean-20260909-a1 > /private/tmp/information-market-sizing-mean-20260909-a1.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --sizing-policy distribution_sampled --sizing-candidate selected --observation-policy available_only --policies selected --quote-rule independent --agents 200 --rounds 60 --seeds 3 --run-id information-market-sizing-sampled-20260909-a1 > /private/tmp/information-market-sizing-sampled-20260909-a1.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --sizing-policy distribution_mean --sizing-candidate empirical --observation-policy available_only --policies selected --quote-rule independent --agents 200 --rounds 60 --seeds 3 --run-id information-market-sizing-empirical-mean-20260909-a1 > /private/tmp/information-market-sizing-empirical-mean-20260909-a1.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.information_market --task simulate --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --sizing-policy distribution_sampled --sizing-candidate empirical --observation-policy available_only --policies selected --quote-rule independent --agents 200 --rounds 60 --seeds 3 --run-id information-market-sizing-empirical-sampled-20260909-a1 > /private/tmp/information-market-sizing-empirical-sampled-20260909-a1.log 2>&1
```

四条分别 exit 0，real 16.50/16.03/15.38/14.79s（并行执行，非独占机器性能）。
每条终端摘要均为 status finished、market_runs 3、rounds 180、agent_decisions
36,000、new_teacher_requests 0、human_participants 0。没有选最快一次替换结果。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.information_diagnostics --task human-early --csv results_human_reference_inputs/lieu_pelster_v1/327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333/data_de_scopic.csv --run-id human-early-tasks-20260909-a1
```

结果：exit 0，real 0.35s；historical_reference_humans 39、joint_decisions 195、
stock_opportunities 1,170、synthetic_null_predictions 195、新人类/Teacher 均为 0。
四个 `private_*` 文件（包括两个任务 bank）全部 0600。

## 6. 测试

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m unittest discover -s tests -p 'test_*.py' -v > /private/tmp/agent-market-tests-20260909-sizing-final.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m compileall -q nmsim experiments tests
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl PYTHONHASHSEED=0 python3 -m experiments.repro_check
git diff --check
```

```text
Ran 671 tests in 153.658s
OK
compileall: no output, exit 0
PASS — identical price history across 3 processes with different PYTHONHASHSEED
 history length: 25 points; final price: 83.480000
git diff --check: no output, exit 0
```

新增测试覆盖条件支持/参数、test payload 隔离、公开/私有输出、跨题标签泄漏、
历史输入目录保护、旧默认结果及一股持仓的订单/成交区别。第一次全套为
670 tests / 149.413s；补一股固定 fixture 和修正跨题导出后又完整执行了上面的
671 项，不把旧结果冒充新快照验证。环境缓存和测试输出使用临时目录；运行
后 inventory 中没有额外的默认输出失败目录。测试的 fake endpoint 文本不是
真实 Teacher 结果。现有第三方 matplotlib/pyparsing 的弃用提示未导致测试失败。

## 7. 兼容、安全和剩余工作

本次科学语义是**新增可选买卖量分布及新的人类重建任务**，不是“完全没有
机制新增”。旧 prompt、动作 Student、默认买卖量、撮合、融资和整数规则不变。
原默认市场结果逐字段兼容；reporting 允许的新字段是公开的 sizing 描述。

输出与历史输入重叠时，在创建任何成功或失败 run 前拒绝，包括 symlink 和
非法 CLI 参数路径。真实采集仍在独立的 `rollout-fidelity-live` worktree、
干净 `b95aca4` 上运行；没有因本次代码更新而重启、补样或修改其 manifest。
以后新采集的运行时 `network_access` 会在首次请求前立即标记为 true；运行中
的旧版本只在收尾同步，不能把它的暂时 false 当作没有调用。

原 a10 manifest `33605f7c50424156fa77e2576cc915fedc1d0cbe9ffc49474be8df105c00bf6f`
和 42/42 artifacts、10k manifest
`3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4`
和 7/7 artifacts 再次通过。相对保存的 archive receipt，bytes/mode/mtime 均未变。
备份、raw results、私人 reasoning、原人类记录不进入 Git。

既有 Higgs watchdog 已只扩展到这次确切的 live run，检查结果为
`scope=active vpn=connected route=present tcp=ok action=none`。
旧脚本 byte-preserving 副本保存在私有 `private_backups/higgs_watchdog/`
下，以 SHA `82f81d12b04c8e030f7ec13bd2e747992dda1c102b532e50002253fb5d71e920`
寻址。没有改 VPN 凭据、路由设置或其他 VPN。

下一步优先读取完成后的 120 请求对照，再决定是否以及在哪里补 Teacher
标签。当前探针是旧均值 Student 的 24 个 rollout 状态，不能自动验证新增
分布产生的不同状态。之后做已冻结模型的同任务人类比较；真实市场制度匹配、
社会信息控制、组成识别和第二独立介质备份仍未完成。目标没有被缩成“代码全绿”。
