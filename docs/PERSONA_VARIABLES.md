# 人设变量协议(Persona Variables Protocol)v1.0 — 冻结规格

> 状态:FROZEN v1.0(2026-07-28,大脑裁决 PR #11 四个阻塞点后定稿)
> 本文件是 persona variable machinery 的唯一科学真源。任何变更需显式 bump 版本 + PR 声明。

## 0. 口径修正(对 PR #11 阻塞点 1)

规范变量总数 = **14 个新建变量 + 2 个继承变量**。
早前口头出现的「12」系计数错误(A1–A4=4、B1–B3=3、C1–C4=4、D1–D3=3,合计 14;E1/E2 为继承,不计入新建)。

## 1. 变量注册表(冻结)

格式:变量 id | 档位(档位 id: 自然语言片段)。渲染器按档位 id 取片段装配,不得改写措辞。

### A. 资金与仓位

- **A1 可投资资产**
  - `a1_2w`: 工作几年攒下的 2 万元积蓄
  - `a1_5w`: 多年攒下的 5 万元积蓄
  - `a1_10w`: 10 万元可投资资金
  - `a1_50w`: 50 万元闲置资金
- **A2 仓位集中度**(注入数值 X)
  - `a2_10` / `a2_30` / `a2_50` / `a2_70` / `a2_90`: 你把全部可投资资金中的 X% 投在了这只股票上
- **A3 浮亏深度**(成本 100 元)
  - `a3_0`: 买入成本 100 元,现价 100 元,基本持平
  - `a3_m10`: 买入成本 100 元,现价 90 元,浮亏 10%
  - `a3_m25`: 买入成本 100 元,现价 75 元,浮亏 25%
  - `a3_m50`: 买入成本 100 元,现价 50 元,浮亏 50%
- **A4 收入稳定性与现金流**
  - `a4_stable`: 你有稳定的工资收入,近期没有大额支出计划
  - `a4_unstable`: 你的收入不太稳定,时好时坏
  - `a4_needcash`: 半年内你需要用到这笔钱(有明确的大额支出计划)

### B. 信念结构

- **B1 买入依据类型**
  - `b1_research`: 这只股票是你自己花了几周时间研究财报和行业后决定买入的
  - `b1_trend`: 这只股票之前一直在涨,你是顺势而为买入的
  - `b1_peers`: 你身边好几位朋友同事都在买这只股票,都说好,你跟着买入的
  - `b1_media`: 你是在新闻和分析师一致看好中买入这只股票的
- **B2 信念深度**
  - `b2_1`: 关于这只股票,你只听说过个大概
  - `b2_3`: 你看过几篇关于这只股票的分析,大致了解
  - `b2_5`: 你曾认真写下过买入这只股票的理由,也想过什么情况下自己可能是错的
- **B3 信念可证伪性**
  - `b3_clear`: 你心里有明确的标尺:如果出现什么情况,你就承认自己错了并卖出
  - `b3_vague`: 你没有具体想过,什么情况会让你改变对这只股票的看法

### C. 心理特质

- **C1 亏损容忍度**
  - `c1_5`: 浮亏超过 5% 你就开始睡不好
  - `c1_15`: 浮亏超过 15% 你就开始睡不好
  - `c1_30`: 浮亏 30% 以内你都能承受
  - `c1_50`: 浮亏一半你也能扛得住
- **C2 认错成本(自尊绑定)**
  - `c2_low`: 在投资上承认错误对你不是难事,认错换股是常事
  - `c2_mid`: 承认错误会让你有些不舒服,但你通常能做到
  - `c2_high`: 承认自己做错了决定让你非常难堪,这只股票又是你坚持要买的
- **C3 持仓公开度**
  - `c3_private`: 你买这只股票的事没人知道
  - `c3_family`: 你买这只股票的事家里人知道
  - `c3_public`: 你买这只股票的事朋友们都知道,还常有人问起
- **C4 耐心与时间尺度**
  - `c4_days`: 一只股票几天没动静你就会开始烦躁
  - `c4_months`: 你愿意拿几个月看看
  - `c4_years`: 你习惯按年持有

### D. 注意力与信息行为

- **D1 盯盘频率**
  - `d1_intraday`: 开盘时间你几乎一直看盘,一天看好多次
  - `d1_daily`: 你每天收盘后看一次
  - `d1_weekly`: 你一两个星期才看一次
- **D2 替代机会敏感度**
  - `d2_low`: 你只关心自己手里这只股票,别的股票涨跌你不太在意
  - `d2_mid`: 你会留意别的股票涨得好不好
  - `d2_high`: 你总在比较哪只股票涨得好,很怕错过别的机会
- **D3 媒体信任度**
  - `d3_trust`: 你大体相信新闻和专家的判断
  - `d3_half`: 你对新闻半信半疑,会和自己的判断对照
  - `d3_self`: 你只相信自己的研究

### E. 继承变量(不新建,由现有引擎派生)

- **E1 社交易感性** ← 继承 persona 的 `social_susceptibility`,不进入 render 文本
- **E2 表达欲/广播行为** ← 由 persona 类型继承(influencer=高频广播,其余=低),不进入 render 文本

## 2. 基准向量(Reference Human,冻结)

除被翻转的变量外,一切实验固定在:

```
A1=a1_10w, A2=a2_50, A3=a3_0, A4=a4_stable,
B1=b1_trend, B2=b2_3, B3=b3_vague,
C1=c1_15, C2=c2_mid, C3=c3_private, C4=c4_months,
D1=d1_daily, D2=d2_mid, D3=d3_half
```

## 3. 现有 6 persona 的变量空间坐标(冻结)

| persona | A1 | A2 | A3 | A4 | B1 | B2 | B3 | C1 | C2 | C3 | C4 | D1 | D2 | D3 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| retail_crowd | a1_5w | a2_70 | a3_m10 | a4_stable | b1_peers | b2_1 | b3_vague | c1_15 | c2_mid | c3_family | c4_months | d1_intraday | d2_high | d3_half |
| fomo_momentum | a1_10w | a2_50 | a3_0 | a4_stable | b1_trend | b2_1 | b3_vague | c1_5 | c2_low | c3_private | c4_days | d1_intraday | d2_high | d3_trust |
| value_institution | a1_50w | a2_30 | a3_0 | a4_stable | b1_research | b2_5 | b3_clear | c1_50 | c2_low | c3_private | c4_years | d1_daily | d2_low | d3_self |
| contrarian_fund | a1_50w | a2_30 | a3_m25 | a4_stable | b1_research | b2_5 | b3_clear | c1_30 | c2_low | c3_private | c4_months | d1_daily | d2_low | d3_self |
| influencer_amplifier | a1_10w | a2_30 | a3_0 | a4_stable | b1_media | b2_3 | b3_vague | c1_15 | c2_high | c3_public | c4_days | d1_intraday | d2_mid | d3_trust |
| quant_arb | **不适用** — 机构角色由显式规则驱动,不经过人设渲染(待导师确认,暂冻结为保持现有 quant_arb 不动) |

坐标是「映射点」,仅供对照与区域采样;不改动 6 个 persona 的任何现有定义与 prompt 文本。

## 4. 新增 fixtures(独立成册,冻结)

**裁决(PR #11 阻塞点 4):新 fixtures 放入新文件 `qualification/persona_fixtures.json`,bundle 版本 `persona_fixtures 1.0`,独立 input_hash/bundle_hash。资格认证的 8 fixtures、48-case 宇宙、Wave0-T1 全部哈希保持不变,一个字节不动。**

四个 fixture 的场景内容(渲染为 Observation 时按现有 fixture schema 字段展开):

- **F1 `sideways_2y_hot_peers`**:持有股票 24 个月,价格在 98–103 元区间横盘;同期科技板块 18 个月上涨约 45%,新闻与邻居发言都在讨论热门股票大涨;本股无任何利空。
- **F2 `deep_loss_bad_news`**:成本 100 元,现价 50 元(浮亏 50%);突发利空:公司下修全年业绩指引 20%;邻居公开情绪 −0.6。
- **F3 `public_holding_friends_ask`**:成本 100 元,现价 75 元;你曾在聚会中多次向朋友推荐这只股票;本周先后两位朋友问你「还拿着吗?」;无新消息。
- **F4 `belief_source_pair`**:同一标的一对版本——(a) 你花三周研究财报后买入;(b) 饭局上听朋友极力推荐后买入;随后出现中度利空:核心产品销量增速放缓。

fixture-变量映射:F1→D2(兼 C4)、F2→A3×C1、F3→C2×C3、F4→B1×B2。其余变量在通用利空观察(可复用 qualification 的 `negative_news_price_unchanged`,只读引用,不复制不改写)上翻转。

## 5. 翻转测试协议(冻结)

- 每变量:最低档 vs 最高档(连续型取两端),其余变量固定为 §2 基准向量,fixture 按 §4 映射;
- **K=30**/档(CLI 可调),复用 K-repeat 分布式推断;
- 主指标:`P(sell)`(卖出决策占比);辅助指标:sentiment 均值、signed order 均值;
- **判定阈值(预注册)**:|ΔP(sell)| ≥ 0.15 **且** bootstrap 95% CI(2000 次重采样,percentile)不含 0 **且** 方向与 §6 预测一致 → 「有效」;否则「空转候选」;
- fake/mock 输出仅为 orchestration null control,不得作为行为证据。

## 6. 方向预测(预注册,判定的一部分)

| 变量 | 预测 |
|---|---|
| A1 | 资产规模对 P(sell) 的方向**本身存疑**(亏不起→死扛 vs 更慌→跑);诚实处理:此变量判定降级为「位移显著」即可,方向开放,结果写入讨论 |
| A2 | 仓位越重,利空下 P(sell) 越高 |
| A3 | 浮亏越深,P(sell) 越低(处置效应) |
| A4 | 现金流压力越大,P(sell) 越高 |
| B1 | 利空下 P(sell):peers/trend > research |
| B2 | 信念越深,P(sell) 越低 |
| B3 | 可证伪性明确,对匹配坏消息 P(sell) 越高 |
| C1 | 容忍度越低,P(sell) 越高 |
| C2 | 认错成本越高,P(sell) 越低 |
| C3 | 持仓越公开,P(sell) 越低 |
| C4 | 耐心越短,横盘场景 P(sell) 越高 |
| D1 | 盯盘越频,噪声场景 P(sell) 越高 |
| D2 | F1 场景下,替代敏感越高,P(sell) 越高(核心预测) |
| D3 | 媒体信任越高,媒体利空下 P(sell) 越高 |

## 7. render() 硬规则

1. 输出 120–300 字第一人称处境描述;
2. **违禁词表(单测扫描,命中即 fail)**:散户、机构、庄家、价值投资者、趋势交易者、处置效应、损失厌恶、从众、羊群、锚定、过度自信、「请表现出」、「你应当」、「你需要扮演」;
3. 不出现变量 id、档位数值表、本协议任何元信息;
4. 同 theta 同 seed → 逐字节一致。

## 8. 不变量声明

本协议仅新增模块与 fixtures 册;不改 `nmsim/prompts.py`、不改 6 persona、不改市场/社交/风控语义、不改既有 CLI 与结果 schema、不动 `results_multi_event/` 与运行中的 live 进程。
