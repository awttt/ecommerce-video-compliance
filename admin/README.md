# 御镜管理后台

多商户管理。公司、成员、额度、命中词和用量快照放在这里。平台管理员可直接创建公司管理员，公司管理员可直接创建员工；账号会在 token4u 中开通并绑定现有审核专属分组，后台自动创建审核 Key。中转站只负责算力和扣费。

视频审核不在后台完成，只在桌面客户端里跑。后台只管理租户、成员、额度、规则、用量和审计，不启动识别引擎，也不保存视频和命中结果。这样后台可以单独部署到服务器，不必带上抽帧、画面模型这一类本地依赖。

## 启动

依赖装在 `admin/requirements.txt`。前端先构建，再由同一个进程托管页面和接口。构建需要 Node 18 以上（`admin/web/.nvmrc` 写的是 20）。

```bash
cd admin
python -m pip install -r requirements.txt
cd web && npm install && npm run build
cd .. && python run.py
```

浏览器打开 http://127.0.0.1:8780 。端口用 `EVC_ADMIN_PORT` 改，默认 8780；对外部署时用 `EVC_ADMIN_HOST` 指定监听地址。改界面时也可以在 `admin/web` 执行 `npm run dev`，Vite 在 5174，把 `/v1` 代理到 8780。

后端测试：

```bash
cd admin && python -m pytest -q
```

## 登录

登录走中转站原来的 `POST /auth/login`（默认 `https://token4u.cc/api/v1`）。密码只转发出去，不写入本地库。中转站返回的 access token 不发给浏览器，后台另外签发自己的登录令牌。

`350944364@qq.com` 是总管理员。这个邮箱第一次登录成功后，会写成平台管理员，不归属任何公司。其他邮箱必须已经是这里的成员，否则即使中转站密码正确，也不能进后台。账号开启两步验证时，再填写身份验证器里的 6 位数字。

## 演示数据

第一次启动、库里还没有成员时写入下面这些成员，方便看租户和额度。已经有成员就不会再覆盖。他们的本地密码不再用于登录。自动化测试不访问中转站，会假装中转站接受密码 `Yujing123`。

| 邮箱 | 角色 |
| --- | --- |
| admin@yujing.local | 平台管理员 |
| li@jiecheng.com | 锦程商贸公司管理员 |
| zhang@jiecheng.com | 锦程商贸普通用户 |

另有优品优选的公司管理员 `zhou@youpin.com`，以及已停用、余额为负的 `zhao@jiecheng.com`。普通用户登录后只能看总览、流程图和自己的命中词提交。

## 数据

数据库分两套：

- 本机库 `EVC_ADMIN_DATABASE_URL`，默认 SQLite `admin/data/admin.db`。放词库和公司规则。
- 云端库 `EVC_ADMIN_CLOUD_DATABASE_URL`，例如 `postgresql+psycopg://...`。放租户、成员、邀请、设备、登录令牌、额度、用量快照和审计日志。

不填云端地址时两边共用一个库，行为和旧版本一致；填了之后同一次 `db.commit()` 会分别提交到两个库，跨库只存编号，不建外键。迁移旧单库：

```bash
cd admin
EVC_ADMIN_CLOUD_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/evc_admin' \
  python scripts/migrate_cloud.py --source sqlite:///data/admin.db --dry-run
```

确认行数后去掉 `--dry-run`。脚本遇到目标库已有数据会拒绝，避免覆盖。词库留在本机库，不搬。

当前建表用 `create_all`，没有单独的迁移框架；新增列由 `db.init_db` 里的补列逻辑处理。

管理密钥只放环境变量 `EVC_RELAY_ADMIN_KEY`，不进数据库，设置页也看不到。使用 token4u 受限密钥时，至少授予 `users.read`、`users.create`、`users.balance.read`、`users.balance.write`、`users.status.write` 和 `usage.read`。留空时开通成员和划拨额度只写本地账本，不请求中转站，页面也不显示同步按钮。

其他变量：

- `EVC_ADMIN_HOST`：监听地址，默认 `127.0.0.1`。
- `EVC_ADMIN_PORT`：端口，默认 `8780`。
- `EVC_ADMIN_JWT_SECRET`：签发登录令牌。正式环境要换成足够长的随机串。
- `EVC_RELAY_BASE_URL`：中转站管理接口，默认 `https://token4u.cc/api/v1`。
- `EVC_RELAY_GATEWAY_URL`：推理网关，默认 `https://token4u.cc/v1`。
- `EVC_RELAY_GROUP_NAME`：新建中转站用户只进这个分组，默认「电商审核专属-bot创建」。
- `EVC_RELAY_GROUP_ID`：审核专属分组的数字编号，远端建号和自动创建 Key 都绑定这个分组。
- `EVC_RELAY_TIMEZONE`：用量日期，默认 `Asia/Shanghai`。

## 和桌面客户端的边界

- 额度对账：成员余额（负数按 0，并标透支）加累计消耗，等于累计净分配。月消耗单独展示。退回用减法，已分配减少，公司池增加。
- 和中转站对账：成员管理页点「从中转站刷新」会依次读取每个成员的 `GET /admin/users/:id`、`GET /admin/usage/stats`（本月和累计），把中转站余额、账号状态和用量写成快照。只读，不改本地账本、不加款。中转站余额和本地余额不一致时页面标红，租户和额度页显示差额。
- 命中词和桌面端同一套：类别是价格、规格、品牌、夸大宣传；来源是口播或画面。夸大宣传不能选画面。补充说明里不能写允许推测。客户端用 `GET /v1/rules?since=<version>` 增量拉取，和内置词合并。
- 视频审核、命中结果、时间轴、剪辑和导出都在桌面客户端完成，后台不参与，也不保存视频文件。
- 累计成本按日快照 upsert 维护；从中转站拉取是手动触发（成员管理、额度分配页的刷新按钮），没有挂定时任务。
- `usage_logs.cost` 已含分组倍率，后台不再乘一次。
