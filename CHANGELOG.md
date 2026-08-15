# 变更日志

## 2026-08-16 — 分析师信号回测引擎

### 新增功能

1. **分析师 TRADE 信号回测**（`analyst_backtest.py`）
   - `AnalystBacktestCoordinator`：对全部 `decision=TRADE` 信号做确定性回放（0 token）
   - 两层评估：**方向正确率**（正股日线：信号日收盘 → N 交易日收盘）与**策略盈亏**（做多对应期权：BULL→CALL、BEAR→PUT）
   - 数据源为 Massive 期权/正股日线（非 5 分钟线），按 1/3/5 日三个持有期结算
   - 前视偏差防护：策略入场用信号**次日**开盘（信号盘中产生，当日开盘属未来数据）
   - 每信号仅 2 次请求（正股+期权各拉一次，覆盖最大持有期后按 horizon 切片），规避 Massive 5 req/min 限速

2. **回测结果持久化**（`db.py`）
   - 新增 `analyst_backtest_outcomes` 表（按 analyst + contract + horizon 幂等 upsert）
   - `save_analyst_backtest_outcome` / `analyst_backtest_outcomes` / `analyst_backtest_summary`（按分析师×持有期聚合）

3. **面板「分析师准确度」表**（`setup_server.py` `/backtest` 页）
   - 展示每分析师每持有期的信号数、方向正确率、策略胜率、平均盈亏
   - 后台回测任务由 `service.py` 触发（`_maybe_start_analyst_backtest`），首次访问自动启动，完成后缓存 1 小时

4. **信号日历**（`db.py` + `setup_server.py` `/backtest` 页）
   - `analyst_backtest_daily_summary()`：按交易日聚合 5 日持有期的方向正确率、策略胜率、平均盈亏
   - 面板「信号日历」表展示每日信号质量，识别信号质量规律

### 测试

- 新增 `tests/test_analyst_backtest.py`（5 个用例：join/去重、BULL/BEAR 方向、前视偏差防护、幂等持久化）
- 测试基线 **145 passed**（此前 140）

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

### 2026-08-15 采集调度 + 界面体验大改

31. **美股交易时段采集**
    - `timeutil.is_us_cash_session()`：周一~周五 09:30-16:00 ET 判定开市（pytz）
    - 采集/推荐/回扫/同步从全天 interval 改为 `CronTrigger` 限定美股时段；收盘后 16:30 补扫、17:15 日报、18:00 回测保持不变
    - 首页候选卡行情标签：开市显示「实时/待采集」，休市显示「休市」，不再误报 missing

32. **今日推荐卡片排版**
    - 分析师判断改为胶囊列表（每分析师一条），不再逗号挤一行
    - 风险提示收敛为独立卡片，仅当行情异常（缺失/过期/冲突）时展示
    - 入场/止盈/止损、评分组成分行展示

33. **评分体系说明重写**
    - 对齐国际常用期权分析框架：方向共识（40%）、历史胜率（20%）、信号完整度（15%）、行情质量（15%）、组合适配（10%）
    - 明确每维度的数据来源与算法；DeepSeek 仅做翻译/文字，不参与数值决策

34. **自选与持仓页**
    - 异常期权相关标的分组置顶并打「异常期权」红标，可展开查看该标的近期 flow 合约
    - `_refresh_stock_meta` 增加富途正股行情兜底（alpaca IEX 缺失时取富途 last）

35. **新闻速递页（/newsfeed）**
    - 展示 newsfeed 频道原文 + DeepSeek 中文翻译与市场影响/关联自选（`ai.analyze_news`，prompt digest 缓存不重复烧钱）
    - `/api/newsfeed` API + 导航入口；新闻采集独立 1h 全天任务

36. **系统诊断页优化**
    - 状态页改为结构化卡片：系统状态/美股时段/最近采集/数据源（富途/IBKR/Discord/AI/飞书）/持仓概览
    - `health()` 增加 30 秒缓存，去掉逐个 provider 的实时网络探测，打开不再卡

### 2026-08-15 分析家族修正 + 功能精简

37. **qmr/mr 分析家族区分**
    - `qmr` 从 `momentum_reversal` 改为 `quant_mean_reversion`（量化均值回归），`mr` 改为 `mean_reversion`（均值回归）
    - 修复「仅一个独立分析家族确认」误判：此前 mr/qmr 归为同一 family 导致 4 个分析师只剩 1 个 family，评分封顶 64
    - 迁移 654 条历史信号的 analyst_family；修正后开市+富途实时行情下 NVDA 71.67 B 级、IWM 72.46 B 级（此前全部 64 分 C 级）
    - 更新 parser.py / rulebook.py family 映射与对应测试

