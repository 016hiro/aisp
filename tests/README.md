# 测试说明

## 运行方式

```bash
# 运行全部测试
uv run python -m pytest tests/ -v

# 运行单个测试文件
uv run python -m pytest tests/test_data.py -v
uv run python -m pytest tests/test_integration.py -v

# 运行单个测试类
uv run python -m pytest tests/test_data.py::TestUsMarket -v

# 运行单个测试用例
uv run python -m pytest tests/test_data.py::TestCnMarket::test_is_st -v
```

## 测试框架约定

### 数据库隔离

涉及数据库操作的测试使用 **内存 SQLite + `StaticPool`** 实现隔离。关键组件：

- **`shared_engine`** fixture：创建内存数据库并初始化全部 10 张表，测试结束后自动销毁
- **`_EngineProxy`**：代理类，将 `dispose()` 变为空操作，防止被测代码在测试过程中关闭内存数据库
- **`mock_engine`** fixture：通过 `monkeypatch` 将所有 data 模块的 `get_engine()` 替换为代理引擎
- **`session`** fixture：提供与 `shared_engine` 绑定的 session，供测试读取验证数据

**为什么需要 `StaticPool`？** 内存 SQLite 的数据只在创建它的连接中可见。`StaticPool` 强制所有请求复用同一个物理连接，确保写入和读取看到相同的数据。

### 外部 API Mock

所有外部 API 调用（yfinance、AkShare、BaoStock）通过 `monkeypatch` + `AsyncMock` 替换为合成数据，测试无需网络连接。A 股模块（cn_market）已从 AkShare 东方财富接口迁移至 BaoStock（TCP socket），绕过 HTTP 代理问题。

### 编写新测试的规范

1. **语言**：测试文档必须使用中文
2. **文件命名**：`tests/test_<模块名>.py`
3. 涉及数据库写入的测试必须使用 `mock_engine` + `session` fixture
4. 外部 API 调用必须 mock，不允许真实网络请求（`test_calendar_in_db` 除外，它读取实际数据库且无网络时自动跳过）

---

## test_data.py — 数据采集模块测试

### 测试基础设施

| 组件 | 说明 |
|---|---|
| `shared_engine` | 异步 fixture，创建内存 SQLite 引擎（StaticPool），建表，测试后销毁 |
| `_EngineProxy` | 代理类，拦截 `dispose()` 使其变为空操作 |
| `mock_engine` | 将 4 个 data 模块的 `get_engine` 全部替换为代理引擎 |
| `session` | 绑定 shared_engine 的 AsyncSession，供断言查询使用 |

### TestUsMarket — 美股数据采集（4 个用例）

| 用例 | 验证内容 |
|---|---|
| `test_fetch_and_upsert` | 构造多标的 yfinance DataFrame，走完 fetch→转换→upsert 全流程，验证 GlobalDaily 所有必填字段非空且无 NaN |
| `test_nan_volume_becomes_none` | yfinance 返回 Volume=NaN 时，数据库中应存为 NULL 而非 NaN。这是一个已修复的 bug 回归测试 |
| `test_change_pct_calculation` | 用已知收盘价序列（100→110→99）验证涨跌幅计算：第一天为 0，第二天 +10%，第三天 -10% |
| `test_upsert_idempotent` | 对同一标的同一日期执行两次 upsert，验证数据库只有 1 条记录（幂等性） |

### TestCommodities — 大宗商品数据采集（2 个用例）

| 用例 | 验证内容 |
|---|---|
| `test_yf_commodity_transform` | mock yfinance 返回 4 个国际商品数据，验证转换后字段正确、无 NaN |
| `test_akshare_commodity_transform` | mock AkShare 返回国内期货数据（碳酸锂等），yfinance 返回空。验证 symbol 以 `AK_` 前缀存储，close 值正确 |

### TestCnMarket — A 股数据采集（12 个用例，数据源：BaoStock）

