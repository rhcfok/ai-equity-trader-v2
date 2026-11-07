---
name: market-regime-model
description: 每日偵測 SPY/QQQ/SOXX 的 Bull/Choppy/Bear 市場狀態——Wasserstein k-means × 126日趨勢混合模型（Horváth–Issa–Muguruza 2021），參數凍結、走前驗證、無前視偏差。跑 scripts/run_daily.py 產出報告（regime + P(Bull)/P(Choppy)/P(Bear) + 近期轉換 + 綜合曝險），並以帶日期的 <toggle> 推送到 Notion 頁面 411b8852cf00804288d5f102d16618dc。觸發：市場狀態報告 / regime report / 牛熊震盪偵測 / push regime to Notion / 每日狀態更新 / market regime。
---

# market-regime-model：SPY/QQQ/SOXX 牛熊震盪狀態偵測

用分佈形狀（Wasserstein k-means，不放參數假設）抓「崩跌與平靜」，用 126 日動能
Schmitt 觸發器（±2% 死區）補上分佈法原理上抓不到的「陰跌熊」，合成三狀態標籤。
所有參數凍結於 SPY 校準值並跨資產轉移驗證（QQQ、SOXX Sharpe ≥0.81、回檔砍半，
見 `docs/REPORT_MULTI.md`；方法與限制見 `docs/REPORT.md`）。

## 每日執行

```bash
cd "<repo>/skills/market-regime-model/scripts"
"D:/AI projects/AI-trading/.venv/Scripts/python.exe" -I run_daily.py
```

- 耗時約 1–2 分鐘：下載最新日線（yfinance）→ 三標的走前驗證重跑 → 寫
  `../out/summary_multi.json`、`../out/<TICKER>/…` 與 `../out/regime_report_<date>.md`，
  並把報告印在 `---REPORT-BEGIN---` / `---REPORT-END---` 之間。
- 失敗重試一次；再失敗就推送「pipeline FAILED」一行通知，不要靜默跳過。

## 推送到 Notion（帶日期的 toggle）

固定頁面：`411b8852cf00804288d5f102d16618dc`（🔁 Market regime model (SPY/QQQ/SOXX)，
parent `ai-trader-v2`）。每次推送**在頁尾追加一個** toggle，不動歷史 toggle；
推送前先 fetch 檢查同日標題是否已存在（存在即跳過）。

格式（Notion-flavored markdown，標題 = 當天一行摘要，內文 = 報告 md）：

```
<toggle heading="h3">📊 Market regime report 2026-11-08 — SPY Bull / QQQ Choppy / SOXX Bear

（regime_report_<date>.md 的內容，pipe table 照貼）

</toggle>
```

## 判讀

| 標籤 | 意義 | 曝險 |
|---|---|---|
| Bull | 分佈=牛市簇 且 126日動能 > +2% | 100% |
| Choppy | 分佈惡化但趨勢未破，或牛市末段防禦 | 25% |
| Bear | 分佈非牛市 且 動能 < −2%（含陰跌熊） | 0% |

- 跨標的的分層（如 SOXX 先翻 Bear → QQQ 轉 Choppy → SPY 仍 Bull）＝風險擴散訊號。
- 偵測不是預測：單日閃崩用遲滯吸收（有意為之）；2 天～5 週的 V 型修正會漏。
- Bear 之後 63 日前瞻報酬歷史為正——Bear 是「暫離」，不是做空訊號。

## 排程

ZCode automation：工作日（週一–五）09:00 本地時間，cron `0 9 * * 1-5`，
執行上述「每日執行 + 推送」兩步。
