# 2026-08-05 Demo 保底快照

这是 8 月 5 日 Demo 的可重放保底工作树。它保留同一套解析、融合评分、日报和模拟盘代码，使用固定脱敏样本运行，不依赖 Discord、IBKR、富途 OpenD、Massive 或 DeepSeek。

## 运行

```powershell
python -m unittest discover -s tests -v
```

Demo验收以 `expected_results.json` 为准：合约键、方向、分数排序和“候选不足时不凑数”的行为必须稳定。

真实采集的截图和SQLite不纳入Git。它们仍位于主项目本机的 `evidence/` 与 `data/options_radar.db`，需要时从原目录复制到临时回放目录。

## 说明

此工作树仅作为回滚和样本回放基线；新的功能开发在 `rewrite/local-v2` 进行。
