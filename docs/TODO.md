# 任务清单

> 2026-08-21 重规划。以 `docs/产品开发文档-v2.md`（第 15 节优先级）为准；旧版 TODO/BUGS 与实际状态脱节，已废弃。
> 铁律不变：GEX 不进评分、AI 不决策、富途防限流、数据库不 DELETE、每步 pytest + walkthrough。

## 阶段 1：GEX 前瞻验证（文档 P2-12，提前做）✅ 2026-08-21

GEX 无法历史回测（无历史 OI），唯一验证路径是「从上线起每日记录 GEX 快照」。**数据从今天开始才有价值，越晚启动验证窗口越短。**

- [x] `gex_snapshots` 表 + 迁移：snapshot_date / symbol / strike / call_gex / put_gex / net_gex / spot / regime / call_wall / put_wall / flip
- [x] 每日收盘后自动快照（美东 16:45）：当日 flow 触达标的（去重）→ `get_gex` → 落库；空结果不落库、失败重试 1 次、串行防 OpenD 限流；/system 页可手动「记录GEX快照」
- [x] 最小验证查询（/system 页「GEX 前瞻验证」卡片：天数/标的数/最近快照）；day-0 已录 4 标的 64 行

## 阶段 2：展示层补全（文档 P1 遗留）✅ 2026-08-21

- [x] Term Structure 展示（gex.py 从同批期权链算 per-expiry ATM IV，ⓘ tooltip「期限」）
- [x] Short Gamma Risk 结构化输出（HIGH→Naked Short BLOCK×2 / Credit Spread CAUTION / Defined Risk PREFERRED，tooltip「Short Gamma 风控」）
- [x] 策略矩阵补全（Mixed/方向不明→不交易；IV 极低+趋势向上→Buy Stock/Bull Call Spread，事件 HIGH 加防财报）
- [x] Smart Watchlist 补列（IV-HV 列，hover 显示 IV/HV 原值；排序下拉：IV Rank / IV-HV / 涨跌）
- [x] Expiration 推荐（财报周避让 → 本周/下周/2W~1M/1M+，策略行展示）

## 阶段 3：市场结构 + 历史验证（文档 7 / 13.3）✅ 2026-08-21

- [x] 20D/50D 均线：Dealer Chart K 线图叠加 MA20（蓝）/MA50（紫）折线 + 图例（70 日数据算均线、显示近 30 日）
- [x] Support/Resistance：20 日高/低虚线（棕）叠加；卡片 ⓘ tooltip「价格结构」（现价/MA20/MA50/20日高/低，评估时落库）
- [x] Test D HV 分层回测：用已存储正股日线序列在信号日算历史 HV（零额外请求），卖方盈亏按 HV <30%/30-60%/>60% 分层
- [x] 评分区分度：推荐落库分数（≥65 / 50-64 / <50）与卖方盈亏分层对照；/backtest 页「历史验证 Test D」折叠区（10 分钟缓存）
- 初步结论（90 天数据）：低/中 HV 卖方 +0.79%/+1.78%，高 HV >60% 为 -0.32%（高波动环境卖权吃亏，与「IV 贵才卖」互补：HV 是已实现波动、IV 是预期，需区分）；评分分层 B+ 样本仅 11 笔、均值 -4.14%（样本不足，继续积累）

## 阶段 4：P2 大项 ✅ 2026-08-21

- [x] GEX Heatmap（文档 10.3）：Strike × 到期 × Net GEX 矩阵（绿正红负、颜色深度=|GEX|），Dealer Chart 弹窗折叠区；gex.py 同批数据算 per-(expiry,strike) 贡献，零额外请求
- [x] Alert 扩展（文档 12.2）：飞书「GEX 预警」——现价逼近 Wall/Flip（±1%）、Regime 翻转（对比 gex_snapshots 前次快照）、IV Rank ≥80；开市时段每 10 分钟检查，每交易日去重；/system 手动「检查GEX预警」
- [x] Flow Type 分类（文档 4，P2）：flow_classifications 表；确定性关键词先分（Spread/Hedging/Closing/Directional），Unknown 再交 DeepSeek 语义归类（AI 只判语义）；Strength = 权利金历史百分位（程序计算，AI 不做数值）；开市每 30 分钟自动跑（每次 20 条），/signals 手动「分类Flow类型」+ 表格新增 类型/强度 列（首跑 30 条：13 条走 AI）

## 明确不开发（文档 16）

0DTE 模块 / GEX 历史回测 / AI 方向预测与策略选择 / 全市场批量 GEX / 自动下单 / 机器学习模型。

## 追加：UI 打磨 + 收尾 ✅ 2026-08-21

- [x] Discord 采集间隔/时段可配置（/setup：1-60 分钟 + 美东起止小时）
- [x] K 线图层化（9 个开关胶囊）+ 看盘软件式右侧价格轴/现价胶囊 + 悬停注释（图层线/均线/Flow 点/K 线 OHLC 十字光标 + GEX 曲线悬停）
- [x] 全局双栏布局（主内容 + 292px 粘性侧栏）、工具栏控件统一等高、`.grid` auto-fit 铺满、系统页 `.tiles` 字体统一、设置页两列表单
- [x] 侧栏与主栏顶边对齐（`:empty{display:none}` 去空占位）；文档/脚本归类（docs/、scripts/）

## 每阶段收尾

`pytest -q` 全绿 → `walkthrough.py` 走查 → 面板肉眼验收（面板用 WMI 脱离方式启动）。
