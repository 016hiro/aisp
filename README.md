# A-ISP：A 股智能策略领航员

> **A**-Share **I**ntelligence & **S**trategy **P**ilot

融合美股情绪、大宗商品联动和 AI 舆情分析的 **A 股次日交易指导系统**。

**V1 为观察模式**——仅生成交易信号并追踪绩效，不涉及仓位管理或模拟交易。

## 核心能力

- **全球数据整合**：自动采集美股指数/个股（yfinance）、大宗商品、BTC 风险偏好指标、A 股个股行情（BaoStock TCP）、行业板块涨跌（AkShare THS 源）
- **三池板块管理**：核心池（长期关注）、冲锋池（短期热点）、机会池（超跌反弹）动态轮转
- **多因子选股评分**：资金面（0.4）+ 动量（0.3）+ 技术面（0.2）+ 质量（0.1），板块内百分位排名
- **BTC 风险偏好**：5 维度复合评分（短/中/长期动量 + 波动率状态 + 回撤位置），注入 LLM prompt 和 Briefing
- **双模型 LLM 分析**：快速模型做舆情分类，强模型做个股深度分析，通过 OpenRouter 灵活切换
- **信号绩效追踪**：T+1 自动评估信号准确率，含硬止损和追踪止盈退出规则
- **每日简报**：5 节 Markdown 日报，涵盖全球情绪（含 BTC）、板块异动、观察股、信号详情和绩效回顾

## 快速开始

### 环境要求