| 用例 | 验证内容 |
|---|---|
| `test_is_st` | ST 标记识别：`*ST信威` → True，`ST大集` → True，`贵州茅台` → False，空字符串 → False |
| `test_is_limit_up` | 涨停判断：普通股 ≥9.9% 为涨停，ST 股 ≥4.9% 为涨停 |
| `test_is_limit_down` | 跌停判断：普通股 ≤-9.9%，ST 股 ≤-4.9% |
| `test_safe_float` | 安全浮点转换：正常数字、字符串数字 → float；None、`"-"`、空字符串、非数字字符串 → None |
| `test_compute_ma_from_closes` | 均线计算：5 个收盘价算 MA5 正确，数据不足时 MA10/MA60 返回 None |
| `test_to_bs_code` | BaoStock 代码转换：沪市6/9开头 → `sh.`前缀，深市 → `sz.`前缀 |
| `test_from_bs_code` | BaoStock 代码反向转换：`sh.600519` → `600519`，无前缀原样返回 |
| `test_aggregate_sector_daily` | 从个股数据聚合板块日线：验证 change_pct 均值、volume/amount 求和、up/down 计数 |
| `test_stock_transform_and_upsert` | 构造 2 条股票记录（普通股 + ST 股），验证 upsert 到 StkDaily 后所有字段正确，包括 is_st、is_limit_up、net_inflow=None |
| `test_sector_transform_and_upsert` | 构造板块记录，验证 upsert 到 SectorDaily，up_count/down_count 正确 |
| `test_sector_map_update` | 两轮板块成分股更新：第一轮 3 只股票全部 active（source="csrc"），第二轮移除 1 只后验证其 is_active=False |
| `test_sector_ma_computation` | 写入 5 天板块收盘价，调用 MA 计算，验证 MA5 正确、MA10 因数据不足为 None |

### TestCalendar — 交易日历（2 个用例）

| 用例 | 验证内容 |
|---|---|
| `test_calendar_in_db` | 使用**真实数据库**中的日历数据验证：2026-02-27（周五）是交易日，2-28/3-1（周末）不是；next/prev 链接正确。若无数据则自动跳过 |
| `test_calendar_generation_logic` | 用 4 个合成交易日（周五→周一→周二→周三），验证日历生成逻辑：周末 is_trading_day=False，prev/next 链接跨周末正确 |

### TestSources — 舆情数据源适配器（3 个用例）

| 用例 | 验证内容 |
|---|---|
| `test_akshare_adapter_structure` | AkShare 公告适配器结构正确：source_name="akshare"，有 fetch_comments 方法 |
| `test_xueqiu_adapter_returns_empty` | 雪球适配器（占位实现）返回空列表，source_name="xueqiu" |
| `test_adapter_registry_complete` | 注册表中包含 akshare 和 xueqiu 两个适配器 |

### TestDataIntegration — 跨模块集成（2 个用例）

| 用例 | 验证内容 |
|---|---|
| `test_stock_sector_map_roundtrip` | 写入股票数据 + 板块映射后，通过板块映射反查股票代码，再查 StkDaily 验证 close 值。跨 StkDaily + StkSectorMap 两张表 |
| `test_upsert_updates_existing` | 对同一股票同一日期先 upsert close=1830，再 upsert close=1850，验证只有 1 条记录且 close 已更新 |

---

## test_integration.py — 核心模块单元测试

| 用例 | 所属模块 | 验证内容 |
|---|---|---|
| `test_config_loads` | `config.py` | Pydantic Settings 默认值加载正确，4 个评分权重之和为 1.0 |
| `test_percentile_rank` | `screening/stock_scorer.py` | 百分位排名：升序、降序、含 None（默认 0.5）、空列表 |
| `test_turnover_suitability` | `screening/stock_scorer.py` | 换手率适宜度评分：3-8% 理想区间=1.0，过低/过高 <1.0，None=0.5，0=0.0 |
| `test_json_parsing` | `engine/llm_client.py` | LLM 返回 JSON 解析三层兜底：直接解析、从文本提取、花括号匹配 |
| `test_stop_loss_checks` | `engine/signals.py` | 硬止损（普通 -10%、ST -5%）和追踪止盈（最高点回撤 50%）逻辑 |
| `test_retry_decorator` | `data/__init__.py` | 异步重试装饰器：前 2 次抛异常，第 3 次成功 |
| `test_adapter_registry` | `data/sources/__init__.py` | 适配器注册表：akshare 和 xueqiu 已注册，类型匹配 |
