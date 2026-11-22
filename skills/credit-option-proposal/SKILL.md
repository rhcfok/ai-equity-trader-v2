---
name: credit-option-proposal
description: 产出信用策略期权交易提案（short-date Top-10 全量榜 + SP/SC/BPS/BCS 分策略 Top-5 置信度提案，全部为卖方收信用结构）。基于 em-probability（gaussian-drift-off-v1）排序，六层证据加权置信度（jlaw / flow-verdict / unusual flow / dark pool / quant2 / stldc），估值为参考列不否决。触发：期权提案 / option proposal / credit 提案 / credit option proposal / 分策略 Top-5 / short-date 提案。（原名 option-proposal，2026-11-22 更名）
---

# credit-option-proposal：信用策略期权交易提案

从 daily-trading-briefing 拆分而来（2026-11-22）。市场状态与方向性计划见 [market-briefing](../market-briefing/SKILL.md)。**全部只读：无订单、无 DB 写入（em_score 重算除外，见运行顺序）。**

## 输出

- `top10_reference_<batch>.json/md`：最新已评分批的 Top-10 全量榜（flow conflict / bearish-vs-put 门 + STLDC / quant2 / 估值参考列）
- `part4_v2.md`：**SP/SC/BPS/BCS 分策略 Top-5 置信度提案**（六层证据加权 + 扫描分列 + Max loss/RoR），可直接作 Notion toggle / builder 的 `--income-board-markdown`

## 运行顺序

```bash
# ① 若最新批未评分（fetch 会打印 unscored_batches）：先重算 EM（写入 em-probability，幂等）
#    ⚠️ 时序规则：必须在 em-model-snapshot 当日刷新之后跑，否则 σ 滞后（看 sigma_as_of 判断，勿看 scored_at——upsert 不刷新它）
python skills/em-expected-move/scripts/em_score.py

# ② Top-10 全量榜（fetch-latest：自动定位最新已评分批 / verdicts / stldc / quant2 / 暗池 / 异动流 / 估值，只读 GET + 分页）
python skills/credit-option-proposal/scripts/shortdate_top10.py --indir <outdir>

# ③ 分策略 Top-5 置信度提案（估值=参考列不否决；含 watchlist-signal-board scan score 参考列）
python skills/credit-option-proposal/scripts/part41_strategy_top5.py --indir <outdir> --out <outdir>/part4_v2.md
#    --offline 模式：复用已有快照（日期自动从 em_scores.json / flow_reconcile_*.json 推断）
```

依赖：`shortdate_data.py`（共享 fetch 模块）；flow_reconcile.py 通过跨技能路径调用（本技能目录 → daily-trading-briefing/scripts 兜底）。

## 置信度权重（win-rate 分级标定，sess_bacf128e · 窗口 2026-10-08→11-13 下跌市）

| 证据层 | 实测 | 权重 |
|---|---|---|
| jlaw Watch（多头侧） | yahoo Watch 61.6%/67.4% 最强；chart Watch 41.4% 反向 → 未分管道折中 | ±2.0 |
| flow-verdict bearish（空头侧）confirmed/partial | 64.8% 5d / 75% 10d（+12~28pts 最强） | +3.0 / +1.5 |
| flow-verdict bullish（多头侧）confirmed/partial | 40.0% 5d 低于 base | +0.5 / +0.25 |
| STLDC CONFIRM 顺势 fresh/stale | chained 56.9%（+2.6pts）；fresh 57.1% > stale 53.5% | +1.5 / +0.75 |
| STLDC 逆势 CONFIRM / ALERT | BEAR-CONFIRM 46.3%；ALERT 无 edge | −1.0 / −0.25 |
| dark pool 顺势 ≥$100M / 逆势 | 未独立分级 → 协同 | ±1.0 / ±0.5 |
| unusual flow \|z\|≥1 顺势/逆势 | 未独立分级 → 协同 | ±1.0 / −1.0 |
| quant2 rank≤20 / ≤100 | not_validated | +1.0 / +0.5 |
| flow conflict | 不可作单向结论 | −2.0（出局）|

方向对齐：SP/BPS 按多头证据、SC/BCS 按空头证据；逆向证据取负。**scan score（watchlist-signal-board）为参考列，不进置信度权重。**

## 门与纪律

- flow `conflict` → 出局；verdict `bearish` → 卖 put 方向（SP/CSP/WHEEL_CSP/BPS）出局；BCS（空头结构）与 bearish 同向允许；SC 与 BEAR-* 同向允许。
- 估值（fair_value / buy_below + Rijnberk reference_*）= **参考列**；不构成否决（2026-11-11 用户指示）。
- 批次/vintage 约定与全部数据坑：见 daily-trading-briefing/SKILL.md「短日期提案任务」与「已知坑」节（credit 单位、collateral 坏列、SHORT_PUT 命名缺口、BCS max_loss 负值、UW profile 403 → watchlist 表核验交易所、REST 1000 行分页等）。
- 报价均为 dated snapshot → 入场前重询 NBBO；无订单、无写入（em_score 重算除外）。

## Notion

页面：J Law 双管线 × Unusual Whales（id `3f5b8852-cf00-8035-916d-d12bae2a3dd2`）。新当日顶层 `<toggle>`（纯 `<toggle>`，不带 heading 属性；`insert_content` position=end）；与 market-briefing 的完整报告 toggle 分开，不混排。
