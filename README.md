# A-ISP：A 股智能策略领航员

> **A**-Share **I**ntelligence & **S**trategy **P**ilot

融合美股情绪、大宗商品联动和 AI 舆情分析的 **A 股次日交易指导系统**。

**V1 为观察模式**——仅生成交易信号并追踪绩效，不涉及仓位管理或模拟交易。

## 核心能力

- **全球数据整合**：美股指数/个股（yfinance）、大宗商品、BTC 风险偏好、A 股行情（BaoStock TCP）、行业板块（AkShare THS）
- **三池板块管理**：核心池（长期关注）、冲锋池（短期热点）、机会池（超跌反弹）动态轮转
- **多层量化选股**：8 因子弹性权重评分 → 威科夫阶段校准 → 突破信号检测 → 交易计划生成 → 方向护栏
- **Deep Agent 分析**：LangChain Deep Agents 框架，可主动搜索新闻/宏观事件（DuckDuckGo），结合量化因子给出判断
- **双模型 LLM 策略**：快速模型做舆情分类/NLP，强模型驱动 Agent 深度分析，支持本地 LLM 优先路由
- **信号绩效追踪**：T+1 自动评估信号准确率，pipeline 内自动评估历史信号
- **每日简报**：5 节 Markdown 日报（全球情绪 + 板块异动 + 信号总览 + 个股分析卡 + 绩效回顾）
- **自然语言观察列表**：`aisp watch "添加天赐材料"` 由 LLM 解析意图，支持 A 股/美股/大宗四类标的
- **Portfolio OCR 导入**：券商截图 → LLM 多模态 OCR → 持仓/交割单入库（CLI + Telegram Bot）

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

复制 `.env.example` 为 `.env` 并修改。所有配置项均支持环境变量覆盖，前缀 `AISP_`，嵌套用 `__` 分隔。

**必填**：

```bash
AISP_OPENROUTER__API_KEY=sk-or-v1-xxxx
AISP_OPENROUTER__ANALYSIS_MODEL=anthropic/claude-sonnet-4
AISP_OPENROUTER__SENTIMENT_MODEL=google/gemini-2.0-flash-001
```

**可选**（均有合理默认值）：

| 配置组 | 环境变量前缀 | 说明 |
|--------|-------------|------|
| 数据库 | `AISP_DB__*` | 默认 `data/aisp.db` |
| 评分权重 | `AISP_SCORING__*` | 8 因子权重（须加和 1.0）+ 弹性系数 α |
| 否决规则 | `AISP_VETO__*` | 宏观/情绪因子否决阈值 |
| 威科夫 | `AISP_WYCKOFF__*` | 吸筹/派发校准参数，`ENABLED=false` 关闭 |
| 突破检测 | `AISP_BREAKOUT__*` | 强/弱信号阈值，`ENABLED=false` 关闭 |
| 交易计划 | `AISP_TRADING_PLAN__*` | ATR 周期、止损缓冲，`ENABLED=false` 关闭 |
| 本地 LLM | `AISP_LOCAL_LLM__*` | 局域网模型优先路由（情感/NLP/OCR） |
| OCR 模型 | `AISP_OCR__*` | 默认 `google/gemini-3.1-flash-lite-preview` |
| Telegram | `AISP_TELEGRAM__*` | Bot Token + 授权用户 |
| 搜索代理 | `AISP_SEARCH_PROXY` | DuckDuckGo 搜索代理 |

> **本地 LLM 路由**：情感分类、Watchlist NLP、OCR 优先走本地，失败后回退远程 OpenRouter。Deep Agent 始终走远程。熔断器：本地失败后 30s 内跳过。

## 使用方式

### 每日工作流

```bash
# 早盘前（08:00）—— 采集美股/大宗/BTC，用最近交易日CN数据运行分析
uv run aisp run-morning

# 收盘后（18:00）—— 采集 A 股数据，更新板块池，追踪信号绩效
uv run aisp run-close
```

**定时调度（macOS launchd）**：

```bash
config/launchd/install.sh install     # 安装（替代 cron，休眠唤醒后补跑）
config/launchd/install.sh status      # 查看状态
config/launchd/install.sh uninstall   # 卸载
```

### 完整命令列表

#### 数据采集

