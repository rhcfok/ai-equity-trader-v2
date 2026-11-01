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
<th>支柱 Pillar</th>
<th>权重 Weight</th>
<th>得分 Score</th>
<th>证据 Evidence</th>
<th>来源与时点 Source / as-of</th>
<th>状态 Data status</th>
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

<b>System-wide invalidation / 全局失效条件:</b> {{REGIME_INVALIDATION_TRIGGER}}.

### 执行规则 | Playbook

<table fit-page-width="true" header-row="true">
<tr>
<th>A 级 Tier A</th>
<th>B 级 Tier B</th>
<th>允许策略 Permitted strategies</th>
<th>裸 CSP Naked CSP</th>
<th>DTE 上限 DTE cap</th>
<th>信用风险 Credit-at-risk</th>
<th>财报处理 Earnings treatment</th>
<th>降档行动 Downgrade action</th>
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
<th>数据集 Dataset</th>
<th>最新日期 Latest</th>
<th>滞后 Age trading days</th>
<th>预期节奏 Expected cadence</th>
<th>状态 Status</th>
<th>例外与影响 Exception / impact</th>
</tr>
{{FRESHNESS_ROWS}}
</table>

### 2.2 对账总览 | Reconciliation overview

<table fit-page-width="true" header-row="true">
<tr>
<th>Universe</th>
<th>Confirmed</th>
<th>Conflict</th>
<th>Partial</th>
<th>Confirmed bullish</th>
<th>Confirmed bearish</th>
<th>No-data / protective</th>
<th>Frozen snapshot</th>
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
<th>标的 Ticker</th>
<th>Agent verdict</th>
<th>Premium today / 3d</th>
<th>net_bias_z</th>
<th>Dark pool / session</th>
<th>Composite</th>
<th>冲突类型 Conflict type</th>
<th>必需行动 Required action</th>
</tr>
{{FULL_CONFLICT_ROWS}}
</table>

Show every conflict, or explicitly label a shortened table as a sample and link to its full canonical register. A conflict cannot be summarized as a clean one-directional conviction.

### 2.4 交叉证据 | Cross-evidence

<table fit-page-width="true" header-row="true">
<tr>
<th>标的或范围 Ticker / scope</th>
<th>来源与方法 Source / method</th>
<th>窗口与时点 Window / as-of</th>
<th>观察 Observation</th>
<th>一致性 Agreement</th>
<th>操作影响 Operational implication</th>
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

{{TIER_CROSS_SOURCE_COMPARISON}}

### 3.1 A 级状态 | Tier A status

{{TIER_A_GATING_EXPLANATION}}

### 3.2 B 级执行板 | Tier B execution board

<table fit-page-width="true" header-row="true">
<tr>
<th>标的 Ticker</th>
<th>方向与类型 Side / order type</th>
<th>触发 Trigger</th>
<th>止损 Stop</th>
<th>止损 % Stop %</th>
<th>RRR</th>
<th>财报 Earnings / safety</th>
<th>STLDC</th>
<th>Dark pool</th>
<th>Unusual flow</th>
<th>Flow verdict</th>
<th>对账 Reconciliation</th>
<th>冲突行动 Conflict action</th>
<th>Alerts / coverage</th>
<th>仓位 Size</th>
<th>来源与时点 Source / as-of</th>
</tr>
{{TIER_B_ROWS}}
</table>

### 3.3 确认引用 | Confirmation references

{{TIER_CONFIRMATION_REFERENCES}}

---

## Part 4 · 期权收入板 | Options Income Plan

<callout icon="📝" color="blue_bg">
All contracts below are proposed and unsubmitted. Do not populate this board when live option bid/ask, expiry, underlying price, event risk, contract terms, and source provenance cannot be validated for the decision timestamp.
</callout>

### 4.0 护栏、持仓语境与置信度 | Guardrails, portfolio context & confidence

