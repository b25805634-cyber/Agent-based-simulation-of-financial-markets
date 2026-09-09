# 真实 Teacher 对照完成：2026-09-09

## 结论先说

120 条真实回答全部完成，没有失败或重试。小模型在这些闭环状态上明显
偏向卖出；新的买卖量分布改善了分布评分，但没有解决动作方向偏差。
这批数据不是人类数据，也不能证明模拟市场像真人。

下一项已经从冻结协议启动：用 195 个重建的六资产任务比较 Teacher 与
39 位公开研究参与者的历史选择。它与当前单资产市场分开解释，不会把
两种任务的成绩混在一起。该采集仍在运行，尚无完整的真人比较结论。

## 1. 已完成的真实端点对照

`rollout-fidelity-live-20260909-a1` 从独立、干净的 `b95aca4` worktree 执行：
2026-09-09 10:08:20.716512 至 10:51:06.451236 UTC，约 42 分 46 秒。
24 个预先选定的旧均值 Student rollout 状态，每状态 K=5，共 120 个
logical requests、120 physical attempts、120 valid endpoint responses。
全部 `finish_reason=stop`，报告模型别名为 `HiggsAI`，失败/未完成/重试均为 0。
模型别名符合协议，不等于独立认证了底层权重。

Teacher 动作是买 16、停 28、卖 76。下面对同一批回答评价原来已经冻结的
三个模型，没有重新训练、校准或选择：

| 原模型 | 动作交叉熵 | 平均状态 TV 距离 |
| --- | ---: | ---: |
| MLP（原先选中的模型） | 0.774145 | 0.233477 |
| linear | 0.775704 | 0.250140 |
| prior | 0.985896 | 0.330051 |

MLP 与 linear 的交叉熵很接近；没有据此进行选模或显著性宣称。
24 状态中 8 个状态的五次动作完全相同，其他状态有变化；K=5 的经验概率
仍然有噪声，且请求可能共享端点和时间效应。

在这些**选定状态**上，MLP 平均买/停/卖概率为
6.052% / 13.424% / 80.524%；Teacher 观察比例为
13.333% / 23.333% / 63.333%。不能把这 24 状态的差异当成全部市场的
总体估计，但它说明原 Student 在这里有偏卖问题。

原离线 test CE 是 0.382743，当前是 0.774145。两套状态分布不同，不能
直接说同一个测试分数“退步了一倍”。可以说：静态测试没有保证自身 rollout
状态上的保真度。旧 test 已披露，不再用于下一轮选择；本次诊断一旦用于
开发，也不能继续伪装成未见确认集。

## 2. 买卖量分布的独立评分

用同一批已完成的回答、原 Student 和此前冻结的 sizing study 做离线分析。
新评分本身调用端点 0 次、拟合 0 次、选模 0 次；`selected` 保留此前验证集
选择，因此与 conditional 数值相同，不是第四个独立模型。

仅在 Teacher 实际买入或卖出时评价对应强度：买入 16 条、卖出 76 条，
总计 92 条。28 个 hold 没有被改造成强度零的买卖样本。

| 每条有效买卖回答等权 | 原均值头 | 经验分布 | 条件分布 |
| --- | ---: | ---: | ---: |
| 全部买卖 CRPS | 0.189859 | 0.147162 | 0.097566 |
| 买入 CRPS，N=16 | 0.068882 | 0.053971 | 0.050673 |
| 卖出 CRPS，N=76 | 0.215328 | 0.166781 | 0.107438 |
| 预测均值 MAE，N=92 | 0.189859 | 0.200843 | 0.169724 |
| 全卖事件 Brier，N=76 | 0.276316 | 0.223628 | 0.117269 |

CRPS 是分布评分，不是交易准确率。改善不能表述成“准确率翻倍”。
负结果保留：买入均值 MAE 从原来的 0.068882 变为 0.078527；按六个
有买入回答的状态等权时，经验分布 CRPS 0.048426，反而低于条件分布
0.051301。卖出涉及 22 个状态。完整报告同时保存 response-weighted 和
equal-case 分母，不挑有利的加权方式。

这些回答来自旧均值策略走到的状态，**并未验证新抽样策略的整个轨迹**。
另外已经冻结一个新计划，从实际抽样市场选择 24 个状态，其中 11 个空仓、
13 个有持仓；它计划 120 请求但尚未执行。某个空仓状态的原 Student 仍有
约 13.93% 的卖出概率。这不是实际超卖（市场仍会拒绝无持仓订单），而是
模型对不可执行动作分配了概率，需要单独处理和对照，不能静默改掉旧模型。

