# Options Radar 项目文档

## 项目目标

从 Discord 的异常期权频道实时采集美股期权信号，合并多位分析师意见，对接 Alpaca 行情 API 做确定性评分筛选，每日输出评分前 5 张候选合约，通过飞书群发送日报与提醒。IBKR 暂时只读同步持仓，富途只做一次性自选导入。系统只读不交易，模拟仓位和回测仅用于评估策略表现。

## 当前版本

**v2-local**（本地 Windows 运行时，2026-08-12 重构版）

## 当前已实现的功能

| 功能 | 状态 | 说明 |
|---|---|---|
| Discord 采集（REST API） | ✅ | 用户 token 分页读取频道历史，支持中文频道名自动解析为 ID |
| Discord 采集（浏览器 DOM） | ✅ | Playwright 登录态读取全部频道，覆盖订阅正文；每 6 分钟采集 |
| 信号解析（确定性 + DeepSeek 补充） | ✅ | raw flow + 分析师卡片 → ParsedSignal，缺失字段用 DeepSeek 结构化补齐 |
| 多分析师共识评分 | ✅ | 权重融合 + 方向一致性 → 确定性打分，家族内部/跨家族冲突检测 |
| Alpaca 行情 API | ✅ | Paper Trading API，Indicative 期权快照、正股快照；OPRA/历史权限另需协议和套餐 |
| 证券元数据固化 | ✅ | instrument_metadata 表持久化英文/中文名、行业、价格、涨跌幅；手动刷新时更新 |
| IBKR Gateway 连接 | ✅ | 暂时只读同步持仓/账户，不再参与主行情路由 |
| Massive 行情 (EOD) | ⏸️ | 保留代码用于回退，当前不参与主行情路由 |
| 富途 OpenD 连接 | ✅ | 行情已登录，自选组已读取；期权权限尚未开通 |
| 回测引擎（任意日期回放） | ✅ | 支持任意历史日期范围结算 1/3/5 日 P&L；真实 Massive K 线 + 合成兜底 |
| 飞书通知（webhook） | ✅ | 群机器人 webhook，日报/提醒直接发群，无需 App 凭据 |
| 飞书通知（自建应用） | ✅ | 支持长连接接收命令、双向对话（需 App 凭据 + 事件订阅） |
| 本地面板（Web UI） | ✅ | http://127.0.0.1:8787，首页推荐/信号/持仓/回测/系统诊断/配置；推荐前5可按交易日查看 |
| 定时任务调度 | ✅ | 60s Discord 采集、5min IBKR 同步与飞书 Top5、17:15 ET 日报、18:00 ET 回测 |
| CLI 命令（e2e/backtest/collect/ingest/report） | ✅ | 离线自检、回测回放、采集、报告生成 |
| 端到端自检（live_test.py） | ✅ | 一键探测 4 API → 昨日分析 → 真实 K 线回放 → 飞书汇总卡片 |

## 尚未实现的功能

| 功能 | 优先级 | 阻塞原因 |
|---|---|---|
| 期权实时 bid/ask 行情 | 高 | 富途无期权权限 / IBKR 无行情订阅，导致推荐停留 C 级观察榜 |
| 真实历史回测（累计真实推荐） | 中 | 需系统运行 2-4 周积累推荐数据 |
| 策略参数自动优化 | 低 | 需 100+ 真实样本触发 weekly optimizer |
| 富途持仓同步 | 低 | OpenD 连接正常但未找到实盘 US 账号 |
| 移动端推送 | 低 | 飞书已经覆盖 |

## 核心业务逻辑

```
Discord REST（用户 token 分页）→ RawMessage
  → parse_flow_message → FlowEvent（raw flow 行）
  → parse_analyst_message → ParsedSignal（分析师卡片）
    → evaluate_consensus（权重 × 置信度 × 方向 → 确定性打分）
      → MarketSnapshot（Alpaca 主行情）
      → PortfolioContext（IBKR 持仓/自选）
        → build_candidate（仓位、止盈止损）
          → 推荐输出（前 5 张，65 分才标记为 eligible/A 级提醒）
            → backtest replay（1/3/5 日结算）
              → Feishu 飞书日报 + A 级提醒
```

## 重要限制条件