- Python >= 3.13（推荐 3.13，3.14 alpha 存在 greenlet 兼容问题）
- [uv](https://docs.astral.sh/uv/) 包管理器

### 安装

```bash
git clone <repo-url> && cd aisp
cp .env.example .env
# 编辑 .env，填入 OpenRouter API Key

uv sync --extra all    # 安装所有依赖（含数据源 + 开发工具）
uv run aisp init-db    # 初始化数据库
```

### 配置

复制 `.env.example` 为 `.env` 并修改：

```bash
# OpenRouter API（必填）
AISP_OPENROUTER__API_KEY=sk-or-v1-xxxx
AISP_OPENROUTER__ANALYSIS_MODEL=anthropic/claude-sonnet-4
AISP_OPENROUTER__SENTIMENT_MODEL=google/gemini-2.0-flash-001

# 数据库路径（可选，默认 data/aisp.db）
AISP_DB__URL=sqlite+aiosqlite:///data/aisp.db

# 评分权重（可选，须加和为 1.0）
AISP_SCORING__WEIGHT_FUND=0.4
AISP_SCORING__WEIGHT_MOMENTUM=0.3
AISP_SCORING__WEIGHT_TECHNICAL=0.2
AISP_SCORING__WEIGHT_QUALITY=0.1
```

所有配置项均支持环境变量覆盖，前缀 `AISP_`，嵌套用 `__` 分隔。

## 使用方式

### 每日工作流

```bash
# 早盘前（07:00-08:30）—— 采集美股和大宗商品数据，运行筛选和分析
uv run aisp run-morning

# 收盘后（15:30）—— 采集 A 股数据，更新板块池，追踪信号绩效
uv run aisp run-close
```

### 单步命令

```bash
uv run aisp fetch-us             # 采集美股数据
uv run aisp fetch-commodities    # 采集大宗商品
uv run aisp fetch-cn             # 采集 A 股全市场行情（默认全量）
uv run aisp screen               # 板块过滤 + 个股评分
uv run aisp analyze              # LLM 分析 + 信号生成
uv run aisp briefing             # 生成每日简报
uv run aisp status               # 查看当前池状态和活跃信号
```

所有命令支持 `--trade-date YYYY-MM-DD` 指定交易日，默认为当天。

### A 股数据拉取模式

`fetch-cn` 支持三种模式，通过 `--mode` 参数指定：

| 模式 | 说明 | 用法 |
|------|------|------|
| `full`（默认） | 拉取 BaoStock 全市场股票（~5000 只） | `uv run aisp fetch-cn` |
| `watchlist` | 仅拉取 `config/symbols.toml` 中 `[[cn_watchlist]]` 定义的自选股 | `uv run aisp fetch-cn --mode watchlist` |
| `codes` | 仅拉取命令行指定的股票代码 | `uv run aisp fetch-cn --mode codes --codes 600519,000858` |

三种模式下，个股日线数据均通过 BaoStock（TCP 协议，不受 HTTP 代理影响）获取；板块涨跌数据独立通过 AkShare THS 源获取（`stock_board_industry_summary_ths`，90 个同花顺行业板块）。`watchlist` 和 `codes` 模式适合日常快速更新，`full` 模式适合初始化或全量刷新。

### 全局选项

```bash
uv run aisp --verbose ...        # 输出详细日志
uv run aisp --log-file app.log   # 同时写入日志文件
```

## 架构概览

```
数据采集层                           筛选层                      决策层
┌──────────────────────┐
│ 美股/大宗 (yfinance)    │──┐   ┌──────────────┐    ┌───────────────┐
└──────────────────────┘   ├──→│ 三池板块管理   │──→│ 多因子个股评分  │
┌──────────────────────┐   │   └──────────────┘   └───────┬───────┘
│ A 股个股 (BaoStock TCP) │──┘                              │
└──────────────────────┘                                  ▼
┌──────────────────────┐                          ┌───────────────┐
│ 板块涨跌 (AkShare THS)  │──→ sector_daily ──────→│  LLM 深度分析   │
└──────────────────────┘                          │       信号生成     │
┌──────────────────────┐                          └───────┬───────┘
│ 舆情 (适配器模式)        │──→ LLM 情绪分类 ──────→        │
└──────────────────────┘                                  │
┌──────────────────────┐                       ┌──────────┴──────────┐
│ BTC 风险偏好 (yfinance)  │──→ prompt/briefing ─┤                      │
└──────────────────────┘                       ▼                      ▼
                                         T+1 绩效追踪          Markdown 日报
```

### 三池板块管理

系统采用三级板块池动态管理候选板块，每个池有独立的进入/退出规则：

| 池类型 | 说明 | 进入规则 | 退出规则 |
|--------|------|----------|----------|
| **核心池 (Core)** | 长期跟踪的战略板块 | 配置文件静态指定，默认：半导体、汽车整车、光伏设备、白酒、银行 | 永不移除 |
| **冲锋池 (Momentum)** | 短期热门板块 | 当日板块涨跌幅排名前 N（默认 10） | 连续 N 个交易日（默认 5）跌出前 N 排名则移除 |
| **机会池 (Opportunity)** | 超跌反弹候选 | 当日跌幅最大的 N 个板块（默认 10），且满足：收盘价 > MA60（长期上行趋势）、当日下跌 | 连续 N 个交易日（默认 3）不再满足条件则移除 |

退出判定基于 `sector_pool_history` 表中的资格记录，向前追溯连续不达标的交易日数量，而非简单计数器。

### 多因子选股模型

对进入三池的板块内所有非 ST、非涨停个股，按以下 4 因子计算综合评分：

| 因子 | 权重 | 原始指标 | 建模方式 | 缺失值处理 |
|------|------|----------|----------|------------|
| **资金面 (Fund)** | 0.40 | 主力净流入 / 成交额 | 板块内百分位排名（0-1） | `net_inflow` 为 None 时取默认值 0.5 |
| **动量 (Momentum)** | 0.30 | 日涨跌幅 `change_pct` | 板块内百分位排名（0-1） | — |
| **技术面 (Technical)** | 0.20 | 量比 × 0.6 + 换手率适宜度 × 0.4 | 量比：百分位排名；换手率：3-8% 为理想区间（得分 1.0），低于 3% 按 `tr/3` 线性衰减，高于 8% 按 `1-(tr-8)/20` 衰减 | — |
| **质量 (Quality)** | 0.10 | log(总市值) | 板块内百分位排名（0-1） | 市值为 None 或 ≤ 0 时取默认 0.5 |

**百分位排名**：板块内所有有效值排序后，第 i 位的排名为 `i / (n-1)`，范围 [0, 1]。单只股票时排名为 0.5。

**综合评分** = Σ(权重 × 因子分)，范围 [0, 1]。每个板块取评分前 N（默认 5）的个股进入 LLM 深度分析。

### BTC 风险偏好指标

BTC 作为全球风险偏好的代理指标，通过 yfinance 获取 45 天历史数据，计算 5 个维度的复合评分（不入库，按需计算）：

| 维度 | 原始指标 | 归一化 | 权重 |
|------|----------|--------|------|
| **短期动量** | 24h 涨跌幅 | clip [-10%, +10%] → 线性映射 [0, 1] | 0.15 |
| **中期趋势** | 7d 涨跌幅 | clip [-20%, +20%] → 线性映射 [0, 1] | 0.25 |
| **长期趋势** | 30d 涨跌幅 | clip [-30%, +30%] → 线性映射 [0, 1] | 0.25 |
| **波动率状态** | 7d 实现波动率 / 30d 实现波动率 | ratio 映射到 [0.5, 2.0]，反转（低 ratio = 稳定 = 高分） | 0.20 |
| **位置指标** | 距 30 日高点回撤幅度 | clip [-30%, 0%] → 线性映射 [0, 1] | 0.15 |

- 实现波动率 = 日对数收益率标准差 × √252（年化）
- 综合评分 `risk_score` ∈ [0, 1]：**> 0.65** 强风险偏好（risk-on）、**0.35-0.65** 中性、**< 0.35** 弱风险偏好（risk-off）
- 集成点：注入 LLM 分析 prompt 的全球市场联动段落 + Briefing Section 1 全球情绪面板

### 双模型 LLM 策略

通过 OpenRouter API 调用，使用 `asyncio.Semaphore(5)` 控制并发：

| 模型角色 | 默认模型 | 用途 | 输入 |
|----------|----------|------|------|
| **情绪模型** (sentiment_model) | `google/gemini-2.0-flash-001` | 批量分类舆情评论情绪 | 每批 10 条评论，分类为 bullish/bearish/neutral/euphoric/panic/noise |
| **分析模型** (analysis_model) | `anthropic/claude-sonnet-4` | 单股深度分析 | 日线数据 + 多因子评分 + 板块动态 + 全球市场（含 BTC）+ 舆情摘要 |

分析模型输出结构化 JSON：`direction`（buy/sell/hold/watch）、`confidence`（0-1）、`reasoning`、`key_risks`、`catalysts`。

JSON 解析三级回退：直接 parse → markdown 代码块提取 → 花括号正则匹配。

### 信号退出逻辑（V1 仅输出建议）

| 规则 | 条件 | 说明 |
|------|------|------|
| **硬止损** | 跌幅 ≤ -10%（ST 股 -5%） | 相对信号日收盘价 |
| **追踪止盈** | 从最高点回撤 ≥ 50% 利润 | 仅在有浮盈时触发 |

### T+1 绩效评估

| 评估结果 | 条件 | 说明 |
|----------|------|------|
| **correct** | T+1 日内涨跌幅 > 0% | 次日开盘→收盘上涨 |
| **wrong** | T+1 日内涨跌幅 < -3% | 次日开盘→收盘跌幅超 3% |
| **neutral** | -3% ≤ T+1 涨跌幅 ≤ 0% | 小幅波动 |

入场价取信号日收盘价，T+1 表现取次交易日的 `(close - open) / open`。

### 项目结构

```
src/aisp/
├── cli.py                 # Typer CLI 入口
├── config.py              # Pydantic Settings 配置
├── logging_config.py      # 日志配置（Rich + 文件）
├── db/
│   ├── models.py          # 10 张 SQLAlchemy 表
│   └── engine.py          # 异步数据库引擎
├── data/
│   ├── us_market.py       # 美股数据采集
│   ├── commodities.py     # 大宗商品采集
│   ├── cn_market.py       # A 股数据采集
│   ├── btc_risk.py        # BTC 风险偏好指标（不入库）
│   ├── calendar.py        # 交易日历管理
│   └── sources/           # 舆情适配器
│       ├── base.py        # 抽象基类 + @register_adapter
│       ├── akshare_announcements.py
│       └── xueqiu.py      # 雪球（预留）
├── screening/
│   ├── sector_pools.py    # 三池板块管理
│   └── stock_scorer.py    # 多因子评分
├── engine/
│   ├── llm_client.py      # OpenRouter 客户端
│   ├── analyzer.py        # LLM 分析编排
│   └── signals.py         # 信号生成与退出逻辑
├── review/
│   └── tracker.py         # T+1 绩效追踪
└── report/
    └── briefing.py        # Markdown 简报生成
```

### 数据库表（10 张）

| 表名 | 用途 | 主键/唯一约束 |
|------|------|---------------|
| `stk_daily` | A 股日线行情 | `(trade_date, code)` |
| `global_daily` | 美股/指数/商品行情 | `(trade_date, symbol)` |
| `stk_sector_map` | 股票-板块多对多映射（THS 行业板块，由 `scripts/scrape_ths_sectors.py` 维护） | `(code, sector_name, source)` |
| `sector_daily` | 板块日线数据（THS 90 行业板块，含涨跌幅/净流入/MA） | `(trade_date, sector_name)` |
| `stk_comments` | 舆情数据（公告/评论） | 自增 ID |
| `daily_signals` | 交易信号 | `(trade_date, code)` |
| `signal_performance` | 信号 T+1 绩效追踪 | `signal_id` |
| `sector_pool_state` | 板块池当前状态 | `(sector_name, pool_type)` |
| `sector_pool_history` | 板块池资格检查日志 | `(trade_date, sector_name, pool_type)` |
| `trading_calendar` | A 股交易日历 | `cal_date` |

所有写入使用 SQLite upsert (`on_conflict_do_update`) 保证幂等性，批量插入采用 500 行分块。

## 开发

```bash
uv run pytest tests/ -v                  # 运行测试
uv run ruff check src/ tests/            # 代码检查
uv run ruff check --fix src/ tests/      # 自动修复
```

## 已知限制

- **V1 为观察模式**，不支持仓位管理、资金模拟和回测（计划 V2 实现）
- AkShare 的 EastMoney 类接口在代理环境下可能被拦截，THS 类接口（板块汇总等）不受影响
- BaoStock 使用 TCP 协议，不受 HTTP 代理影响，但需登录（无需账号，自动匿名登录）
- 国内期货接口 (`futures_main_sina`) 可能返回空数据，已做降级处理
- 板块成分股数据来源于 thsdk 项目的 GitHub CSV（通过 THS 私有 TCP 协议获取），需定期运行 `scripts/scrape_ths_sectors.py` 更新
- LLM 分析质量依赖 prompt 和模型能力，信号仅供参考

## 许可

MIT