## 3. 真人任务比较的准备与启动

新协议 `human-early-teacher-k1-shuffled-contexts/0.1` 固定 195 × K1，
请求按 SHA 固定打乱，每题独立会话，不把整套任务或后来历史给模型。
源数据是 39 位历史参与者、13 组、195 个六资产联合选择；这批确有 195 个
不同 prompt/state hash，但不等于 195 个独立人或 195 条独立市场路径。

195/195 fake null 全流程通过；真实模式 dry-run 的 endpoint requests 为 0。
fake 的 56.923% 行动匹配只是“一律不交易”基线，macro recall 为 1/3。
不能把 fake null 叫做 Teacher 成绩。

真实采集 `human-early-teacher-live-20260909-a1` 从新的独立、干净
`.worktrees/human-early-live`、提交 `b763c0c2c0f3a7b2d1f96968bc62599cabcba9fb`
启动，workers=2，计划上限恰为 195 个逻辑请求。原 120 请求进程已正常退出，
没有重启或拼接。已有的 Higgs watchdog 仅增加对此确切 run ID 的识别，
仍每 20 秒检查活动实验、每 15 分钟记录心跳，尊重 PAUSED 和冷却规则。

公开输出包括 Teacher-valid 同子集的 hold-null、3×3 混淆表、每类 recall、
缺类 null 与 macro recall。失败仍计入 coverage，不当成 hold。
完整 task bank、逐题回答、rationale 和含逐题失败的比较都为 0600。
原始响应文字的 SHA 和脱敏持久副本的 artifact SHA 分开命名。

原 UI 的信息保留方式仍有假设；这是条件在真人过去历史上的重建任务，
不是稳定个体的自主闭环，也不是当前单资产市场的人类验证。

## 4. 产物与精确身份

以下路径相对 `.worktrees/agent-market-loop`。除仍在运行的真人任务采集外，
均已完成、验证注册 artifacts，并核验输入未变。新的离线评分和计划从干净
`b763c0c2c0f3a7b2d1f96968bc62599cabcba9fb` 执行。

| 目录前缀 / run ID | manifest byte SHA-256 | 验证 |
| --- | --- | --- |
| `results_rollout_fidelity/runs/rollout-fidelity-live-20260909-a1` | `85ca26d2baaf0817aecc9f5dd4fbe6cc011dd21d561ce94266dafc5a3089f003` | 9/9 |
| `results_rollout_sizing_scores/runs/rollout-sizing-scores-20260909-a1` | `5901bd227ae7e37f386c19c1820a38edde575a5ae7eb8cd241622a78c31ac89f` | 7/7 |
| `results_rollout_fidelity/runs/rollout-sizing-probe-plan-20260909-a1` | `c6f77bbcbbedc41b52f75d83dbce47b7e75b77b93c6aacd4e960b6aeb588e779` | 6/6 |
| `results_human_early_teacher/runs/human-early-teacher-plan-20260909-a1` | `eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc` | 7/7 |
| `results_human_early_teacher/runs/human-early-teacher-fake-20260909-a1` | `e366f10a021d75e0febb389712bdb415e9783a904617c6d31bea562fae96c4de` | 11/11 |
| `results_human_early_teacher/runs/human-early-teacher-live-dry-20260909-a1` | `71d5da39abb04c1a487eae5b3ab80bd3608c50a6ed486d9bcb17677d25e23c64` | 7/7 |

