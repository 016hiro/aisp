# A-ISP：A 股智能策略领航员

> **A**-Share **I**ntelligence & **S**trategy **P**ilot

融合美股情绪、大宗商品联动和 AI 舆情分析的 **A 股次日交易指导系统**。

**V1 为观察模式**——仅生成交易信号并追踪绩效，不涉及仓位管理或模拟交易。

## 核心能力

- **全球数据整合**：自动采集美股指数/个股（yfinance）、大宗商品、BTC 风险偏好指标、A 股个股行情（BaoStock TCP）、行业板块涨跌（AkShare THS 源）
- **三池板块管理**：核心池（长期关注）、冲锋池（短期热点）、机会池（超跌反弹）动态轮转
- **8因子弹性权重选股 + 威科夫校准 + 突破检测 + 精细化交易计划**：资金面 + 动量 + 量价 + 质量 + 技术指标(RSI/MACD/均线) + 宏观联动 + 舆情 + 板块动量，弹性权重自动提权极端因子 + 硬规则否决兜底 + 威科夫吸筹/派发阶段后置校准乘数 + 突破信号检测（盘整/均线/新高低三类，强信号注入 LLM prompt）+ 量化交易计划（入场区间/止损/目标价/风险收益比，LLM 验证调整）+ 方向护栏（R:R<1.0 自动降级 + 连跌趋势过滤）
- **BTC 风险偏好**：5 维度复合评分（短/中/长期动量 + 波动率状态 + 回撤位置），注入 LLM prompt 和 Briefing
- **Deep Agent 分析**：个股深度分析采用 LangChain Deep Agents 框架，内置任务规划和上下文管理，可主动搜索新闻和宏观事件（DuckDuckGo），结合量化因子 + 实时资讯给出判断
- **双模型 LLM 策略**：快速模型做舆情分类和自然语言解析，强模型驱动 Agent 做个股深度分析，通过 OpenRouter 灵活切换
- **信号绩效追踪**：T+1 自动评估信号准确率（滚动30日窗口），pipeline 内自动评估历史信号并更新统计数据
- **每日简报**：5 节 Markdown 日报——全球情绪（含 BTC）、板块异动、信号总览、个股详细分析卡（日线数据 + RSI(3/6/9)/MACD(6,13,5) 技术面 + 8 因子评分表 + 板块概况 + 交易计划表 + LLM 分析/风险/催化剂）、绩效回顾
- **自然语言观察列表**：`aisp watch "添加天赐材料"` 由 LLM 解析意图并自动补全代码/symbol，支持 A 股/美股/大宗四类标的

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

# 评分权重（可选，8因子须加和为 1.0）
AISP_SCORING__WEIGHT_FUND=0.20
AISP_SCORING__WEIGHT_MOMENTUM=0.10
AISP_SCORING__WEIGHT_TECHNICAL=0.10
AISP_SCORING__WEIGHT_QUALITY=0.05
AISP_SCORING__WEIGHT_INDICATORS=0.20
AISP_SCORING__WEIGHT_MACRO=0.10
AISP_SCORING__WEIGHT_SENTIMENT=0.15
AISP_SCORING__WEIGHT_SECTOR=0.10
AISP_SCORING__ELASTICITY=2.0           # 弹性系数，越大极端因子提权越多

# 否决规则阈值（可选）
AISP_VETO__MACRO_FLOOR=0.15            # 宏观因子低于此值禁止买入
AISP_VETO__SENTIMENT_FLOOR=0.10        # 情绪低于此值禁止买入
AISP_VETO__SENTIMENT_CEILING=0.95      # 情绪高于此值警告追高

# 威科夫校准层（可选）
AISP_WYCKOFF__ENABLED=true             # 是否启用威科夫校准
AISP_WYCKOFF__MIN_BARS=60              # 最少需要的K线数
AISP_WYCKOFF__ACC_MAX_MULTIPLIER=1.25  # 吸筹阶段最大加分乘数
AISP_WYCKOFF__DIST_MIN_MULTIPLIER=0.60 # 派发阶段最大折价乘数

