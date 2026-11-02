---
name: daily-trading-briefing
description: 每个交易日用确定性 0–100 regime 评分（scripts/regime_score.py）并整合 J Law 双管线（chart + yahoo，Supabase trade-jlaw-v2）与 UW-option-breakout 的 flow verdict，产出中英双语交易决策简报（Regime 判定 + A/B 级候选 + 触发价/止损/目标 + 回避名单），写入本地 md 并同步 Notion。触发：每日简报 / 盘前决策 / daily briefing / 交易决策简报 / 更新简报。
---

# daily-trading-briefing：每日交易决策简报

把 J Law 双管线结果与期权流判读整合成中英双语、研究用途的交易决策简报；只生成条件性人工计划，绝不创建、传送或提交订单。

## 输入与上游依赖

- Supabase `trade-stldc-v2`（广度支柱）、`public.market_regime_daily`（regime 输出，见 Step 3）。
- Supabase `trade-jlaw-v2`：当日 `yahoo` 行 + 最新 `chart` 行（chart 常滞后一天，属正常；10-10 起已修复 null/错位问题）。
- **flow verdict**：从 Supabase 交接表 `"flow-verdicts"` 读取（由 agent B / UW-option-breakout 写入）。本机工作区的 `flow_verdicts_<date>.json` 只是本地缓存，不是权威来源。
- 自行拉数可用 `UW-option-breakout/scripts/pull_jlaw.py`。
- **市场脆弱度仪表盘 Market Fragility Dashboard**：`https://vincizh.github.io/market-fragility-dashboard/`（每日 ~07:19 UTC 更新，运行时可能读到前一日快照——引用页面上的 as-of 日期）。作为 Regime 段的跨检输入，见 Step 3。

### 期权数据来源优先级 | Options-data source hierarchy

- **Unusual Whales API / MCP 是期权主数据源。** 对每个候选在同一决策时点用它核验 U.S.-listed 标的、期权链、bid/ask（NBBO）、到期日/DTE、IV、OI、GEX、异常期权流、财报与宏观事件；无法证明 U.S. exchange 的标的一律排除。
- **Supabase 是流程与审计来源。** `flow-verdicts`、normalized flow、dark pool、GEX 快照与 Davis / CSP-BPS / UW opportunities 账本仅用于 reconciliation、历史证据和候选排序；账本 mark 绝不替代新的 UW 报价。
- **UW 数据不可用或关键字段不完整时 fail closed。** 省略受影响 ticket 或整板暂停；不得改用前一日的账本报价、不得伪造 live quote。
- **不得将 ORATS 作为默认或硬性门槛。** 只有用户明确要求迁移至 ORATS，且该连接和权限已独立验证时，才可把它列为额外 cross-check；它不阻断既定 UW 工作流。

### Step 0 — 签收 flow verdict（agent A 侧校验规则）

读 `"flow-verdicts"` 表当日行，按顺序校验：

1. **存在性**：`verdict_date` = 最近交易日的行存在
2. **完整性**：`status = 'complete'`；若为 `partial`，每 5 分钟重查一次，最多 3 次
3. **新鲜度**：`updated_at` 在 24 小时内
4. **覆盖率**：`rated_count` = `candidates_total`（`no-data` verdict 计入 rated）；不足视为 partial（2026-10-13 起 agent B 已全量覆盖，`quota_exhausted` 已废除，残留 `reason='uw_error'` 的 unrated 属 API 失败）
5. **schema**：`payload` 含 `verdicts` / `unrated` / `market_tide` 三个键
6. **降级路径**：超时仍未 complete → 按 `unrated` 口径跑简报（A/B 级只含已有 verdict 的标的），简报头部注明"flow 数据不全，结论偏保守"
7. **数据新鲜度 + 期权流对账（2026-10-29 起必跑）**：

```bash
python skills/daily-trading-briefing/scripts/flow_reconcile.py --date <date> --out .
```

