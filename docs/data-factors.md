# A-ISP 数据因子清单

> 供 review 决定实施范围。按"已有 → 待新增（高/中/低）"组织，每个因子标注数据源、验证状态和用途。

---

## 数据源可用性（实测 2026-03-17）

| 数据源 | 状态 | 覆盖范围 |
|--------|------|----------|
| baostock | ✅ | 个股日线（含估值 PE/PB）、季度财报、股本结构 |
| AkShare → 同花顺 | ✅ | 板块汇总 |
| AkShare → 乐股 | ✅ | 全市场活跃度（涨停/跌停/炸板） |
| AkShare → 东方财富 push2his | ✅ | 个股资金分层（120日）、日线历史 |
| AkShare → 东方财富 push2ex | ✅ | 涨停池、昨日涨停溢价 |
| AkShare → 东方财富 datacenter-web | ✅ | 龙虎榜 |
| AkShare → 东方财富 push2 | ❌ | `stock_individual_info_em` 不可用（代理问题） |
| AkShare → 新浪 | ✅ | 交易日历、期货 |
| yfinance | ✅ | 美股/大宗商品/ETF |

---

## 一、已有因子

### 个股（`StkDaily` + 计算衍生）

| 因子 | 来源 | 说明 |
|------|------|------|
| OHLCV | baostock | 开高低收量额 |
| 涨跌幅 / 换手率 / 量比 | baostock | 日频 |
| 主力净流入 | AkShare | **总额**，不分档 |
| 总市值 | AkShare | 未区分流通 |
| ST / 涨停 / 跌停 | baostock | bool 标记 |
| RSI(6) / MACD | 计算 | 从 OHLCV 衍生，存入 `_raw_indicators` |
| MA 位置 | 计算 | MA5/10/20/60 多空对比 |
| 趋势 | 计算 | 连跌天数、累计跌幅、MA5 vs MA20 |
| 近 15 日 K 线 | 计算 | 存入 `_recent_ohlcv`，传入 LLM |
| 威科夫阶段 | 计算 | 吸筹/派发 + 支撑阻力位 |
| 突破信号 | 计算 | 箱体/均线/新高新低 |
| 量化交易计划 | 计算 | 入场/止损/目标/R:R |

### 板块（`SectorDaily`）

涨跌幅、涨跌家数、成交额、净流入、MA5/10/20/60

### 全球（`GlobalDaily`）

美股三大指数、大宗商品（金/铜/油）、中概 ETF(KWEB)、BTC 风险指标

---

## 二、待新增 — 高优先级

> 预期直接改变分析质量，全部已验证可获取。

### 2.1 市场情绪温度

**范围**：市场级，每日算一次，所有股票共享。
**用途**：判断情绪周期（冰点→修复→高潮→退潮），决定整体激进程度。

| 因子 | API | 示例值 |
|------|-----|--------|
| 两市总成交额 | `stock_sse_deal_daily` + `stock_szse_summary` | ~1.5万亿 |
| 涨停 / 跌停家数 | `stock_market_activity_legu` | 63 / 13 |
| 炸板率 | 同上 | 27.6% |
| 连板最高高度 | `stock_zt_pool_em(date)` → max(连板数) | 5 |
| 昨日涨停今日溢价 | `stock_zt_pool_previous_em(date)` → mean(涨跌幅) | -1.63% |

### 2.2 个股身位

**用途**：50亿流通盘 vs 800亿流通盘涨5%含义不同；板块属性决定涨跌停幅度；连板是短线核心语言。

| 因子 | 来源 | 说明 |
|------|------|------|
| 板块属性 | code 前缀推导 | 主板/创业板(300)/科创板(688)/北交所(8)，零成本 |
| 流通市值 | baostock `query_profit_data` → liqaShare × close | 英维克: 8.50亿股 × 97.22 ≈ 826亿 |
| 总市值 | baostock `query_profit_data` → totalShare × close | 英维克: 9.77亿股 × 97.22 ≈ 950亿 |
| 连板天数 | `stock_zt_pool_em` 或 OHLCV 计算 | 0=非涨停, 1=首板, N=N连板 |
| 是否次新 | baostock `query_stock_basic` → 上市日期 | <1年=次新 |

### 2.3 资金分层

**来源**：`stock_individual_fund_flow(stock, market)`，单次返回 120 日历史。
**用途**：区分机构建仓(超大单持续)、游资点火(大单脉冲)、散户抢筹(小单为主)。

| 因子 | 说明 |
|------|------|
| 超大单净流入（净额 + 净占比%） | 机构级别 |
| 大单净流入（净额 + 净占比%） | 主力/游资 |
| 中单净流入（净额 + 净占比%） | |
| 小单净流入（净额 + 净占比%） | 散户 |
| 主力连续流入天数 | 从超大+大单连续正值计算 |

