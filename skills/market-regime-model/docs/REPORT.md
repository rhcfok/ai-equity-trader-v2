# 市場狀態轉換偵測模型：Wasserstein K-means × Trend Hybrid
### 精準捕捉牛市、熊市與震盪市之間的轉換點

**日期**：2026-11-07　|　**資料**：SPY 日資料 1993-01-29 → 2026-11-06（8,502 交易日）
**範圍**：樣本外走前驗證（walk-forward）2005-01 → 2026-11（約 21.8 年）

---

## 1. 研究背景

本專案源自 [Not A Hedge Fund 的 Substack note](https://substack.com/@notahedgefund/note/c-380548948)，該文介紹 Horváth、Issa & Muguruza 的論文
**《Clustering Market Regimes using the Wasserstein Distance》**（[arXiv:2110.11848](https://arxiv.org/abs/2110.11848)，2021；期刊版見 [Risk.net JCF 2024](https://www.risk.net/journal-of-computational-finance/7959937/clustering-market-regimes-using-the-wasserstein-distance)）。

### 1.1 論文核心方法

- **資料表示**：把每個滾動視窗內的報酬轉成**經驗分布**（排序後的次序統計量，每個原子權重 1/N）——不用任何參數假設（不是常態、不是動差）。
- **Wasserstein 距離**（論文 Eq. 21）：兩個等原子數分布 μ、ν 的排序向量 (αᵢ)、(βᵢ)：

  Wp(μ,ν)ᵖ = (1/N) Σᵢ |αᵢ − βᵢ|ᵖ

  一維情形最優傳輸映射就是單調配對，故此式是精確解。
- **Wasserstein 重心**（barycenter，Eq. 22）：p=1 取逐分量中位數；p=2 取逐分量平均。
- **聚類品質**：以高斯核 MMD² 的簇內中位數（self-similarity，Eq. 13）衡量，較低 = 簇內更同質。
- **論文結果**（SPY 小時資料 2005–2020，k=2）：WK-means 除了抓到 2008 金融海嘯與 2020 疫情崩盤，還抓到 2010 歐債、2011 美債降評、2015/16 中國股災等次級事件，表現優於動差 k-means（MK-means）與 HMM。合成跳躍模型（Merton jump diffusion）上 regime-on 準確率 86.9% vs MK-means 27.3%、HMM 0.66%。

### 1.2 相關文獻

- Hamilton (1989) 兩狀態馬可夫轉換模型是狀態偵測的祖師爺；HMM 的已知弱點：參數多、轉移機率固定、狀態辨識滯後、易來回抖動（見 [Hamilton 2005 綜述](https://econweb.ucsd.edu/~bharmilto/palgrav1.pdf)）。
- 後續延伸：Luan & Hamp (2023)《[Automated Regime Detection in Multidimensional Time Series Using Sliced Wasserstein K-Means](https://arxiv.org/abs/2310.01285)》把方法推廣到多維時間序列（2025 期刊版見 [AIMS DSFE](https://www.aimspress.com/article/doi/10.3934/DSFE.2025016)）。

---

## 2. 本模型（本專案的設計）

### 2.1 純 WK-means 引擎（依論文改為日頻）

| 項目 | 論文設定 | 本專案設定 | 理由 |
|---|---|---|---|
| 資料頻率 | 小時 | **日**（log return） | 一般使用者可負擔的頻率 |
| 視窗長度 | h₁=35（約1週小時bar） | **w=40 日**（約2個月） | 對應同等資訊量；w=20/60 做敏感度 |
| 視窗步進 | h₂=28（重疊） | 1 日（每日更新） | 要抓「轉換點」需要日頻標籤 |
| 簇數 k | 2（多/空） | **3（牛/震/熊）** | 原筆記與使用者的三分法 |
| 距離 | Wp | **W2**（p=2，重心=逐分量平均） | 較平滑、收斂快；p=1 亦支援 |

每個交易日 t，模型看「以 t 結尾的 40 天報酬分布」，與 3 個 Wasserstein 重心比對：

1. **軟歸屬**：P(簇j|視窗t) ∝ exp(−W2²/T)，T=距離平方的中位數（尺度無關 softmax）→ 得到每日的狀態機率路徑。
2. **遲滯（hysteresis）**：新狀態須連續 m=3 天機率 ≥ θ=0.55 才切換；機率 ≥ 0.85（崩盤逃生門）立即切換。論文只用視窗重疊隱式平滑；這裡顯式化，直接抑制假訊號。
3. **語義標籤**：每次重訓後按重心年均報酬排序自動映射 Bull/Choppy/Bear（防 label switching）。

### 2.2 誠實的樣本外協議（無前視偏差）

- 首 12 年（1993–2004）訓練，**2005 年起每日只有當日以前資料**。
- 每 252 個交易日以暖啟動（warm start）重訓重心；HMM 基準每 126 日重訓、以精確 forward filter（非平滑）算過濾機率。
- 回測訊號 t 日收盤決定、t+1 日才套用（1 日 lag），換倉成本 2bp。

### 2.3 關鍵發現：純分布聚類抓不住「陰跌熊」

在 40 日視窗下，緩慢陰跌（如 2022 年 −25% 走了 9 個月）的**日報酬分布**與震盪市極為相似（日均 −0.1% vs 0%，波動 25% vs 20%）——分布形狀主要反映波動與尾部，不反映漂移。實測：

- 純 WK-means（k=3）在 2022 全年標為「Choppy」（降低曝險至 25% 而非 0%）；
- k=4 實驗：多出的第 4 簇確實是「陰跌簇」（年化 −18%、波動 30%），但 2022 視窗只有 40% 落在該簇、2018Q4 只有 6% —— 純分布法**原理上**無法穩定區分陰跌與震盪；
- 基準 HMM 有同樣問題的另一面（亂跳，7.7 次/年）。

### 2.4 解法：WK × Trend 混合模型（headline）

標準系統化做法是「波動狀態 × 趨勢狀態」雙因子分解。本模型：

- **分布狀態**（WK-means）：牛市 / 震盪 / 崩跌；
- **趨勢狀態**：126 日動能的 **Schmitt 觸發器**（±2% 死區，杜絕零點附近的抖動；參數經 40 格網格測試，126d×2% 為最佳平台）。

合成規則：

| 分布狀態 \ 趨勢 | 上（> +2%） | 下（< −2%） |
|---|---|---|
| **Bull** | **Bull** | Choppy（末段防禦） |
| **Choppy / Bear** | Choppy | **Bear**（含陰跌熊） |

---

## 3. 結果

### 3.1 熊市轉換偵測（5 個 ≥15% 回檔事件，全數命中）

| 事件 | 峰值 | −10% 警訊日 | 最大回檔 | 模型轉 Bear 日 | 領先/落後 |
|---|---|---|---|---|---|
| 金融海嘯 | 2007-10-09 | 2008-01-08 | −55.2% | **2007-11-09** | **領先 60 天** |
| 2018 Q4 | 2018-09-20 | 2018-12-14 | −19.3% | **2018-11-20** | **領先 24 天** |
| 疫情崩盤 | 2020-02-19 | 2020-02-27 | −33.7% | 2020-03-09 | 落後 11 天（離底 3/23 前一週） |
| 2022 熊市 | 2022-01-03 | 2022-02-22 | −24.5% | 2022-02-22 | **當日** |
| 2025 關稅崩跌 | 2025-02-19 | 2025-03-13 | −18.8% | 2025-04-07 | 落後 25 天（恰為底部日） |

對照：純 WK-means 只中 2/5（中位落後 138 天）；HMM 中 4/5 但換倉 7.7 次/年、訊號抖動大。

### 3.2 樣本外回測（2005-01 → 2026-11，Bull=100% / Choppy=25% / Bear=0%，2bp 成本）

| | **WK×Trend** | WK-means | MK-means | HMM | Buy & Hold |
|---|---|---|---|---|---|
| CAGR | 8.0% | 8.6% | 7.9% | 8.1% | 8.8% |
| 年化波動 | 10.7% | 11.7% | 12.4% | 11.0% | 18.9% |
| **Sharpe** | **0.772** | 0.767 | 0.677 | 0.761 | 0.541 |
| **MaxDD** | **−14.5%** | −18.2% | −31.3% | −19.0% | **−59.6%** |
| Calmar | **0.553** | 0.472 | 0.254 | 0.425 | — |
| 換倉次數/年 | 3.6 | 1.6 | 3.8 | 7.7 | 0 |

最大回檔從 −59.6% 壓到 **−14.5%**，Sharpe 從 0.54 提升到 0.77，CAGR 只讓 0.8 個百分點——轉換偵測的價值主要在**尾部風險消除**。

### 3.3 簇品質（MMD self-similarity，論文指標，越低越同質）

| | Bull 簇 | Bear 簇 | Choppy 簇 |
|---|---|---|---|
| WK-means | 0.0199 | 0.0464 | 0.0321 |
| MK-means | 0.0309 | 0.0220 | 0.0381 |

WK 的牛市簇（市場多數時間）明顯更同質；與論文「WK 簇內一致性更高」的結論方向一致。

### 3.4 敏感度（模型不是刀鋒上的參數）

- **趨勢參數**（mom×band 網格）：126d 搭配 1–5% 死區全落在 Sharpe 0.77±0.01、偵測 5/5 的平台上；63d 反應最快（領先 8 天）但 Sharpe 較低；252d 太慢。
- **視窗/簇數**：w=40/k=3 最佳（Sharpe 0.77）；w=20（0.70）、w=60（0.64）遞減，k=2（0.54）明顯差——三分狀態是對的。
- **遲滯**（θ×m 網格）：見 `out/sensitivity_theta_m.csv`，θ=0.45–0.65、m=1–5 的 Sharpe 都在 0.7 上下穩定。

### 3.5 目前狀態（截至 2026-11-06 收盤）

- **混合模型：Bull**（自 2026-04-09 起，約 7 個月）
- 分布狀態：Bull；P(Bull)=0.65、P(Choppy)=0.35、**P(Bear)=0.00**
- 解讀：11/5 單日 −4.4% 尚未改變狀態，但震盪機率已升至 0.35——若續跌使 126 日動能跌破 −2% 且分布狀態轉 Choppy，模型將轉 Bear。這正是「機率 + 遲滯」設計的用意：不因單日暴跌誤發訊號，但會在證據累積後快速切換（參考 2020-03-09 崩盤中及時轉 Bear、2025-04-07 底部日轉 Bear 後 6 月轉回）。

---

## 4. 侷限與後續方向

1. **單資產、單頻率**：僅 SPY 日資料。Luan & Hamp (2023) 的 sliced-Wasserstein 可推廣到（報酬、成交量、跨資產）多維。
2. **趨勢因子非分布法**：混合模型引入動能是對論文方法的務實補強，而非其一部分；兩因子權重（目前是硬規則）可改為機率軟融合。
3. **參數校準**：w、k、θ、m、mom、band 六個參數以 SPY 歷史校準，跨市場（A 股、港股、商品）需重校準——論文自己也提醒高頻使用前須校準。
4. **成本模型簡化**：2bp 固定成本；實務槓桿/滑點會侵蝕 3.6 次/年的換倉。
5. **2020 疫情型閃崩**：任何 40 日視窗法都只能「快速跟進」、無法事前偵測；需搭配期权/GEX 等前瞻指標（原 note 對選單交易者的啟發正在於此）。
6. **機率溫度**：軟歸屬的 T 取中位距離，屬啟發式；可改為廣義 softmax / PLLS 校準。

## 5. 使用方式

```bash
cd "D:\AI projects\AI-trading\regime-model"
../.venv/Scripts/python.exe -I run_all.py     # 全流程：下載→擬合→回測→圖表
../.venv/Scripts/python.exe -I test_smoke.py  # 單元測試（合成資料/遲滯/濾波器）
```

**檔案結構**

| 檔案 | 內容 |
|---|---|
| `wkmeans.py` | Wp 距離、重心、WK-means、軟歸屬、MMD 指標 |
| `baselines.py` | MK-means（動差+歐氏）、Gaussian HMM 包裝 |
| `pipeline.py` | 資料、視窗、走前驗證、forward filter、遲滯、混合模型 |
| `backtest.py` | 回測、績效、回檔事件、偵測落後 |
| `run_all.py` | 一鍵流程與所有圖表 |
| `out/` | 圖（fig1–fig5）、CSV（轉換日/偵測/績效/敏感度）、summary.json |

**每日更新**：重跑 `run_all.py`（yfinance 自動抓最新收盤），看 `out/summary.json` 的 `regime` 與 `probabilities`。

## 6. 主要參考文獻

- Horváth, Issa & Muguruza (2021), *Clustering Market Regimes using the Wasserstein Distance*, [arXiv:2110.11848](https://arxiv.org/abs/2110.11848)
- Luan & Hamp (2023), *Automated Regime Detection in Multidimensional Time Series Using Sliced Wasserstein K-Means*, [arXiv:2310.01285](https://arxiv.org/abs/2310.01285)
- Hamilton (1989), *A New Approach to the Economic Analysis of Nonstationary Time Series*, Econometrica
- Not A Hedge Fund (2026-11-06), [Substack note](https://substack.com/@notahedgefund/note/c-380548948)