```bash
uv run aisp fetch-us                  # 采集美股数据
uv run aisp fetch-commodities         # 采集大宗商品
uv run aisp fetch-cn                  # 采集 A 股行情（默认自选股）
uv run aisp fetch-cn --mode full      # 全量采集（~5000 只）
uv run aisp fetch-cn --mode codes --codes 600519,000858  # 指定股票
```

A 股个股通过 BaoStock（TCP 协议，不受 HTTP 代理影响），板块数据通过 AkShare THS 源（90 个同花顺行业板块）。

#### 分析流水线

```bash
uv run aisp screen                    # 板块过滤 + 个股评分
uv run aisp analyze                   # LLM 分析 + 信号生成
uv run aisp briefing                  # 生成每日简报
uv run aisp run-analysis-pipeline     # auto-fetch → screen → analyze → evaluate → briefing
uv run aisp run-morning               # fetch-us → commodities → btc → screen → analyze → briefing
uv run aisp run-close                 # fetch-cn → update pools → track performance
```

`run-analysis-pipeline` 会自动检测并补全缺失的 CN/全球数据。

#### 查看与状态

```bash
uv run aisp show                      # TUI 简报浏览器（j/k 滚动，← → 切换日期）
uv run aisp status                    # 查看当前池状态和活跃信号
```

#### 持仓/交割单（OCR 导入）

```bash
uv run aisp import-positions screenshot1.png screenshot2.png   # 导入持仓截图
uv run aisp import-trades trade1.png trade2.png                # 导入交割单截图
uv run aisp positions                  # 查看持仓快照（默认最新日期）
uv run aisp trades                     # 查看交割记录（默认最近 7 天）
uv run aisp trades --days 30           # 最近 30 天
```

支持 `--yes` 跳过确认、`--date` 覆盖日期。格式：PNG/JPG/JPEG/WEBP。

#### Telegram Bot

```bash
uv run aisp telegram                   # 启动 Bot（长轮询）
```

交互：`/positions` 或 `/trades` → 发截图 → `/done` → OCR → 确认入库。图片 SHA256 去重。

#### 观察列表

```bash
# 自然语言模式（LLM 驱动）
uv run aisp watch "添加天赐材料"        # 自动补全代码，写入 A 股自选
uv run aisp watch "关注苹果和英伟达"    # 批量添加到美股列表
uv run aisp watch "删除工商银行"        # 移除

# 结构化命令
uv run aisp watch-add 002709 天赐材料                  # A 股（默认）
uv run aisp watch-add GOOG Alphabet --type us          # 美股
uv run aisp watch-add "NG=F" "Natural Gas" --type yf   # 国际大宗
uv run aisp watch-rm GOOG --type us                    # 删除
uv run aisp watch-ls                                   # 列出 A 股自选
uv run aisp watch-ls --type us                         # 列出美股
```

`--type` 可选：`cn`（A 股，默认）、`us`（美股）、`yf`（国际大宗）、`ak`（国内大宗）。配置文件：`config/symbols.toml`。

#### 调试工具

```bash
uv run aisp dump-prompts               # 导出 Agent prompt 到文件（不调用 LLM）
uv run aisp dump-prompts --codes 002709 -o prompts/  # 指定股票
```

#### 全局选项

所有命令支持 `--trade-date YYYY-MM-DD`（默认当天）、`--verbose`、`--log-file`。

## 架构概览

```
                              数据采集                    筛选 & 校准                     决策 & 输出
                         ┌─────────────────┐
                         │  美股/大宗/BTC   │
                         │   (yfinance)    │──┐
                         └─────────────────┘  │    ┌──────────────┐    ┌──────────────┐
                                              ├───→│  三池板块管理  │───→│ 8 因子弹性评分 │
                         ┌─────────────────┐  │    └──────────────┘    └──────┬───────┘
                         │  A 股 (BaoStock) │──┘                              │
                         │  板块 (AkShare)  │                                 ▼
                         └─────────────────┘                   ┌──────────────────────────┐
                                                               │  威科夫校准 → 突破检测     │
                         ┌─────────────────┐                   │  → 交易计划生成            │
                         │  舆情 (适配器)    │──→ 情绪分类 ──→   └──────────┬───────────────┘
                         └─────────────────┘                              │
                                                                          ▼
                                                               ┌──────────────────────────┐
                                                               │   Deep Agent LLM 分析     │
                                                               │   + 方向护栏 → 信号生成    │
                                                               └──────────┬───────────────┘
                                                                          │
                                                               ┌──────────┴───────────┐
                                                               ▼                      ▼
                                                        T+1 绩效追踪          Markdown 日报
```

