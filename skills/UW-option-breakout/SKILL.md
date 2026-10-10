---
name: UW-option-breakout
description: 用 Unusual Whales 期权流对候选标的做第三重确认/否决，产出结构化的 flow verdict（bullish / bearish / mixed / protective / no-data + 关键证据），供 daily-trading-briefing 等下游 skill 消费。触发：期权流确认 / option flow overlay / UW 确认 / flow verdict / option breakout 确认。
---

# UW-option-breakout：Unusual Whales 期权流叠加分析

**职责边界**：本 skill 只做"期权流 → 结构化判读"，**不写交易简报、不做分级、不给买卖点**。简报产出归 `daily-trading-briefing` skill，二者通过 flow verdict JSON 衔接。

## 输入

- 候选标的列表（通常来自 jlaw 双管线：chart Valid/高分 Watch、yahoo 高分 Watch），每条至少含 symbol。
- 可选：regime 判定需要的 `get_market_tide`。

## 前置条件

- Unusual Whales MCP 工具可用（`get_flow_alerts`、`get_market_tide`）。
- Supabase 可访问 + `.env` 含小写 `supabase_url` / `supabase_service_role`（仅当需要自行拉 jlaw 候选时用 `scripts/pull_jlaw.py`）。

## 工作流

### Step 1 —（可选）拉取 J Law 候选

```bash
python scripts/pull_jlaw.py --out .
```

产出 `briefing_yahoo.json` / `briefing_chart.json`（最新一个交易日的全部行）。候选提取规则由调用方决定；常用口径：chart Valid 全部 + chart/yahoo score ≥ 12 的 Watch。脚本已内置 100s timeout + 5 次指数退避重试（Supabase REST 偶发 RemoteDisconnected 属已知现象）。注意日期列名是 `run_date`。

### Step 2 — 逐标的拉期权流

对每个候选调用 `get_flow_alerts(ticker_symbol=<symbol>, newer_than=<5 个自然日前>, limit=20)`。

### Step 3 — 判读规则（每条流按下表归类）

| verdict | 判据 | 参考案例 |
|---|---|---|
| **bullish** | 近月（≤6 周）call 在 ask 侧成交、sweep/floor 单、RepeatedHits/AscendingFill、单笔 premium ≥ 15 万美元、vol/OI ≥ 3 | ANET 10-30 225C sweep $626K；CRDO 10-23 210C $309K ask 侧 + sweep |
| **bearish** | 流几乎全是 put 且有大额 bid 侧 put（尤其 DescendingFill）→ 无论 chart 分数多高都应否决做多 | FORM 11-20 140P $326K bid 侧 DescendingFill |
| **protective** | 远期（≥1 年）put 大额 ask 侧 = 保护盘，信号降级但不否决 | CSCO 2027-09 100P $470K；NVDA 11-20 215P |
| **mixed** | call + put 对冲并存，方向不明 | 不加分不否决 |
| **covered-call-selling** | 远端深度虚值 call 大额 bid 侧（疑似备兑/看顶），不改近月结论但需标注 | CRDO 2027-02/03 380C/390C ~$1.18M bid 侧 |
| **no-data** | 流为空或样本太少无法判读 | 非美股/低流动性标的常见 |

**同时记录**（下游简报要用）：
- 每笔关键大单：方向、行权价、到期日、premium、ask/bid 侧、rule（RepeatedHits / Sweep / Floor 等）
- `next_earnings_date`（flow alerts 返回里自带；J Law 硬规则：earnings < 2 周不建仓，2–4 周财报前减仓）
- 财报 < 2 周的标的直接标 `earnings_block: true`（参考：BAC 财报前 4 天否决）

### Step 4 — regime 辅助信号

`get_market_tide`（最近交易日；注意非交易日会报错，用上一交易日即可）：午后 net call premium 持续深度为负 = 防御 regime，写入输出的 `market_tide` 字段。

## 输出

写 `flow_verdicts_<date>.json` 到工作区：

```json
{
  "date": "2026-10-10",
  "market_tide": {"date": "2026-10-09", "bias": "defensive", "note": "午后 net call premium 约 -$130M"},
  "verdicts": [
    {
      "symbol": "CRDO",
      "verdict": "bullish",
      "caveats": ["covered-call-selling"],
      "key_flows": [
        {"expiry": "2026-10-23", "strike": "210C", "premium_usd": 309000, "side": "ask", "rule": "RepeatedHits", "sweep": true, "vol_oi": 1.6}
      ],
      "next_earnings_date": "2026-12-07",
      "earnings_block": false
    }
  ]
}
```

## 已知坑

- OI 变化需次日确认，vol/OI 高 ≠ 新开仓，描述时保持"unconfirmed"口径。
- 远端深度虚值 call 的 bid 侧大单通常是卖出（备兑或看顶），不要当成看多证据。
- 非交易日 `get_market_tide` 会报错，回退到上一个交易日。
