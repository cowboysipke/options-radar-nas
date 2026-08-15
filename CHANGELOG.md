# 变更日志

## 2026-08-12 — v2-local 重构版

### 新增功能

1. **Discord REST 采集源**（`discord_rest.py`）
   - 用户 token + HTTP API 分页读取频道历史，彻底告别 DOM 抓取
   - 支持中文频道名自动解析为 ID（`/guilds/{guild_id}/channels`）
   - 8 个单元测试覆盖含 guild 解析场景

2. **回测可离线回放**（`backtest_service.py`）
   - `replay(start, end)`：任意历史日期区间直接结算 1/3/5 日 P&L
   - `replay_summary()`：产出结构化指标表
   - 4 个新测试

3. **合成 K 线兜底**（`history_adapters.py`）
   - `SyntheticHistoryAdapter`：行情源不可达时自动生成确定性 OHLC（种子 = contract_key + 日期）
   - 支持 `backtest.use_synthetic_when_unavailable`（默认 true）
   - `CompositeHistoryAdapter`：Massive 优先 → IBKR 回退 → 合成兜底

4. **飞书 Webhook 发送器**（`feishu.py`）
   - `FeishuWebhookSender`：群机器人 webhook，无需 App 凭据或绑定
   - `default_feishu_sender()`：自动选择 webhook 或 app 凭据
   - 4 个新测试

5. **端到端自检**（`live_test.py` + `e2e_fixture.py`）
   - 一键探测 Massive / DeepSeek / Discord / 飞书
   - 真实采集昨日 Discord 会话完成分析
   - 真实 Massive 期权 K 线回放演示（GTLB +29.5%止盈、SM +30.3%止盈）
   - 一个月的确定性回测演示（22 样本、57 笔结算）
   - CLI 命令：`python live_test.py` + `python -m options_radar e2e`

6. **openCode 修复脚本**（`fix-opencode.ps1` + `修复opencode.cmd`）
   - 清除损坏的 opencode 状态库，一键重建启动

### Bug 修复

1. **IBKR 期权链返回空**（`ibkr_provider.py`）
   - 修复：合约符号剥离 `US.` 前缀（`US.QQQ` → `QQQ`）
   - 修复：期权链选择从 "优先 SMART" 改为 "选数据最全的链"（SMART 只返回一个到期日，AMEX 有完整链）

2. **fpd/fqd 分析师命名不一致**
   - 统一 `fqd` 为规范名称；`fpd` 保留为别名
   - `parser.py`、`rulebook.py`、`setup_server.py`、`config.example.yaml` 同步
   - 涉及文件：6 个

3. **飞书 webhook 响应 `code: 0` 误判为失败**
   - 修复：`int(body.get("code") or -1)` 改为判 `None` 而非 falsy

4. **测试文件 `test_feishu.py` 的 `importlib.reload` 污染**
   - 修复：`test_import_has_no_lark_dependency` 改为子进程验证，不再破坏模块类身份

5. **Playwright 备用采集器 DOM 选择器**
   - 选择器从 `li[id^='chat-messages']` 改为多选择器组合 + `_wait_messages()` 水合等待

6. **IBKR 延迟行情支持**（`ibkr_provider.py`）
   - `_market_data_type` 改为可配置（默认 3=Delayed，config `ibkr.market_data_type`）
   - `_ensure()` 每次连接后主动调 `reqMarketDataType`

7. **富途从行情路由分离**（`config.local.yaml`）
   - `providers.market_priority` 移除 `futu`；`enabled.futu: false`

8. **富途导入自选错误处理**（`service.py`）
   - `import_futu_watchlist()` 加 try/except + 中文错误提示

9. **面板今日推荐/信号无数据**（`service.py`）
   - 新增 `_dashboard_date()`：自动回退到最近一个有数据的交易日

10. **live_test 产物路径 + UTF-8 编码**（`live_test.py`）
    - `data_dir` 改为 `data-local/`，启动设 `chcp 65001`

