# 已知缺陷

## 历史缺陷（均已解决，保留根因记录；最新任务规划见 TODO.md）

### BUG-013: pa/fpd「执行观点: 交易」被误判为 WATCH
- **状态**：已解决（2026-08-16）
- **现象**：pa/fpd 频道的「执行观点: 交易」消息（实际文本用冒号分隔）全部解析为 WATCH，209 条真实 TRADE 信号丢失（pa 138 + fpd 71）
- **根因**：`parser.py _decision` 正则 `执行观点\s*交易` 未覆盖冒号分隔；mr（`decision: trade` 英文）与 qmr（`结论：交易`，正则含冒号）解析正确，仅 pa/fpd 分支缺 `[:：]?`
- **修复**：正则改为 `执行观点\s*[:：]?\s*交易`；新增 3 个真实消息回归测试；`reparse_analyst_messages()` 重解析 651 条历史消息（TRADE 162→371）
- **连带影响**：共识评分中 pa/fpd 家族投票恢复，多家族共识事件从 47 增至 85

### BUG-014: 逐笔结算明细「退出」列显示 filled/no-fill
- **状态**：已解决（2026-08-16）
- **现象**：/backtest 逐笔结算明细「退出」列与「状态」列重复显示 filled/no-fill
- **根因**：`signal_outcomes` 表无 `exit_reason` 字段，`replay_summary` 用 `outcome.status` 冒充退出原因
- **修复**：表加 `exit_reason` 列（含迁移），`replay()` 保存真实退出原因，页面显示中文（止损/止盈/持有到期/无K线/超限价/未成交）

### BUG-001: IBKR 期权实时/延迟行情不可用
- **状态**：降级为兼容项（Alpaca 已成为主行情，IBKR 只读持仓保留）
- **现象**：IBKR 对期权/正股请求行情返回 Error 10089/10091（需额外订阅）
- **排查发现**：探测时美股为 PRE_MARKET_BEGIN（北京时间 18:56），无活跃报价
- **已实施修复**：`_market_data_type` 默认从 1（Live）改为 3（Delayed），由配置 `ibkr.market_data_type` 控制；`_ensure()` 里主动调 `reqMarketDataType`
- **验证方法**：美股开盘后（北京时间 21:30+）运行 `live_test.py`，观察推荐中 `market_status` 是否从 eod 变为 realtime/delayed
- **本轮诊断**：纽约时间 15:50 时，额外诊断连接在账户初始化阶段超时，未能把 10089/10091 与实时盘口请求重新关联；面板现已记录对应 IBKR 市场错误码。

### BUG-011: Alpaca Paper 期权历史接口受 OPRA agreement 限制
- **状态**：已解决（2026-08-16）
- **根因**：历史期权 bars（`/v1beta1/options/bars`）**无需 OPRA**；之前的 403 是 `end` 参数设在未来日期（`end>=今天`）触发的边界检查，误判为「OPRA 未签」
- **修复**：`AlpacaProvider.aggregate_bars` 将 `end` clamp 到昨天、`limit` 上限 10000；历史期权数据成为回测主源（~200 req/min，覆盖率 97.5%，提速 40 倍）
- **保留限制**：实时期权快照（snapshot）仍需 OPRA；`end>=今天` 的请求仍会 403

### BUG-012: Alpaca 批量股票快照遇非法符号返回 400
- **状态**：已解决
- **根因**：`/v2/stocks/snapshots` 对含 `..`/前缀 `.` 的符号（如富途自选里的 `US..VIX`）整批请求返回 400
- **修复**：按 50 个一批分块请求，并过滤非法符号；当前 142 个自选已能写入 89 个价格

### BUG-009: 推荐价格展示过多小数位
- **状态**：已解决
- **修复**：页面和飞书展示层统一保留 2 位小数，底层数值不转换为字符串。

### BUG-010: realtime 字段存在但 bid/ask 缺失仍被标记 realtime
- **状态**：已解决
- **修复**：ProviderRegistry 改为 `quote_missing`，只有完整 bid/ask pair 才能进入可执行状态。

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
