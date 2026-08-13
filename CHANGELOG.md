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
