# Windows 本地版

本地版与 NAS 版共用解析、评分、行情融合、模拟盘和回测代码；当前分支优先用于真实功能验收。

## 启动

双击 `启动异常期权助手.cmd`。首次启动会自动创建独立 Python 3.12 环境并安装依赖，随后打开：

```text
http://127.0.0.1:8787/
```

管理口令显示在启动窗口，并保存在 `data-local/setup-token`。

## 数据源

- 富途 OpenD：默认 `127.0.0.1:11111`。
- IBKR：自动检测 TWS/Gateway 的 `7497/7496/4002/4001`。
- Massive：免费 EOD 与历史期权数据。
- Alpaca：免费 indicative 期权数据。
- MarketData.app：免费延迟历史兜底。
- Tradier：默认关闭，可选 Sandbox 或 Live。

打开面板的“数据源”页面即可检测、启停和调整优先级。实时执行推荐只接受新鲜实时 bid/ask。

## 测试

```powershell
.\.venv-local\Scripts\python.exe -m pytest -q
```