### 配置变更

- `config.example.yaml`：增加 `secret_refs`（disord_user_token / feishu_webhook）、`backtest` 段
- `config.local.yaml`：本地配置（IBKR + Massive + 富途 + 飞书 webhook + DeepSeek）
- `.env.example`：增加 `DISCORD_USER_TOKEN` / `FEISHU_WEBHOOK_URL`
- `opencode.jsonc`：移除 `opencode-devcontainers` 插件

### 测试统计

- 基线：111 passed
- 当前：**132 passed**

### 2026-08-12 第二阶段修复

11. **面板日期选择器**（`service.py`、`setup_server.py`）
    - `?date=YYYY-MM-DD` 参数 + 交易日下拉框，`_dashboard_date()` 自动回退
    - `PAGE_INFO` 补充 `/backtest` 路由

12. **Discord Token 有效性探测**（`discord_rest.py`）
    - `health()` 内调 `GET /users/@me`，401 标记 `token_invalid`

13. **持仓页公司名称**（`service.py`、`setup_server.py`）
    - IBKR `reqContractDetails` 获取 `longName`，懒加载缓存
    - 持仓表格增加"公司名称""最新价""涨跌"列

14. **回测页历史回放**（`service.py`、`setup_server.py`）
    - `dashboard_backtest()` 自动对 DB 中已有交易日结算 `replay`
    - 回测页展示成交/收益/回撤 + 逐笔明细

15. **推荐页评价标准说明**（`setup_server.py`）
    - `/` 页面底部加可展开的评分维度表（共识/历史/信号质量/行情/组合适配）

16. **CMD 启动脚本修复**（`启动异常期权助手.cmd`）
    - 删 `chcp 65001`（解决中文乱码）、加 `title` + `pause`（+21 测试：discord_rest 8、backtest_replay 4、feishu webhook 4、e2e 1、channel resolution 1、已有的其他测试修复）

### 2026-08-13 本轮功能修复

17. **持仓页非阻塞与手动刷新**
    - 页面只读缓存，不再同步调用 IBKR 合约详情
    - 新增 `/api/portfolio/refresh`，手动后台刷新持仓和公司名称
    - 顶部导航增加“回测”入口

18. **推荐 Top5 与执行字段**
    - 页面显示分数前5名，不再限制为3名
    - 展开 `execution` 字段显示 bid/ask/入场/止盈/止损
    - 增加确定性推荐理由和“可执行/待行情”状态

19. **飞书 Top5**
    - 每5分钟发送当前交易日 Top5
    - 不再设置60分过滤，低分候选也会按排名发送
    - 卡片包含理由、行情状态和执行字段状态

20. **实机视觉走查**
    - 首页、日期切换、信号、回测、设置、API动作已验证
    - 持仓页阻塞问题已修复并增加手动刷新

21. **任务 A：盘口诊断与展示规范**
    - bid/ask/入场/止盈/止损展示统一保留 2 位小数
    - ProviderRegistry 增加 `quote_missing`，避免用单个 realtime 字段误判盘口可用
    - IBKR Provider 记录 10089/10090/10091/162/200/321 市场数据错误到系统诊断
    - 新增缺失字段状态，禁止用猜测值伪造 bid/ask

### 2026-08-14 Alpaca Phase A1

22. **Alpaca Trading API Paper 探针**
    - Paper 账户认证成功，options trading level=3
    - 正股快照正常返回实时 bid/ask/last
    - 期权合约查询正常
    - Indicative 期权快照正常返回 bid/ask/last
    - 历史期权 bars 返回 `OPRA agreement is not signed`，暂不能作为真实回测数据

23. **Alpaca 主路由准备**
    - 本地配置切换为 `market_priority: [alpaca]`
    - 增加 `AlpacaHistoryAdapter`
    - IBKR 保留为只读持仓源，Massive/Futu 不再参与行情主路由

