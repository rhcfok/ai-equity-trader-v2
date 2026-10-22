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

- Unusual Whales 数据可用。两条已验证通道：桌面 MCP 连接器，或直接调官方 MCP over HTTP（`https://api.unusualwhales.com/api/mcp`，JSON-RPC initialize + tools/call）与白名单 REST（`/api/option-trades/flow-alerts`、`/api/market/market-tide`、`/api/stock/{ticker}/earnings`），鉴权均为 `Authorization: Bearer $UW_API_KEY` + 头 `UW-CLIENT-API-ID: 100001`。key 取自仓库 `.env` 的 `UW_API_KEY`，绝不打印或落盘。 快照抓取参考实现：`scripts/mcp_fetch_snapshots.py`（含传输错误重试与缓存续传）。
- Supabase 可访问 + `.env` 含小写 `supabase_url` / `supabase_service_role`（仅当需要自行拉 jlaw 候选时用 `scripts/pull_jlaw.py`）。
- Python venv：uv 工具链可能缺失（其 trampoline 会失效报 "entity not found"）；重建用 `python -m venv .venv` + `pip install -r requirements.txt`（Python 3.12 实测可用）。

## 工作流

### Step 1 —（可选）拉取 J Law 候选

```bash
python scripts/pull_jlaw.py --out .
```

产出 `briefing_yahoo.json` / `briefing_chart.json`（最新一个交易日的全部行）。候选提取规则由调用方决定；常用口径：chart Valid 全部 + chart/yahoo score ≥ 12 的 Watch。脚本已内置 100s timeout + 5 次指数退避重试（Supabase REST 偶发 RemoteDisconnected 属已知现象）。注意日期列名是 `run_date`。

### Step 2 — 逐标的拉期权流

对每个候选调用 `get_flow_alerts(ticker_symbol=<symbol>, newer_than=<5 个自然日前>, limit=20)`。

### Step 2.5 — 全量覆盖策略（full coverage）

**每日目标：候选全覆盖 —— 每个候选当天都必须产出 verdict（含 no-data），不允许因配额主动跳过。** 自 2026-10-13 起取消原「每日 ≈20 次」上限（典型候选量 ~50，即 ~50 次调用 + 判读）。覆盖口径：未显式给出候选清单时，默认覆盖当日 `trade-jlaw-v2` 全部行的去重 symbol 集（含 Skip/低分行——它们通常判为 no-data，但同样计入 rated）。仍按优先顺序执行，以便中途失败时先保高优先级：

1. **双管线交集（P0）**：chart 与 yahoo 同一天都达标的标的（如 COP/MA/QQQ 型）——最强信号，最先查
2. **chart Valid（P0）**：全部 Valid，无论分数
3. **其余候选（P1）**：按分数降序（chart 分优先，并列时 yahoo 分高者优先）**全量查完，不打折**
4. **限流防护（必做）**：逐只调用之间 sleep 2–3s；遇 429/超时/限流错误对该标的指数退避重试（最多 3 次）。重试后仍失败的标的列入 `unrated`（`reason='uw_error'`，附错误摘要），交接行保持 `status='partial'` 并继续按 5 分钟间隔重查。**`quota_exhausted` 已废除——禁止作为主动跳过的理由**
5. **unrated 语义（仅限真实失败）**：`unrated` 数组只允许 `reason='uw_error'`（API 重试后仍失败）。流为空或样本太少应直接给 `no-data` verdict 并计入 rated，不进 unrated

双 agent 并行时按管线分摊（一台查 chart 候选、一台查 yahoo 候选），各自产出 flow verdict 后合并 JSON。

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

`get_market_tide`（最近交易日；注意非交易日会报错，用上一交易日即可）：午后 net call premium 持续深度为负 = 防御 regime，写入输出的 `market_tide` 字段。判读只看 ET≥13:00 的 afternoon 桶；**若默认调用只返回盘前/半日桶（afternoon 桶为空或 0 个），当日会话未收盘或返回了重标签的部分日——改用显式 `?date=<上一交易日>` 重取完整会话（正常为 81 桶、09:30–16:10）再判读，不要拿半日数据出 bias**。

## 输出

写 `flow_verdicts_<date>.json` 到工作区，**并 upsert 到 Supabase 交接表 `"flow-verdicts"`**（下游 agent A 从这里签收，不读本机文件）。完跑后**必须**再把 payload 解包成逐 symbol 行 upsert 到归一化表 `public.flow_verdict_rows`（见交接协议第 6 条），供下游 SQL 联表查询：

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
  ],
  "unrated": []
}
```

`unrated` 仅在 API 重试后仍失败时非空（`reason='uw_error'`，附错误摘要）；正常完跑必须是空数组。

### 交接协议（agent B 侧写入规则）

表 `"flow-verdicts"`：`verdict_date` (date, PK) / `payload` (jsonb，整个 JSON) / `status` ('partial'|'complete') / `candidates_total` / `rated_count` / `producer` / `updated_at`（自动刷新）。

1. **开跑即登记**：upsert 一行 `status='partial'`、`candidates_total`=当日候选总数——让 A 知道"在跑了"
2. **分批更新**：每查完一批标的就 upsert 更新 `payload` 和 `rated_count`（PK 是 verdict_date，重复写自动合并）
3. **完跑置位**：全部查完后 `status='complete'`，`rated_count` 定稿
4. **全覆盖兜底**：`rated_count` 必须 = `candidates_total`（`no-data` verdict 计入 rated），才可置 `complete`；若仍有 `reason='uw_error'` 的 unrated，保持 `partial` 并按 5 分钟间隔重查
5. REST upsert：POST `/flow-verdicts`，header 加 `Prefer: resolution=merge-duplicates`
6. **归一化行同步（完跑必做）**：status 置 `complete` 前，执行 `python scripts/upsert_flow_verdict_rows.py --json flow_verdicts_<date>.json`，把 `verdicts[]` 逐 symbol 解包并 upsert 到 `public.flow_verdict_rows`（PK `(verdict_date, symbol)`，重复执行自动合并、幂等）。表结构见 `sql/flow_verdict_rows.sql`——**首次使用前需在 Supabase SQL 编辑器执行一次该 DDL**。存量 blob 行可用 `python scripts/upsert_flow_verdict_rows.py --pull-from-supabase` 一次性回填（6 行历史 ≈ 1,100+ 条 symbol 行）。**upsert 后核对该日期行数 = `rated_count`，不一致不得报完跑**（2026-10-19→10-22 曾因漏跑本步导致归一化表停滞，下游 option-davis / uw-options-income-engine 读的是这张表）。

## 已知坑

- UW 限流：逐只调用间保持 2–3s 间隔；429/超时按指数退避重试（最多 3 次）。**不要为省调用主动跳过候选——全覆盖是硬要求（2026-10-13 起，原 ≈20 次/日配额已废除）**。
- **快照抓取的传输错误必须显式重试**：read 阶段的 `TimeoutError` 与 SSL `UNEXPECTED_EOF`（`URLError`）不会被 429/HTTP 状态码重试逻辑捕获——驱动需同时捕获这两类异常再退避（2026-10-16 实测两次中断均属此类，重跑即可从缓存续传）。
- OI 变化需次日确认，vol/OI 高 ≠ 新开仓，描述时保持"unconfirmed"口径。
- 远端深度虚值 call 的 bid 侧大单通常是卖出（备兑或看顶），不要当成看多证据。
- 非交易日 `get_market_tide` 会报错，回退到上一个交易日；默认调用返回半日/重标签数据时用显式 `?date=` 重取（见 Step 4）。
