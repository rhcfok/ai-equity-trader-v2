---
name: daily-trading-briefing
description: 每个交易日用确定性 0–100 regime 评分（scripts/regime_score.py）并整合 J Law 双管线（chart + yahoo，Supabase trade-jlaw-v2）与 UW-option-breakout 的 flow verdict，产出中英双语交易决策简报（Regime 判定 + A/B 级候选 + 触发价/止损/目标 + 回避名单），写入本地 md 并同步 Notion。触发：每日简报 / 盘前决策 / daily briefing / 交易决策简报 / 更新简报。
---

# daily-trading-briefing：每日交易决策简报

把 J Law 双管线结果与期权流判读整合成中英双语、研究用途的交易决策简报；只生成条件性人工计划，绝不创建、传送或提交订单。

**政策优先级 / Policy precedence：2026-11-04 20:24 HKT 用户反馈为未来运行的权威规则**，覆盖旧 poor-regime CSP 禁令与 10:10 配比；本次仅更新未来工作流，不重建今日报告、不修改评分数学、脚本、测试、Git、排程或 Notion。

## 输入与上游依赖

- Supabase `trade-stldc-v2`（广度支柱）、`public.market_regime_daily`（regime 输出，见 Step 3）。
- Supabase `trade-jlaw-v2`：当日 `yahoo` 行 + 最新 `chart` 行（chart 常滞后一天，属正常；10-10 起已修复 null/错位问题）。
- **flow verdict**：从 Supabase 交接表 `"flow-verdicts"` 读取（由 agent B / UW-option-breakout 写入）。本机工作区的 `flow_verdicts_<date>.json` 只是本地缓存，不是权威来源。
- 自行拉数可用 `UW-option-breakout/scripts/pull_jlaw.py`。
- **市场脆弱度仪表盘 Market Fragility Dashboard**：`https://vincizh.github.io/market-fragility-dashboard/`（每日 ~07:19 UTC 更新，运行时可能读到前一日快照——引用页面上的 as-of 日期）。作为 Regime 段的跨检输入，见 Step 3。

### 期权数据来源优先级 | Options-data source hierarchy

- **Unusual Whales API / MCP 是期权主数据源。** 对每个候选在同一决策时点用它核验 U.S.-listed 标的、期权链、bid/ask（NBBO）、到期日/DTE、IV、OI、GEX、异常期权流、财报与宏观事件；无法证明 U.S. exchange 的标的一律排除。
- **Supabase 是候选池与 EM measurement 审计来源。** 先合并三个既有账本 `public."option-opportunity-davis"`、`public."csp-bps-opportunity"`、`public."uw-option-opportunities"`，再用 `public."em-probability"` 精确映射和排序；`flow-verdicts`、normalized flow、dark pool、GEX 用于独立核验。账本 mark 绝不替代新的 UW 报价，不以 agent 新造 ticket 补足来源供给。
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

Regime 不再靠文字判断，由 `scripts/regime_score.py` 输出 **0–100 综合分 + 档位**。保持评分数学、五支柱、边界与迟滞不变；简报只解读分数，不得另起档位结论。收入执行政策应用下表及 Part 4 的 Nov 4 更新；脚本若仍输出旧 CSP 禁令、类别限制或 DTE 上限，报告不得照搬旧 playbook。

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

**档位与执行规则 Bands → playbook（评分不变；收入政策 Nov 4 更新）**

| 分数 | 档位 | A 级仓位 | B 级仓位 | 允许的收入策略 | SP/CSP 候选类别 | CSP DTE 上限 | credit_at_risk 上限 |
|---|---|---:|---:|---|---|---:|---:|
| ≥ 70 | RISK_ON 进攻 | ×1.0 | ×1.0 | 全部（仍须独立合格） | 全部用户 watch 类别 | 35d | 3.0% NLV |
| 55–70 | CONSTRUCTIVE 建设性 | ×1.0 | ×0.75 | SP/CSP / BPS / Diagonal / CC | 全部用户 watch 类别 | 35d | 2.5% NLV |
| 45–55 | NEUTRAL 中性 | ×0.6 | ×0.5 | SP/CSP / BPS / CC / BCS | 全部用户 watch 类别 | 35d | 2.0% NLV |
| 30–45 | CAUTIOUS 谨慎 | ×0.5 | 0 | SP/CSP / CC / BPS / BCS（逐票核验） | 全部用户 watch 类别 | 35d | 1.25% NLV |
| < 30 | DEFENSIVE 防守 | ×0.25 | 0 | SP/CSP / CC / BPS / BCS（逐票核验） | 全部用户 watch 类别 | 35d | 0.5% NLV |

