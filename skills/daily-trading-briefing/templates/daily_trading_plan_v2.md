<toggle heading="h1">
📊 交易计划 V2 | Daily Trading Plan V2 — {{REPORT_DATE}}

<callout icon="⚠️" color="yellow_bg">
<b>状态 Status:</b> {{STATUS}} · 市场会话 Market session: {{MARKET_SESSION_DATE}} · 生成时间 Generated: {{GENERATED_AT_TIME_ZONE}} · 数据截止 Data cutoff: {{DATA_CUTOFF}}.<br />
This is research, scenario analysis, or an unsubmitted manual plan. It never creates, submits, or changes an order. Independently re-check all prices, option quotes, liquidity, events, and account risk.
</callout>

<b>数据控制 Data control:</b> {{DATA_CONTROL_STATUS}}. All market-dependent values below carry an as-of or session date.

## Part 1 · 市场状态 | Market Regime

<b>有效档位 Effective regime:</b> {{EFFECTIVE_BAND}} · <b>原始档位 Raw regime:</b> {{RAW_BAND}} · <b>得分 Score:</b> {{REGIME_SCORE}}/100 · <b>迟滞 Hysteresis:</b> {{HYSTERESIS_STATE}}.

### 1.1 Regime scorecard | Regime 评分卡

<table fit-page-width="true" header-row="true">
<tr>
<td>支柱 Pillar</td>
<td>权重 Weight</td>
<td>得分 Score</td>
<td>证据 Evidence</td>
<td>来源与时点 Source / as-of</td>
<td>状态 Data status</td>
</tr>
{{REGIME_SCORECARD_ROWS}}
</table>

### 1.2 Regime inputs | 市场结构输入

- <b>Market tide:</b> {{MARKET_TIDE_WITH_AS_OF}}
- <b>Chart pipeline:</b> {{CHART_PIPELINE_RUN_DATE_AND_COVERAGE}}
- <b>Yahoo pipeline:</b> {{YAHOO_PIPELINE_RUN_DATE_AND_COVERAGE}}
- <b>Flow coverage:</b> {{FLOW_COVERAGE_AND_VERDICTS}}
- <b>Unusual-flow highlights:</b> {{UNUSUAL_FLOW_HIGHLIGHTS}}
- <b>Fragility dashboard:</b> {{FRAGILITY_LPI_FUNDING_AS_OF_AND_LIMITATIONS}}
- <b>STLDC:</b> {{STLDC_BREADTH_WITH_AS_OF}}
- <b>Dark pool:</b> {{DARKPOOL_SUMMARY_WITH_SESSION_DATE}}
- <b>Cross-source read / 综合解读:</b> {{CROSS_SOURCE_READ}}

### 1.3 背离、解读与失效条件 | Divergences, Read-through & Invalidation

{{REGIME_DIVERGENCES_AND_INTERPRETATION}}

<b>Risk-review trigger / 风险复核触发:</b> {{REGIME_INVALIDATION_TRIGGER}}. Disclose the affected scope; a regime downgrade alone is not a blanket ban on new SP/CSP/WHEEL_CSP.

### 执行规则 | Playbook

<table fit-page-width="true" header-row="true">
<tr>
<td>A 级 Tier A</td>
<td>B 级 Tier B</td>
<td>允许策略 Permitted strategies</td>
<td>SP/CSP 类别资格 Eligible categories</td>
<td>DTE 上限 DTE caps</td>
<td>信用风险 Credit-at-risk</td>
<td>财报处理 Earnings treatment</td>
<td>降档行动 Downgrade action</td>
</tr>
<tr>
<td>{{TIER_A_SIZE}}</td>
<td>{{TIER_B_SIZE}}</td>
<td>{{PERMITTED_STRATEGIES}}</td>
<td>{{NAKED_CSP_UNIVERSE}}</td>
<td>{{DTE_CAP}}</td>
<td>{{CREDIT_RISK_CAP}}</td>
<td>{{EARNINGS_POLICY}}</td>
<td>{{DOWNGRADE_ACTION}}</td>
</tr>
</table>

