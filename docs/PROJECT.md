# Options Radar 项目文档

## 项目目标

Flow-First 的 AI 期权交易决策系统（卖方优先）：从 Discord 异常期权频道采集信号，叠加 GEX（Gamma 环境）、Vol（期权贵贱）、价格结构（支撑阻力）、事件/Short Gamma 风险过滤，AI 只做翻译与审计（不决策、不碰数值），输出卖方优先方案。系统只读不交易，模拟仓位和回测仅用于评估策略表现。

## 当前版本

**v2-local**（本地 Windows 运行时，2026-08-21）

## 当前已实现的功能

| 功能 | 状态 | 说明 |
|---|---|---|
| Discord 采集（REST + 浏览器 DOM） | ✅ | 用户 token 分页读历史 + Playwright 登录态读订阅正文；频率/时段可在 /setup 配置 |
| 信号解析（确定性 + DeepSeek 补全） | ✅ | raw flow + 分析师卡片 → ParsedSignal；缺失字段 DeepSeek 结构化补齐 |
| 多分析师共识评分（权利金质量优先） | ✅ | 40% 权利金质量 + 20% 信号 + 15% 组合 + 15% 方向 + 10% 历史胜率 |
| 富途 OpenD 行情 | ✅ | 实时盘口/OI/greeks/IV、期权链、`iv_rank`+`hv_30d`（替代自算 HV） |
| **GEX Engine** | ✅ | `gex.py` 加总聚合：Call/Put Wall、Gamma Flip、Regime、净 GEX 曲线、Term Structure、Heatmap |
| **Dealer Chart 弹窗** | ✅ | K线（红涨绿跌）+ MA20/50 + 20日支撑阻力 + GEX 曲线 + Flow 圆点 + Heatmap；图层开关 + 悬停注释 |
| GEX 前瞻验证 | ✅ | `gex_snapshots` 表每日收盘自动快照（无历史 OI，只能前瞻）；/system 展示进度 |
| IV / Skew / Term Structure | ✅ | ATM IV、Put/Call Skew、各到期 ATM IV 期限结构（同批期权链零额外请求） |
| 策略矩阵 + Short Gamma 结构化 + Expiration 推荐 | ✅ | Sell Put/Sell Call/Credit Spread/Buy Stock/不交易；BLOCK/CAUTION/PREFERRED；本周/下周/2W~1M |
| 事件风险 / 财报 | ✅ | `get_earnings_screener` → HIGH/MEDIUM/LOW + 财报天数 + 预期波动 |
| Smart Watchlist | ✅ | 自选页 IV Rank / IV-HV / GEX / 近30日迷你K线 / GEX按钮 / 期权事件链接 / 排序 |
| Alert 扩展（飞书） | ✅ | 现价逼近 Wall/Flip ±1%、Regime 翻转、IV Rank≥80；按日去重 |
| Flow Type 分类 | ✅ | 关键词先分 + DeepSeek 语义归类；Strength=权利金百分位（程序计算） |
| 回测引擎 | ✅ | 90 交易日数据，卖方/买方/正股计划三套口径；Test D（HV 分层 + 评分区分度） |
| 飞书通知 | ✅ | webhook 日报/Top5/预警 + 自建应用双向对话 |
| 本地面板（Web UI） | ✅ | 纯 stdlib，双栏布局 + 侧栏快捷操作，各页统一设计系统 |
| 定时任务调度 | ✅ | Discord 采集、飞书 Top5/预警、回扫、收盘 GEX 快照、分类、日报、回测、备份 |

## 尚未实现 / 待验证

| 功能 | 优先级 | 阻塞原因 |
|---|---|---|
| 期权实时 bid/ask | 高 | 富途美股期权权限未开通 / IBKR 无行情订阅（当前停留 C 级观察榜，属正常） |
| GEX 前瞻验证结论 | 中 | 需持续运行数周积累快照（wall 突破率、flip 后波动） |
| 评分 B+ 分层统计意义 | 中 | 样本仅 11 笔，需继续积累 |
| NAS Docker 部署验证 | 低 | 目前仅验证本地 Windows 模式 |
| 策略参数自动优化 | 低 | 需 100+ 真实样本触发 weekly optimizer |

## 核心业务逻辑

