---
name: UW-option-breakout
description: 整合 J Law 双管线（chart + yahoo，Supabase trade-jlaw-v2）与 Unusual Whales 期权流，产出可直接执行的交易决策简报（A/B 级候选、触发价、止损、目标、回避名单）。触发：交易简报 / decision briefing / 期权流确认 / option breakout / UW 简报 / 盘前决策。
---

# UW-option-breakout：J Law × Unusual Whales 交易决策简报

把两个独立分析管线（jlaw-v2-chart、jlaw-v2-yahoo）的结果，用 Unusual Whales 期权流做第三重确认，输出分级的、可直接挂单执行的交易简报。

## 前置条件

- Supabase 项目可访问，表 `trade-jlaw-v2` 已有当日的 `yahoo` 行和最近交易日的 `chart` 行（由 jlaw-v2-yahoo / jlaw-v2-chart 两条管线各自 upsert）。
- 工作区 `.env` 含小写 `supabase_url` 与 `supabase_service_role`。
- Unusual Whales MCP 工具可用（`get_flow_alerts`、`get_market_tide`、`get_upcoming_earnings` 等）。

## 工作流（按顺序执行）

### Step 1 — 拉取 J Law 双管线数据

运行 `scripts/pull_jlaw.py`（依赖：requests；读工作区 `.env`）：

```bash
python scripts/pull_jlaw.py --out .
```

产出：
- `briefing_yahoo.json`：最新一个 yahoo 交易日的全部行
- `briefing_chart.json`：最新一个 chart 交易日的全部行（chart 通常滞后一天，属正常）

脚本已内置 100s timeout + 5 次指数退避重试（Supabase REST 偶发 RemoteDisconnected 属已知现象）。

### Step 2 — 合并候选名单

从两个 JSON 里提取：

- chart 侧：classification 为 **Valid** 的全部 + score ≥ 12 的 **Watch**
- yahoo 侧：score ≥ 10 的 **Watch**（yahoo 很少出 Valid；零 Valid 是市况信号，见 Step 4）
- 标记**双管线一致**的标的（两边都正面，如 AMZN 型），权重上调

每个候选记录：symbol、score、px、pivot、stop、stop_pct、rrr、classification、来源管线。

### Step 3 — Unusual Whales 期权流叠加

对每个候选调用：

1. `get_flow_alerts(ticker, newer_than=<5 个自然日前>)` 取约 20 条
2. 判读规则：
   - **偏多确认**：近月（≤6 周）call 在 ask 侧成交、sweep/floor 单、RepeatedHits、单笔 premium ≥ 15 万美元、vol/OI ≥ 3
   - **偏空反对**：流几乎全是 put 且有大额 bid 侧 put（尤其 DescendingFill）→ 直接列入回避，无论 chart 分数多高（参考案例：FORM）
   - 远期（≥1 年）put 大额 ask 侧 = 保护盘，信号降级但不否决（参考案例：CSCO、NVDA 的 215P）
   - 混合流（call + put 对冲并存）= 中性，不加分不否决
3. `get_market_tide` 最近交易日：午后 net call premium 持续为负 = 防御 regime
4. `get_upcoming_earnings` 或直接查每只财报日：
   - **earnings < 2 周 → 不建仓**（J Law 硬规则，参考案例：BAC 财报前 4 天否决）
   - earnings 2–4 周 → 可建仓，但简报应注明"财报前减仓/收紧止损"

### Step 4 — 分级输出

**Regime 判定（写进简报头部）**：
- yahoo 侧零 Valid + market tide 午后 call 净流出 → **谨慎做多**，正常仓位减半
- yahoo 出 Valid 且 tide 偏多 → 正常仓位

**分级**：

| 级别 | 条件 | 处理 |
|---|---|---|
| **A 级** | chart Valid + 期权流偏多确认 + earnings ≥ 2 周 | buy-stop 挂单，全额（regime 谨慎时减半） |
| **B 级** | chart 高分 Watch（≥12）+ 期权流不反对 + earnings 安全 | buy-stop 挂单，等触发；止损 >10% 减半仓 |
| **回避** | 期权流明确反对（大额 put 主导），或 earnings < 2 周 | 不做多，观望 |

**每个 A/B 级标的必须给出**：
- 触发价 = pivot 上方 ~0.1%（buy-stop）
- 止损 = chart 侧 stop（注明百分比距离）
- 第一目标 = pivot + RRR × (pivot − stop)，或前高
- earnings 日期与对应纪律
- 期权流关键证据（单笔大额、方向、行权价、到期日）

**通用纪律（写进每份简报）**：
1. 全部 buy-stop，不触发不进
2. 距 pivot >5% 不追
3. 止损不摊平
4. 财报前一日减仓一半或止损收紧至成本
5. 标注数据时效（chart 行的实际日期）

### Step 5 — 写简报

输出 `briefing_<date>.md` 到工作区，结构：Regime → A 级（含参数表）→ B 级（汇总表）→ 回避/冲突（含原因）→ 执行要点 → 数据来源。回复用户时给精简表格版 + 文件链接 + "研究分析非投资建议"。

## 已知坑

- 表名 `trade-jlaw-v2` 带连字符，REST URL 直接用；`.env` 的 base 若已含 `/rest/v1` 不要重复拼。
- 日期列名是 `run_date`（不是 trade_date）。
- chart 管线的 `entry_price` 历史上有过 % 错位 bug（10.4 应为 104 之类），使用前目检异常小数值；空 entry/stop 多为"无 pivot"的修复期图表，不要凭空填。
- chart 标签只允许降级不允许升级 yahoo 结论；硬冲突（差 2 级以上）以 chart 严格方为准。
- Yahoo 无数据的非美股品种（AIR、AVEX 等）会持续缺席 yahoo 行，属预期，不必报警。

## 文件

- `scripts/pull_jlaw.py` — 从 Supabase 拉最新双管线行到本地 JSON
- `briefing_2026-10-10.md`（工作区样例）— 首份按本 skill 产出的简报，可作格式参照