输出 `flow_reconcile_<date>.md/.json`：(a) 各上游表最新日期与滞后工作日数（`flow_verdict_rows` / `"flow-verdicts"` / `option_flow_symbol_signals` / `darkpool_symbol_daily` / `gex_snapshot` / `trade-jlaw-v2` …）及暗池快照重复检测；(b) 每只标的的 verdict × 全量 alert 溢价（`verdict_quant` / `premium_tilt`）× Full Tape 溢价倾斜（今日 / 3 日）× robust `net_bias_z` × 暗池的对账，复用 income engine 的 `compute_flow_composite()`，状态 `confirmed` / `conflict` / `partial`。
**简报"数据缺口 Data gaps"只能引用该新鲜度表**，不得沿用前一日文字（教训：10-29 简报连续五天写 `option_flow_symbol_signals` "停更 10-13"，实际有 10-28 数据；agent 手写缺口导致 IV-aware Full Tape 信号被弃用）。

## 工作流

### Step 1 — 拉取与合并候选

- chart 侧：classification = **Valid** 全部 + score ≥ 12 的 Watch
- yahoo 侧：score ≥ 10 的 Watch（yahoo 很少出 Valid；**零 Valid 本身是市况信号**）
- 仅保留已确认在美国交易所上市的标的；非美国交易所、exchange 缺失或无法核验的标的不进入方向性或期权收入候选池。
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

**对账覆盖（读 `flow_reconcile_<date>.json`）**：
- "三重确认 Triple-confirmed" 只给 `status = confirmed` 的标的（verdict、综合 flow 标签、暗池三者同向）
- `status = conflict` 的标的（`verdict_vs_premium` / `darkpool_vs_options` / `verdict_vs_alert_premium`）**不得写成单一方向结论**：A 级降为 B 级、期权收入板仓位减半或跳过，并在"回避/冲突"段原样列出冲突类型（案例：10-28 MU verdict bearish 而 Full Tape 溢价 +0.74、META verdict bullish 而暗池 −$2.85B）
- `verdict_stale = true`（verdict 超 4 日）按 `no-data` 处理
- 引用 alert 数量用 `alert_count_total`（全量），不要用 `alert_count`（旧口径上限 20）

### Step 3 — Regime 判定（确定性评分 Deterministic score，2026-10-29 起）

Regime 不再靠文字判断，由 `scripts/regime_score.py` 输出 **0–100 综合分 + 档位 + 固定执行规则**。简报只解读分数，不得另起结论。

**3a. 先读脆弱度仪表盘**（WebFetch `https://vincizh.github.io/market-fragility-dashboard/`）：提取 LPI 档位（LOW / NORMAL / ELEVATED / HIGH / CRITICAL）、funding 共振腿 x/3、as-of 日期。拉取失败 → 不传参数，stress 支柱记为缺失（权重自动重分配），简报注明。

**3b. 运行评分**

```bash
python skills/daily-trading-briefing/scripts/regime_score.py \
  --date <date> --fragility-tier <LPI 档位> --funding-legs <x> --fragility-as-of <as-of> \
  --out . --write-supabase
```

产出 `regime_<date>.json`（完整明细）、`regime_<date>.md`（双语 Regime 段，Notion `<table>` 语法，可直接粘贴）、Supabase `public.market_regime_daily` 一行（下游期权收入引擎读取 band/playbook）。

**五个支柱 Five pillars**

| 支柱 | 权重 | 输入 |
|---|---:|---|
| 趋势 Trend | 30% | SPY/QQQ 对 SMA50/SMA200、SMA50 斜率；SPY 三态 Markov（Bull/Sideways/Bear，按 20 日波动标准化收益打标，无前视）下一交易日概率 |
| 波动率 Vol | 25% | VIX 水平、VIX/VIX3M 期限结构、VIX 一年分位、VRP（VIX − SPY 20 日实现波动） |
| 广度 Breadth | 20% | `trade-stldc-v2` BULL vs BEAR confirm/alert 比例；`trade-jlaw-v2` chart（Valid+Watch）占比相对自身历史分位；chart+yahoo Valid 数 |
| 期权流 Flow | 15% | `"flow-verdicts".payload.market_tide` **5 日均值** + 正值天数（不再用单日 tide 定调） |
| 脆弱度 Stress | 10% | 仪表盘 LPI 档位 − 每条已确认 funding 腿 10 分 |

**档位与执行规则 Bands → playbook（固定映射）**

