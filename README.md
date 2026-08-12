# Options Radar：飞牛 NAS 异常期权助手

Options Radar 在飞牛 NAS 的一个 Docker 容器中持续运行：采集 Discord 异常期权消息，合并多位分析师意见，通过富途 OpenD 检查实时美股期权行情、持仓和自选，最后在中文面板与飞书中给出每日 0–3 张候选合约。

首版只支持 `linux/amd64`，适合由常见 Intel/AMD 笔记本改造的飞牛 NAS。

## 先记住三件事

1. **只运行一个容器。** 飞牛 Docker 管理器导入一份 Compose 文件即可。
2. **富途是首版唯一行情与组合数据源。** OpenD、Chromium、OCR、SQLite、DeepSeek 适配器和中文面板都在同一个镜像中。
3. **交易功能锁定。** 系统只读取行情、持仓和自选，只做推荐、模拟仓位和回测；不会解锁交易，也不会提交真实订单。

## 先跑通闭环（离线自检，推荐第一步）

不连任何外部服务，先用固定样例把整条流水线跑一遍验证环境：

```powershell
.venv\Scripts\python.exe -m options_radar --config config.yaml e2e --date 2026-08-05
```

输出中 `"ok": true` 且 `raw_messages / parsed_signals / flow_events / recommendations / signal_outcomes / backtest_bars` 全部大于 0，即表示「采集→解析→共识→行情→回测」闭环可用。行情与回测 K 线不可达时自动使用确定性合成数据，保证离线也能闭环。

回测也可以直接对任意历史区间结算：

```powershell
.venv\Scripts\python.exe -m options_radar --config config.yaml backtest --start 2026-08-05 --end 2026-08-14
```

## 采集 Discord 的两种方式

系统按优先级自动选择后端：

1. **官方 REST API（首选）**：设置环境变量 `DISCORD_USER_TOKEN`（个人账号 token），或把 token 写入 `secret_refs.discord_user_token` 指向的文件。采集层直接用 Discord HTTP API 按消息 ID 分页读取频道历史，稳定且不依赖浏览器 DOM，彻底解决“网页结构变了就采不到”的问题。注意：使用个人账号自动化的做法不符合 Discord 服务条款，需自行承担风险。
2. **浏览器回退**：没有 token 时使用 Playwright 持久化浏览器扫码登录。采集器已修正消息列表选择器并等待页面水合后再提取 DOM。

## 最快安装

1. 下载 [`nas-quickstart/compose.yaml`](nas-quickstart/compose.yaml)。
2. 在飞牛 NAS 打开 **Docker → Compose/项目 → 新建项目 → 导入 YAML**。
3. 导入后启动项目，等待镜像和 OpenD 首次准备完成。
4. 打开 `options-radar` 容器日志，找到：

   ```text
   SETUP CODE: xxxxxxxxxxxxxxxx
   ```

5. 浏览器访问 `http://NAS_IP:8787`，输入该口令。
6. 在“设置”中填写富途登录信息、DeepSeek API Key、飞书 App ID/Secret 和 Discord 频道名称。
7. 按页面提示完成富途验证码及 Discord 扫码。
8. 打开首页，点击“同步富途”和“立即采集”，确认状态正常。

小白版逐步说明见 [`nas-quickstart/README.md`](nas-quickstart/README.md)，故障处理与更新方法见 [`docs/nas.md`](docs/nas.md)。

## 日常使用

平时只需要看飞书日报或访问中文面板：

- `/`：今日推荐和快捷操作
- `/contracts`：候选合约与评分
- `/rules`：Discord 使用指南和分析师订阅规则
- `/portfolio`：富途持仓、自选和集中度
- `/analysts`：分析师权重与表现
- `/backtest`：模拟盘和回测
- `/system`：OpenD、Discord、DeepSeek、飞书及数据库状态
- `/setup`：首次配置和登录维护

飞书支持：

```text
今日推荐
查看持仓
添加自选 TSLA
删除自选 TSLA
为什么推荐第1名
重新同步富途
系统状态
```

每天最多展示 3 张评分达到 65 分的合约。候选不足时保持空缺，不凑数。

## 自选组怎么用

- 系统同步富途中的全部美股自选组。
- 建议在富途 App 中创建一个名为 `Options Radar` 的自选组，专门放入希望重点筛选的股票。
- 飞书的“添加自选/删除自选”默认维护 `Options Radar` 组。
- 在富途 App 修改自选后，点击面板“同步富途”，或等待下一次自动同步。

推荐只从 Discord 强信号与富途持仓/自选的交集中选择。

## 数据与AI分工

```text
Discord REST 分页读取（无 token 时回退 Playwright DOM/OCR）
  → 本地规则解析
  → 缺失自由文本由 DeepSeek 结构化补充
  → Schema 校验
  → 确定性融合评分、过滤、仓位与进出场计算
  → 富途 OpenD / IBKR / Massive 行情、期权链、持仓和自选验证
  → 中文面板、飞书提醒与日报
  → 模拟盘、1/3/5 日回放与策略优化
```

DeepSeek只负责文本识别、摘要和解释。综合评分、推荐数量、最大风险、止盈止损、回测收益与分析师权重由确定性代码计算。

## 飞书通知：两种接入方式

1. **群机器人 Webhook（最简单）**：创建飞书群 → 添加自定义机器人 → 把 webhook 地址写入 `FEISHU_WEBHOOK_URL`（或 `secret_refs.feishu_webhook`）。无需 App 凭据、无需绑定，日报和提醒直接发到该群。
2. **自建应用（可双向对话）**：在飞书开放平台创建应用并开启事件订阅（长连接模式），填 `FEISHU_APP_ID` 与 `FEISHU_APP_SECRET`。用户私聊机器人发送“绑定”后，日报/提醒发送到该对话；支持“今日推荐 / 查看持仓 / 添加自选 TSLA / 为什么推荐第N名 / 系统状态”等命令。

面板 `/system` 里点“飞书测试”可验证凭据并投递测试卡片。

## 更新

首版使用手动更新，过程清晰可控：

1. 在飞牛 Docker 管理器中拉取 `ghcr.io/cowboysipke/options-radar-nas:stable` 最新镜像。
2. 重新创建或重启 Compose 项目。
3. 打开 `/system` 检查版本和各服务状态。

`/data` 使用 Docker 命名卷 `options-radar-data` 持久化，更新镜像不会删除配置、数据库、浏览器登录、截图证据和备份。

## 本地验证

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q options_radar tests
```

端到端自检与回测回放命令见上文「先跑通闭环」。

所有 Markdown 日报使用 UTF-8 BOM 和 Windows 兼容换行；网页和 JSON 明确声明 UTF-8。