全部类别包括 core、satellite、watch1、watch2、watch3 及其他用户 watch 分类；类别资格不等于准入或风险批准。弱 regime 影响仓位、账户信用风险与纪律，**不 blanket-block 新 SP/CSP/WHEEL_CSP**。报价、事件、flow、DP、支撑/方向、抵押与账户风险仍逐票检查；未知 NLV 不宣称 risk-approved sizing。CC 短腿 ≤20d、BPS/BCS <45d；不随 regime 放宽。

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
5. 失效条件：regime 生效档位下调一档 → 方向性 A/B 候选降一档；收入候选按新仓位/credit_at_risk 预算与逐票条件重核，不因降档一律停新开 SP/CSP。SPY 跌破 SMA50 当日即在简报头部预警（趋势支柱会在次日反映）。

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

该 builder 同时保存选取字段的 source snapshot。市场关闭不自动取消条件性研究板：核验最近完整 session 的 UW quote 观测时点和条款，并标注 historical/conditional。关键字段无法验证时，withheld 受影响 ticket；不得把历史 `live` 标记当作当前报价。
先按 Part 4 用本地候选/EM JSON snapshots 映射、独立核验并选择，再用 `scripts/render_weekly_income_board.py --manifest <board.json> --out <part4.md>` 渲染；manifest 使用 `board_rule.selection_mode="ranked_30_20_50"`（所有 regime 的未来默认），软目标 6 SP/CSP/WHEEL_CSP : 4 CC/WHEEL_CC : 10 BPS/BCS。校验最多20个 strategy units、每标的≤2、精确 EM identity/vintage/provenance、DTE、NBBO、spread 最大亏损、CSP collateral、CC 实股覆盖及账户风险；不足/偏离如实披露。`full_50_50`、`regime_spread_only` 仅供历史归档重现，不得用于未来默认。再把输出作为 `--income-board-markdown` 传入。报告只显示已选 rows；完整三个账本和 EM 来源保留本地 snapshot，不附原始账本（不使用 `--retain-income-ledgers`）。mapper、renderer、builder 仅本地审阅，无订单。
2. 复制 `templates/daily_trading_plan_v2.md` 到 `briefing_<date>.md` 并替换占位符。固定结构：Part 1 Regime scorecard / inputs / playbook → Part 2 intelligence + data freshness + canonical reconciliation / conflict register / cross-evidence → Part 3 directional Tier plan → Part 4 options-income board → Part 5 data gaps → Part 6 execution rules → sources. 每个市场依赖数字必须带 session/as-of；区分 report date、market session、source as-of、generated time。
   **Part 3.0a 多源比较（2026-11-02 用户更新，下一次运行起生效）：** 在原有 A/B 多源比较前，逐组列出 `trade-stldc-v2` 同一 as-of 的 **BULL-ALERT、BULL-CONFIRM、BEAR-ALERT、BEAR-CONFIRM** 全部 tickers 与各组数量，不限于 A/B 候选。仅纳入已核验美国交易所上市的标的；逐组去重、按字母排序，空组写 None / 无，不截成 top-N。明确标注 source/as-of；缺日期、exchange 未核验或同标的阶段矛盾时省略并披露 scope/QA exclusion。保留原有 Tier A/B × STLDC × dark pool × unusual flow × option-flow 对照；STLDC technical confirm 不等同 Part 2 的 flow/reconciliation confirmed，不自动升级交易等级。四组名单与排除项保存到 source snapshot 并在发布前验证数量与名单一致。