| 分数 | 档位 | A 级仓位 | B 级仓位 | 允许的收入策略 | 裸卖 CSP | CSP DTE 上限 | 信用风险预算 |
|---|---|---:|---:|---|---|---:|---:|
| ≥ 70 | RISK_ON 进攻 | ×1.0 | ×1.0 | 全部 | 全类别 | 45d | 3.0% NLV |
| 55–70 | CONSTRUCTIVE 建设性 | ×1.0 | ×0.75 | CSP / BPS / Diagonal / CC | core + satellite | 39d | 2.5% |
| 45–55 | NEUTRAL 中性 | ×0.6 | ×0.5 | CSP / BPS / CC / BCS | core + satellite + watch1 + watch2 + watch3 | 35d | 2.0% |
| 30–45 | CAUTIOUS 谨慎 | ×0.5 | 0 | BPS / CC / BCS（仅定义风险） | 不新开 | 21d | 1.25% |
| < 30 | DEFENSIVE 防守 | ×0.25 | 0 | CC / BCS | 不新开 | 14d | 0.5% |

**迟滞 Hysteresis**：脚本同时计算前两个交易日的分数。档位变化需连续两日确认，或当日分数越过边界 > 3 分；否则维持前一档位，并标注 `pending A→B`。简报头部写生效档位，括号内写原始档位。

**背离 Divergences**（脚本自动检测，必须写进简报）：趋势强但广度弱（窄幅领涨）、广度改善但趋势弱、期权流追多但波动率承压、tide 防御与上升趋势背离、脆弱度高但 VIX 平静、当日 tide 与 5 日均值反向、Markov 状态与 STLDC SPY regime 不一致。

**3c. 写入简报 Regime 段**

1. 粘贴 `regime_<date>.md`（标题行含分数与档位）
2. 其后保留 **3–4 条脆弱度双语要点**（LPI / funding / 信用与久期 / 放大器，引用 as-of 日期）——作为 stress 支柱的证据说明，不另行定调
3. "解读 Read"一行：只解释**哪个支柱拖分/加分**与背离，不得与档位相矛盾（例：56 分 CONSTRUCTIVE 待确认 → 维持 NEUTRAL 规则，因广度 31 拖累）
4. 与上一份简报对比分数变化（如 53 → 56 ⬆️）

**禁止 Do not**：用"零 Valid"单独下结论（它只是广度支柱的一项）；用单日 tide 改变档位；引用 `gex_snapshot` 的 SPY/QQQ 墙位作 regime 证据（当前墙位贴近现价、`zero_gamma` 为空，仅反映近月仓位）。

### Step 4 — 分级

| 级别 | 条件 | 处理 |
|---|---|---|
| **A 级** | chart Valid + flow bullish + earnings ≥ 2 周 | buy-stop，仓位 × playbook `size_mult` |
| **B 级** | chart 高分 Watch（≥12）+ flow 不反对 + earnings 安全 | buy-stop 等触发；仓位 × playbook `tier_b_size_mult`（为 0 则只观察）；止损 >10% 再减半；RRR < 0.5 标注"可跳过" |
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
5. 失效条件：regime 生效档位下调一档 → 全部候选降一档；SPY 跌破 SMA50 当日即在简报头部预警（趋势支柱会在次日反映）

### Step 5 — 双语产出 + 同步

**格式要求：中英双语**。章节标题双语（如 `## 2. A 级：三重确认 | Tier A: Triple-Confirmed`）；表格表头双语（`触发 Trigger`）；长段落中文一行、英文一行对照。

1. 先运行 `scripts/build_daily_trading_plan_v2.py` 生成本地审阅版（只读 Supabase，无 Notion / Supabase 写入）：

```bash
export SUPABASE_SERVICE_ROLE_KEY="$SUPABASE_KEY"  # 仅当 session 已授权且只用于本地审阅
python skills/daily-trading-briefing/scripts/build_daily_trading_plan_v2.py \
  --date <date> --out <output-dir> \
  --regime-json <output-dir>/regime_<date>.json \
  --reconcile-json <output-dir>/flow_reconcile_<date>.json \
  --fragility-notes <output-dir>/fragility_dashboard_source.md \
  [--income-board-markdown <output-dir>/part4_weekly_20_contract_board.md]
```

