# 已知缺陷

## 当前未解决

### BUG-001: IBKR 期权实时/延迟行情不可用
- **状态**：未解决（账号权限问题，非代码缺陷）
- **现象**：IBKR 对任何期权/正股请求实时行情返回 Error 10089/10091（需额外订阅），延迟行情同样不可用
- **影响**：推荐无法获取 bid/ask，评分停留在 C 级观察榜，无法达到合格线 65 分
- **根因**：用户 IBKR 账号未订阅美股行情数据包（Market Data Bundle）
- **解决方案**：IBKR 账户管理 → Market Data Subscriptions → 订阅 US Securities Snapshot Bundle（约 $10/月）
- **临时措施**：系统自动回退到 Massive EOD 数据（有 last/volume，无 bid/ask）

### BUG-002: 富途期权权限缺失
- **状态**：未解决（账号权限问题，非代码缺陷）
- **现象**：富途 `get_option_chain` 返回 `期权无获取US.XXX数据的权限，请先开通美股期权权限`
- **影响**：富途不能作为期权的行情源（正股行情可用）
- **根因**：用户富途账号未开通美股期权交易权限
- **解决方案**：富途牛牛 App → 业务办理 → 期权 → 开通美股期权权限（通常需 2 万+ 资产 + 知识测试）
- **临时措施**：富途仅用于正股实时行情增强（待实现）

### BUG-003: 富途持仓同步失败
- **状态**：未解决（不影响核心功能）
- **现象**：`sync_broker` 走富途路径时返回 `active REAL US trading account not found`
- **影响**：无法用富途读取持仓；IBKR 持仓同步正常，已覆盖
- **根因**：富途 OpenD 未配置/登录实盘 US 交易账号

### BUG-004: 面板服务实例创建的 `feishu_state.db` 出现在项目根目录
- **状态**：已缓解（加入 .gitignore），未根治
- **现象**：`live_test.py` 运行后在项目根目录生成 `feishu_state.db`
- **根因**：`live_test.py` 传入的 `data_dir` = 项目根目录，而 `local_runtime` 使用的是 `data-local/`
- **临时措施**：gitignore 已排除 `feishu_state.db*`
- **修复建议**：统一 `live_test.py` 的 `data_dir` 为 `data-local`

### BUG-005: 富途 UT8 编码输出在控制台显示乱码
- **状态**：未解决（显示问题，不影响功能）
- **现象**：`futu-api` SDK 打印的日志中中文字段在 PowerShell 控制台显示为问号
- **根因**：PowerShell 5.1 默认 GBK 编码，futu-api 输出 UTF-8
- **影响**：仅影响控制台显示，不影响数据和业务逻辑
- **修复建议**：每次脚本启动时设置 `[Console]::OutputEncoding = UTF-8`

## 已解决

| 编号 | 问题 | 解决日期 | 修复方式 |
|---|---|---|---|
| ~~BUG-100~~ | Discord DOM 采集返回 0 条消息 | 2026-08-12 | 新增 `DiscordRestSource`（REST API），Playwright 选择器修正为备用 |
| ~~BUG-101~~ | 回测无法闭环（需 100+ 样本） | 2026-08-12 | 新增 `replay()` 支持任意日期回放 + 合成 K 线兜底 |
| ~~BUG-102~~ | 飞书配置断链（本地路径错误） | 2026-08-12 | 强制 secret_refs 为 data-local/secrets/* + 新增 webhook 模式 |
| ~~BUG-103~~ | IBKR 期权链返回 0（US. 前缀 + SMART 链单一） | 2026-08-12 | `_plain_symbol` 剥离前缀 + 选数据最全链 |
| ~~BUG-104~~ | openCode bash 工具完全故障 | 2026-08-12 | 禁用 `opencode-devcontainers` 插件 + 清除状态DB |
