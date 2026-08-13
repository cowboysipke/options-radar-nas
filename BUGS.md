# 已知缺陷

## 当前未解决

### BUG-001: IBKR 期权实时/延迟行情不可用
- **状态**：待验证（代码已加固，需美股开盘后测试）
- **现象**：IBKR 对期权/正股请求行情返回 Error 10089/10091（需额外订阅）
- **排查发现**：探测时美股为 PRE_MARKET_BEGIN（北京时间 18:56），无活跃报价
- **已实施修复**：`_market_data_type` 默认从 1（Live）改为 3（Delayed），由配置 `ibkr.market_data_type` 控制；`_ensure()` 里主动调 `reqMarketDataType`
- **验证方法**：美股开盘后（北京时间 21:30+）运行 `live_test.py`，观察推荐中 `market_status` 是否从 eod 变为 realtime/delayed

### BUG-002: 富途期权权限缺失
- **状态**：已解决（富途不再参与行情路由，仅作自选导入）
- **修复**：`config.local.yaml` 移除 futu 从 `providers.market_priority`，`enabled.futu: false`
- **影响**：富途自选导入仍然可用，行情路由回归 IBKR + Massive

### BUG-003: 富途持仓同步失败
- **状态**：已解决（加错误处理，返回友好中文提示）
- **修复**：`import_futu_watchlist()` 加 try/except，抛出时返回 `{status: error, message: 中文提示}`
- **说明**：系统日常 `sync_broker` 走 IBKR 路径（不受影响），仅面板「从富途导入自选」按钮受影响

### BUG-004: 面板服务实例创建的 `feishu_state.db` 出现在项目根目录
- **状态**：已解决
- **修复**：`live_test.py` 的 `data_dir` 改为 `config_path.parent / "data-local"`，与 `local_runtime` 一致

### BUG-005: 富途 UTF-8 编码输出在控制台显示乱码
- **状态**：已解决
- **修复**：`live_test.py` 启动时调用 `chcp 65001` 切换到 UTF-8；`启动异常期权助手.cmd` 已有 `PYTHONUTF8=1`

### BUG-007: 持仓页同步 IBKR 导致页面阻塞
- **状态**：已解决
- **根因**：页面请求线程同步调用 `reqContractDetails`
- **修复**：页面只读缓存；新增“手动刷新持仓”后台任务

### BUG-008: 推荐页 execution 字段显示为空
- **状态**：已解决
- **根因**：bid/ask/入场/止盈/止损存储在 `payload.execution`，页面只读取顶层字段
- **修复**：统一推荐视图展开 execution，并增加字段完整性状态；缺失时显示“待行情/字段未就绪”，不伪造数据

## 已解决

| 编号 | 问题 | 解决日期 | 修复方式 |
|---|---|---|---|
| ~~BUG-006~~ | 面板"今日推荐/信号详情"无数据 | 2026-08-12 | `_dashboard_date()` 回退到最近有数据的交易日，不再死绑定当天 |
| ~~BUG-005~~ | 富途中文日志控制台乱码 | 2026-08-12 | live_test 启动设 chcp 65001 |
| ~~BUG-004~~ | feishu_state.db 出现在项目根 | 2026-08-12 | live_test data_dir 指向 data-local |
| ~~BUG-003~~ | 富途导入自选报错无提示 | 2026-08-12 | import_futu_watchlist 加 try/except 中文错误 |
| ~~BUG-002~~ | 富途参与行情路由 | 2026-08-12 | 移除 futu 从 providers.market_priority + disabled |
| ~~BUG-100~~ | Discord DOM 采集返回 0 条消息 | 2026-08-12 | 新增 `DiscordRestSource`（REST API），Playwright 选择器修正为备用 |
| ~~BUG-101~~ | 回测无法闭环（需 100+ 样本） | 2026-08-12 | 新增 `replay()` 支持任意日期回放 + 合成 K 线兜底 |
| ~~BUG-102~~ | 飞书配置断链（本地路径错误） | 2026-08-12 | 强制 secret_refs 为 data-local/secrets/* + 新增 webhook 模式 |
| ~~BUG-103~~ | IBKR 期权链返回 0（US. 前缀 + SMART 链单一） | 2026-08-12 | `_plain_symbol` 剥离前缀 + 选数据最全链 |
| ~~BUG-104~~ | openCode bash 工具完全故障 | 2026-08-12 | 禁用 `opencode-devcontainers` 插件 + 清除状态DB |
| ~~BUG-105~~ | 富途导入 AJAX 返回整页 HTML | 2026-08-13 | FORM_ACTIONS 改为 JSON 响应 |