3. Part 2 是唯一的完整对账和冲突登记册：新鲜度只引用 `flow_reconcile_<date>.json`，确认/冲突/部分数必须加总至 universe。Part 3 只能引用 Part 2 的确认与冲突，不得把 conflict 标的写成干净单向结论。Part 5 只写控制结论或每个真实缺口的影响与抑制动作，绝不复制前一日的手写 stale claim。
4. Part 4 只在同一 decision timestamp 已验证 U.S.-listed underlying、UW NBBO、expiry、DTE、earnings、合约条款、来源与逐票风险时填写；`est`、board mark、dated quote、live quote 必须分开。显示按精确储存 `p_win_expiry DESC`（`p_profit50_first` 次级）选出的最多20个 units，软配比30/20/50、每标的≤2；无合格供给则不足，不因软配比缺口整体否决其他独立合格供给。新增 CC 只以核验的未占用实股支持；旧 premium 不重计。显示概率及 source_table/opportunity_key、model_version/sigma_as_of、策略/腿/DTE、报价时点、每单位与合计 gross credit、collateral/最大风险/ROC、解释性置信度、冲突、来源与开仓验证。缺失/无效/事件失效的 EM 行放未排名队列，不作0%。未来四周保留催化剂、存量/收入动作、命名的 post-print re-strike queue、only-if/veto、结构/新配比、credit 计入字段；不得只写泛化 re-screen。$5–6k/周、四个独立重建周 $20–24k 为 gross-credit 规划非收益承诺；不得为目标放宽任何硬规则。
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
- Notion 表格必须用 `<table>` 增强 Markdown 语法，不能用标准 markdown 表格或 `<th>`；每个 `<tr>` / `<td>` 标签各占一行，`<td>` 内只放纯文本、无粗体/斜体等格式。
- **Notion 解析器 2026-10-21 变更**：`<details>/<summary>` 已弃用（自动转 `<toggle>`），与 `<table>` 组合直接报错 "bare <tr> is not valid"；一律写 `<toggle>`（标题在首行 + 空行 + 正文）。页面存储序列化为 `<b>` 粗体、`\$`/`\|`/`\<` 转义、多行 `<td>` 单元格——update_content 的 old_str 须原样复制；优先用纯文本尾锚（句尾无格式片段）定位
- Unusual Whales 是当前合约板的首选链源，不可把 Supabase 历史账本当作当日 NBBO；任何 U.S.-exchange 资格、NBBO、DTE、OI、earnings 或合约条款无法验证的标的必须跳过或让收入板 fail closed。ORATS 仅可在用户明确授权的已验证迁移中作为额外 cross-check，绝不阻断 UW 工作流。
- `regime_score.py` 依赖 Yahoo chart API（与 jlaw-yahoo-runner 相同），偶发 429 会自动重试；SPY 未出当日收盘时按 SPY 日期对齐（VIX 当日值不会被提前使用）
- `option_flow_symbol_signals.net_bias_z` 2026-10-29 起为 robust z（signed-log 溢价 + median/MAD，±5 截断），历史行已回填；旧版 mean/stdev z 会被单一异常值（MU +6.66）压扁全体——跨日比较以回填后数值为准
- `trade-stldc-v2` 2026-10-27 起覆盖从 67 扩至 257 只，比例类指标不受影响，但跨期比较绝对数无意义
- 脆弱度仪表盘：LPI 为周度合成（滞后数日）、页面 ~07:19 UTC 更新，引用时带 as-of 日期；卡片偶见未来日期 quirk 与个别 stale 源，读数时计入

## 样例

- `briefing_2026-10-10.md`（工作区）— 首份按本 skill 产出的双语简报（含 10-10 chart 更新版）

---

## Part 4 · 期权收入计划 | OPTIONS INCOME PLAN（Nov 4 20:24 HKT；未来运行生效）

> 本节为收入计划权威规则；既有 runbook 引用本节。不重建今日报告，不变更 regime 或 EM 模型数学。*Apply prospectively; no autonomous orders or model changes.*

### 4-A Regime 与逐票资格 | Regime and independent eligibility

- CAUTIOUS / DEFENSIVE **不 blanket-block 新 SP/CSP/WHEEL_CSP**，也不强制 spread-only。保持仓位纪律与 `credit_at_risk` 总预算：分别1.25% / 0.5% NLV；其他档位预算见 Step 3。全部用户 watch 类别可筛选，但高概率、配比或 credit 目标不能覆盖账户、资金/抵押、最大亏损与集中度限制。未知 NLV 或风险容量时，不制造 risk-approved units。
- 逐票核验 freshness、U.S.-exchange、UW NBBO/流动性/OI、合约条款、财报/宏观、flow、DP、支撑/方向/GEX、最大亏损、抵押、账户与冲突。过期 DP 不算当前确认；冲突按 Part 2 减仓/跳过，重度派发（buy%<25 且 net<−$100M）仍否决做多与卖 put。缺关键字段只 withheld 受影响票；共享关键数据全部失效才整板 withheld。
- **DTE 不变：SP/CSP/WHEEL_CSP ≤35d（全部档位）；BPS/BCS <45d；CC/WHEEL_CC 与既有 short call/strangle ≤20d。** 本次未放宽 CC；不得为目标 credit 延长。月度 IV 层可用于合规 spread / diagonal 空腿，不改变上述上限；短腿到期清标的财报≥2天。FOMC/巨头发报夜不加裸 gamma；定义风险仍须事件核验，非自动豁免。DEFENSIVE short strangle 禁令（仅指数 iron condor）保持，非本板新配额。
- 区分 dated/conditional pre-market proposal 与 entry-ready。经核验的最近完整 session UW 报价可支持条件性研究；开市后须重核，绝不称历史报价 live。无法验证观测时间/条款时，不提供精确 strike/credit，仅列恢复所需字段；未验证票计入 credit 为0，不等于其概率为0。