# 突破信号检测层（可选）
AISP_BREAKOUT__ENABLED=true            # 是否启用突破检测
AISP_BREAKOUT__STRONG_THRESHOLD=0.60   # 强信号阈值（注入LLM prompt）
AISP_BREAKOUT__WEAK_THRESHOLD=0.35     # 弱信号阈值（仅调乘数）
AISP_BREAKOUT__VOL_CONFIRM_RATIO=1.5   # 放量确认基准

# 交易计划生成层（可选）
AISP_TRADING_PLAN__ENABLED=true       # 是否启用交易计划
AISP_TRADING_PLAN__ATR_PERIOD=20      # ATR 计算周期
AISP_TRADING_PLAN__STOP_BUFFER_PCT=0.01  # 止损缓冲比例
```

所有配置项均支持环境变量覆盖，前缀 `AISP_`，嵌套用 `__` 分隔。

## 使用方式

### 每日工作流

```bash
# 早盘前（08:00）—— 采集美股/大宗/BTC，用最近交易日CN数据运行分析
uv run aisp run-morning

# 收盘后（18:00，BaoStock 17:30 入库后）—— 采集 A 股数据，更新板块池，追踪信号
uv run aisp run-close
```

**定时调度（macOS launchd）**：

```bash
# 安装 launchd agents（替代 cron，支持休眠唤醒后补跑）
config/launchd/install.sh install

# 查看状态
config/launchd/install.sh status

# 卸载
config/launchd/install.sh uninstall
```

> macOS cron 在机器休眠时不执行且不补跑，launchd 的 `StartCalendarInterval` 会在唤醒后自动补跑错过的任务。

### 单步命令

```bash
uv run aisp fetch-us             # 采集美股数据
uv run aisp fetch-commodities    # 采集大宗商品
uv run aisp fetch-cn             # 采集 A 股行情（默认自选股，--mode full 全量）
uv run aisp screen               # 板块过滤 + 个股评分
uv run aisp analyze              # LLM 分析 + 信号生成
uv run aisp briefing             # 生成每日简报
uv run aisp run-analysis-pipeline  # screen → analyze → briefing 三合一
uv run aisp show                 # TUI 简报浏览器（交互式，方向键/j/k 滚动，← → 切换日期）
uv run aisp status               # 查看当前池状态和活跃信号
```

所有命令支持 `--trade-date YYYY-MM-DD` 指定交易日，默认为当天。

### 持仓/交割单管理

通过券商 APP 截图导入持仓和交割记录，使用多模态 LLM OCR 提取数据：

```bash
# 导入持仓截图（支持多张截图合并去重）
uv run aisp import-positions screenshot1.png screenshot2.png
uv run aisp import-positions screenshot.jpg --yes    # 跳过确认直接入库

# 导入交割单截图
uv run aisp import-trades trade1.png trade2.png
uv run aisp import-trades trade.jpg --yes

# 查看持仓快照（默认最新日期）
uv run aisp positions
uv run aisp positions --date 2024-03-13

# 查看交割记录（默认最近7天）
uv run aisp trades
uv run aisp trades --date 2024-03-13
uv run aisp trades --days 30
```

OCR 流程：校验截图 → LLM 多模态提取 → Rich 表格预览 + 置信度 → 用户确认 → 写入 DB。支持 PNG/JPG/JPEG/WEBP 格式。

默认 OCR 模型 `google/gemini-3.1-flash-lite-preview`（通过 OpenRouter），可通过 `AISP_OCR__MODEL` 覆盖。

### Telegram Bot 截图导入

通过 Telegram Bot 直接发送截图完成持仓/交割单导入，替代手动传图+跑命令：

```bash
# 启动 Bot（前台长轮询）
uv run aisp telegram