<b>DTE hierarchy:</b> {{DTE_RULE_HIERARCHY}}<br />
<b>Earnings exclusions:</b> {{EARNINGS_EXCLUSIONS}}<br />
<b>Confidence:</b> High = {{HIGH_CONFIDENCE_RULE}}; Medium = {{MEDIUM_CONFIDENCE_RULE}}; Low = {{LOW_CONFIDENCE_RULE}}.<br />
<b>Quote status:</b> {{QUOTE_STATUS_LEGEND}}.

### 4.0A 持仓分诊优先级 | Portfolio triage priority

<b>Snapshot as-of:</b> {{PORTFOLIO_SNAPSHOT_AS_OF}}.<br />
<b>Existing-book first action:</b> {{PORTFOLIO_TRIAGE_ACTION}}.<br />
<b>Income separation:</b> {{EXISTING_BOOK_CREDIT_EXCLUSION}}.

### 4.1 收入板 | Income board

<table fit-page-width="true" header-row="true">
<tr>
<th>Rank</th>
<th>标的 Ticker</th>
<th>策略 Strategy</th>
<th>合约腿 Contract legs</th>
<th>到期 Expiry</th>
<th>DTE</th>
<th>Credit / quote status</th>
<th>Quote as-of</th>
<th>Max loss / ROC</th>
<th>财报 Earnings</th>
<th>置信度 Confidence</th>
<th>仓位 Sizing</th>
<th>冲突 / 重叠 Conflict / overlap</th>
<th>理由 Rationale</th>
<th>来源 Source</th>
<th>开仓验证 Opening validation</th>
</tr>
{{INCOME_BOARD_ROWS_MAX_20}}
</table>

### 4.1a 历史来源摘录（只在审计附录需要时显示）| Historical source extracts (audit appendix only)

{{INCOME_SOURCE_EXTRACTS}}

### 4.2 四周作战计划与财报后重设 | Four-week operating plan & post-print re-strike

<table fit-page-width="true" header-row="true">
<tr>
<th>周 Week / date range</th>
<th>催化剂 Catalysts</th>
<th>存量 / 收入动作 Book / income action</th>
<th>财报后重设队列 Post-print re-strike</th>
<th>仅当条件 / 否决 Only-if conditions / vetoes</th>
<th>允许策略与配比 Permitted structures / mix</th>
<th>Credit treatment</th>
</tr>
{{FOUR_WEEK_QUEUE_ROWS}}
</table>

<b>Operating rule:</b> {{QUEUE_NOT_A_TICKET_RULE}}.

### 4.3 替补、排除与周度 / 月度预算 | Alternates, exclusions & weekly/monthly budget

- <b>First alternate:</b> {{FIRST_ALTERNATE_OR_NONE}}
- <b>Not on board:</b> {{EXCLUSIONS_AND_AUDITABLE_REASONS}}

<table fit-page-width="true" header-row="true">
<tr>
<th>周 Week</th>
<th>日期范围 Date range</th>
<th>事件 Events</th>
<th>目标 Credit target</th>
<th>置信度 / 假设 Confidence / assumptions</th>
<th>备注 Notes</th>
</tr>
{{BUDGET_ROWS}}
</table>

<b>Monthly total:</b> {{MONTHLY_TOTAL_AND_CC_INCLUSION_STATUS}}.

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
6. {{SYSTEM_WIDE_INVALIDATION_RULE}}
7. This plan is not a transaction instruction. If the user independently chooses to act, first re-check live price, spread, liquidity, event calendar, contract terms, fees, suitability, and platform support.

---

## 数据来源、方法与免责声明 | Sources, Methodology & Disclaimer

<table fit-page-width="true" header-row="true">
<tr>
<th>来源 Source / dataset</th>
<th>会话或时点 Session / as-of</th>
<th>覆盖 Coverage</th>
<th>方法或版本 Method / version</th>
<th>已知限制 Known limitation</th>
</tr>
{{SOURCE_PROVENANCE_ROWS}}
</table>

研究分析，非投资建议。Research analysis only; not investment advice.
</toggle>