1. **交易锁定**：系统不会解锁交易，不会提交真实订单。`ibkr readOnly=true`。
2. **确定性评分**：综合评分、仓位、止盈止损完全由 Python 确定性代码计算。DeepSeek 仅处理 OCR/文本结构化/摘要。
3. **只运行一个实例**：SQLite WAL 模式不支持多进程并发写入。
4. **每日最多 3 张候选合约**：评分不足时空缺。
5. **推荐仅从 Discord 强信号 + 持仓/自选交集** 中选择。
6. **期权实时行情需要账号权限**：富途需开通美股期权，IBKR 需订阅行情数据包。
7. **行情字段不造假**：bid/ask/入场/止盈/止损缺失时显示“待行情/字段未就绪”，不使用猜测值。
8. **持仓页不在页面请求中连接 IBKR**：公司名称等元数据通过“手动刷新持仓”后台更新，避免页面阻塞。
9. **盘口状态优先于单字段状态**：只有完整 bid/ask 报价对才允许标记为可执行；单独的 volume/last/Greek realtime 不代表盘口可用。

## 不能被破坏的功能

- 132 个单元/集成测试（100% 通过）
- Discord REST 采集（7 频道，中文名→ID 解析）
- 确定性评分链路（fixture → 解析 → 共识 → 推荐）
- 飞书 webhook 通知
- CLI `e2e` 自检（全链路零外部依赖闭环验证）

## 技术栈

| 层次 | 技术 |
|---|---|
| 语言 | Python 3.8 |
| 数据库 | SQLite（WAL 模式，单文件部署） |
| 配置 | YAML（含环境变量模板 `${VAR:default}`） |
| 行情 SDK | ib-insync 0.9.86、futu-api 10.9 |
| Discord | HTTP REST API（用户 token + `urllib`） |
| 飞书 | Webhook（群机器人）或 lark-oapi（自建应用） |
| AI | DeepSeek API（v4-flash / v4-pro） |
| 历史 K 线 | Massive API（OCC 期权聚合） |
| 测试 | pytest 8.x + unittest |
| 定时任务 | APScheduler 3.x |
| Web 面板 | Python stdlib `http.server`（零前端框架） |
| OCR 备用 | rapidocr-onnxruntime（中文混合识别） |

## 项目目录结构

```
options-radar/
├── options_radar/          # 主代码包
│   ├── service.py          # 核心管线（采集 → 解析 → 评分 → 推荐）
│   ├── discord_rest.py     # Discord REST 采集源（用户 token）
│   ├── discord_source.py   # Discord Playwright 采集源（备用）
│   ├── parser.py           # 信号确定性解析
│   ├── scoring.py          # 共识评分（确定性）
│   ├── models.py           # 数据模型（dataclasses）
│   ├── db.py               # SQLite 数据访问层
│   ├── config.py           # 配置加载（YAML + 环境变量扩展）
│   ├── ibkr_provider.py    # IBKR 行情 + 持仓
│   ├── futu_provider.py    # 富途行情 + 持仓 + 自选
│   ├── massive_client.py   # Massive 行情 + K 线
│   ├── backtest_service.py # 回测协调器（replay + optimize）
│   ├── history_adapters.py # K 线适配器（Massive/IBKR/合成）
│   ├── feishu.py           # 飞书传输层（webhook + inbox/outbox）
│   ├── ai_provider.py      # DeepSeek 适配器
│   ├── paper.py            # 模拟交易引擎
│   ├── optimizer.py        # 回测策略优化器
│   ├── rulebook.py         # Discord 使用指南/分析师订阅规则
│   ├── reports.py          # 日报生成
│   ├── cli.py              # CLI 命令入口
│   ├── local_runtime.py    # 本地 Windows 运行时
│   ├── nas_runtime.py      # NAS Docker 运行时
│   ├── setup_server.py     # 中文 Web 面板
│   ├── e2e_fixture.py      # 端到端测试夹具
│   └── ...
├── tests/                  # 单元/集成测试（132 passed）
├── data-local/             # 本地数据（DB + secrets，gitignore）
├── config.local.yaml       # 本地配置（gitignore）
├── live_test.py            # 一键实测脚本
├── fix-opencode.ps1        # opencode 修复脚本
├── 启动异常期权助手.cmd      # 本地启动脚本
└── README.md
```

## 启动方式

**本地面板（推荐）：**
```
双击：启动异常期权助手.cmd
或：.\.venv\Scripts\python.exe -m options_radar.local_runtime
→ 浏览器打开 http://127.0.0.1:8787/
```

**CLI 自检：**
```
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe live_test.py
```

## 测试方式

```
.\.venv\Scripts\python.exe -m pytest tests -q      # 132 passed
.\.venv\Scripts\python.exe -m options_radar e2e     # 离线全链路自检
.\.venv\Scripts\python.exe live_test.py              # 在线全链路实测（含飞书发送）
```

面板补充说明：

- `/`：按评分显示前 5 名，并显示理由和执行字段状态
- `/portfolio`：页面只读缓存；点击“手动刷新持仓”才调用 IBKR 更新组合和公司名称
- `/backtest`：顶部导航可直接进入历史回放
- 飞书：每 5 分钟发送当前交易日 Top5，不设 60 分门槛