### 2.4 位置信息

**来源**：纯计算，用已有 OHLCV 数据，不需要新 API。
**用途**：低位启动 vs 高位加速是完全不同的逻辑；累计换手判断筹码是否充分交换。

| 因子 | 说明 |
|------|------|
| 距 60 日高/低点 % | `(close - extreme) / extreme × 100` |
| 距 120 日高/低点 % | 需扩大 lookback（当前可能不够） |
| 年内涨跌幅 | 年初首个交易日 close → 当前 |
| 5/10/20 日累计换手 | 从已有 turnover_rate 累加 |

---

## 三、待新增 — 中优先级

> 有价值但有条件限制，按需实现。

### 3.1 龙虎榜

**来源**：`stock_lhb_detail_em(start_date, end_date)`，按日期批量查。
**限制**：仅涨幅偏离>7%、换手>20%、连续3日偏离>20%等触发条件的票才有。
**用途**：区分游资接力 vs 机构加仓。

| 因子 | 说明 |
|------|------|
| 是否上榜 + 上榜原因 | "涨幅偏离值达7%" 等 |
| 龙虎榜净买额 | 买入 - 卖出总额 |
| 上榜后 1/2/5/10 日涨跌 | 龙虎榜效应 |

### 3.2 融资融券

**来源**：东方财富 datacenter-web API（`RPTA_WEB_RZRQ_GGMX` 报表），沪深通用，支持批量查询。
**限制**：仅两融标的有数据（约 2000 只沪市 + 2000 只深市）。
**备用**：沪市 `stock_margin_detail_sse(date)`；深市 SZSE xlsx 直连（需 `io.BytesIO` 修复 AkShare bug）。

| 因子 | 说明 |
|------|------|
| 融资余额 (RZYE) | 融资未偿还余额 |
| 融券余量 (RQYL) | 融券未偿还股数 |
| 融资净买入 (RZJME) | 当日融资买入 - 偿还 |
| 融券净卖出 (RQJMG) | 当日融券卖出 - 偿还 |
| 融资融券余额 (RZRQYE) | 合计 |

### 3.3 业绩快照

**来源**：baostock `query_profit_data` + `query_growth_data`，按 (code, year, quarter) 查询。
**用途**：A 股核心问题不是"基本面好不好"，而是"是否超预期"。

| 因子 | baostock 字段 | 英维克 Q3'25 |
|------|--------------|-------------|
| 净利润 | `netProfit` | 4.14亿 |
| 净利率 | `npMargin` | 10.3% |
| 毛利率 | `gpMargin` | 27.3% |
| ROE | `roeAvg` | 12.8% |
| EPS(TTM) | `epsTTM` | 0.51 |
| 利润同比 | `YOYNI` | +17.6% |
| EPS 同比 | `YOYEPSBasic` | +10.8% |

### 3.4 估值

**来源**：baostock 日线自带，只需 `fetch-cn` 时多取字段，零额外成本。

| 因子 | baostock 字段 | 英维克 |
|------|--------------|--------|
| PE(TTM) | `peTTM` | 190.3 |
| PB(MRQ) | `pbMRQ` | 28.7 |
| PS(TTM) | `psTTM` | 16.9 |
| PCF(TTM) | `pcfNcfTTM` | 3067.5 |

---

## 四、不做

| 因子 | 原因 |
|------|------|
| 筹码峰 / 集中度 | 计算复杂且精度存疑 |
| 盘口 / 分时 | 系统是收盘后分析，非盘中实时 |
| 完整资产负债表 | 偏中长期，与日频信号定位不符 |
| 国内宏观 (M1/M2/PMI) | 月度数据，日频影响小 |
| 公告全文解析 | 非结构化，搜索工具已部分覆盖 |

---

## 五、实施参考

### 因子注入方式

| 因子类别 | 注入位置 | 频率 |
|----------|---------|------|
| 市场情绪温度 | system/user prompt 新段落 | 每日1次，所有股票共享 |
| 个股身位 | user prompt 股票信息段 | 随个股 |
| 资金分层 | user prompt 新段落 | 随个股 |
| 位置信息 | user prompt 趋势段扩展 | 随个股（纯计算） |
| 龙虎榜 | extra_instructions 条件注入 | 仅上榜票 |
| 业绩 / 估值 | user prompt 新段落 | 季度更新 / 日频 |

### API 调用成本

