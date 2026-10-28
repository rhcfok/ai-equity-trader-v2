---
name: daily-trading-briefing
description: 每个交易日整合 J Law 双管线（chart + yahoo，Supabase trade-jlaw-v2）与 UW-option-breakout 的 flow verdict，产出中英双语交易决策简报（Regime 判定 + A/B 级候选 + 触发价/止损/目标 + 回避名单），写入本地 md 并同步 Notion。触发：每日简报 / 盘前决策 / daily briefing / 交易决策简报 / 更新简报。
---

# daily-trading-briefing：每日交易决策简报

把 jlaw 双管线结果与期权流判读整合成一份可直接挂单执行的双语简报。

## 输入与上游依赖

- Supabase `trade-jlaw-v2`：当日 `yahoo` 行 + 最新 `chart` 行（chart 常滞后一天，属正常；10-10 起已修复 null/错位问题）。
- **flow verdict**：从 Supabase 交接表 `"flow-verdicts"` 读取（由 agent B / UW-option-breakout 写入）。本机工作区的 `flow_verdicts_<date>.json` 只是本地缓存，不是权威来源。
- 自行拉数可用 `UW-option-breakout/scripts/pull_jlaw.py`。
- **市场脆弱度仪表盘 Market Fragility Dashboard**：`https://vincizh.github.io/market-fragility-dashboard/`（每日 ~07:19 UTC 更新，运行时可能读到前一日快照——引用页面上的 as-of 日期）。作为 Regime 段的跨检输入，见 Step 3。

### Step 0 — 签收 flow verdict（agent A 侧校验规则）

读 `"flow-verdicts"` 表当日行，按顺序校验：

1. **存在性**：`verdict_date` = 最近交易日的行存在
2. **完整性**：`status = 'complete'`；若为 `partial`，每 5 分钟重查一次，最多 3 次
3. **新鲜度**：`updated_at` 在 24 小时内
4. **覆盖率**：`rated_count` = `candidates_total`（`no-data` verdict 计入 rated）；不足视为 partial（2026-10-13 起 agent B 已全量覆盖，`quota_exhausted` 已废除，残留 `reason='uw_error'` 的 unrated 属 API 失败）
5. **schema**：`payload` 含 `verdicts` / `unrated` / `market_tide` 三个键
6. **降级路径**：超时仍未 complete → 按 `unrated` 口径跑简报（A/B 级只含已有 verdict 的标的），简报头部注明"flow 数据不全，结论偏保守"

## 工作流

### Step 1 — 拉取与合并候选

- chart 侧：classification = **Valid** 全部 + score ≥ 12 的 Watch
- yahoo 侧：score ≥ 10 的 Watch（yahoo 很少出 Valid；**零 Valid 本身是市况信号**）
- 标记**双管线一致**的标的（两边都正面），权重上调
- 每个候选记录：symbol、score、current_price、pivot/entry、stop、stop_pct、rrr、target_price、vcp_stage、classification、来源管线
- 同一标的 chart 与 yahoo 参数不一致时，**止损取 chart 侧**（通常更紧；案例：AMT chart stop 176 vs yahoo 158.7）

### Step 2 — 叠加 flow verdict

读取签收后 `payload` 中的 `verdicts`，对每个候选映射：

- `bullish` → 通过第三重确认
- `bearish` → **直接列入回避**，无论 chart 分数（FORM 案例）
- `protective` / `covered-call-selling` → 降级为"观察"，注明原因
- `mixed` / `no-data` → 中性，不加分不否决
- `earnings_block: true`（财报 < 2 周）→ **不建仓**（BAC 案例）；财报 2–4 周 → 注明"财报前减仓/收紧止损"

### Step 3 — Regime 判定

- yahoo 零 Valid + market tide 防御 → **谨慎做多**，正常仓位减半
- chart Valid 数骤降（如 3 → 1）→ 简报头部注明"进一步收紧"
- yahoo 出 Valid 且 tide 偏多 → 正常仓位

**市场脆弱度跨检（Market Fragility Dashboard，2026-10-22 起）**：用 WebFetch 拉取仪表盘，提取 LPI 合成（档位/ΔLPI 趋势/尾部风险倍数）、funding 共振腿（x/3、SOFR−IORB、reserves 及 reserves/GDP 分位、后备工具用量）、信用离散（HY OAS、CCC−BB 分位与 z 值）、利率极值（30Y、term premium 分位）、放大器堆栈（dealer gamma 正负、basis 代理、CTA 状态、去杠杆计数）。然后：