该 builder 同时保存选取字段的 source snapshot。市场关闭或 Unusual Whales 合约链无法在同一决策时点验证时，必须让 income board fail closed，不得把历史 `live` 标记当作当前报价。
当有同一收盘时点、Unusual Whales API 验证的 NBBO 板时，先用 `scripts/render_weekly_income_board.py --manifest <board.json> --out <part4.md>` 校验 20 张总数、10 张 SP/CSP/WHEEL_CSP vs 10 张 BPS/BCS、每标的 ≤2、DTE、bid/ask、spread 最大亏损、CSP collateral 和 U.S.-exchange 资格，再把输出作为 `--income-board-markdown` 传入。默认报告只显示已选的 Top 20；完整 Davis / CSP-BPS / UW 来源行保留在 source snapshot，只有审阅明确需要时才传 `--retain-income-ledgers` 附到报告。renderer 和 builder 都只生成本地审阅文件，不创建订单。
2. 复制 `templates/daily_trading_plan_v2.md` 到 `briefing_<date>.md` 并替换占位符。固定结构：Part 1 Regime scorecard / inputs / playbook → Part 2 intelligence + data freshness + canonical reconciliation / conflict register / cross-evidence → Part 3 directional Tier plan → Part 4 options-income board → Part 5 data gaps → Part 6 execution rules → sources. 每个市场依赖数字必须带 session/as-of；区分 report date、market session、source as-of、generated time。
3. Part 2 是唯一的完整对账和冲突登记册：新鲜度只引用 `flow_reconcile_<date>.json`，确认/冲突/部分数必须加总至 universe。Part 3 只能引用 Part 2 的确认与冲突，不得把 conflict 标的写成干净单向结论。Part 5 只写控制结论或每个真实缺口的影响与抑制动作，绝不复制前一日的手写 stale claim。
4. Part 4 只在同一 decision timestamp 已验证 U.S.-listed underlying、Unusual Whales API NBBO、expiry、DTE、earnings、合约条款与来源时填写；`est`、board mark、live quote 必须分开，若无合格报价则明确写“income board withheld”。先写带 as-of 的持仓语境与分诊优先级；存量持仓信用与新提案信用分开，不可把未经验证的 CC/diagonal/roll 算进预算。只显示每周按置信度选出的 **20 张合约**（可在同一 ticket 写 qty），每标的最多 2 张，**SP/CSP/WHEEL_CSP 10 张 vs BPS/BCS 10 张（50:50）**；SP/CSP/WHEEL_CSP ≤35d、BPS/BCS <45d。显示策略、合约腿、DTE、报价时点、每张与合计 gross credit、CSP collateral / spread 最大风险与 ROC、置信度文字、冲突/重叠、来源和开仓前验证。未来 **4 个重筛周**每行必须明确：催化剂、存量/收入动作、命名的 post-print re-strike queue、仅当条件与 veto、允许结构/配比、以及 credit 是否计入；不得只写泛化“re-screen”。周度规划范围是 $5–6k gross credit；四个独立重建周的可核算规划为 $20–24k gross credit。绝不为了凑周目标而延长 DTE、增加同标的、推断 CC 持仓或绕开 veto。绝不把该板当作系统可执行订单。
5. **同步 Notion**：页面 `J Law 双管线 × Unusual Whales 期权流 — 交易决策简报`（id `3f5b8852-cf00-8035-916d-d12bae2a3dd2`，位于 `ai-trader-v2-kimi` 下）。**2026-10-21 起解析器弃用 `<details>/<summary>`**（与 `<table>` 组合会被拒绝："bare <tr> is not valid"）——改用规范 `<toggle>` 语法：`<toggle>` 首行为标题（如 `📊 交易决策简报 <date> Trading Decision Briefing — ...`），随后空行，正文，`</toggle>`。当日重跑时用 `update_content` 定点替换**仅当日**的 toggle 内容（old_str 从当日 `notion-fetch` 原样复制；存储序列化为 `<b>` 粗体、`\$`/`\|`/`\<` 转义、多行 `<td>` 单元格；优先用无格式纯文本尾锚定位）；解析失败回退：先插最小探针 toggle，fetch 读其序列化形式再定点展开。历史日期的折叠块保留不动
6. **发布前标签核对（必做，先于第 5 步 Notion 同步）**：

```bash
python skills/daily-trading-briefing/scripts/flow_reconcile.py --date <date> --check-board briefing_<date>.md --strict
```

退出码 1 = 简报中有标签与 `flow_verdict_rows` 不符（如 10-29 PANW 写 "bullish verdict" 实为 mixed、CAT 写 "verdict 待核" 实为 mixed）或把新鲜表写成 stale。逐条修正后重跑至 0 再同步 Notion。
7. 回复用户给精简表格版 + 文件链接 + "研究分析非投资建议"

### 自动化执行 | Scheduled run