# 或使用 launchd 守护进程（崩溃自动重启）
config/launchd/install.sh install
```

**配置**（`.env`）：
```bash
AISP_TELEGRAM__BOT_TOKEN=123456:ABC-DEF...      # BotFather 获取
AISP_TELEGRAM__ALLOWED_USER_IDS=[123456789]      # 限制授权用户（空=不限制）
```

**交互流程**：
```
/positions → 发截图 → /done → OCR → [Confirm] [Cancel] [Change Date] → 入库
/trades    → 发截图 → /done → OCR → [Confirm] [Cancel] [Change Date] → 入库
```

- 图片 SHA256 去重，重复图片自动跳过
- OCR 使用 `analysis_model`（默认 Claude Sonnet）保证识别质量
- 支持压缩图片和原图文件（PNG/JPG/WEBP）

### 观察列表管理

#### 自然语言模式（LLM 驱动）

```bash
uv run aisp watch "添加天赐材料"          # 自动补全代码 002709，写入 A 股自选
uv run aisp watch "关注苹果和英伟达"      # 批量添加 AAPL + NVDA 到美股列表
uv run aisp watch "删除工商银行"          # 从 A 股自选移除
uv run aisp watch "观察列表里有什么"      # 查看当前列表
uv run aisp watch "add gold futures"      # 英文也可以
```

LLM 会根据标的名称自动判断所属列表（A股/美股/国际大宗/国内大宗），补全代码/symbol，支持一次操作多个标的。使用 `sentiment_model`（快速模型）解析。

#### 结构化命令

```bash
uv run aisp watch-add 002709 天赐材料                  # A 股（默认）
uv run aisp watch-add GOOG Alphabet --type us          # 美股
uv run aisp watch-add "NG=F" "Natural Gas" --type yf   # 国际大宗
uv run aisp watch-add 螺纹钢 螺纹钢 --type ak          # 国内大宗
uv run aisp watch-rm GOOG --type us                    # 删除
uv run aisp watch-ls --type us                         # 列出美股列表
```

`--type` 可选值：`cn`（A 股，默认）、`us`（美股）、`yf`（国际大宗）、`ak`（国内大宗）。美股可追加 `--asset-type index/stock/commodity`。

配置文件为 `config/symbols.toml`，所有操作保留原有格式和注释。

### A 股数据拉取模式

`fetch-cn` 支持三种模式，通过 `--mode` 参数指定：

| 模式 | 说明 | 用法 |
|------|------|------|
| `watchlist`（默认） | 仅拉取 `config/symbols.toml` 中 `[[cn_watchlist]]` 定义的自选股 | `uv run aisp fetch-cn` |
| `full` | 拉取 BaoStock 全市场股票（~5000 只） | `uv run aisp fetch-cn --mode full` |
| `codes` | 仅拉取命令行指定的股票代码 | `uv run aisp fetch-cn --mode codes --codes 600519,000858` |

三种模式下，个股日线数据均通过 BaoStock（TCP 协议，不受 HTTP 代理影响）获取；板块涨跌数据独立通过 AkShare THS 源获取（`stock_board_industry_summary_ths`，90 个同花顺行业板块）。`watchlist`（默认）和 `codes` 模式适合日常快速更新，`full` 模式适合初始化或全量刷新。

`run-analysis-pipeline` 会自动检测目标日期是否有 CN 数据，如缺失则自动拉取观察列表股票数据后再继续。

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

### 8因子弹性权重选股模型

对进入三池的板块内所有非 ST、非涨停个股，按以下 8 因子计算综合评分：

| 因子 | 基础权重 | 原始指标 | 建模方式 | 缺失值处理 |
|------|----------|----------|----------|------------|
| **资金面 (Fund)** | 0.20 | 主力净流入 / 成交额 | 板块内百分位排名（0-1） | `net_inflow` 为 None 时取默认值 0.5 |
| **动量 (Momentum)** | 0.10 | 日涨跌幅 `change_pct` | 板块内百分位排名（0-1） | — |
| **量价 (Technical)** | 0.10 | 量比 × 0.6 + 换手率适宜度 × 0.4 | 量比：百分位排名；换手率：3-8% 为理想区间 | — |
| **质量 (Quality)** | 0.05 | log(总市值) | 板块内百分位排名（0-1） | 市值为 None 或 ≤ 0 时取默认 0.5 |
| **技术指标 (Indicators)** | 0.20 | RSI(0.3) + MACD方向(0.3) + 均线位置(0.4) | RSI(3/6/9) 多周期 Wilder平滑 → [30,70]映射[0,1]；MACD(6,13,5) histogram正负；MA5/10/20/60加权位置 | 数据不足默认 0.5 |
| **宏观联动 (Macro)** | 0.10 | 关联全球资产涨跌幅 × 0.6 + BTC风险评分 × 0.4 | 配置映射表查关联资产，涨跌幅归一化 | 无关联默认 0.5 |
| **舆情 (Sentiment)** | 0.15 | 7日内评论情绪加权聚合 | euphoric×1.5 + bullish×1 - bearish×1 - panic×1.5，归一化到 [0,1] | 无数据默认 0.5 |
| **板块动量 (Sector)** | 0.10 | 板块breadth×0.4 + MA趋势×0.3 + 资金流向×0.3 | breadth=上涨占比；MA趋势=收盘>MA20；流向=净流入/成交额归一化 | 无数据默认 0.5 |

**弹性权重机制**：因子越极端（远离 0.5 中性值），权重越大。公式：`raw_weight = base_weight × (1 + α × |score - 0.5| × 2)`，归一化后得到实际权重。默认弹性系数 α=2.0。

**硬规则否决 (Veto)**：极端情况下一票否决，分数直接设为 0：
- 宏观因子 < 0.15 → 禁止买入
- 情绪因子 < 0.10 → 禁止买入（极度恐慌）
- 情绪因子 > 0.95 → 警告追高风险

**7+1 级信号方向**：strong_buy / buy / weak_buy / hold / watch / weak_sell / sell / strong_sell

**百分位排名**：板块内所有有效值排序后，第 i 位的排名为 `i / (n-1)`，范围 [0, 1]。单只股票时排名为 0.5。

**综合评分** = Σ(弹性权重 × 因子分)，范围 [0, 1]。每个板块取评分前 N（默认 5）的个股进入 LLM 深度分析。

### 威科夫上下文校准层

8 因子评分之后，额外运行威科夫（Wyckoff）阶段检测作为**后置校准层**（非第 9 个因子），用乘数调整 `total_score`。基于 60+ 日 OHLCV 数据，纯函数实现，无 DB 依赖。

**检测流程**：
1. **盘整识别**：ATR(20)/close 中位数 < 阈值（默认 3%）且持续 ≥ 20 个交易日，同时总价格区间保持紧凑
2. **前趋势判断**：盘整前的 MA20 vs MA60 判定 down/up/flat
3. **事件检测**：在盘整区间的末端 5 根 K 线中识别关键威科夫事件

| 阶段 | 前趋势 | 关键事件 | 校准效果 |
|------|--------|----------|----------|
| **吸筹 (Accumulation)** | 下跌 | Spring（假跌破+低量+收回）、SOS（放量突破）、LPS（缩量回踩） | 乘数 1.0→1.25（按置信度线性插值） |
| **派发 (Distribution)** | 上涨 | UT（假突破+弱收）、SOW（放量跌破）、LPSY（缩量反弹失败） | 乘数 1.0→0.60（按置信度），LPSY 确认 → 0.0（否决） |

每个事件贡献置信度权重（spring/sos/ut/sow: 0.4, lps/lpsy: 0.2），累加后 cap 到 1.0。所有参数可通过 `AISP_WYCKOFF__*` 环境变量覆盖。`AISP_WYCKOFF__ENABLED=false` 可完全关闭。

威科夫阶段信息会注入 LLM 深度分析 prompt，辅助 Agent 做出更准确的判断。

### 突破信号检测层

威科夫校准之后，额外运行突破信号检测，覆盖三类场景：

| 检测类型 | 触发条件 | 输出 |
|----------|----------|------|
| **盘整突破** | 收盘价突破威科夫阻力/支撑位（或回看 N 日高低点） | 中文描述 + 强度评分 |
| **均线突破** | 收盘价上穿/下穿 MA20 或 MA60 | 中文描述 + 强度评分 |
| **N 日新高/低** | 收盘价创 60 日新高或新低 | 中文描述 + 强度评分 |

**强度评分公式**（0-1）：量比(0.30) + 收盘位置(0.20) + 实体比例(0.15) + 盘整天数(0.20) + 突破幅度(0.15)

**信号分级**：
- **强信号**（≥ 0.60）：生成中文突破描述注入 LLM prompt，如"今日放量3.2倍突破盘整42日阻力位25.80，收盘站稳26.50"
- **弱信号**（≥ 0.35）：仅微调评分乘数（看多 +5%，看空 -8%）
- 低于 0.35 忽略

与威科夫的分工：威科夫负责阶段判定和乘数校准，突破检测负责事件描述和弱信号微调。突破检测复用威科夫输出的支撑/阻力位，避免重复计算。

所有参数可通过 `AISP_BREAKOUT__*` 环境变量覆盖。`AISP_BREAKOUT__ENABLED=false` 可完全关闭。

### 精细化交易计划

在威科夫校准和突破检测之后，系统自动生成结构化交易计划，将已有的支撑/阻力、均线、ATR 等数据合成为具体操作价位：

| 项目 | 计算方式 | 说明 |
|------|----------|------|
| **入场区间** | 当前价下方最近支撑 ~ 当前价 | 区间宽度 ≤ 1.5×ATR(20)，涨停/跌停时为空 |
| **止损位** | max(支撑-1%缓冲, 入场低-1×ATR) | 下限为跌停价，ST 用 0.5×ATR |
| **目标价** | 入场上方前两个阻力位 | 无阻力时用 1.5/2.5×ATR，上限为涨停价 |
| **风险收益比** | (目标1-入场中点)/(入场中点-止损) | ≥3→积极, ≥1.5→正常, <1.5→保守 |
| **涨跌停** | 普通±10%, 创业板/科创板±20%, ST±5% | 自动识别板块类型 |

**两层结合**：量化模块先生成初始计划（纯函数，无 LLM 调用），注入 Agent prompt 后由 LLM 验证/调整，补充集合竞价策略和盘中操作建议。最终合并结果展示在信号详情卡片中。

**A 股特殊规则**：
- 集合竞价（9:15-9:25）策略建议
- T+1 规则提醒（当日买入次日才能卖出）
- 涨停/跌停封板时不生成入场区间，仅输出风险提示

**R:R 保护机制**：
- 止损距入场中点不低于 1.5%（防止极端 R:R 值）
- R:R 结果 clamp 至 [0, 10.0] 区间

所有参数可通过 `AISP_TRADING_PLAN__*` 环境变量覆盖。`AISP_TRADING_PLAN__ENABLED=false` 可完全关闭。

### 方向护栏（Direction Guardrails）

LLM 返回信号方向后，系统自动执行两层护栏检查，防止方向与量化数据矛盾：

| 规则 | 触发条件 | 动作 |
|------|----------|------|
| **R:R 一致性** | 看多信号(BUY/WEAK_BUY) + R:R < 1.0 | 自动降级一档（BUY→WEAK_BUY→HOLD） |
| **下跌趋势过滤** | 连续3+天下跌 + 累计跌幅>5% + MA5<MA20 | 强制 HOLD，阻止买入信号 |

趋势数据（`_trend`）在评分阶段计算并存入 `raw_data`，包含连跌天数、累计跌幅和 MA 交叉状态。

### 数据自动补全

`run-analysis-pipeline` 执行前自动检查并补全缺失数据：
- **CN 数据**：检测 `stk_daily` 中目标日期是否有数据，缺失则自动拉取 watchlist 股票
- **全球数据**：检测 `global_daily` 中目标日期记录数，不足 3 条则自动拉取 US 市场 + 大宗商品

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

### Deep Agent 深度分析

个股分析采用 LangChain Deep Agents 框架（`engine/agent.py`），基于 `create_deep_agent` 构建，内置任务规划（`write_todos`）和上下文管理能力，Agent 可在分析过程中主动调用工具获取外部信息：

| 工具 | 功能 | 触发场景 |
|------|------|----------|
| `search_news` | 搜索个股最新新闻 | 股价大涨/大跌、因子异常 |
| `search_sector_news` | 搜索板块动态和政策 | 板块整体异动 |
| `search_macro_events` | 搜索宏观/地缘政治事件 | 全球市场波动、大宗商品暴涨暴跌 |

搜索通过 DuckDuckGo（`ddgs` 包），可通过 `AISP_SEARCH_PROXY` 环境变量配置代理。

**工作流程**：量化因子数据 → Deep Agent 审阅 → 自主规划搜索策略 → 主动搜索补充信息 → 综合判断 → 结构化 JSON 输出

### 双模型 LLM 策略

通过 OpenRouter API 调用，使用 `asyncio.Semaphore(5)` 控制并发：

| 模型角色 | 默认模型 | 用途 | 输入 |
|----------|----------|------|------|
| **情绪模型** (sentiment_model) | `google/gemini-2.0-flash-001` | 批量分类舆情评论情绪 + 自然语言观察列表 | 每批 10 条评论 / 自然语言指令 |
| **分析模型** (analysis_model) | `anthropic/claude-sonnet-4` | Deep Agent 驱动的个股深度分析 | 日线 + 8 因子 + 板块 + 全球市场 + 搜索结果 |

分析模型输出结构化 JSON：`direction`（7+1 级）、`confidence`（0-1）、`reasoning`（含搜索到的关键信息）、`key_risks`、`catalysts`。

JSON 解析三级回退：直接 parse → markdown 代码块提取 → 花括号正则匹配。解析失败时自动追加消息要求 LLM 重新输出 JSON。

所有 LLM Prompt 模板集中在 `config/prompts.toml`，`engine/prompts.py` 负责加载和格式化，方便调优迭代。

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
│   ├── models.py          # 12 张 SQLAlchemy 表
│   └── engine.py          # 异步数据库引擎
├── watch_nlp.py           # 自然语言观察列表管理
├── data/
│   ├── us_market.py       # 美股数据采集
│   ├── commodities.py     # 大宗商品采集
│   ├── cn_market.py       # A 股数据采集
│   ├── btc_risk.py        # BTC 风险偏好指标（不入库）
│   ├── symbols.py         # 标的配置加载与管理
│   ├── calendar.py        # 交易日历管理
│   └── sources/           # 舆情适配器
│       ├── base.py        # 抽象基类 + @register_adapter
│       ├── akshare_announcements.py
│       └── xueqiu.py      # 雪球（预留）
├── screening/
│   ├── sector_pools.py    # 三池板块管理
│   ├── stock_scorer.py    # 8因子弹性权重评分 + 威科夫校准
│   ├── indicators.py      # 技术指标纯函数(RSI/MACD/均线)
│   ├── wyckoff.py         # 威科夫阶段检测纯函数(后置校准层)
│   ├── breakout.py        # 突破信号检测纯函数(三类突破+强度评分)
│   ├── trading_plan.py    # 精细化交易计划生成(入场/止损/目标/风险收益比)
│   └── factor_engine.py   # 弹性权重引擎 + 否决规则
├── portfolio/
│   ├── ocr.py             # LLM 多模态 OCR (LangChain ChatOpenAI)
│   └── importer.py        # 持仓/交割单 DB 写入 (upsert)
├── telegram/
│   ├── bot.py             # Telegram Bot 主逻辑 (ConversationHandler)
│   ├── dedup.py           # 图片 SHA256 去重
│   └── formatter.py       # Telegram 消息格式化 (HTML)
├── engine/
│   ├── llm_client.py      # OpenRouter 客户端
│   ├── analyzer.py        # LLM 分析编排
│   ├── prompts.py         # Prompt 加载器(模板在 config/prompts.toml)
│   ├── agent.py           # Deep Agent (deepagents) + 搜索工具
│   └── signals.py         # 信号生成与退出逻辑
├── review/
│   └── tracker.py         # T+1 绩效追踪
└── report/
    └── briefing.py        # Markdown 简报生成
```

### 数据库表（13 张）

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
| `position_snapshot` | 持仓快照（每日，OCR/手动导入） | `(snapshot_date, code)` |
| `trade_record` | 交割单/成交记录（OCR/手动导入） | `(trade_date, code, direction, price, qty)` |
| `image_hash` | 图片 SHA256 去重记录（Telegram Bot） | `hash` (unique) |

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