```text
Completed real probe — rollout-fidelity-identities/1.0
scientific_config_hash: dab611ba17da18bc5e9aa1ba149c0b5dcc7e9caae64c20a61c6cf4f36d06f26c
model_request_config_hash: 6852fdda456d3d8889ae422a4c5d8a128ef663fe62a0c944437c8038f5e7fdc1
execution_config_hash: a790fce44fc5907e030126b078f5fb3b5a4ef991c03760ca6cbbb44af55bce39
full_effective_config_hash: 5ffcb1aa41c23744b7c581750cc0819e9425997107d3438e0d907aba690bd4f0
plan_hash: 2b7d53cd54c9cc11637b5ffae682a946fba236dc47ead40db8878a24f47fb690

Offline scores — rollout-sizing-scores-experiment/1.0
scientific_config_hash: fe820f74319a8614f31bc2cd97d23d11f0dbfff009194e0d38471cdcababf1f6
model_request_config_hash: e0446522a06b4d416fe6688aa9068878d2f72e9a4eec54c7926120e40f5ee8b2
execution_config_hash: 4c8561d20e69cf7fdb8bf2d31811e9778bd300a7ee643e27a692ef4010956724
full_effective_config_hash: 87c592e3dde95349e63bdbacaa6818a04c343a1ef7df1641872b85ad3bf17864
score_semantic_hash: 12dd1bc9f086049a86e49112eed0902fb96c20e204c439cc9bd08e59f66cbf89

New sizing plan — rollout-fidelity-identities/1.0
scientific_config_hash: 1f9957b2ec3ad3a77f1bf0bc477c175707b12879ff999aa12b98c2ab33d1fa8d
model_request_config_hash: 5dadd812e1b8770d20c2ed474f694b43af15c3b7ab67d1f534f4eba79126aafd
execution_config_hash: fca890c875325410192fe5b10654ce7f6814b1ca51ec691c6086a35a58c578e5
full_effective_config_hash: 331ac9c3ca4c1b87ac9d0b5a591292826de23507ed66dfd8108484461a71998c
plan_hash: caa73d37af5dc42338657a7e101d9205cfdeeeeaa4517ad40d575cc64d3d676d

Human plan — human-early-teacher-identities/0.1
scientific_config_hash: 11688f93cef0b20c84afe53f5b9baa480503d22c4b7f4bd37cc1936b22dd7450
model_request_config_hash: 81c9702d31c4d329d44eb3f1cb2eab05f0164fee10d0260d8b8a7853a46911cc
execution_config_hash: 59ac809537621ea8acd50c91b36d2f042199ccc445e1b7b663f2438da420a487
full_effective_config_hash: 0b47283ab47cf5758a871956cb0423f32eb39d6d954927f7743239531381ad0c
plan_hash: 77e49bd86cf89aa019b04c9d98bc413fb1f369a7455da1892336f731f8a90ae1
```

Fake、dry 和 real 共用 human scientific_config_hash，但其请求/执行身份
不同；不能把 fake 的零请求身份或 dry 的 full hash 当作 real 的身份。
全部具体配置和四类身份在各自 `identities.json`；真实 human run 的 manifest
仍在更新，这里不把其中间 byte SHA 当成最终认证。

真实 120 回答的内容寻址备份已保存到工作区根目录：
`private_backups/agent_market/rollout_fidelity/85ca26d2baaf0817aecc9f5dd4fbe6cc011dd21d561ce94266dafc5a3089f003/`。
共 10 文件、2,376,771 bytes；9/9 artifacts 和两个 private 文件 0600
通过独立临时恢复验证，原 bytes/mode/mtime 未变。第二独立介质仍未配置。

## 5. 命令与验证

以下为实际有效 argv；真实调用的路由/凭据仅通过进程环境传入，不公开其值。
旧 120 的 cwd 是 `.worktrees/rollout-fidelity-live`，新 human real 的 cwd 是
`.worktrees/human-early-live`；其余命令 cwd 为 `.worktrees/agent-market-loop`。

```sh
python3 -m experiments.rollout_fidelity --task acquire --plan-run /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_rollout_fidelity/runs/rollout-fidelity-plan-20260909-a1 --plan-manifest-sha256 3611a9eda44418ba82becbeb6c1e8eda3f4469c6d69497b2955505cfd443f4c8 --provider openai --live --confirm-request-count 120 --workers 2 --run-id rollout-fidelity-live-20260909-a1 --out /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_rollout_fidelity
```