Apply Nov 4 20:24 HKT income policy without changing regime scoring math: all user watch categories are eligible for screening in every band; SP/CSP/WHEEL_CSP ≤35d, CC/WHEEL_CC short calls ≤20d, BPS/BCS <45d. CAUTIOUS / DEFENSIVE retain credit_at_risk caps of 1.25% / 0.5% NLV and tighter sizing/discipline, not a blanket put ban. Eligibility still requires independent quote, event, flow, DP, support, collateral and account checks; unknown NLV is not risk approval.

---

## Part 2 · 情报、对账与新鲜度 | Intelligence, Reconciliation & Freshness

### 2.0 情报要点 | Intelligence highlights

- <b>Vintage / 版本:</b> {{INTELLIGENCE_VINTAGE_OR_NOT_AVAILABLE}}
- <b>Core narrative / 核心叙事:</b> {{INTELLIGENCE_NARRATIVE}}
- <b>Event calendar / 事件日历:</b> {{NEXT_EVENT_CALENDAR}}
- <b>Boundary / 使用边界:</b> Third-party or user-provided intelligence is attributed context only; it cannot override the regime, earnings, reconciliation, or risk controls.

### 2.1 数据新鲜度 | Data freshness

<table fit-page-width="true" header-row="true">
<tr>
<td>数据集 Dataset</td>
<td>最新日期 Latest</td>
<td>滞后 Age trading days</td>
<td>预期节奏 Expected cadence</td>
<td>状态 Status</td>
<td>例外与影响 Exception / impact</td>
</tr>
{{FRESHNESS_ROWS}}
</table>

### 2.2 对账总览 | Reconciliation overview

<table fit-page-width="true" header-row="true">
<tr>
<td>Universe</td>
<td>Confirmed</td>
<td>Conflict</td>
<td>Partial</td>
<td>Confirmed bullish</td>
<td>Confirmed bearish</td>
<td>No-data / protective</td>
<td>Frozen snapshot</td>
</tr>
<tr>
<td>{{RECON_UNIVERSE}}</td>
<td>{{RECON_CONFIRMED}}</td>
<td>{{RECON_CONFLICT}}</td>
<td>{{RECON_PARTIAL}}</td>
<td>{{RECON_CONFIRMED_BULLISH}}</td>
<td>{{RECON_CONFIRMED_BEARISH}}</td>
<td>{{RECON_NO_DATA_PROTECTIVE}}</td>
<td>{{RECON_SNAPSHOT_ID}}</td>
</tr>
</table>

- <b>Triple-confirmed bullish:</b> {{TRIPLE_CONFIRMED_BULLISH}}
- <b>Triple-confirmed bearish:</b> {{TRIPLE_CONFIRMED_BEARISH}}
- <b>Partial:</b> {{PARTIAL_RECONCILIATION_SUMMARY}}

### 2.3 冲突登记册 | Conflict register

<table fit-page-width="true" header-row="true">
<tr>
<td>标的 Ticker</td>
<td>Agent verdict</td>
<td>Premium today / 3d</td>
<td>net_bias_z</td>
<td>Dark pool / session</td>
<td>Composite</td>
<td>冲突类型 Conflict type</td>
<td>必需行动 Required action</td>
</tr>
{{FULL_CONFLICT_ROWS}}
</table>

Show every conflict, or explicitly label a shortened table as a sample and link to its full canonical register. A conflict cannot be summarized as a clean one-directional conviction.

### 2.4 交叉证据 | Cross-evidence

<table fit-page-width="true" header-row="true">
<tr>
<td>标的或范围 Ticker / scope</td>
<td>来源与方法 Source / method</td>
<td>窗口与时点 Window / as-of</td>
<td>观察 Observation</td>
<td>一致性 Agreement</td>
<td>操作影响 Operational implication</td>
</tr>
{{CROSS_EVIDENCE_ROWS}}
</table>

---

## Part 3 · 方向性交易计划 | Directional Tier Plan

### 3.0 A/B 级摘要 | Tier summary

<b>Active playbook:</b> {{PLAYBOOK_NAME}}<br />
<b>Tier A:</b> {{TIER_A_COUNT_AND_SUMMARY}}<br />
<b>Tier B:</b> {{TIER_B_COUNT_AND_SUMMARY}}

