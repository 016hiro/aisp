# A-ISP：A 股智能策略领航员

> **A**-Share **I**ntelligence & **S**trategy **P**ilot

融合美股情绪、大宗商品联动和 AI 舆情分析的 **A 股次日交易指导系统**。

**V1 为观察模式**——仅生成交易信号并追踪绩效，不涉及仓位管理或模拟交易。

## 核心能力

- **全球数据整合**：自动采集美股指数/个股（yfinance）、大宗商品、A 股全市场行情及板块数据（AkShare）
- **三池板块管理**：核心池（长期关注）、冲锋池（短期热点）、机会池（超跌反弹）动态轮转
- **多因子选股评分**：资金流（0.4）+ 动量（0.3）+ 技术面（0.2）+ 质量（0.1），板块内百分位排名
- **双模型 LLM 分析**：快速模型做舆情分类，强模型做个股深度分析，通过 OpenRouter 灵活切换
- **信号绩效追踪**：T+1 自动评估信号准确率，按板块归因统计
- **每日简报**：5 个板块的 Markdown 日报，涵盖全球情绪、板块异动、观察股、信号详情和绩效回顾

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
uv run aisp fetch-cn             # 采集 A 股全市场行情
uv run aisp screen               # 板块过滤 + 个股评分
uv run aisp analyze              # LLM 分析 + 信号生成
uv run aisp briefing             # 生成每日简报
uv run aisp status               # 查看当前池状态和活跃信号
```

所有命令支持 `--trade-date YYYY-MM-DD` 指定交易日，默认为当天。

### 全局选项

```bash
uv run aisp --verbose ...        # 输出详细日志
uv run aisp --log-file app.log   # 同时写入日志文件
```

## 架构概览

```
数据采集层                         筛选层                      决策层
┌─────────────────┐
│ 美股/大宗 (yfinance)│──┐    ┌──────────────┐    ┌───────────────┐
└─────────────────┘  ├──→│ 三池板块管理   │──→│ 多因子个股评分  │
┌─────────────────┐  │    └──────────────┘    └───────┬───────┘
│ A 股 (AkShare)    │──┘                              │
└─────────────────┘                                   ▼
                                              ┌───────────────┐
┌─────────────────┐                           │  LLM 深度分析  │
│ 舆情 (适配器模式)  │──→ LLM 情绪分类 ──────→│   信号生成     │
└─────────────────┘                           └───────┬───────┘
                                                      │
                                          ┌───────────┴──────────┐
                                          ▼                      ▼
                                    T+1 绩效追踪          Markdown 日报
```

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
│   ├── calendar.py        # 交易日历管理
│   └── sources/           # 舆情适配器
│       ├── base.py        # 抽象基类
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

| 表名 | 用途 |
|---|---|
| `stk_daily` | A 股日线行情 |
| `global_daily` | 美股/指数/商品行情 |
| `stk_sector_map` | 股票-板块多对多映射 |
| `sector_daily` | 板块日线数据（含预计算均线） |
| `stk_comments` | 舆情数据（公告/评论） |
| `daily_signals` | 交易信号 |
| `signal_performance` | 信号 T+1 绩效追踪 |
| `sector_pool_state` | 板块池当前状态 |
| `sector_pool_history` | 板块池资格检查日志 |
| `trading_calendar` | A 股交易日历 |

## 开发

```bash
uv run pytest tests/ -v                  # 运行测试
uv run ruff check src/ tests/            # 代码检查
uv run ruff check --fix src/ tests/      # 自动修复
```

## 已知限制

- **V1 为观察模式**，不支持仓位管理、资金模拟和回测（计划 V2 实现）
- AkShare 依赖 requests 库，在某些代理环境下可能无法连接
- 国内期货接口 (`futures_main_sina`) 可能返回空数据，已做降级处理
- LLM 分析质量依赖 prompt 和模型能力，信号仅供参考

## 许可

MIT