### 三池板块管理

| 池类型 | 说明 | 进入规则 | 退出规则 |
|--------|------|----------|----------|
| **核心池** | 长期跟踪的战略板块 | 配置文件静态指定 | 永不移除 |
| **冲锋池** | 短期热门板块 | 当日涨跌幅排名前 N | 连续 N 日跌出排名则移除 |
| **机会池** | 超跌反弹候选 | 跌幅最大 + MA60 上行 | 连续 N 日不满足条件则移除 |

退出判定基于 `sector_pool_history` 表中的资格记录（向前追溯连续不达标天数），而非简单计数器。

### 8 因子弹性权重选股

对三池板块内非 ST、非涨停个股，按 8 因子计算综合评分（0-1）：

| 因子 | 权重 | 来源 |
|------|------|------|
| 资金面 (Fund) | 0.20 | 主力净流入 / 成交额 |
| 动量 (Momentum) | 0.10 | 日涨跌幅 |
| 量价 (Technical) | 0.10 | 量比 + 换手率适宜度 |
| 质量 (Quality) | 0.05 | log(总市值) |
| 技术指标 (Indicators) | 0.20 | RSI(3/6/9) + MACD(6,13,5) + 均线位置 |
| 宏观联动 (Macro) | 0.10 | 关联全球资产涨跌幅 + BTC 风险评分 |
| 舆情 (Sentiment) | 0.15 | 7 日内评论情绪加权聚合 |
| 板块动量 (Sector) | 0.10 | 上涨占比 + MA 趋势 + 资金流向 |

- **弹性权重**：因子越极端（远离 0.5），权重越大。`raw = base × (1 + α × |score-0.5| × 2)`，归一化
- **硬规则否决**：宏观 < 0.15 或情绪 < 0.10 → 禁止买入；情绪 > 0.95 → 追高警告
- **两条评分路径**：`score_all_pools()`（批量，每板块取 Top-N）vs `score_by_codes()`（指定股票，无 Top-N 限制）

### 后置校准层

8 因子评分之后依次运行三层后置校准，逐步精化信号：

**威科夫阶段校准**：基于 60+ 日 OHLCV 检测吸筹/派发阶段，用乘数调整评分。吸筹期（Spring/SOS/LPS）加分至 1.25×，派发期（UT/SOW/LPSY）折价至 0.60×，LPSY 确认直接否决。

**突破信号检测**：三类检测——盘整突破、均线突破（MA20/MA60）、N 日新高低。强信号（≥0.60）生成中文描述注入 LLM prompt，弱信号（≥0.35）仅微调乘数。复用威科夫支撑/阻力位。

**交易计划生成**：综合支撑/阻力、均线、ATR 生成入场区间、止损位、目标价和风险收益比。自动适配 A 股涨跌停规则（普通±10%、创业板/科创板±20%、ST±5%）。量化计划注入 LLM prompt 后由 Agent 验证调整。

### Deep Agent 分析 & 方向护栏

个股分析采用 LangChain Deep Agents，Agent 可主动搜索个股新闻、板块动态、宏观事件（DuckDuckGo），结合量化因子给出结构化判断（direction / confidence / reasoning / risks / catalysts）。

LLM 返回后自动执行**方向护栏**：
- R:R < 1.0 的看多信号自动降级（BUY → WEAK_BUY → HOLD）
- 连续 3+ 天下跌且累计 >5% 且 MA5 < MA20 → 强制 HOLD

### 双模型 LLM 策略

| 模型角色 | 默认模型 | 用途 |
|----------|----------|------|
| 情绪模型 | `gemini-2.0-flash-001` | 舆情分类、Watchlist NLP、OCR |
| 分析模型 | `claude-sonnet-4` | Deep Agent 个股深度分析 |

通过 OpenRouter API 调用。Prompt 模板集中在 `config/prompts.toml`。JSON 解析三级回退（直接 parse → 代码块提取 → 花括号正则）。

### 信号方向 & 绩效评估

**8 级信号方向**：strong_buy / buy / weak_buy / hold / watch / weak_sell / sell / strong_sell

**T+1 绩效评估**：correct（T+1 涨）、wrong（T+1 跌超 3%）、neutral（-3%~0%）