### 3.0a 多源比较 | Tier versus STLDC, Dark Pool & Unusual Flow

<b>STLDC 分组名单 | Stage ticker lists:</b> trade-stldc-v2 / {{STLDC_AS_OF}}; full verified U.S.-exchange universe, not only Tier A/B. Deduplicate and sort every group; do not truncate.

- <b>BULL-ALERT ({{STLDC_BULL_ALERT_COUNT}}):</b> {{STLDC_BULL_ALERT_ALL_TICKERS}}
- <b>BULL-CONFIRM ({{STLDC_BULL_CONFIRM_COUNT}}):</b> {{STLDC_BULL_CONFIRM_ALL_TICKERS}}
- <b>BEAR-ALERT ({{STLDC_BEAR_ALERT_COUNT}}):</b> {{STLDC_BEAR_ALERT_ALL_TICKERS}}
- <b>BEAR-CONFIRM ({{STLDC_BEAR_CONFIRM_COUNT}}):</b> {{STLDC_BEAR_CONFIRM_ALL_TICKERS}}

{{STLDC_SCOPE_QA_EXCLUSIONS_OR_NONE}}

STLDC technical alert/confirm is not an option-flow verdict or Part 2 reconciliation confirmation; it does not automatically qualify a trade. Empty groups must read None / 无.

{{TIER_CROSS_SOURCE_COMPARISON}}

### 3.1 A 级状态 | Tier A status

{{TIER_A_GATING_EXPLANATION}}

### 3.2 B 级执行板 | Tier B execution board

<table fit-page-width="true" header-row="true">
<tr>
<td>标的 Ticker</td>
<td>方向与类型 Side / order type</td>
<td>触发 Trigger</td>
<td>止损 Stop</td>
<td>止损 % Stop %</td>
<td>RRR</td>
<td>财报 Earnings / safety</td>
<td>STLDC</td>
<td>Dark pool</td>
<td>Unusual flow</td>
<td>Flow verdict</td>
<td>对账 Reconciliation</td>
<td>冲突行动 Conflict action</td>
<td>Alerts / coverage</td>
<td>仓位 Size</td>
<td>来源与时点 Source / as-of</td>
</tr>
{{TIER_B_ROWS}}
</table>

### 3.3 确认引用 | Confirmation references

{{TIER_CONFIRMATION_REFERENCES}}

---

## Part 4 · 期权收入板 | Options Income Plan

<callout icon="📝" color="blue_bg">
All units below are proposed and unsubmitted. A verified, explicitly dated completed-session UW quote may support conditional pre-market research, never a live entry price. Withhold exact tickets when quote observation time, expiry, underlying price, event risk, contract terms or provenance cannot be validated; do not withhold solely because U.S. markets are currently closed.
</callout>

### 4.0 护栏、持仓语境与模型测量 | Guardrails, portfolio context & model measurement

<b>DTE hierarchy:</b> {{DTE_RULE_HIERARCHY}}<br />
<b>Earnings exclusions:</b> {{EARNINGS_EXCLUSIONS}}<br />
<b>Qualitative confidence (explanation only):</b> High = {{HIGH_CONFIDENCE_RULE}}; Medium = {{MEDIUM_CONFIDENCE_RULE}}; Low = {{LOW_CONFIDENCE_RULE}}.<br />
<b>Quote status:</b> {{QUOTE_STATUS_LEGEND}}.<br />
<b>Model vintage:</b> latest completed-session model_as_of = {{EM_MODEL_AS_OF}}; sigma_as_of = {{EM_SIGMA_AS_OF}}; model_version = {{EM_MODEL_VERSION}}.

Rank exact stored public.em-probability p_win_expiry DESC; require finite pWin in [0,1] and nonblank model_version. p_profit50_first is only a tie/secondary metric. Each displayed percentage must trace to source_table + opportunity_key and match source report_date, ticker, expiry, exact strategy and short_strike, with latest completed-session sigma_as_of. No CC/WHEEL_CC alias transfer, ticker-only join, 1−delta approximation, old score for a new strike, autonomous p-threshold gate, or EM model-math change. Report date and each ledger's source date may differ; match the actual source row, not an invented common date.

