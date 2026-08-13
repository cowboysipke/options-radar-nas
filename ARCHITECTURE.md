# 架构文档

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    本地面板 (HTTP :8787)                    │
│                  setup_server.py (stdlib)                 │
├─────────────────────────────────────────────────────────┤
│                     OptionsRadarService                    │
│                      service.py                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │  Discord  │  │  Parser  │  │  Scoring  │  │  Backtest │ │
│  │ REST/WEB  │  │          │  │           │  │ replay    │ │
│  └────┬──────┘  └────┬─────┘  └─────┬─────┘  └────┬─────┘ │
│       │              │              │              │       │
│  ┌────▼──────────────▼──────────────▼──────────────▼───┐ │
│  │               SQLite Database (WAL)                  │ │
│  │          data-local/options_radar.db                 │ │
│  └──────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│  行情层                                                  │
│  ┌────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │  IBKR  │  │ Massive  │  │  富途    │  │ 合成兜底  │   │
│  │ Gateway│  │  (EOD)   │  │  OpenD   │  │Synthetic │   │
│  └────────┘  └──────────┘  └──────────┘  └──────────┘   │
├─────────────────────────────────────────────────────────┤
│  通知层                             AI 层               │
│  ┌────────────┐                   ┌──────────┐         │
│  │ Feishu     │                   │ DeepSeek │         │
│  │ webhook    │                   │ API      │         │
│  └────────────┘                   └──────────┘         │
└─────────────────────────────────────────────────────────┘
```

## 模块职责

### 核心管线 (`service.py`)
- 统一入口：`OptionsRadarService` 管理所有子模块生命周期
- `collect()`：协调 Discord 采集 → 解析 → 评分 → 推荐
- `_evaluate()`：对单个交易日的所有 flow events 执行行情查询 + 共识评分
- `sync_broker()`：优先 IBKR，失败回退富途
- 调度器：APScheduler 后台定时任务

### 采集层
| 模块 | 职责 | 数据来源 |
|---|---|---|
| `discord_rest.py` | HTTP REST API 分页读取频道历史 | Discord 官方 API（用户 token） |
| `discord_source.py` | Playwright 浏览器 DOM 采集（备用） | Discord Web 客户端 |

### 解析层 (`parser.py`)
- `parse_flow_message()`：正则解析 raw flow 行 → `FlowEvent`
- `parse_analyst_message()`：正则 + DeepSeek 补充 → `ParsedSignal`
- 完全确定性（缺失时 LLM 补充，但 schema 校验是代码做）

### 评分层 (`scoring.py`)
- `evaluate_consensus()`：5 维度加权打分
  - 共识（40%）：家族方向一致性
  - 历史（20%）：分析师历史表现
  - 信号质量（15%）：完整性 + 置信度 + 新鲜度
  - 行情质量（15%）：bid/ask + spread + OI
  - 组合适配（10%）：持仓集中度 + 自选匹配

### 行情层
| 模块 | 供应商 | 数据 |
|---|---|---|
| `ibkr_provider.py` | IBKR Gateway | 持仓、账户、期权链。实时/延迟行情需订阅 |
| `futu_provider.py` | 富途 OpenD | 标的实时行情、自选组。期权需权限 |
| `massive_client.py` | Massive API | EOD 收盘价、期权日线 OHLC（回测核心） |
| `history_adapters.py` | 组合 | Massive 优先 → IBKR 回退 → 合成兜底 |
| `provider_registry.py` | 路由 | 多供应商字段级融合，冲突检测 |

### 回测层 (`backtest_service.py`)
- `replay(start, end)`：任意历史日期范围结算 1/3/5 日 P&L
- `replay_summary()`：产出指标表（胜率、平均收益、最大回撤）
- `optimize_weekly()`：策略参数网格搜索 + 影子版本评估

### 条件层 (`paper.py`)
- `build_candidate()`：产出期权候选合约（仓位、止盈止损、失效条件）
- `PaperEngine.maybe_open()`：模拟开仓；`mark()`：市价平仓

### 通知层 (`feishu.py`)
- `FeishuWebhookSender`：群机器人 webhook（最简单）
- `LarkMessageSender`：自建应用（需要 App 凭据 + lark-oapi）
- `FeishuStore`：SQLite inbox/outbox 持久化（防丢失+重试）

### AI 层 (`ai_provider.py`)
- `DeepSeekProvider`：结构化提取 + OCR + 中英释义
- 预算控制（月度 ¥30 上限）+ 日限额（pro 模型 10 次/天）

## 数据流

```
Discord频道消息
    │
    ▼
RawMessage (dedup by content_hash)
    │
    ├─ flow 频道 ──► parse_flow_message() ──► FlowEvent (event_key + session_date)
    │
    └─ 分析师频道 ──► parse_analyst_message() ──► ParsedSignal (flow_event_key 关联)
        │
        ▼
    FlowEvent ←──────── join ───────── ParsedSignal[] (by event_key)
        │
        ▼
    MarketSnapshot (复合行情)  ←── ProviderRegistry (ibkr/massive/futu 字段级融合)
        │
        ▼
    evaluate_consensus (打分) ──► ConsensusEvaluation (score, grade, votes, risk_flags)
        │
        ▼
    build_candidate (仓位/止盈止损) ──► OptionCandidate
        │
        ├─► 推荐输出（按分数排序前5；score ≥ 65 才是 eligible）
        ├─► PaperEngine.maybe_open (模拟盘)
        └─► 飞书日报 + A级提醒
                │
                ▼
        backtest replay (1/3/5日结算)
```

持仓页面：页面只读取内存缓存；“手动刷新持仓”启动后台 IBKR 同步和合约元数据刷新，避免 HTTP 请求线程阻塞。

## 模块依赖关系

```
service.py
  ├─ config.py
  ├─ db.py (Database)
  ├─ discord_rest.py ──► discord_source.py (共享接口)
  ├─ parser.py ──► models.py
  ├─ scoring.py ──► models.py
  ├─ ibkr_provider.py / futu_provider.py / massive_client.py
  ├─ provider_registry.py ──► provider_adapters.py
  ├─ history_adapters.py ──► massive_client.py
  ├─ backtest_service.py ──► history_adapters.py, optimizer.py, paper.py
  ├─ ai_provider.py (DeepSeekProvider)
  ├─ feishu.py
  ├─ reports.py
  └─ local_runtime.py / nas_runtime.py
```

## 重要技术决策

| 决策 | 理由 |
|---|---|
| Discord 用**用户 token REST API**，不用 WebSocket/bot | 最高可靠性，不需要服务器加入 bot，分页游标支持增量 |
| 回测用 **Massive 期权日线 OHLC** | 账号不需要额外权限，EOD 免费，数据质量足够 |
| 飞书通知**优先 webhook** | 无需 App 凭据、无需绑定，10 秒配置即可用 |
| **确定性评分**，不用 LLM 打分 | 避免 LLM 随机性和过拟合，所有分数可审计、可回测 |
| **SQLite WAL 单文件** | 零运维，兼容本地和 NAS Docker 部署，支持并发读 |
| **Python 3.8 下限** | 兼容 NAS 主流 Python 版本 |
| IBKR 只读连接（`readOnly=true`） | 杜绝任何可能的误操作，不要求交易权限 |
| 合成 K 线兜底（离线模式） | 确保无外部服务也能闭环验证（e2e 命令） |
| Web 面板纯 stdlib | 零前端依赖，NAS 容器镜像最小化 |