- 在 Regime 段"解读 Read"行之前织入 **3–4 条中英双语要点**（LPI / funding / 信用与久期 / 放大器），引用页面 as-of 日期
- "解读 Read"行尾追加一句与 tide 的交叉判读（如：脆弱度 ELEVATED 但 funding 未确认 → 谨慎不转向）
- 与期权流 tide 方向相悖时（如 tide constructive 而 SPY verdict 翻 bearish）显式标注背离
- 拉取失败则跳过该块并在简报中注明，不阻塞出报

### Step 4 — 分级

| 级别 | 条件 | 处理 |
|---|---|---|
| **A 级** | chart Valid + flow bullish + earnings ≥ 2 周 | buy-stop，全额（regime 谨慎时减半） |
| **B 级** | chart 高分 Watch（≥12）+ flow 不反对 + earnings 安全 | buy-stop 等触发；止损 >10% 减半仓；RRR < 0.5 标注"可跳过" |
| **回避** | flow bearish / earnings_block / 硬冲突 | 不做多，注明原因 |

**每个 A/B 级标的必须给出**：
- 触发价 = pivot/entry 上方约 0.1%（buy-stop）
- 止损 + 百分比距离
- 第一目标 = entry + RRR × (entry − stop)，或 chart 的 target_price
- earnings 日期与纪律
- 期权流关键证据（引用 flow verdict 的 key_flows）
- 分数变化（与上一版简报对比，如 15 → 12 ⬇️；旧触发价作废须显式声明）

**通用纪律（每份简报必写）**：
1. 全部 buy-stop，不触发不进
2. 止损即纪律，不摊平
3. 财报前一日减仓一半或收紧至成本
4. 标注数据时效（chart/yahoo 各自 run_date）
5. 失效条件：tide 连续两日深度 call 净流出或 SPY 破关键均线 → 全部降一档

### Step 5 — 双语产出 + 同步

**格式要求：中英双语**。章节标题双语（如 `## 2. A 级：三重确认 | Tier A: Triple-Confirmed`）；表格表头双语（`触发 Trigger`）；长段落中文一行、英文一行对照。

1. 写 `briefing_<date>.md` 到工作区（覆盖当日旧版），结构：Regime（含脆弱度跨检块）→ A 级（参数表）→ B 级（汇总表）→ 回避/冲突（含原因）→ 执行要点 → 数据来源
2. **同步 Notion**：页面 `J Law 双管线 × Unusual Whales 期权流 — 交易决策简报`（id `3f5b8852-cf00-8035-916d-d12bae2a3dd2`，位于 `ai-trader-v2-kimi` 下）。**2026-10-21 起解析器弃用 `<details>/<summary>`**（与 `<table>` 组合会被拒绝："bare <tr> is not valid"）——改用规范 `<toggle>` 语法：`<toggle>` 首行为标题（如 `📊 交易决策简报 <date> Trading Decision Briefing — ...`），随后空行，正文，`</toggle>`。当日重跑时用 `update_content` 定点替换**仅当日**的 toggle 内容（old_str 从当日 `notion-fetch` 原样复制；存储序列化为 `<b>` 粗体、`\$`/`\|`/`\<` 转义、多行 `<td>` 单元格；优先用无格式纯文本尾锚定位）；解析失败回退：先插最小探针 toggle，fetch 读其序列化形式再定点展开。历史日期的折叠块保留不动
3. 回复用户给精简表格版 + 文件链接 + "研究分析非投资建议"

## 已知坑

- 表名 `trade-jlaw-v2` 带连字符；日期列是 `run_date`；`.env` base 若已含 `/rest/v1` 不要重复拼
- chart 行 entry_price 历史上有过 % 错位 bug（10.4 应为 104），使用前目检异常值；空 entry/stop 多为"无 pivot"修复期图表，不要凭空填
- chart 标签只降级不升级 yahoo 结论；硬冲突以 chart 严格方为准
- Yahoo 无数据的非美股品种（AIR、AVEX 等）持续缺席属预期
- 曾见数据源错价疑点（如 NOW 现价 140.86 异常），发现后在简报中标注
- Notion 表格必须用 `<table>` 增强 Markdown 语法，不能用标准 markdown 表格
- **Notion 解析器 2026-10-21 变更**：`<details>/<summary>` 已弃用（自动转 `<toggle>`），与 `<table>` 组合直接报错 "bare <tr> is not valid"；一律写 `<toggle>`（标题在首行 + 空行 + 正文）。页面存储序列化为 `<b>` 粗体、`\$`/`\|`/`\<` 转义、多行 `<td>` 单元格——update_content 的 old_str 须原样复制；优先用纯文本尾锚（句尾无格式片段）定位
- 脆弱度仪表盘：LPI 为周度合成（滞后数日）、页面 ~07:19 UTC 更新，引用时带 as-of 日期；卡片偶见未来日期 quirk 与个别 stale 源，读数时计入