```
Discord（REST/DOM）→ 解析 → flow_events / parsed_signals
  → _evaluate：富途行情 → GEX/Vol/事件/Short Gamma 注入 execution
    → scoring（权利金质量优先）→ 推荐（卖方优先，DTE 14~60）
      → 面板渲染（Dealer Chart / Heatmap / tooltip）
        → 飞书 Top5 + 预警 + 日报 + 每日 GEX 快照（前瞻验证）
```

## 重要限制条件

1. **交易锁定**：只读，不提交真实订单（`ibkr readOnly=true`）。
2. **AI 不决策**：评分/仓位/止盈止损/GEX/IV 全由确定性代码计算；DeepSeek 只做翻译、审计、文案、Flow 语义归类。
3. **GEX 不进评分**：GEX 是环境层+风控层，回答支撑/阻力/波动环境，不做方向预测。
4. **只运行一个实例**：SQLite WAL 不支持多进程并发写。
5. **行情字段不造假**：bid/ask 缺失显示「待行情/字段未就绪」。
6. **富途防限流**：期权链串行分批、快照 100/批、空结果不落库、失败重试 1 次；不使用共享长连接。
7. **数据库红线**：诊断/recompute 只 UPDATE/INSERT，绝不 `DELETE FROM recommendations`。
8. **持仓页不阻塞**：只读缓存，手动刷新走后台任务。

## 不能被破坏的功能

- 208 个单元/集成测试（pytest 全绿）
- Discord REST 采集（7 频道，中文名→ID 解析）
- 确定性评分链路（fixture → 解析 → 共识 → 推荐）
- 飞书 webhook 通知 / GEX 快照 / 预警 / 分类
- 面板 walkthrough 13/14（唯一环境项 = IBKR 网关未开）

## 技术栈

| 层次 | 技术 |
|---|---|
| 语言 | Python 3.8 |
| 数据库 | SQLite（WAL，单文件） |
| 配置 | YAML（`${VAR:default}` 展开） |
| 行情 | futu-api 10.9（主源）、Alpaca（历史日线）、Massive（回测兜底）、ib-insync（只读持仓） |
| Discord | HTTP REST（用户 token）+ Playwright DOM |
| 飞书 | Webhook / lark-oapi 自建应用 |
| AI | DeepSeek API（flash/pro，月度预算 + 缓存 + 用量护栏） |
| 测试 | pytest + unittest |
| 调度 | APScheduler |
| Web 面板 | Python stdlib `http.server` + 内联 CSS/JS（零前端框架，纯 SVG 图表） |

## 项目目录结构

```
options-radar/
├── options_radar/          # 主代码包（service/db/scoring/gex/vol/parser/…）
├── tests/                  # 单元/集成测试（208 passed）
├── docs/                   # 文档（产品开发文档-v2、PROJECT、CHANGELOG、TODO、BUGS、ARCHITECTURE、strategy、nas）
├── scripts/                # 运维脚本（setup/start/start-nas/修复opencode）
├── data-local/             # 本地数据（DB + secrets + evidence + backups，gitignore）
├── config.*.yaml           # 配置（local / example / nas.example）
├── requirements*.txt       # 依赖
├── Dockerfile / docker-compose.yml
├── live_test.py            # 在线全链路实测
├── walkthrough.py          # 面板 14 项实机走查
├── 启动/停止异常期权助手.cmd / stop_radar.ps1   # 本地启动/停止（根目录，双击使用）
└── README.md / README_LOCAL.md
```

## 启动方式

**本地面板（推荐）：**
```
双击：启动异常期权助手.cmd
或：.\.venv\Scripts\python.exe -m options_radar.local_runtime
→ http://127.0.0.1:8787/
```

## 测试方式

```
.\.venv\Scripts\python.exe -m pytest -q        # 208 passed
.\.venv\Scripts\python.exe walkthrough.py      # 面板 14 项走查（13/14，唯一环境项=IBKR 网关）
.\.venv\Scripts\python.exe live_test.py        # 在线全链路实测（含飞书发送）
```

## 面板页面

- `/`：卖方推荐卡片（5 行 + ⓘ tooltip + GEX 位置条 + 曲线按钮）
- `/signals`：信号明细（类型/强度/分析师明细展开，支持 symbol 跨 30 天过滤）
- `/portfolio`：自选与持仓（IV Rank / IV-HV / GEX / 迷你K线 / 期权事件链接 / 排序）
- `/backtest`：分析师信号回测 + Test D 历史验证
- `/system`：总览 + 数据源 + GEX 前瞻验证；侧栏含测试/备份/快照/预警按钮
- `/setup`：一次性配置（含 Discord 采集间隔/时段）
