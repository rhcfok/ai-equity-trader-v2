---
name: daily-trading-briefing
description: 每个交易日整合 J Law 双管线（chart + yahoo，Supabase trade-jlaw-v2）与 UW-option-breakout 的 flow verdict，产出中英双语交易决策简报（Regime 判定 + A/B 级候选 + 触发价/止损/目标 + 回避名单），写入本地 md 并同步 Notion。触发：每日简报 / 盘前决策 / daily briefing / 交易决策简报 / 更新简报。
---

# daily-trading-briefing：每日交易决策简报

把 jlaw 双管线结果与期权流判读整合成一份可直接挂单执行的双语简报。

## 输入与上游依赖

- Supabase `trade-jlaw-v2`：当日 `yahoo` 行 + 最新 `chart` 行（chart 常滞后一天，属正常；10-10 起已修复 null/错位问题）。
- **flow verdict**：来自 `UW-option-breakout` skill 产出的 `flow_verdicts_<date>.json`。若不存在，先运行该 skill。
- 自行拉数可用 `UW-option-breakout/scripts/pull_jlaw.py`。

## 工作流

### Step 1 — 拉取与合并候选

- chart 侧：classification = **Valid** 全部 + score ≥ 12 的 Watch
- yahoo 侧：score ≥ 10 的 Watch（yahoo 很少出 Valid；**零 Valid 本身是市况信号**）
- 标记**双管线一致**的标的（两边都正面），权重上调
- 每个候选记录：symbol、score、current_price、pivot/entry、stop、stop_pct、rrr、target_price、vcp_stage、classification、来源管线
- 同一标的 chart 与 yahoo 参数不一致时，**止损取 chart 侧**（通常更紧；案例：AMT chart stop 176 vs yahoo 158.7）

### Step 2 — 叠加 flow verdict

读取 `flow_verdicts_<date>.json`，对每个候选映射：

- `bullish` → 通过第三重确认
- `bearish` → **直接列入回避**，无论 chart 分数（FORM 案例）
- `protective` / `covered-call-selling` → 降级为"观察"，注明原因
- `mixed` / `no-data` → 中性，不加分不否决
- `earnings_block: true`（财报 < 2 周）→ **不建仓**（BAC 案例）；财报 2–4 周 → 注明"财报前减仓/收紧止损"

### Step 3 — Regime 判定

- yahoo 零 Valid + market tide 防御 → **谨慎做多**，正常仓位减半
- chart Valid 数骤降（如 3 → 1）→ 简报头部注明"进一步收紧"
- yahoo 出 Valid 且 tide 偏多 → 正常仓位

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

1. 写 `briefing_<date>.md` 到工作区（覆盖当日旧版），结构：Regime → A 级（参数表）→ B 级（汇总表）→ 回避/冲突（含原因）→ 执行要点 → 数据来源
2. **同步 Notion**：页面 `J Law 双管线 × Unusual Whales 期权流 — 交易决策简报`（id `3f5b8852-cf00-8035-916d-d12bae2a3dd2`，位于 `ai-trader-v2-kimi` 下）。用 notion-update-page 把整个简报包在一个 `<details>` 折叠块里；更新当日简报用 `replace_content` 整体替换，历史日期的折叠块保留不动
3. 回复用户给精简表格版 + 文件链接 + "研究分析非投资建议"

## 已知坑

- 表名 `trade-jlaw-v2` 带连字符；日期列是 `run_date`；`.env` base 若已含 `/rest/v1` 不要重复拼
- chart 行 entry_price 历史上有过 % 错位 bug（10.4 应为 104），使用前目检异常值；空 entry/stop 多为"无 pivot"修复期图表，不要凭空填
- chart 标签只降级不升级 yahoo 结论；硬冲突以 chart 严格方为准
- Yahoo 无数据的非美股品种（AIR、AVEX 等）持续缺席属预期
- 曾见数据源错价疑点（如 NOW 现价 140.86 异常），发现后在简报中标注
- Notion 表格必须用 `<table>` 增强 Markdown 语法，不能用标准 markdown 表格

## 样例

- `briefing_2026-10-10.md`（工作区）— 首份按本 skill 产出的双语简报（含 10-10 chart 更新版）
