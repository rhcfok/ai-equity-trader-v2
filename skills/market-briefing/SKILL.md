---
name: market-briefing
description: 编译每日市场简报（Part 1 Regime / 2 情报对账 / 3 方向性计划含 3.0b 新晋阶段与 3.4 异动流暗池纪要 / 5 数据缺口 / 6 执行规则）——不含期权提案（期权提案由 credit-option-proposal 技能单独产出）。触发：市场简报 / market briefing / 每日简报（不含期权）。
---

# market-briefing：市场简报编译（Part 1/2/3/5/6）

从 daily-trading-briefing 拆分而来（2026-11-22）。**本技能只产出市场与方向性部分**；期权提案（Part 4）由 [credit-option-proposal](../credit-option-proposal/SKILL.md) 技能单独产出与发布。

## 输出

一份 markdown 报告（Notion-flavored `<toggle>` 包裹），含：
- Part 1 Regime（确定性评分 0-100 + 五支柱 + playbook）
- Part 2 情报（substack 授权引用）/ 数据新鲜度 / 对账总览 / 冲突登记册 / 交叉证据
- Part 3 方向性计划：Tier A/B 摘要 · 3.0a STLDC 四组名单与多源比较 · **3.0b 近 7 日新晋 BULL-ALERT/CONFIRM** · 3.1 A 级 · 3.2 B 级执行板 · **3.4 异动流与暗池纪要**
- Part 5 数据缺口 · Part 6 执行规则 · 数据源表
- Part 4 仅留占位说明（指向 credit-option-proposal 技能）

## 运行

```bash
export SUPABASE_URL=$(grep -E '^SUPABASE_URL=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")
export SUPABASE_SERVICE_ROLE_KEY=$(grep -E '^SUPABASE_KEY=' .env | cut -d'=' -f2- | tr -d '"' | tr -d "'")

# ① 数据签收 + 对账（写 flow_reconcile_<date>.json，只读）
python skills/daily-trading-briefing/scripts/flow_reconcile.py --date <rd> --out <outdir>

# ② Regime 评分（先 WebFetch 脆弱度仪表盘取 LPI 档位 / funding legs / as-of；只读）
python skills/daily-trading-briefing/scripts/regime_score.py --date <rd> \
  --fragility-tier <LOW|NORMAL|ELEVATED|HIGH|CRITICAL> --funding-legs <0-3> \
  --fragility-as-of <date> --dump-inputs --out <outdir>
#    ⚠️ 不加 --write-supabase

# ③ 编译报告（--no-options：不含期权提案，无需 income board 输入）
python skills/daily-trading-briefing/scripts/build_daily_trading_plan_v2.py \
  --date <rd> --no-options --out <outdir> \
  --regime-json <outdir>/regime_<rd>.json \
  --reconcile-json <outdir>/flow_reconcile_<rd>.json \
  --fragility-notes <outdir>/fragility_dashboard_source.md

# ④ strict check（退出码必须 0）
python skills/daily-trading-briefing/scripts/flow_reconcile.py --date <rd> \
  --check-board <outdir>/daily_trading_plan_v2_<rd>_review.md --strict

# ⑤ Notion：insert_content / end 推送整个报告 toggle
#    ⚠️ 用纯 <toggle>（不带 heading="h1"，与页面历史块字号一致）；历史块绝不触碰
```

## 纪律

- 全程只读（GET）；strict check 非 0 不得发布；数据缺口只引用当期 reconcile 新鲜度表。
- builder 的 shared 依赖（flow_reconcile.py / regime_score.py / render_weekly_income_board.py）仍在 `skills/daily-trading-briefing/scripts/`，本技能按上述相对路径调用。
- regime 分数解读权在脚本；报告不得另起档位结论。详见 daily-trading-briefing/SKILL.md 的权重标定与数据约定。

## Notion

页面：J Law 双管线 × Unusual Whales（id `3f5b8852-cf00-8035-916d-d12bae2a3dd2`）。新当日顶层 `<toggle>`，`insert_content` position=end；标题跟随历史块命名 `📊 交易决策简报 <rd> Trading Decision Briefing — …`；用纯 `<toggle>` 不带 heading 属性。