24. **Alpaca Phase A2 实测**
    - Paper Trading 账户认证成功，options trading level=3
    - 正股实时快照和期权 Indicative bid/ask 正常
    - 期权历史 bars 返回 `OPRA agreement is not signed`，已记录为权限限制
    - `live_test.py` 的真实回放入口改为 Alpaca，不再主动调用 Massive

### 2026-08-14 Phase B：自选持仓证券元数据固化

25. **instrument_metadata 表**
    - SQLite 新增证券元数据表（symbol/name_en/name_zh/industry/current_price/change_pct/source/updated_at）
    - `_refresh_stock_meta` 批量拉取 Alpaca 股票快照（分块 50，过滤非法符号）与资产名称
    - 中文名通过 DeepSeek 翻译，缺失时才调用且每轮限制 15 次，结果持久化
    - 手动刷新持仓时后台更新元数据，broker 同步失败不再中断元数据刷新
    - 持仓页表格新增中文名/行业/更新时间列
    - 修复 DeepSeek 文本操作对“只返回名称”非 JSON 输出的宽松解析

### 2026-08-15 Phase C 前置：浏览器 DOM 采集

26. **浏览器 DOM 采集替换 REST 为主通道**
    - Playwright 持久化 profile 登录态读取全部频道，覆盖订阅/论坛正文
    - 频道精简为 flow/pa/mr/qmr/fpd/newsfeed（移除 guide/subscriptions）
    - newsfeed 只入库不解析；调度改为每 6 分钟
    - 每频道 60 秒超时，单频道失败不阻塞整轮
    - 处理 Discord「在浏览器中继续」深链拦截；修正 `_logged_out` 误判
    - 实机验证：flow 实时采到当日卡片；分析师主频道 8/13 凌晨(UTC 8/12 16:15)消息已入库

### 2026-08-15 推荐 B+C 与界面改版

27. **推荐策略 B+C**
    - `_dashboard_date` 回退到最近有 flow_events 的交易日，页面随采集更新
    - 无分析师配对时展示 flow-only 候选（按 premium 排序，上限 10），标注「仅flow/无分析师确认」
    - 日期下拉框列出有 flow 或推荐的所有交易日

28. **苹果官网简约风界面**
    - 全局重写：浅灰背景、毛玻璃顶栏、大圆角卡片、胶囊按钮、系统字体栈
    - 首页卡片化候选：方向徽标（红涨绿跌）、评分等级、价格行、理由、状态徽标
    - 信号明细加状态徽标（分析师N/仅flow）；持仓涨跌红涨绿跌
    - 纯 inline CSS/JS，零外部依赖，保留文案以兼容测试

### 2026-08-15 富途实时行情主源 + 信号明细增强

29. **富途 OpenD 期权行情权限打通并设为主行情源**
    - 用户开通富途美股期权行情权限后，OpenD `get_option_chain`/`get_market_snapshot` 返回真实 bid/ask/OI/IV/希腊字母
    - 修复 `FutuUnifiedProvider.health()` 读取不存在的 `connected` 字段导致永远 `offline`（改为读 `ready`）
    - `config.local.yaml`：`market_priority: [futu, alpaca]`、`enabled.futu: true`、`market.provider: futu`
    - 候选卡 bid/ask/OI 填充富途 realtime 数据；alpaca indicative 兜底
    - 富途历史 K 线（`request_history_kline`）仍会超时，回测继续走 Massive/Alpaca/合成兜底

30. **信号明细增强**
    - 表格扩为：标的/合约/交易量/时间/方向/状态 六列
    - 交易量列显示 `$3.2M`/`$968K` 格式（新增 `_format_premium`）
    - 方向列聚合分析师 BULL/BEAR/中性（红涨绿跌徽章）
    - 每行可展开「分析师明细」：分析家族中文名（价格行为/均值回归/量化均值回归/流价背离）、方向、决策、置信度、理由
    - 首页候选卡同步加交易量小字；推荐视图补 `direction`（从 `final_direction`）与 `premium`（从 flow_events 回填）