## 样例

- `briefing_2026-10-10.md`（工作区）— 首份按本 skill 产出的双语简报（含 10-10 chart 更新版）

---

## Part 3 · 期权收入计划 | OPTIONS INCOME PLAN（v4 收入规则 income rules，2026-10-28 用户确认生效 / effective next run）

> 本节为 Part 3 收入计划的权威规则（source of truth）；cron 提示词引用本节。*This section is the source of truth for the income plan; the cron prompt references it.*

- **目标 Target**：**$20-30k/月 ≈ $7-8k/周 credit**（70-80% 来自 credit）。达成路径：①裸 put 账本修复释放保证金；②BPS/BCS 定义风险乘数；③11-20/12-18 月度 IV 溢价层；④CC 存量收益（另列，见硬规则 2）。*Path: repair the naked-put book, defined-risk multi-leg scaling, monthly IV layers; CC income tracked separately.*

### 3.0-A DTE 规则 | DTE rules
- **CSP / short call / strangle：DTE ≤ 20 天**。
- **Credit Spread（BPS/BCS）：DTE < 35 天**（月度 IV 溢价可用 / monthly IV premium allowed）。
- 月度到期（11-20/12-18 型）仅用于 spread / diagonal 空腿；短腿到期清标的财报 ≥2 天；**FOMC / 巨头发报夜不加新裸 gamma**（BPS/BCS 定义风险可做）。
- 超限例外（>20 DTE 的 CSP/strangle）：三重背书（技术 + dp 干净/吸筹 + GEX put wall 在 strike 上方）**加 Rijnberk 估值锚（CSP strike ≤ equity_valuation 的 buy_below / fair_value）**。*Exceptions need triple backing plus the Rijnberk valuation anchor.*

### 3.0-B 提案板 | Proposal board（每周 20 张合约 20 contracts/week）
- 每周提案 **20 张合约**；**同一标的同时最多 2 张（same ticker ≤2）**；20 张合计 credit **≈ $7-8k/周**。
- 来源叠加 source stack：option-davis / csp-bps (manus) / uw-option-opportunities 板 + **Tier B 方向确认提案**（Part 1' chart+flow 确认标的）+ **估值锚 CSP（Rijnberk `equity_valuation` × GEX put wall；CSP strike ≤ buy_below / fair_value）**。
- **每张合约必须携带：置信度 Confidence（高 High / 中 Medium / 低 Low）+ 一行理由 rationale**。评级 rubric：
  - **高 High**：verdict bullish/确认 ≥2 日 + dp 干净或吸筹 + 板排名靠前或估值锚；
  - **中 Medium**：单日确认 / mixed 但 dp 支持 / alerts 5-20；
  - **低 Low**：样本 <5 alerts、贴线门槛、疑似数据（RRR 0 / stop <1%）→ **Low 为可跳过 optional/skippable**。
- **硬规则 Hard rules**：① 同一标的 ≤2 张；② **现有持仓的 covered call 不入提案**（持仓管理收益，另列 tracked separately）；③ dp 重度派发（buy%<25 且 net<−$100M）否决做多与卖 put；④ FOMC / 发报夜不加裸 gamma；⑤ 防御 regime 下 short strangle 否决（只做指数 iron condor）；⑥ 疑似数据行（RRR 0 / stop <1%）不挂单。

### Post-print re-strike queue（财报后重设 strike 队列）
每次财报后：dp 复核 → CSP/BPS 按 post-print GEX wall 与 Rijnberk 估值锚重设 strike；dp 派发名单维持否决；verdict 翻转的名单先进观察再恢复。*After each print: dp re-check → re-strike anchored to post-print GEX wall and valuation; distribution names stay vetoed; flipped verdicts re-enter via watch.*

### 周度预算与节奏 | Weekly budget & cadence
20 张合约合计 **≈ $7-8k/周 credit**；月度 ≈ **$20-30k**（含 11-20/12-18 月度 IV 层与滚仓 credit）。节奏：周一建板（核对 marks）→ 周中事件前停手 → 周四按决议/发报刷新加层 → 周五到期/滚仓。防守：2x credit 或短腿 touch 即撤；21 DTE / 2x credit 管存量；任一失效触发（tide 连续深流出 / NDX 破 55DMA×3 / SOXX 破 500×2 / LPI>60）→ 停新开仓。*Build Monday → stand down before events → layer Thursday → expire/roll Friday; defense rules as listed.*
