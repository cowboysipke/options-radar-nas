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
- 当前：**132 passed**（+21 测试：discord_rest 8、backtest_replay 4、feishu webhook 4、e2e 1、channel resolution 1、已有的其他测试修复）
