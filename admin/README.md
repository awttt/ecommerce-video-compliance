# 御镜管理后台

本机多商户管理。公司、成员、额度、命中词和用量快照放在这里。平台管理员可直接创建公司管理员，公司管理员可直接创建员工；账号会在 token4u 中开通并绑定现有审核专属分组，后台自动创建审核 Key。中转站只负责算力和扣费。画面识别和口播识别仍在御镜桌面客户端完成，这个后台不抽帧、不调用模型、不生成剪辑。

只监听 `127.0.0.1`。不要改成对外网卡。

## 启动

依赖装在项目根的 `.venv`。前端先构建，再由同一个进程托管页面和接口。

```bash
cd admin/web && npm install && npm run build
cd .. && ../.venv/bin/python run.py
```

浏览器打开 http://127.0.0.1:8780 。端口用 `EVC_ADMIN_PORT` 改，默认 8780。改界面时也可以在 `admin/web` 执行 `npm run dev`，Vite 在 5174，把 `/v1` 代理到 8780。

后端测试：

```bash
cd admin && ../.venv/bin/python -m pytest -q
```

## 登录

登录走中转站原来的 `POST /auth/login`（默认 `https://token4u.cc/api/v1`）。密码只转发出去，不写入本地库。中转站返回的 access token 不发给浏览器，后台另外签发自己的登录令牌。

`350944364@qq.com` 是总管理员。这个邮箱第一次登录成功后，会写成平台管理员，不归属任何公司。其他邮箱必须已经是这里的成员，否则即使中转站密码正确，也不能进后台。账号开启两步验证时，再填写身份验证器里的 6 位数字。

## 演示数据

第一次启动、库里还没有成员时写入下面这些成员，方便看租户、额度和审核。已经有成员就不会再覆盖。他们的本地密码不再用于登录。自动化测试不访问中转站，会假装中转站接受密码 `Yujing123`。

| 邮箱 | 角色 |
| --- | --- |
| admin@yujing.local | 平台管理员 |
| li@jiecheng.com | 锦程商贸公司管理员 |
| zhang@jiecheng.com | 锦程商贸普通用户 |

另有优品优选的公司管理员 `zhou@youpin.com`，以及已停用、余额为负的 `zhao@jiecheng.com`。普通用户登录后只能看总览、流程图、自己的视频和自己的命中词提交。

## 数据

默认 SQLite：`admin/data/admin.db`。生产可以设 `EVC_ADMIN_DATABASE_URL`（例如 Postgres）。当前建表用 `create_all`，换库时表要已存在或由这次启动创建，没有单独的迁移脚本。

上传的视频在 `admin/data/videos`，和桌面客户端的 `data/videos` 分开。演示里的三条视频记录没有文件。

管理密钥只放环境变量 `EVC_RELAY_ADMIN_KEY`，不进数据库，设置页也看不到。使用 token4u 受限密钥时，至少授予 `users.create`、`users.balance.read`、`users.balance.write`、`users.status.write` 和 `usage.read`。留空时开通成员和划拨额度只写本地账本，不请求中转站。

其他变量：

- `EVC_ADMIN_JWT_SECRET`：签发登录令牌。正式环境要换成足够长的随机串。
- `EVC_RELAY_BASE_URL`：中转站管理接口，默认 `https://token4u.cc/api/v1`。
- `EVC_RELAY_GATEWAY_URL`：推理网关，默认 `https://token4u.cc/v1`。
- `EVC_RELAY_GROUP_NAME`：新建中转站用户只进这个分组，默认「电商审核专属-bot创建」。
- `EVC_RELAY_GROUP_ID`：审核专属分组的数字编号，远端建号和自动创建 Key 都绑定这个分组。
- `EVC_RELAY_TIMEZONE`：用量日期，默认 `Asia/Shanghai`。

## 和桌面客户端的边界

- 额度对账：成员余额（负数按 0，并标透支）加累计消耗，等于累计净分配。月消耗单独展示。退回用减法，已分配减少，公司池增加。
- 命中词和桌面端同一套：类别是价格、规格、品牌、夸大宣传；来源是口播或画面。夸大宣传不能选画面。补充说明里不能写允许推测。
- 视频结果固定 8 列：时间点、类别、来源、原文、位置、置信度、判定理由、备注。
- 累计成本按日快照 upsert 维护；从中转站定时拉取还没有挂到进程上。
- `usage_logs.cost` 已含分组倍率，后台不再乘一次。