| 类别 | 调用量 | 频率 |
|------|--------|------|
| 市场情绪温度 | 3-4 次 | 每日1次（市场级） |
| 个股身位 | 1 次/股 | 季度（股本变化慢） |
| 资金分层 | 1 次/股 | 每日（120日历史一次取全） |
| 位置信息 | 0 次 | 纯计算 |
| 龙虎榜 | 1 次 | 每日（按日期批量查） |
| 融资融券 | 1 次/股 | 每日（datacenter-web API） |
| 业绩 | 1 次/股 | 季度 |
| 估值 | 0 次 | 已含在日线字段中 |

---

## 六、存储方案

原则：**同粒度加列，不同粒度建表**。

### 现有表扩列：`stk_daily`

资金分层、估值与日线同粒度 (trade_date + code)，直接加列，分两批 upsert：

```
+── 资金分层（AkShare stock_individual_fund_flow）──
  main_net            FLOAT NULL  # 主力净额
  main_pct            FLOAT NULL  # 主力净占比%
  super_large_net     FLOAT NULL  # 超大单净额
  super_large_pct     FLOAT NULL  # 超大单净占比%
  large_net           FLOAT NULL  # 大单净额
  large_pct           FLOAT NULL  # 大单净占比%
  medium_net          FLOAT NULL  # 中单净额
  medium_pct          FLOAT NULL  # 中单净占比%
  small_net           FLOAT NULL  # 小单净额
  small_pct           FLOAT NULL  # 小单净占比%

+── 估值（baostock 日线多取字段）──
  pe_ttm              FLOAT NULL
  pb_mrq              FLOAT NULL
```

### 新建表

```
── 14. market_sentiment ──
全市场情绪，每日一行，所有股票共享。
唯一约束: trade_date

  trade_date          DATE        # 唯一
  total_amount        FLOAT       # 两市总成交额（元）
  limit_up_count      INT         # 涨停家数
  real_limit_up       INT         # 真实涨停（去ST）
  limit_down_count    INT         # 跌停家数
  blast_rate          FLOAT       # 炸板率%
  max_streak          INT         # 连板最高高度
  prev_zt_premium     FLOAT       # 昨日涨停今日溢价%
  activity_rate       FLOAT       # 活跃度%

── 15. stk_profile ──
个股静态/慢变信息，季度更新。
唯一约束: code

  code                STR         # 唯一
  name                STR
  board_type          STR         # main_sh/main_sz/chinext/star/bse
  total_shares        FLOAT       # 总股本
  liq_shares          FLOAT       # 流通股本
  listing_date        DATE        # 上市日期
  updated_at          DATETIME

── 16. stk_quarterly ──
季度财报快照。quarter: 1=一季报, 2=半年报, 3=三季报, 4=年报。
唯一约束: (code, year, quarter)  索引: code 独立索引

  code                STR         # 独立索引
  year                INT         # 如 2025
  quarter             INT         # 1/2/3/4
  net_profit          FLOAT       # 净利润
  np_margin           FLOAT       # 净利率
  gp_margin           FLOAT       # 毛利率
  roe                 FLOAT       # ROE
  eps_ttm             FLOAT       # EPS(TTM)
  yoy_profit          FLOAT       # 利润同比%
  yoy_eps             FLOAT       # EPS同比%
  yoy_equity          FLOAT       # 净资产同比%
  pub_date            DATE        # 公告日期
  updated_at          DATETIME

── 17. stk_margin ──
融资融券，仅两融标的有数据（~4000只）。
唯一约束: (trade_date, code)  索引: code, trade_date 各自独立索引
来源: 东方财富 datacenter-web API (RPTA_WEB_RZRQ_GGMX)

  trade_date          DATE        # 独立索引
  code                STR         # 独立索引
  rzye                FLOAT       # 融资余额
  rzjme               FLOAT       # 融资净买入（买入-偿还）
  rqyl                FLOAT       # 融券余量（股）
  rqjmg               FLOAT       # 融券净卖出（卖出-偿还）
  rzrqye              FLOAT       # 融资融券余额合计

── 18. stk_lhb ──
龙虎榜，事件型稀疏数据。
唯一约束: (trade_date, code)  索引: code, trade_date 各自独立索引

  trade_date          DATE        # 独立索引
  code                STR         # 独立索引
  name                STR
  reason              TEXT        # 上榜原因
  net_buy             FLOAT       # 龙虎榜净买额
  buy_amount          FLOAT       # 买入总额
  sell_amount         FLOAT       # 卖出总额
  turnover_rate       FLOAT
  liq_market_cap      FLOAT       # 流通市值
  post_1d             FLOAT NULL  # 上榜后1日涨跌%
  post_2d             FLOAT NULL
  post_5d             FLOAT NULL
  post_10d            FLOAT NULL
```