These are modeled measurements, not verified empirical win rates. p_win_expiry means short-leg OTM at expiry, not full covered-call net profit or spread breakeven profit. Missing, invalid, event-invalid or stale-vintage measurements stay unranked with reasons, never 0%. Qualitative confidence does not replace probability ranking.

Nullable p_profit50_first and calib_cov_30d remain N/A with extra skepticism and a disclosed limitation, not zero or an automatic threshold gate. Missing p50/calibration alone does not invalidate an otherwise valid pWin rank; any displayed p50 must be finite in [0,1] and source-matched.

### 4.0A 持仓分诊优先级 | Portfolio triage priority

<b>Snapshot as-of:</b> {{PORTFOLIO_SNAPSHOT_AS_OF}}.<br />
<b>Existing-book first action:</b> {{PORTFOLIO_TRIAGE_ACTION}}.<br />
<b>Income separation:</b> {{EXISTING_BOOK_CREDIT_EXCLUSION}}.<br />
<b>CC coverage:</b> {{VERIFIED_UNENCUMBERED_SHARES_AND_UNIT_ALLOCATION}}. NEW CC/WHEEL_CC overwrite proposals require verified, already owned, unencumbered 100 shares per unit in the account snapshot, net of other coverage commitments. Retain share_ownership_verified, shares_available and share_snapshot_as_of; show stock_capital_per_contract as share-capital context, not additional premium. No naked call, assumed ownership, invented shares or recounting prior premium. Unknown account capacity is not risk-approved sizing.

### 4.1 收入板 | Selected income board

**Allocation:** Soft target approximately 20 strategy units: 6 SP/CSP/WHEEL_CSP : 4 CC/WHEEL_CC : 10 BPS/BCS (30/20/50), at most 2 units per ticker across all strategies. A spread unit includes both legs; do not count legs as extra units. Screen all three existing ledgers first: option-opportunity-davis, csp-bps-opportunity, uw-option-opportunities; UW supplies new quote/contract validation and Supabase supplies ledger candidates plus EM measurement audit. Never substitute agent-created tickets for scarce ledger supply.

Attempt the approximate mix using the highest modeled p_win_expiry among independently eligible supply; fill missing-category slots from remaining independently eligible, scored and risk-approved supply, up to 20. Never manufacture CC shares, probabilities or approved units. CAUTIOUS / DEFENSIVE do not blanket-block puts; retain sizing and 1.25% / 0.5% NLV credit_at_risk caps plus all ticket-level checks. Disclose fewer units, actual mix/deviation and gross-credit shortfall rather than forcing quotas.

<b>Selection mode:</b> ranked_30_20_50 (future default for every regime). full_50_50 and regime_spread_only are legacy archive modes only.<br />
<b>Actual units / mix / deviation:</b> {{ACTUAL_STRATEGY_UNITS_MIX_AND_DEVIATION}}.<br />
<b>Supply / account-risk constraints:</b> {{INDEPENDENT_ELIGIBILITY_AND_RISK_CAPACITY_SUMMARY}}.

<table fit-page-width="true" header-row="true">
<tr>
<td>Rank</td>
<td>标的 Ticker</td>
<td>策略 Strategy / units</td>
<td>合约腿 Contract legs / short strike</td>
<td>到期 Expiry</td>
<td>DTE</td>
<td>Win probability p_win_expiry</td>
<td>p50 p_profit50_first</td>
<td>Model vintage sigma_as_of / model_version</td>
<td>EM source_table + opportunity_key / source report_date</td>
<td>Credit per unit / total / quote status</td>
<td>Quote as-of</td>
<td>Collateral / share coverage / max loss / ROC</td>
<td>财报 Earnings</td>
<td>解释性置信度 Qualitative confidence</td>
<td>仓位与账户检查 Sizing / account checks</td>
<td>冲突 / 重叠 Conflict / overlap</td>
<td>理由 Rationale</td>
<td>报价与证据来源 Quote / evidence source</td>
<td>开仓验证 Opening validation</td>
</tr>
{{INCOME_BOARD_ROWS_MAX_20}}
</table>