38. **风险提示文案改进**
    - 「行情缺失或服务异常」→ 区分「暂未取到实时行情」与「行情时间戳已过期（休市），需等开市后刷新」
    - 「富途原生盘口或OI不完整」→ 区分「盘口与OI为过期数据（休市）」与「盘口或OI暂不完整」

39. **功能精简**
    - IBKR 仅保留持仓同步（`sync_broker`/`/api/ibkr/sync`/`ibkr-sync` 调度）；移除作为行情 provider 的所有入口
    - 移除 `discover_ibkr` 接口、`/api/ibkr/discover` 路由与「检测IB Gateway」按钮
    - provider registry 精简为 futu/alpaca/massive；移除 tradier/marketdata_app 构造与 secret 引用
    - 历史行情仅走 Alpaca/Massive（移除 IBKR 历史适配）；`_resolve_contract`（遗留 ibkr 期权链）删除
    - /providers 页只保留 futu/alpaca/massive 操作 + IBKR 持仓同步按钮

### 2026-08-15 设置页清理 + 推荐数据刷新 + 富途分组 + 停止脚本

40. **设置页清理联动**
    - SECRET_FORM_FIELDS / SECRET_ENV_FILES 移除 MarketData.app / Tradier / IBKR Flex
    - 频道表单从 fqd/guide/subscriptions 更新为 fpd/newsfeed；`DEFAULT_CONFIG.channel_names` 同步
    - /system 描述与 IBKR 卡片改为「持仓同步」；/providers 描述更新

41. **推荐数据重新评估**
    - 重跑 `_evaluate(8/11)` 覆盖旧快照：risk_flags 换成新文案（休市/过期），「仅一个独立分析家族确认」按真实 family 判定（38 条中 29 条移除，9 条确为单家族保留）
    - 周六休市行情仍 stale，分数维持 C 级；周一开盘自动恢复 70+

42. **自选与持仓显示富途分组**
    - `instrument_metadata` 增加 `group_name` 列（ALTER 迁移 + 写入逻辑）
    - `_refresh_stock_meta` 后台拉取富途分组名并持久化；页面「分组」列显示（持仓/核心/特别关注/美股/能源…）

43. **newsfeed AI 分析策略**
    - 页面默认显示最近 10 条；每次最多分析 5 条新消息（prompt digest 缓存保证不重复花钱）

44. **停止脚本入库**
    - 新增 `停止异常期权助手.cmd` + `stop_radar.ps1`：双击安全终止 Options Radar 进程并释放 8787 端口（不会误杀 PIME）

### 2026-08-15 首页风险提示重构

45. **休市统一备注 + 紧凑风险标签**
    - 休市类风险（行情时间戳过期 / 盘口OI过期）不再逐条显示在候选卡，改由页面顶部 `market-banner` 统一备注「当前周末休市，行情为最近收盘快照」
    - 候选卡风险区从大红框改为紧凑行内 `risk-inline` + `risk-chip` 小标签（12px/11.5px），只显示该合约独有风险（如 DTE 超范围、Delta 缺失、价差过大）
    - 原始数据 JSON 仍保留完整 risk_flags 供审计

### 2026-08-15 布局平铺 + newsfeed 周报 + 回测启动

46. **页面布局平铺 + 多终端适配**
    - 主容器 960px → 1280px，大屏候选卡 3-4 列平铺（`grid: repeat(auto-fill,minmax(240px,1fr))`）
    - 响应式断点：≤900px 两列、≤640px 单列并收缩 padding
    - 信号明细/持仓/回测表格外层包 `.table-wrap`（手机横向滑动）

47. **newsfeed 最近 7 天综述 + 单条仅翻译**
    - `ai.analyze_news` 改为仅中文翻译（不再做市场影响，省 token）
    - 新增 `ai.summarize_news_week`：聚合最近 7 天新闻产出中文周报（大事记 + 市场主题 + 自选关联），prompt-digest 缓存
    - `/newsfeed` 页顶部「最近 7 天新闻综述」+ 下方单条中文列表
    - 新增「回填最近7天新闻」按钮（`/api/actions/news-backfill`，浏览器滚 40 页拉历史）

48. **回测启动（Massive 真实历史）**
    - 历史行情数据源切换为 Massive（`CompositeHistoryAdapter(massive, None)`），已验证返回真实期权日线（NVDA 217.5C 8/10-8/14）
    - `replay(8/11,8/11)` 跑通：7 条成交 / 8 条未成交，真实收益率（META +30.8%、QQQ +6.6% 等）
    - `/backtest` 页增强：统计卡（结算笔数/胜率/平均收益率/最大回撤）+ 逐笔结算明细表 + 分析师胜率拆分（mr 40%、qmr 33%）
    - replay 改后台线程执行，页面不阻塞（修复页面断连：`parts.append(...).format` 绑定到 None 的 bug）