### 4-B 候选池与 CC 实股 | Ledger-first supply and covered calls

1. **先读全三个既有账本**：`option-opportunity-davis`、`csp-bps-opportunity`、`uw-option-opportunities`，保留 exact `source_table`、`opportunity_key`、source `report_date`、ticker、expiry、strategy、short_strike 与完整腿身份到本地 JSON snapshot。跨来源相同合约去重、保留 provenance；不得在供给少时用 agent 新造票代替。Tier/J Law、估值锚（Rijnberk `equity_valuation` × GEX put wall；CSP strike≤buy_below/fair_value）是既有候选的独立证据，不是补票来源。
2. UW 是新报价/选约主数据；Supabase 是候选与 EM 审计主数据。更换 expiry/strategy/short strike 后必须有匹配的新储存 EM 行；旧 score 不沿用，不以 ticker-only join 连接。缺少匹配评分的改约放未排名重设队列。
3. **CC/WHEEL_CC 可包含 NEW overwrite 提案**，但必须来自可追踪的候选池并核验同一账户 snapshot 中每 unit ≥100股美国上市实股，已持有且未占用/未承诺覆盖其他短 call。候选必须保留 `share_ownership_verified`、`shares_available`、`share_snapshot_as_of` 与覆盖分配；`stock_capital_per_contract` 只作 renderer 的实股资本语境，不是额外 premium。不得裸卖 call、假定持股或为了4个 CC 配额买/虚构100股。既有已卖 CC 属持仓管理，prior premium 不重计；仅新的合格 overwrite gross credit 可单列新提案规划。

### 4-C EM 精确映射与排序 | Stored measurement, not empirical win rate

- 排序主键为 **`public."em-probability".p_win_expiry DESC`**，`p_profit50_first DESC` 仅 tie/secondary metric；相同值以稳定 identity 排序。保留储存精度，百分比只做显示单位转换/明确舍入；不按旧置信度、ticker score 或1−delta替代排序。
- 每个显示百分比必须带 `source_table + opportunity_key`，并能回查储存 measurement。候选与 score 的 source `report_date`、ticker、expiry、strategy、short_strike **全部精确一致**，还须 source_table/opportunity_key 一致；不跨 report date、不复用另一 strike，**不在 CC 与 WHEEL_CC 等策略别名之间移植评分**。报告日期、各账本最新源日期与 session 可不同，须分开注明并匹配各自源行，不强迫所有源日期等于报告日。
- `p_win_expiry` 必须为有限数值且在[0,1]；`model_version` 必须非空/非空白；`sigma_as_of` 必须匹配 `model_as_of` 指定的**最新已完成市场 session 模型 vintage**（非当前未完成 session）。保留 model vintage、事件校验状态和 measurement provenance；无效/缺失/事件失效或 vintage/identity 不符的行进入 **unranked queue**，显示缺失原因、不显示0%、不计入 ranked supply。
- `p_profit50_first` 与 `calib_cov_30d` 可为 null：保持 **N/A**、披露更高不确定性/额外审慎，不转换为0或自动阈值 veto。有值 p50 必须有限且在[0,1]；同 pWin 的次级比较仅用有效 p50（N/A 不制造优势）。缺失 p50/calibration 本身不使有效 pWin 行自动失去排名资格。
- **测量边界**：`p_win_expiry` 是模型的短腿到期 OTM 概率，**不是**已验证经验胜率、完整 CC 净盈利概率或 spread breakeven 盈利概率；`p_profit50_first` 是模型50%获利先达指标，非回测保证。不设自主 p-threshold 准入门槛，不改变 EM 公式/参数或回写数学；逐票风险与事件 veto 独立于概率。High/Medium/Low 可保留为解释文字，不能覆盖数值排序或替代概率。