Show selected rows only; qty contributes to the 20-unit ceiling and ticker cap. Both probability cells trace to the exact EM source key/vintage in their row; no unmatched percentages. Keep all raw ledger and score rows in local JSON snapshots, never append raw ledgers to this report. Generated table cells must contain plain text only, with each tr/td element on its own line; use no th or formatting inside td.

### 4.1a 未排名 / 待验证队列 | Unranked / pending-validation queue

{{UNRANKED_QUEUE_IDENTITIES_REASONS_AND_RECOVERY_ONLY}}

Use identity/status/reason summaries only, not raw ledger extracts or ranked percentages. Unverified/unapproved/unselected tickets contribute no counted credit; missing probability is not zero probability.

### 4.2 四周作战计划与财报后重设 | Four-week operating plan & post-print re-strike

<table fit-page-width="true" header-row="true">
<tr>
<td>周 Week / date range</td>
<td>催化剂 Catalysts</td>
<td>存量 / 收入动作 Book / income action</td>
<td>财报后重设队列 Post-print re-strike</td>
<td>仅当条件 / 否决 Only-if conditions / vetoes</td>
<td>允许策略与配比 Permitted structures / mix</td>
<td>Credit treatment</td>
</tr>
{{FOUR_WEEK_QUEUE_ROWS}}
</table>

Preserve all seven fields for four concrete weeks, with named catalysts and post-print candidates; apply the new approximate 6/4/10 mix and ticket-level policy. New strikes require newly matched stored EM measurements after DP/GEX/event checks. No generic re-screen-only rows, assumed CC shares or counted old/unverified premium.

<b>Operating rule:</b> {{QUEUE_NOT_A_TICKET_RULE}}.

### 4.3 替补、排除与周度 / 月度预算 | Alternates, exclusions & weekly/monthly budget

- <b>First alternate:</b> {{FIRST_ALTERNATE_OR_NONE}}
- <b>Not on board:</b> {{EXCLUSIONS_AND_AUDITABLE_REASONS}}
- <b>Gross-credit shortfall:</b> {{VERIFIED_GROSS_CREDIT_AND_TARGET_SHORTFALL}}

<table fit-page-width="true" header-row="true">
<tr>
<td>周 Week</td>
<td>日期范围 Date range</td>
<td>事件 Events</td>
<td>目标 Credit target</td>
<td>置信度 / 假设 Confidence / assumptions</td>
<td>备注 Notes</td>
</tr>
{{BUDGET_ROWS}}
</table>

<b>Monthly total:</b> {{MONTHLY_TOTAL_AND_CC_INCLUSION_STATUS}}. $5–6k/week and $20–24k across four independently rebuilt weeks are gross-credit planning ranges, not profit promises. Any broader $30k ambition requires later independent verification; new verified CC proposal credit is separate from prior premium. Do not force income by relaxing eligibility, DTE or risk limits.

---

## Part 5 · 数据缺口 | Data Gaps

{{DATA_GAPS_CONTROL_STATEMENT_ONLY}}

---

## Part 6 · 执行要点 | Execution Rules

1. {{ORDER_DISCIPLINE}}
2. {{STOP_DISCIPLINE}}
3. {{RAW_VS_EFFECTIVE_REGIME_RULE}}
4. {{CONFLICT_TREATMENT_RULE}}
5. {{EARNINGS_WINDOW_RULE}}
6. {{SYSTEM_WIDE_INVALIDATION_RULE}}. Regime downgrade or stress triggers mean risk review, sizing reduction and ticket-level revalidation, not automatically stopping all new SP/CSP. Apply specific account/event/data vetoes to their affected scope and explain it.
7. This plan is not a transaction instruction. If the user independently chooses to act, first re-check live price, spread, liquidity, event calendar, contract terms, fees, suitability, and platform support.

---

## 数据来源、方法与免责声明 | Sources, Methodology & Disclaimer

<table fit-page-width="true" header-row="true">
<tr>
<td>来源 Source / dataset</td>
<td>会话或时点 Session / as-of</td>
<td>覆盖 Coverage</td>
<td>方法或版本 Method / version</td>
<td>已知限制 Known limitation</td>
</tr>
{{SOURCE_PROVENANCE_ROWS}}
</table>

研究分析，非投资建议。Research analysis only; not investment advice.
</toggle>