exit 0；120 resolved / 120 valid / 120 physical attempts；unresolved 0；
failure_counts `{}`；human_participants 0。终端 stdout 与 manifest 均一致。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.rollout_sizing_scores --source-run results_rollout_fidelity/runs/rollout-fidelity-live-20260909-a1 --source-manifest-sha256 85ca26d2baaf0817aecc9f5dd4fbe6cc011dd21d561ce94266dafc5a3089f003 --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --run-id rollout-sizing-scores-20260909-a1 > /private/tmp/rollout-sizing-scores-20260909-a1.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.rollout_fidelity --task plan --market-run results_information_market/runs/information-market-sizing-sampled-20260909-a1 --market-manifest-sha256 83619446c8102e553ebd6ca92c8daae3f9651076b085c103287fb296eee6c9e8 --model-run results_information_market/runs/information-student-10k-20260909-a1 --model-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 --distribution-manifest-sha256 a798f9941a43f96ed1ce3eb0f2e01562bf37990c347fb358dcecfcd76280b5d9 --max-states 24 --replicates 5 --run-id rollout-sizing-probe-plan-20260909-a1
```

分别 exit 0，real 0.40s / 13.66s；评分 92 个 non-hold 回答，计划 24 状态；
两条命令 new_teacher_requests 均为 0。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.human_early_teacher --task plan --source-run results_information_diagnostics/runs/human-early-tasks-20260909-a1 --source-manifest-sha256 5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286 --run-id human-early-teacher-plan-20260909-a1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.human_early_teacher --task acquire --plan-run results_human_early_teacher/runs/human-early-teacher-plan-20260909-a1 --plan-manifest-sha256 eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc --provider fake_test_teacher --run-id human-early-teacher-fake-20260909-a1 > /private/tmp/human-early-teacher-fake-20260909-a1.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl /usr/bin/time -p python3 -m experiments.human_early_teacher --task acquire --plan-run results_human_early_teacher/runs/human-early-teacher-plan-20260909-a1 --plan-manifest-sha256 eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc --provider openai --dry-run --run-id human-early-teacher-live-dry-20260909-a1
python3 -m experiments.human_early_teacher --task acquire --plan-run /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_human_early_teacher/runs/human-early-teacher-plan-20260909-a1 --plan-manifest-sha256 eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc --provider openai --live --confirm-request-count 195 --workers 2 --run-id human-early-teacher-live-20260909-a1 --out /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_human_early_teacher
```

前三条 exit 0，real 0.23s / 0.98s / 0.19s，均为零实际端点请求；最后一条
是真实采集，尚未结束，不能报告为“195 条已完成”。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m unittest discover -s tests -p 'test_*.py' -v > /private/tmp/agent-market-tests-20260909-fidelity-human.log 2>&1
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl python3 -m compileall -q nmsim experiments tests
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache MPLCONFIGDIR=/private/tmp/agent-market-mpl PYTHONHASHSEED=0 python3 -m experiments.repro_check
git diff --check
```

```text
Ran 717 tests in 160.981s
OK
compileall: no output, exit 0
PASS — identical price history across 3 processes with different PYTHONHASHSEED
 history length: 25 points; final price: 83.480000
git diff --check: no output, exit 0
```

## 6. 边界

动作基线表另做如下只读描述性计算，不生成新的研究 run，不进行选择：

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache python3 - <<'PY'
import math,statistics
from pathlib import Path
from nmsim.information_artifacts import read_json,verify_run
from nmsim.information_student import make_predictor
source=Path('results_rollout_fidelity/runs/rollout-fidelity-live-20260909-a1')
model=Path('results_information_market/runs/information-student-10k-20260909-a1')
verify_run(source,'85ca26d2baaf0817aecc9f5dd4fbe6cc011dd21d561ce94266dafc5a3089f003')
verify_run(model,'a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af')
cases={c['case_id']:c for c in read_json(source/'probe_plan.json')['cases']}
summary=read_json(source/'fidelity_summary.json')
for name,payload in read_json(model/'student_study.json')['models'].items():
    predict=make_predictor(payload);loss=0;count=0;tv=[]
    for row in summary['cases']:
        c=cases[row['case_id']];p=predict(c['visible_fields'],c['account_state'])['action_probs']
        loss+=sum(-n*math.log(p[('buy','hold','sell').index(a)]) for a,n in row['teacher_action_counts'].items())
        count+=row['valid_replicates'];tv.append(sum(abs(a-b) for a,b in zip(p,row['teacher_empirical_probs']))/2)
    print(name,'N',count,'CE',round(loss/count,6),'TV',round(statistics.mean(tv),6))
PY
```

输出：linear N 120 CE 0.775704 TV 0.25014；mlp N 120 CE 0.774145
TV 0.233477；prior N 120 CE 0.985896 TV 0.330051。这里的 N 是端点回答数。

新增的是冻结模型的离线评分、验证新 sizing 轨迹的计划输入，以及独立的
六资产人类任务比较协议。没有更改原 Student 权重、旧 prompt、market
clearing、融资、默认 CLI 或历史 schema。原 probe plan/1.0 不变；新的
sizing 输入必须显式绑定，不能覆盖已冻结的 acquisition 参数。所有正式
入口走 registry 和 ManagedRunContext；测试用 fake/纯函数而非真实 Provider。

下一步仍是完成真人比较、针对已发现的方向/边界误差做明确的新协议和新
验证，而不是继续使用已看的诊断选一个最好看的版本。真实市场制度匹配、
社会信息控制、长期监控界面、组成可识别性及第二独立备份仍未完成。
项目目标没有完成，也不因代码或测试全绿而自动完成。