**退出建议**：硬止损 -10%（ST -5%），追踪止盈（从最高点回撤 ≥50% 利润）

### 项目结构

```
src/aisp/
├── cli.py                 # Typer CLI 入口
├── config.py              # Pydantic Settings 配置
├── tui.py                 # TUI 简报浏览器 (Textual)
├── watch_nlp.py           # 自然语言观察列表管理
├── db/
│   ├── models.py          # 13 张 SQLAlchemy 表
│   └── engine.py          # 异步数据库引擎
├── data/
│   ├── us_market.py       # 美股数据采集
│   ├── commodities.py     # 大宗商品采集
│   ├── cn_market.py       # A 股数据采集 (BaoStock TCP)
│   ├── btc_risk.py        # BTC 风险偏好指标
│   ├── symbols.py         # 标的配置 (symbols.toml)
│   ├── calendar.py        # 交易日历管理
│   └── sources/           # 舆情适配器 (@register_adapter)
├── screening/
│   ├── sector_pools.py    # 三池板块管理
│   ├── stock_scorer.py    # 8 因子评分 + 校准编排
│   ├── factor_engine.py   # 弹性权重引擎 + 否决规则
│   ├── indicators.py      # RSI/MACD/均线 纯函数
│   ├── wyckoff.py         # 威科夫阶段检测
│   ├── breakout.py        # 突破信号检测
│   └── trading_plan.py    # 交易计划生成
├── engine/
│   ├── llm_client.py      # OpenRouter 客户端 + 本地 LLM 路由
│   ├── analyzer.py        # LLM 分析编排 + 方向护栏
│   ├── agent.py           # Deep Agent + 搜索工具
│   ├── prompts.py         # Prompt 加载器
│   └── signals.py         # 信号生成与退出逻辑
├── portfolio/
│   ├── ocr.py             # LLM 多模态 OCR
│   └── importer.py        # 持仓/交割单 DB 写入
├── telegram/
│   ├── bot.py             # Telegram Bot (ConversationHandler)
│   ├── dedup.py           # 图片 SHA256 去重
│   └── formatter.py       # 消息格式化 (HTML)
├── review/
│   └── tracker.py         # T+1 绩效追踪
└── report/
    └── briefing.py        # Markdown 简报生成
```

### 数据库表（13 张）

| 表名 | 用途 | 唯一约束 |
|------|------|----------|
| `stk_daily` | A 股日线行情 | `(trade_date, code)` |
| `global_daily` | 美股/指数/商品行情 | `(trade_date, symbol)` |
| `stk_sector_map` | 股票-板块映射（THS 行业） | `(code, sector_name, source)` |
| `sector_daily` | 板块日线（THS 90 行业板块） | `(trade_date, sector_name)` |
| `stk_comments` | 舆情数据 | 自增 ID |
| `daily_signals` | 交易信号 | `(trade_date, code)` |
| `signal_performance` | 信号 T+1 绩效 | `signal_id` |
| `sector_pool_state` | 板块池当前状态 | `(sector_name, pool_type)` |
| `sector_pool_history` | 板块池资格日志 | `(trade_date, sector_name, pool_type)` |
| `trading_calendar` | A 股交易日历 | `cal_date` |
| `position_snapshot` | 持仓快照 | `(snapshot_date, code)` |
| `trade_record` | 交割单/成交记录 | `(trade_date, code, direction, price, qty)` |
| `image_hash` | 图片去重（Telegram） | `hash` |

所有写入使用 SQLite upsert 保证幂等性，批量插入 500 行分块，全异步（aiosqlite + greenlet）。

## 开发

```bash
uv run pytest tests/ -v                  # 运行测试
uv run ruff check src/ tests/            # 代码检查
uv run ruff check --fix src/ tests/      # 自动修复
```

## 已知限制

- V1 为观察模式，不支持仓位管理、资金模拟和回测
- AkShare EastMoney 类接口在代理下可能被拦截，THS 类接口不受影响
- BaoStock TCP 协议不受 HTTP 代理影响，自动匿名登录
- 国内期货接口 (`futures_main_sina`) 可能返回空数据，已降级处理
- 板块成分股来自 THS 私有协议 CSV，需定期 `scripts/scrape_ths_sectors.py` 更新
- LLM 分析质量依赖 prompt 和模型，信号仅供参考

## 许可

MIT
