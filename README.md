# 日本经济与日元汇率模型分析 · Japan External Economy & Yen Monitor

一份关于日本对外资产负债表、跨境资金流动与日元汇率的深度分析报告，
带**自动数据刷新**：GitHub Actions 每个工作日两次从官方来源抓取最新市场数据，
页面据此重算派生指标并检查情景阈值。

A deep-dive report on Japan's external balance sheet, cross-border capital flows and
the yen — with an **auto-refresh** layer: a GitHub Action pulls the live series from
official sources twice每 weekday, and the page recomputes the derived metrics and
re-checks the scenario thresholds against them.

---

## 快速开始 / Quick start

```bash
# 1. 建仓并推送
gh repo create japan-yen-monitor --public --source=. --remote=origin --push
# 或手动：
#   git init && git add -A && git commit -m "init"
#   git remote add origin https://github.com/<you>/japan-yen-monitor.git
#   git push -u origin main

# 2. 开启 GitHub Pages
#    Settings → Pages → Source: Deploy from a branch → main / (root)
#    页面地址：https://<you>.github.io/japan-yen-monitor/

# 3. 允许 Action 写回仓库
#    Settings → Actions → General → Workflow permissions
#      → Read and write permissions  ✅

# 4. 先手动跑一次，验证抓取
#    Actions → "Refresh market data" → Run workflow
```

跑完后 `data/latest.json` 会被提交，页面右上角的「数据状态」会变成绿色。

---

## 目录结构 / Layout

```
.
├── index.html                    报告本体（单文件，内联 CSS/JS）
├── data/latest.json              最新数据（由 Action 写入）
├── scripts/refresh.py            抓取脚本
├── .github/workflows/refresh.yml 定时与手动触发
└── README.md
```

---

## 数据来源 / Data sources

抓取脚本只抓**会变的市场数据**。年度统计（IIP、国际收支、资金循环）的更新频率是
季度/年度，不适合每日轮询，需人工更新报告正文。

| 字段 | 来源 | 频率 |
|---|---|---|
| USD/JPY | FRED `DEXJPUS` | 日 |
| 美债 10y / 2y / 30y | FRED `DGS10` / `DGS2` / `DGS30` | 日 |
| 联邦基金有效利率 | FRED `DFF` | 日 |
| 日本 BIS 实际有效汇率 | FRED `RBJPBIS` | 月 |
| 日本隔夜拆借利率 | FRED `IRSTCI01JPM156N` (OECD) | 月 |
| 日本 CPI 指数 | FRED `JPNCPIALLMINMEI` | 月 |
| **JGB 2/10/30/40y** | **財務省 国債金利情報 `jgbcm.csv`** | **日** |
| 国际收支（经常收支） | 財務省 国際収支総括表 CSV | 月/半期 |

> JGB 用的是财务省日次公表的一手 CSV（Shift-JIS），不是二手数据商。

### 派生指标（前端与脚本各算一次，互为校验）

- 美日 10 年 / 2 年 / 政策利差
- 日美实际政策利率与实际利差
- **对冲后美债 10y vs JGB 10y** —— 报告第 7.2 节的核心计算
  - ⚠️ 对冲成本 = 联邦基金 − 日本隔夜 + **假设基差 0.20pp**。
    基差是**假设值，不是抓取值**；脚本在 `_hedge_note` 里显式标注。

---

## 「自动调整结论」是什么、不是什么

**是：** 页面在拿到新数据后会
1. 重算全部派生指标（利差、实际利率、对冲后收益比较）；
2. 用**写死的规则**检查情景阈值是否被突破，并在页面顶部给出判定
   （例如：`USD/JPY > 163` → 触及情景 B 的下沿；`对冲后 JGB 优势 < 0` →
   第 7.2 节的核心论点被推翻）；
3. 显示每个数字**自报告发布以来**的变化。

**不是：** 它**不会**用 LLM 重写分析文本。报告里的因果推演、模型评价和情景概率
是人（我）的判断，需要人重新做。自动层只负责**让数字保持最新、并诚实地告诉你
哪些结论已经被数据推翻**。

这是有意的设计：一个会自己改写自己结论的报告，读者无法分辨哪部分有证据支撑。

---

## 数据可信度约定

报告正文里的每个数字都带标记：

- <kbd>已核实</kbd> —— 来自一手机构（MOF / BOJ / IMF / METI / JETRO / GPIF /
  美联储 H.15 / 内閣府 SNA），本次已逐项检索确认
- <kbd>概数</kbd> —— 来自二手报道，未复核到一手表格
- <kbd>假设</kbd> —— 计算中使用的假设值（如交叉货币基差）

第十章明确列出**无法核实**的项目，未用推断填补。

---

## 已知限制 / Known limitations

1. **年度统计不会自动更新。** IIP（每年 5 月）、国际收支年度值（每年 5 月）、
   资金循环（每季）需要人工更新正文表格。
2. **MOF CSV 的表头在部分环境下乱码**，脚本靠列位置解析；财务省若调整列顺序会静默出错。
   脚本会把 BOP 值标为「参考值」。
3. **交叉货币基差是假设值**（0.20pp）。要精确需要彭博/路透报价。
4. **抓取失败时保留旧值并标 stale**，绝不写猜测值。页面会显示 stale 标记。
5. 首次运行前 `data/latest.json` 里是报告发布时的基准值。

---

## 本地测试

```bash
python scripts/refresh.py          # 抓取并写入 data/latest.json
python -m http.server 8000         # 然后打开 http://localhost:8000
```

---

## 许可 / License

报告内容 CC BY 4.0；脚本 MIT。
报告中的分析判断不构成投资、法律或税务建议。