### 4-D 30/20/50 软目标与本地 CLI | Selection and local snapshots

- 软目标约 **20个 strategy units：6 SP/CSP/WHEEL_CSP : 4 CC/WHEEL_CC : 10 BPS/BCS（30/20/50）**；**每 ticker 合计≤2 units**，qty计入总量与上限。一个 spread unit 含两条腿，不额外按两张计数。
- 在已独立合格且有有效 EM 的供给中按上述数值优先，尝试近似各桶软目标；某桶不足时从**剩余独立合格供给**按最高 `p_win_expiry`（p50次级）补选至最多20。配额非硬 veto，不保留人为闲置配额，也不制造缺失 CC 股数、概率或账户批准容量。无更多合格供给则不足20，披露实际 units/比例、mix deviation、缺口/原因与 gross-credit shortfall。
- 只展示 selected rows，不重复全量原始账本。未排名/排除队列仅列身份、状态、原因与恢复要求；原始 ledger/EM 行留本地 source snapshots。显示每 unit 与合计 gross credit、CC share coverage、CSP collateral、spread 最大亏损/ROC、NBBO时点、事件、冲突、来源与 opening validation。
- 新 mapper `scripts/map_em_probabilities.py` 的接口为 `attach_probabilities(candidates, scores, model_as_of)` 与 `select_top20(...)`；只读本地 JSON、精确 attach/selection 与输出审阅 artifacts，**不 fetch、不计算/修改 EM 数学、不写数据库**。上游负责导出三账本与储存 EM 行、独立风险核验；mapper 不替代这些检查。

```bash
# 从工作区根目录运行；candidate/scores 均为已导出的本地 JSON snapshots。
python skills/daily-trading-briefing/scripts/map_em_probabilities.py \
  --candidates <output-dir>/income_candidates_<date>.json \
  --scores <output-dir>/em_scores_<date>.json \
  --model-as-of <latest-completed-session> \
  --out <output-dir>/income_selection_<date>.json
python skills/daily-trading-briefing/scripts/render_weekly_income_board.py \
  --manifest <output-dir>/board_<date>.json --out <output-dir>/part4_weekly_20_contract_board.md
```

候选 JSON 保留上述 exact identities、独立 eligibility/事件/账户/CC覆盖证据；scores JSON 保留储存 p_win_expiry、p_profit50_first、sigma_as_of、model_version 与对应 identity。CLI/help 和输入 schema 必须先与实现核对；不猜字段、不重新取数。将 selection 经核验的 selected rows 写入 board manifest，`board_rule.selection_mode="ranked_30_20_50"` 为所有 regime 的未来默认，目标6/4/10但允许有依据的不足/偏离；`full_50_50` 与 `regime_spread_only` **仅 legacy archive modes**，不得成为未来默认或恢复旧 CSP 禁令。

### 4-E 四周作战计划、重设与预算 | Four-week operating plan

- 保留未来**四个具体作战周**，每行字段不变：日期、催化剂、存量/收入动作、命名的 post-print re-strike queue、only-if/veto、允许结构/配比、credit treatment。各周采用新6/4/10软目标/逐票政策，列真实替补与验证需求；只写 re-screen 不合格。
- 每次财报后：DP复核 → CSP/BPS 按 post-print GEX wall 与 Rijnberk 估值锚重设 strike → 对新 identity 取匹配储存 EM；缺新 measurement 则未排名。派发名单保持 veto；verdict 翻转先观察再恢复。Queue 不是可开仓票、不会贡献已收 premium。
- **$5–6k/周 gross-credit 规划；四个独立重建周 $20–24k**。$20–30k 是更宽月度 ambition，$30k仅由后续重新验证的额外收入支持，不能预设；不等于净利润/收益承诺。旧持仓 premium、未验证/未批准/未选票不得重计，目标不足如实显示。
- 节奏：周一建板核对 UW quotes → 周中事件前停手 → 周四按决议/发报刷新 → 周五到期/滚仓。存量防守：2x credit 或短腿 touch 即撤；21 DTE / 2x credit 管存量。tide连续深流出 / NDX破55DMA×3 / SOXX破500×2 / LPI>60 或 regime降档触发**风险复核、减仓及逐票重筛**，不机械停止全部新 SP/CSP；若具体事件、账户或共享数据硬规则失效，暂停受影响票/范围，写明原因。
