# 飞牛 NAS 一键导入

这个目录只需要使用 [`compose.yaml`](compose.yaml)。它会运行一个容器，并创建一个名为 `options-radar-data` 的持久卷。

## 方式一：一键脚本（SSH/终端，推荐）

在 NAS 上通过 SSH 或终端执行：

```bash
cd nas-quickstart
cp .env.example .env    # 可选：改时区 / 端口 / 代理
./deploy.sh             # 首次部署，自动拉镜像、起容器、打印 SETUP CODE
./deploy.sh --update    # 以后更新到最新镜像（数据卷保留）
```

脚本会打印面板地址与 `SETUP CODE`。`deploy.sh` 支持通过 `.env` 配置 `HTTP_PROXY`/`HTTPS_PROXY`（中国大陆访问 Discord / OpenD 下载时使用）。

## 方式二：图形界面导入（飞牛 Docker 应用）

## 第一步：拉取镜像

在飞牛 NAS 打开Docker应用，进入镜像页面，拉取：

```text
ghcr.io/cowboysipke/options-radar-nas:stable
```

也可以直接跳到下一步，启动Compose时会自动拉取。

## 第二步：导入 Compose

1. 打开 **Docker → Compose/项目**。
2. 选择“新建项目”或“导入Compose”。
3. 上传本目录中的 `compose.yaml`。
4. 项目名填 `options-radar`。
5. 点击部署或启动。

首版镜像为 `linux/amd64`，适合Intel/AMD笔记本改造的飞牛NAS。

## 第三步：查看 SETUP CODE

1. 打开容器列表。
2. 点击 `options-radar`。
3. 打开日志。
4. 找到：

```text
SETUP CODE: xxxxxxxxxxxxxxxxxxxxxxxx
```

首次启动还会准备Chromium和富途OpenD，请耐心等待日志继续输出。

## 第四步：打开中文面板

浏览器访问：

```text
http://NAS_IP:8787
```

例如NAS地址是 `192.168.1.20`，就打开：

```text
http://192.168.1.20:8787
```

输入刚才的 `SETUP CODE`。

## 第五步：填写一次性配置

在“设置”页面依次完成：

1. 检查Discord服务器和频道名称。
2. 填写富途ID、邮箱或手机号。
3. 填写富途登录密码。
4. 填写DeepSeek API Key。
5. 填写飞书App ID和App Secret。
6. 点击“保存并启用”。

富途密码明文不会写入配置文件，系统只保留OpenD登录协议需要的MD5凭据；富途交易密码不需要填写。

## 第六步：登录富途 OpenD

1. 点击“发送验证码”。
2. 把手机收到的验证码填入页面。
3. 若页面提示图形验证码，也一起填写。
4. 点击“提交验证码”。
5. 等待OpenD状态显示 `READY`。
6. 点击“同步富途”。

同步成功后，`/portfolio` 页面会显示富途持仓和自选。美股期权行情是否实时，以 `/system` 中实际权限和报价时间为准。

## 第七步：设置富途自选组

1. 打开富途App。
2. 新建自选组 `Options Radar`。
3. 加入你希望系统重点筛选的股票。
4. 回到NAS面板点击“同步富途”。

系统也会读取其他美股自选组。飞书的“添加自选”和“删除自选”默认维护 `Options Radar` 组。

## 第八步：Discord扫码

1. 等待设置页出现Discord二维码。
2. 用Discord手机App扫码确认。
3. 等待系统验证频道。
4. 点击首页“立即采集”。
5. 打开 `/rules` 检查“使用指南”和“分析师订阅面板”是否已读取。

## 第九步：检查结果

打开：

- `/`：今日推荐
- `/portfolio`：富途持仓与自选
- `/rules`：Discord规则
- `/system`：所有连接状态

再向飞书机器人发送：

```text
系统状态
今日推荐
```

日报每天只给0–3张达到门槛的合约，没有合格候选时保持空缺。

## 更新镜像

1. 在飞牛Docker管理器拉取 `stable` 最新镜像。
2. 重新创建或重启Compose项目。
3. 打开 `/system` 检查状态。

配置、数据库和登录资料保存在 `options-radar-data` 卷。更新时保留这个卷。

## 重要说明

该容器的交易功能处于锁定状态：只查询富途行情、持仓和自选，只生成分析、模拟仓位和回测结果，不解锁交易，不提交真实订单。

详细维护和故障处理见 [`../docs/nas.md`](../docs/nas.md)。
