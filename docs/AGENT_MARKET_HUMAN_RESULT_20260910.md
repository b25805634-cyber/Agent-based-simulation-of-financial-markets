# 真人任务 Teacher 对照结果：2026-09-10

## 结论

在冻结的重建任务协议上，MiniMax-M2.7 Teacher **没有表现出足以称为
“像真人”的匹配结果**。这不是对所有市场、所有模型或所有人的普遍定理；
它是一个明确的负结果，适用于这份公开六资产实验数据、前五个 paid
rounds、给定的信息窗口和当前 prompt。不能用它支持“LLM 已经模拟真人”。

## 运行事实

运行 ID：`human-early-teacher-live-20260909-a1`。使用干净提交
`b763c0c2c0f3a7b2d1f96968bc62599cabcba9fb` 的独立 worktree，固定计划
`human-early-teacher-plan-20260909-a1`，39 位公开研究参与者、13 个 peer
groups、195 个六资产联合任务，K=1，每题独立请求。

| 单位 | 数量 |
| --- | ---: |
| 计划 / 尝试 / resolved logical requests | 195 / 195 / 195 |
| physical endpoint attempts | 195 |
| provider responses | 194 |
| 合法 joint decisions | 193 |
| provider failure | 1 |
| invalid joint response | 1 |
| new human participants | 0 |

失败不被改成 hold，两个失败任务保留在 coverage 统计中。195 个请求全部
完成处理，但“complete”表示请求账本闭合，不表示科学假设通过。

## 与历史真人选择的比较

有效配对分母是 193 个 joint decisions，即 1,158 个 stock opportunities。
Teacher 与历史选择的完整联合决策只有 **4/193 = 2.073%**。

| 指标 | Teacher | 不交易 hold-null |
| --- | ---: | ---: |
| stock action agreement | 48.446% | 57.081% |
| exact joint agreement | 4/193 | 25/193 |
| mean absolute signed-share error | 13.9249 | 2.3005 |
| model buy / hold / sell actions | 221 / 574 / 363 | 0 / 1,158 / 0 |
| human buy / hold / sell actions | 383 / 661 / 114 | 383 / 661 / 114 |

Teacher 相对于真人明显更常卖出（363 对 114），而真人的 sell action 只占
9.84%，Teacher 为 31.35%。Teacher 并非简单地“一律 hold”，但其买卖方向
和数量仍与真人历史选择差异很大。

按六资产行动的 3×3 混淆表（行是真人，列是 Teacher）如下：

| human \\ Teacher | buy | hold | sell | human support | recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| buy | 99 | 169 | 115 | 383 | 25.849% |
| hold | 95 | 390 | 176 | 661 | 59.002% |
| sell | 27 | 15 | 72 | 114 | 63.158% |

宏平均 recall（仅三个实际出现的真人类别）为 49.336%。同一 193-task
分母上的 hold-null 为 0% / 100% / 0%，宏平均 33.333%，股票动作匹配
57.081%。因此 Teacher 在动作分类上比 hold-null 的某些指标更有信息，
但仍没有达到可称为人类行为复现的标准；本项目没有预先规定一个“通过线”，
也不在看到结果后发明一个。

## 解释边界

这是 Liêu–Pelster 的公开六资产实验任务，不是当前单资产、内生价格的市场。
价格序列虽已从公开资料和终值方程恢复，但原始 UI 是否始终保留练习价格、
如何显示历史记录以及生产 joint-budget validator 仍未完全验证。任务允许
同一次提交中的卖出收入资助买入，这是显式的新重建假设。

因此结果可以说：当前 Teacher 在一个明确的真人历史任务上，与记录选择的
方向和数量匹配较差，且有明显 sell 偏置。不能说：模型完全没有任何人类
特征、所有 LLM 都如此、真人数据本身错误，或当前市场一定会产生某种价格
曲线。39 人、13 组、单一公开实验条件也不是人口总体；K=1 更不能估计
同状态的 Teacher 随机分布。

所有逐题历史、Teacher response、private rationale、失败细节和完整 task
bank 均保存在私有 0600 artifacts 中；GitHub 只上传本报告和代码，未上传
这些内容。

## 产物与身份

真实运行 manifest：

```text
e892a0b4b20daab229ffde9ed22db4bfea97c2aeea12527a55ed049317253007
registered artifacts: 11/11; regular files: 12; total bytes: 10,501,017
plan_hash: 77e49bd86cf89aa019b04c9d98bc413fb1f369a7455da1892336f731f8a90ae1
source task-plan manifest: eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc
source human-task manifest: 5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286
scientific_config_hash: 11688f93cef0b20c84afe53f5b9baa480503d22c4b7f4bd37cc1936b22dd7450
model_request_config_hash: 8fa70a411f33af865bc0c433c229f16c749a98ece76d8d3070f6f728f2aab652
execution_config_hash: 92072a3e7095df16405e3ded95231fd474410b6ba82fa37024b885db2bad04b7
full_effective_config_hash: ee598957c6e8a48581585568e611261ec95517831b94e2ad56b83293cd0df23a
```

私有备份：
`private_backups/agent_market/human_early_teacher/e892a0b4b20daab229ffde9ed22db4bfea97c2aeea12527a55ed049317253007/`。
备份和独立临时 restore drill 均通过，源文件 bytes/mode/mtime 未变；第二
独立介质仍未配置。

## 确切命令和验证

```sh
PYTHONPYCACHEPREFIX=/private/tmp/human-early-live-pycache \
MPLCONFIGDIR=/private/tmp/agent-market-mpl \
python3 -m experiments.human_early_teacher --task acquire \
  --plan-run /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_human_early_teacher/runs/human-early-teacher-plan-20260909-a1 \
  --plan-manifest-sha256 eb0785c1c5346db72ee17da137aa9bc410a3f8140f27fecb80cb387d98a252cc \
  --provider openai --live --confirm-request-count 195 --workers 2 \
  --run-id human-early-teacher-live-20260909-a1 \
  --out /Users/aldrich/Desktop/agent模拟市场/.worktrees/agent-market-loop/results_human_early_teacher
```

运行结束：exit 0，195/195 requests resolved，193 valid，1 provider failure，
1 invalid joint response。真实请求使用固定 MiniMax-M2.7、temperature 1、
top-p .95、top-k 40、JSON object、max_tokens 190000、HiggsAI alias、stop
termination、one application attempt、zero SDK retries；这些配置身份与
execution hash 分开保存，真实响应不声称完全确定。

```sh
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-pycache \
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q nmsim experiments tests
PYTHONHASHSEED=0 python3 -m experiments.repro_check
git diff --check
```

代码验证结果：717 tests，160.981s，OK；compileall exit 0；repro_check
通过；git diff --check 无输出。真实运行目录另经 `verify_run` 核验 11/11
registered artifacts，所有 `private_*` 文件为 0600。

## 下一步

不要把这批结果硬塞进当前 Student 的单资产训练集，也不要通过 RAG/行为
金融学术语把 Teacher 的 sell 偏置解释成人类效应。先冻结一个新的任务
对照协议，分别审查 action feasibility、资金约束、方向偏置和数量分布；
再决定是收集更多同任务人类选择、改用一个中性状态生成器，还是继续维持
当前模型作为明确的“非人类基线”。市场层的空仓非法动作质量、社会信息
控制、长期闭环和组成识别仍是独立问题。

科学语义变更：本报告新增一个经过真实端点的负向 Teacher–human 对照
证据，没有修改旧 prompt、Student、市场结算、历史 runs 或已有人类标签。