当用户要求固定时点运行时，使用 Manus ScheduleTask，不使用驻留 shell cron。当前已批准的默认节奏为工作日 15:30 `Asia/Hong_Kong`（六字段 cron：`0 30 15 * * 1-5`）。每次触发必须以最近完成的美国市场 session 为 report date，重新运行数据签收、regime、对账、Unusual Whales 合约核验、Top-20 renderer 和 strict board check；不得复用旧报价或旧候选。Notion 只添加新的当日顶层 `<toggle>`，或定点替换同一 report date 的 toggle；历史块绝不改动。数据不完整、Unusual Whales 报价/事件无法验证或 strict check 失败时，保守发布并明确 withheld 项，不发布未经核验的收入板。

## 已知坑

- 表名 `trade-jlaw-v2` 带连字符；日期列是 `run_date`；`.env` base 若已含 `/rest/v1` 不要重复拼
- chart 行 entry_price 历史上有过 % 错位 bug（10.4 应为 104），使用前目检异常值；空 entry/stop 多为"无 pivot"修复期图表，不要凭空填
- chart 标签只降级不升级 yahoo 结论；硬冲突以 chart 严格方为准
- Yahoo 无数据的非美股品种（AIR、AVEX 等）持续缺席属预期
- 曾见数据源错价疑点（如 NOW 现价 140.86 异常），发现后在简报中标注
- Notion 表格必须用 `<table>` 增强 Markdown 语法，不能用标准 markdown 表格
- **Notion 解析器 2026-10-21 变更**：`<details>/<summary>` 已弃用（自动转 `<toggle>`），与 `<table>` 组合直接报错 "bare <tr> is not valid"；一律写 `<toggle>`（标题在首行 + 空行 + 正文）。页面存储序列化为 `<b>` 粗体、`\$`/`\|`/`\<` 转义、多行 `<td>` 单元格——update_content 的 old_str 须原样复制；优先用纯文本尾锚（句尾无格式片段）定位
- Unusual Whales 是当前合约板的首选链源，不可把 Supabase 历史账本当作当日 NBBO；任何 U.S.-exchange 资格、NBBO、DTE、OI、earnings 或合约条款无法验证的标的必须跳过或让收入板 fail closed。ORATS 仅可在用户明确授权的已验证迁移中作为额外 cross-check，绝不阻断 UW 工作流。
- `regime_score.py` 依赖 Yahoo chart API（与 jlaw-yahoo-runner 相同），偶发 429 会自动重试；SPY 未出当日收盘时按 SPY 日期对齐（VIX 当日值不会被提前使用）
- `option_flow_symbol_signals.net_bias_z` 2026-10-29 起为 robust z（signed-log 溢价 + median/MAD，±5 截断），历史行已回填；旧版 mean/stdev z 会被单一异常值（MU +6.66）压扁全体——跨日比较以回填后数值为准
- `trade-stldc-v2` 2026-10-27 起覆盖从 67 扩至 257 只，比例类指标不受影响，但跨期比较绝对数无意义
- 脆弱度仪表盘：LPI 为周度合成（滞后数日）、页面 ~07:19 UTC 更新，引用时带 as-of 日期；卡片偶见未来日期 quirk 与个别 stale 源，读数时计入

## 样例

- `briefing_2026-10-10.md`（工作区）— 首份按本 skill 产出的双语简报（含 10-10 chart 更新版）

---

## Part 4 · 期权收入计划 | OPTIONS INCOME PLAN（v4 收入规则 income rules，2026-11-01 用户确认生效 / effective next run）

> 本节为 Part 4 收入计划的权威规则（source of truth）；ScheduleTask runbook 引用本节。*This section is the source of truth for the income plan; the scheduled-task runbook references it.*

- **目标 Target**：通常 **$5-6k/周 gross-credit 规划**，四个独立重建周对应 **$20-24k/月**。$20-30k 是更宽的月度 ambition，只有额外收入在当时点重新验证后才可计入；不是收益、利润或达标承诺。达成路径：①合规的 short-put/CSP/WHEEL_CSP；②BPS/BCS 定义风险乘数；③在每周重新验证后使用合适的月度 IV 层。*The target is a gross-credit planning range, not a return or profit promise.*

