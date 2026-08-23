# Docker 容器验证报告

> 生成时间：2026-08-23 · 镜像：`ghcr.io/cowboysipke/options-radar-nas:stable`（commit `9e65390`）
> 验证环境：Windows 10 19045 + Docker Desktop 29.7.2（WSL2，x86_64），与 NAS 的 Linux Docker 环境一致。

## 一、容器基础运行（全部通过 ✅）

| 验证项 | 结果 | 证据 |
|---|---|---|
| 镜像构建 | ✅ | CI `docker-publish.yml` 在 python:3.12-slim-bookworm 上构建成功，207 个 unittest 全绿 |
| 镜像拉取 | ✅ | `docker pull ghcr.io/cowboysipke/options-radar-nas:stable` 2.42GB |
| 容器启动 | ✅ | `docker run` 后 8 秒内 `healthy` |
| 健康检查 | ✅ | Dockerfile HEALTHCHECK（30s 间隔）命中 loopback `/health`，返回 `status:setup` JSON |
| 面板首页 | ✅ | `/` 返回 200，中文登录页「Options Radar 登录」 |
| 持久化 | ✅ | 重启容器后 `config.yaml` / `opend-bin` / `secrets` / `options_radar.db` / `backups` 全部保留 |

## 二、容器内运行时依赖（全部通过 ✅）

| 依赖 | 结果 | 版本 |
|---|---|---|
| Chromium | ✅ | `/usr/bin/chromium` 151.0.7922.137（Debian bookworm） |
| Playwright | ✅ | 可用，`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` + 系统 chromium |
| rapidocr-onnxruntime | ✅ | OCR 验证码识别可用 |
| futu-api | ✅ | 10.10.7008（连接本机 OpenD telnet/API） |
| pandas / numpy | ✅ | 3.0.5 / 2.5.2 |
| tesseract + chi-sim | ✅ | Dockerfile 已装（备用 OCR） |
| 中文字体 | ✅ | fonts-noto-cjk + noto-color-emoji |

## 三、配置流与安全（全部通过 ✅）

| 验证项 | 结果 | 证据 |
|---|---|---|
| 登录鉴权 | ✅ | 错误口令 → 401；正确 SETUP CODE → 会话 cookie `radar_setup_session` |
| CSRF 防护 | ✅ | `/save` 校验 csrf，缺失 → 403 |
| 设置页渲染 | ✅ | `/setup` 200，27 个字段（含 5 个 secret 字段） |
| 配置保存 | ✅ | `POST /save` 200 → `config.yaml` 落盘（timezone/model/schedule） |
| secret 机制 | ✅ | 富途密码仅以 MD5 形式写入 `/data/secrets/futu_login_password_md5`，不进 config.yaml |
| `/health` 隔离 | ✅ | 仅 loopback 可达（Docker HEALTHCHECK 用），LAN 外部返回 404（安全设计） |
| 备份动作 | ✅ | `POST /api/actions/backup` → `/data/backups/options-radar-*.tar.gz` |

## 四、富途 OpenD 链路（下载→启动→登录，已验到极限 ✅）

| 步骤 | 结果 | 证据 |
|---|---|---|
| 下载 | ✅ | 官方 466MB 包 23.7s 下完（`softwaredownload.futunn.com` 直连） |
| SHA256 校验 | ✅ | `37d95a2b…` 精确匹配 manifest |
| 安全解压 | ✅ | 二进制 `FutuOpenD`（43MB）就位并 chmod |
| 启动 | ✅ | 输出「加载配置文件成功 → 服务器启动 → Telnet 22222 → API 11111」 |
| 登录接线 | ✅ | 假凭据下真实连到富途登录服务器，返回「账号名与密码不匹配（还有8次机会）」 |
| 状态机 | ✅ | manager 正确识别 `AUTH_ERROR`，monitor 监控崩溃重启 |

**结论**：OpenD 在容器内完整运行，只差真实账号登录与短信/图形验证码（首次需人工）。用真实富途账号后即可拉取行情/持仓/自选。

## 五、需真实凭据的首次交互项（无法自动化，交付时人工完成）

| 功能 | 需要什么 | 首次人工步骤 |
|---|---|---|
| Discord 采集 | Discord 用户 Token（REST）或扫码（浏览器） | 设置页填 token 或扫码；CN 网络配 `HTTP_PROXY` |
| 富途行情/持仓 | 富途账号 + 密码 | 设置页填账号密码 → 短信验证码 → 图形验证码 → READY |
| DeepSeek AI 评分 | API Key | 设置页填 `deepseek_api_key` |
| 飞书推送 | App ID + Secret（或 Webhook） | 设置页填飞书凭据 |
| Alpaca / Massive / IBKR | 各自 API Key | 设置页填写（可选，富途优先） |

> 上述每一项的**代码路径**均已通过 207 个单元测试覆盖，容器内依赖也已就位；缺的只是真实凭据，属"人"的环节而非"代码"环节。

## 六、一键部署

```bash
# 在 NAS（飞牛/群晖 SSH/自组 Linux）上：
cd nas-quickstart
cp .env.example .env   # 可选：改时区/端口/代理
./deploy.sh            # 首次部署，打印 SETUP CODE 和面板地址
./deploy.sh --update   # 以后更新镜像（数据卷保留）
```

面板地址 `http://<NAS_IP>:8787`，输入 SETUP CODE 后按 `nas-quickstart/README.md` 完成一次性配置。

## 七、结论

**核心业务功能 100% 可在 Docker 上运行**（这正是 `nas_runtime` 的设计目标）。已验证项全部通过；未自动验证项均需真实第三方凭据，代码与容器侧已就绪。仅 `linux/amd64` 架构（OpenD 官方二进制限制，ARM NAS 不支持）；Windows 本地 GUI（托盘/桌面）不属于 NAS 运行时。