### 3.0-A DTE 规则 | DTE rules
- **SP / CSP / WHEEL_CSP：DTE ≤ 35 天**（2026-11-01 用户更新）；**short call / strangle：DTE ≤ 20 天**。
- **Credit Spread（BPS/BCS）：DTE < 45 天**（月度 IV 溢价可用 / monthly IV premium allowed）。
- 月度到期（11-20/12-18 型）仅用于 spread / diagonal 空腿；短腿到期清标的财报 ≥2 天；**FOMC / 巨头发报夜不加新裸 gamma**（BPS/BCS 定义风险可做）。
- 超限例外（>35 DTE 的 SP/CSP/WHEEL_CSP 或 ≥45 DTE 的 BPS/BCS）：不允许列入本板；重筛更近到期日，不得以目标 credit 作为例外理由。*No duration exceptions are permitted for this board.*

### 3.0-B 提案板 | Proposal board（每周 20 张合约 20 contracts/week）
- 每周提案 **20 张合约**；**同一标的同时最多 2 张（same ticker ≤2）**；**SP/CSP/WHEEL_CSP 10 张 vs BPS/BCS 10 张（50:50）**。四个独立重建周的 base gross-credit 规划为 **$20–24k/月**（每周 **$5–6k**）；$30k 不得预先假设，且不能为凑数放宽 DTE、NBBO、earnings、dark-pool、flow 或集中度硬规则。
- 来源叠加 source stack：**Unusual Whales API 同一决策时点的 U.S.-listed contract/NBBO/OI/IV/expiry/GEX/event 数据优先**，再叠加 option-davis / csp-bps (manus) / uw-option-opportunities 历史板、**Tier B 方向确认提案**（Part 1' chart+flow 确认标的）和 **估值锚 CSP（Rijnberk `equity_valuation` × GEX put wall；CSP strike ≤ buy_below / fair_value）**。没有 UW 当日完整验证，不得将历史合同列为可开仓 ticket。
- **只列被选中的最高置信度 20 张，不在用户报告重复全量来源清单**；原始来源字段保存在 local source snapshot。每张合约必须携带：置信度 Confidence（高 High / 中 Medium / 低 Low）+ 一行理由 rationale。评级 rubric：
  - **高 High**：verdict bullish/确认 ≥2 日 + dp 干净或吸筹 + 板排名靠前或估值锚；
  - **中 Medium**：单日确认 / mixed 但 dp 支持 / alerts 5-20；
  - **低 Low**：样本 <5 alerts、贴线门槛、疑似数据（RRR 0 / stop <1%）→ **Low 为可跳过 optional/skippable**。
- **硬规则 Hard rules**：① 只使用 U.S.-listed / U.S.-exchange 标的且同一标的 ≤2 张；② **现有持仓的 covered call 不入提案**（持仓管理收益，另列 tracked separately）；③ dp 重度派发（buy%<25 且 net<−$100M）否决做多与卖 put；④ FOMC / 发报夜不加裸 gamma；⑤ 防御 regime 下 short strangle 否决（只做指数 iron condor）；⑥ 疑似数据行（RRR 0 / stop <1%）不挂单。

### Post-print re-strike queue（财报后重设 strike 队列）
每次财报后：dp 复核 → CSP/BPS 按 post-print GEX wall 与 Rijnberk 估值锚重设 strike；dp 派发名单维持否决；verdict 翻转的名单先进观察再恢复。*After each print: dp re-check → re-strike anchored to post-print GEX wall and valuation; distribution names stay vetoed; flipped verdicts re-enter via watch.*

### 周度预算与节奏 | Weekly budget & cadence
20 张合约的 gross-credit 规划为 **$5-6k/周**；四个独立重建周可对应 **$20-24k** gross credit。$30k 上沿只能由后续经重新验证的额外收入（例如合规滚仓或新板）形成，不能写入未验证的本周板；这不是预期利润。报告必须包含未来 **4 个作战周**：日期、事件催化剂、存量/收入动作、post-print re-strike queue、only-if / veto、允许结构与配比、以及 credit 计入方式。节奏：周一建板（核对 marks）→ 周中事件前停手 → 周四按决议/发报刷新加层 → 周五到期/滚仓。防守：2x credit 或短腿 touch 即撤；21 DTE / 2x credit 管存量；任一失效触发（tide 连续深流出 / NDX 破 55DMA×3 / SOXX 破 500×2 / LPI>60）→ 停新开仓。*Build Monday → stand down before events → layer Thursday → expire/roll Friday; defense rules as listed.*
