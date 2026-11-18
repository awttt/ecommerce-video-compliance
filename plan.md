# evc-admin 开发计划

> 目标：在不改动 token4u 中转站代码的前提下，为第一家入驻公司提供多租户管理（成员、额度、关键词审批、用量对账）。
>
> 配套交互原型：`evc-admin-prototype.html`（单文件，浏览器直接打开）；
> 架构图：`docs/evc-admin-architecture.html`。

---

## 1. 背景与边界

### 1.1 为什么独立建服务

- 中转站是 sub2api 的 fork（Go + Ent + Gin + Postgres + Redis），仓库内有 `sync-upstream-release.sh` 定期合并上游。在 fork 里加 `tenant` 会持续产生上游冲突，且改动落在扣费主链路上，风险高。
- 中转站目前只有 `users` 和 `admin / user` 两种角色（`backend/internal/domain/constants.go`），没有公司、组织、成员关系。
- 结论：**中转站继续只做算力、账号、计费；公司这个维度只存在于 evc-admin。**

### 1.2 本期做 / 不做

| 做 | 不做 |
|---|---|
| 租户、成员、席位管理 | 中转站里加 tenant 字段或租户钱包 |
| 额度分配 / 回收 / 对账 | 改中转站扣费逻辑 |
| 审核规则按客户端现有结构审批和下发 | 代理算力请求 |
| 用量快照与看板（只看中转站扣费） | 把口播再单列成一笔费用 |
| 三角色固定权限 + 审计 | 可配置权限编辑器 / Casbin |
| 客户端中转站地址配置化 | 公司管理员查看命中结果、时间轴和导出 |
| 建号时只分配专属分组 | 共用电脑的本地视频历史隔离（第四期） |

### 1.3 核心映射关系

```
1 个公司成员 = 1 个中转站 user
tenant_members.relay_user_id  ←→  token4u users.id
中转站 usage_logs.user_id      ←→  业务侧的成员用量归因
```

口播成本已经含在中转站分组扣费里。看板和等式只使用 `usage_logs` 的金额，不再按腾讯云时长另记一笔。

---

## 2. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | 与现有客户端同为 Python，OpenAPI 可直接对接 |
| ORM / 迁移 | SQLAlchemy 2.0 async + asyncpg + Alembic | 表结构变更走迁移，不用裸 SQL |
| 数据库 | PostgreSQL 16，独立 database + 独立账号 | 可与 token4u 共实例，**不共库和迁移** |
| 缓存（可选） | Redis | 词表版本缓存、简单限流；MVP 可不要 |
| 认证 | 自建 JWT（access + refresh）+ Argon2 | 不复用中转站 JWT 密钥 |
| 权限 | 角色枚举 + 权限常量 dict + 租户作用域依赖 | 三角色固定，不上 Casbin |
| 后台 UI | React 18 + Ant Design 5 + Vite + TanStack Query | 复用团队已有前端经验；页面少可直接手写 |
| 定时任务 | APScheduler 或独立 cron 容器 | 每日用量快照、月度额度回收 |
| 部署 | Docker Compose + Caddy（HTTPS） | 与 token4u 同机，Postgres 不对外暴露 |

---

## 3. 与中转站的集成契约

### 3.1 只使用这些管理接口

所有调用使用一把**受限的自定义管理密钥**，只存 evc-admin 服务端环境变量：

```
POST   /api/v1/admin/users                       建账号（email/password/allowed_groups/balance）
POST   /api/v1/admin/users/:id/balance           加余额（带 idempotency_key）
GET    /api/v1/admin/users/:id/usage             单成员用量
GET    /api/v1/admin/users/:id/balance-history   余额流水（对账用）
POST   /api/v1/admin/dashboard/users-usage       批量用户用量（看板首屏）
GET    /api/v1/admin/usage/stats                 区间统计（user_id + start_date + end_date）
GET    /api/v1/admin/groups/:id/usage-summary    分组用量（第二版每公司独立分组时用）
```

依据：
- 建用户参数见 `token4u/backend/internal/handler/admin/user_handler.go:62-73`（`email`、`password`、`allowed_groups`、`balance` 等齐全）。
- 批量用量见 `token4u/backend/internal/server/routes/admin.go:295`，实现在 `dashboard_handler.go:583-625`。
- 权限目录见 `token4u/backend/internal/pkg/adminaccess/catalog.go`。
- 管理端**没有**替用户创建 api key 的接口（`admin.go:185-190` 只有 `PUT /api-keys/:id` 改分组），key 由客户端登录后自建或手工填写。

### 3.2 三条硬边界

1. **不代理算力**：客户端直连 gateway `/v1/chat/completions`，evc-admin 只处理业务数据。
2. **管理密钥只在服务端**：公司管理员永远看不到，也不能直接调中转站管理接口。
3. **tenant_id 只从 JWT 取**：绝不读请求体里的租户参数；所有查询走带租户过滤的 Repository。

### 3.3 必须在中转站确认的开关

| 开关 | 要求 | 原因 |
|---|---|---|
| `backend_mode_enabled` | **关闭** | 开启时非 admin 登录一律 403（`auth_handler.go:149-156`），成员客户端无法登录 |
| 自助注册 | **关闭** | 否则员工可自行注册，绕过公司归属（客户端已有 `REGISTRATION_DISABLED` 错误码处理） |
| 邮箱验证 | 确认管理员建号不触发 | 客户端已有 `EMAIL_VERIFY_REQUIRED` 处理；成员建议用真实企业邮箱 |
| 分组倍率 | 确认 1.8 且只允许专属分组 | `app/core/relay.py` 注释中的约定 |

---

## 4. 数据模型

单库多租户：每张业务表带 `tenant_id`（全局表可为 NULL），所有查询强制过滤。

```text
tenants              公司：id, name, status, seat_quota, relay_group_id, pool_balance, created_at
tenant_members       成员：id, tenant_id, email, name, role, relay_user_id, seat_quota, status, created_at
invites              邀请：id, tenant_id, token_hash, email, role, expires_at, used_at
devices              设备：id, member_id, name, last_seen, client_version
member_tokens        设备令牌：id, member_id, token_hash, device_id, expires_at, revoked_at
keyword_entries      正式命中词：id, tenant_id(NULL=全局), text, category, source, status, version, deleted_at
keyword_submissions  提交：id, tenant_id, member_id, text, category, source, scope, status, reviewer_id, reviewed_at, remark
tenant_rules         公司规则：tenant_id, categories(json), ignore_phrases(json), note, updated_at
quota_allocations    额度分配：id, tenant_id, member_id, amount, period, idempotency_key(UNIQUE), operator_id, created_at
usage_snapshots      用量快照：id, tenant_id, member_id, date, requests, input_tokens, output_tokens, cost, UNIQUE(tenant_id,member_id,date)
audit_logs           审计：id, tenant_id, actor_id, role, action, object, before, after, ip, created_at   （只追加）
```

约束：
- `tenant_members.role` 用枚举 + CHECK：`platform_admin / tenant_admin / member`。平台管理员的 `tenant_id` 为空，不占任何公司席位。
- `keyword_entries` 与客户端 `PhraseRule` 一致：`text` 1 到 40 字，`category` 只能是价格、规格、品牌、夸大宣传，`source` 只能是口播或画面。同一租户内按来源、类别、原文去重。通过的命中词全局加公司不超过 300 条。
- `ignore_phrases` 最多 30 条，`note` 最多 400 字。补充说明不能放宽「禁止推测」。
- `quota_allocations.idempotency_key` 唯一索引。先插入「处理中」记录，再调中转站；中转站不认这个键。调用成功才把记录改成成功。调用失败保持失败，同一键重试不再加款。调用成功但写记录失败时，用中转站余额流水补记，不能再调一次加款。
- `usage_snapshots` 按 `(tenant_id, member_id, date)` 唯一，重复跑快照是 upsert。
- 所有删除都是软删除（`deleted_at`），界面不提供物理删除。停用成员立刻腾出该公司的一个席位。

---

## 5. API 设计

### 5.1 认证与角色

```
POST /v1/auth/login          邮箱 + 密码 → access/refresh JWT（含 sub / role / tenant_id）
POST /v1/auth/refresh
POST /v1/auth/logout
```

JWT 的 `tenant_id` 是租户作用域的唯一来源。普通用户不登录后台。

客户端只向中转站登一次。登录成功后，用中转站 access token 调 `POST /v1/devices/bind`。evc-admin 向中转站确认这个用户，对上 `tenant_members` 后签发 `member_token`，存在本机。关键词提交和拉取用这张令牌。对不上成员的账号不同步规则，继续用本机已有规则。

### 5.2 业务接口

| 模块 | 接口 | 权限 |
|---|---|---|
| 租户 | `GET/POST /v1/tenants`、`PUT /v1/tenants/:id/status` | 平台管理员 |
| 成员 | `GET /v1/members`、`POST /v1/members/invite`、`POST /v1/invites/:token/accept`、`POST /v1/members/:id/disable`、`POST /v1/devices/:id/revoke` | 平台 / 公司管理员（本公司）；接受邀请的人打开链接 |
| 额度 | `POST /v1/quotas/allocate`（带 idempotency_key）、`GET /v1/quotas/records` | 平台 / 公司管理员（不超公司池剩余） |
| 规则 | `GET /v1/rules?since=<version>`（支持 ETag）、`PUT /v1/rules/settings`、`POST /v1/rules/submissions`、`GET /v1/rules/submissions`、`POST /v1/rules/:id/approve`、`/reject`、`DELETE`（软删） | 命中词按 scope 与 tenant 审批；类别开关、忽略词、补充说明由公司管理员直接保存 |
| 用量 | `GET /v1/usage/summary?from&to`、`GET /v1/usage/by-member`、`GET /v1/usage/reconcile` | 平台全部 / 公司本公司 / 成员仅自己 |
| 审计 | `GET /v1/audit-logs` | 平台全部 / 公司本公司 / 成员仅自己的提交 |

### 5.3 权限常量（代码内 dict，不建表）

```python
PERMISSIONS = {
    "platform_admin": {"tenant.manage", "member.manage", "quota.allocate",
                       "keyword.submit", "keyword.review", "keyword.review.global",
                       "usage.read.all", "audit.read.all", "settings.write"},
    "tenant_admin":    {"member.manage", "quota.allocate", "keyword.submit",
                        "keyword.review", "usage.read.tenant", "audit.read.tenant"},
    "member":          {"keyword.submit", "usage.read.self", "audit.read.self"},
}
```

三条规则写进服务端：
- 公司管理员分配额度前先校验公司池剩余，再调中转站，全程幂等键；
- 不允许审批自己提交的词（平台管理员可带备注强制通过，但进审计）；
- 不允许公司管理员审批全局词库。

---

## 6. 关键流程

### 6.1 开通成员

只走邀请链接，邮件里不放密码。

```
邀请 → 邮件只有一次性链接
     → 对方设密码 → 校验席位数
     → POST /users(email, password, allowed_groups=[专属分组], balance)
     → 写 tenant_members.relay_user_id，占用一个席位
     → 客户端用这个邮箱密码登录中转站
     → 再用中转站 token 换 member_token
```

中转站建号如果会自动挂上默认分组，要改掉。新建用户只保留这次传入的专属分组。客户端发现专属分组不在可用分组里时，登录可以成功，但不下发画面密钥，页面提示联系管理员。不改用其他分组，也不删除已有密钥。

分组没分配上时，验收就是这条失败，而不是改绑到别的分组。

### 6.2 额度分配

```
提交 → 先写入处理中的 quota_allocations（幂等键已占用）
     → 校验公司池剩余
     → POST /users/:id/balance 做 add
     → 成功后把记录改成成功，并写 audit_logs
```

回收不做进对账。需要把成员余额退回公司池时，用余额接口的 `subtract`，禁止用 `set`。退回的金额加回可分配余额，看板不把它再加一次。

看板只展示两列钱：成员当前余额、本月已消耗。公司池展示剩余可分配。每日状态条比较的是：

```
成员当前余额合计 + 本月已消耗  与  已分配累计  是否一致
```

不平则变红。中转站若把余额扣成负数，该成员按 0 展示，并在状态条注明透支，不把负数加进合计。

### 6.3 审核规则

界面上仍可叫关键词。落库和发给客户端时，按现在软件里的 `DetectionRules`：

- 命中词：`text`、`category`（价格 / 规格 / 品牌 / 夸大宣传）、`source`（口播 / 画面）。词里的 `X` 仍表示数字。
- 类别开关、忽略词、补充说明是公司级设置，公司管理员直接保存，不逐条审批。
- 全局命中词由平台管理员审，公司命中词由本公司管理员审。不能审自己提交的词。

```
客户端提交一条命中词 → 待审 → 通过则版本号 +1 / 驳回
                   → 客户端 GET /v1/rules?since=<version>
                   → 与内置词合并：内置词保留，再叠加已通过的全局词和本公司词
                   → 忽略词命中时丢掉；补充说明只追加，不能放宽禁止推测
```

创建任务时，客户端仍把当时的整套规则抄进该任务。之后审批通过的新词不影响已经排队的视频。软删除的词不再出现在增量里。

### 6.4 用量归集

```
每日 02:00 cron → 逐成员 GET /usage/stats?start_date&end_date&timezone=Asia/Shanghai
               → upsert usage_snapshots → 看板只读本地表
```

口径注意：
- `usage_logs.cost` 已按分组倍率（当前 1.8×）算完，evc-admin **不得再次乘倍率**；
- 日期必须显式传 `timezone`，否则跨日错位一天；
- 第一版所有公司共用一个分组，按分组统计会混淆，只能按 user_id 集合汇总。

### 6.5 离职回收

禁用中转站用户（`UpdateUser status=disabled`）→ 吊销 key → 解绑全部设备令牌 → 席位立即空出。四件必须同时做。单台设备下线只吊销该 `member_token`，不禁用中转站账号。

---

## 7. 开发阶段

### M0 前置确认与环境准备（先做，否则后面都测不了）

- [ ] 中转站关闭后台模式与自助注册，确认邮箱验证策略
- [ ] 建专属分组，确认倍率 1.8，拿到分组 ID / 名称
- [ ] 生成受限管理密钥（只含 users.read / users.write / users.balance.write / usage.read），存环境变量
- [ ] 客户端中转站地址配置化（支持自托管 IP + HTTPS 证书）
- [ ] 确认建号不会自动挂上专属分组以外的分组
- [ ] 确认 `POST /api/v1/asr/credentials` 在该地址可用。这是现有 fork 已有接口，上游同步时不能丢

**验收**：客户端用测试账号在新地址能完成登录、建 key、跑通一次视频审核。

### M1 服务骨架 + 权限 + 审计

- [ ] FastAPI 项目骨架、配置管理、Alembic 初始化
- [ ] JWT 登录（access + refresh）、Argon2 密码哈希
- [ ] 三角色权限常量、`deps` 依赖做权限 + 租户作用域校验
- [ ] 带 tenant_id 过滤的 Repository 基类
- [ ] 审计中间件（只追加）
- [ ] 跨租户越权测试：公司管理员 A 读 B 的数据必须 403

**验收**：两个公司各一个测试账号，互相访问对方资源全部被拒；审计表有完整记录。

### M2 租户与成员

- [ ] 租户 CRUD、席位管理、状态切换
- [ ] 邀请链接开通（调 `POST /users` 且只带专属分组，写 `relay_user_id`）
- [ ] 成员禁用（四件事一起做，含腾出席位）；单设备吊销
- [ ] 客户端用中转站 token 换 `member_token`
- [ ] 后台页面：租户管理、成员管理

**验收**：新开一个成员后，客户端能用他设置的密码登录并跑通一次请求。专属分组不在可用分组里时，不建画面密钥，并提示联系管理员。禁用后客户端立即不可用，席位可以再邀一个人。

### M3 额度与用量

- [ ] 额度分配，先占幂等键再加款
- [ ] 每日用量快照 cron（含 timezone 处理）
- [ ] 对账只比较成员当前余额合计加本月已消耗，是否等于已分配累计
- [ ] 用量看板：按成员、按天、导出
- [ ] 审计日志查询页

**验收**：重复提交同一分配请求只产生一条记录、只加一次余额。调用成功但本地记账失败时，不会再加一次。看板的余额和已消耗与中转站一致。

### M4 审核规则

- [ ] 命中词按文字、类别、来源存储；公司级类别开关、忽略词、补充说明单独保存
- [ ] 客户端提交命中词 + 增量拉取（ETag），并与内置词合并
- [ ] 审批页：待审 / 通过 / 驳回，展示类别和来源，禁止审批自己的词
- [ ] 软删除与审计
- [ ] 全局命中词与公司命中词分作用域

**验收**：提交一条「口播 / 价格」命中词 → 审批通过 → 客户端在 30 秒内用 `since` 拉到，并按口播原话匹配。驳回、删除、忽略词覆盖的原文不出现。已经排队的任务仍用提交时抄下的规则。

### M5 可选增强

- [ ] 第二家公司入驻时改为一公司一分组，`/groups/usage-summary` 一次取公司总量
- [ ] 共用电脑按 Windows 账号分本地视频历史（客户端侧）
- [ ] 若客户要求员工完全不知道中转站：evc-admin 托管密码 + 设备令牌 + 单点登录（需改客户端登录流程）

---

## 8. 测试策略

| 类型 | 重点用例 |
|---|---|
| 权限 | 三角色 × 每个接口的允许/拒绝矩阵；`tenant_id` 从 token 取而非请求体 |
| 越权 | 公司管理员 A 读/改 B 的租户、成员、词、审计，全部 403 |
| 幂等 | 同一 `idempotency_key` 重复提交，只加一次余额、只留一条记录 |
| 对账 | 成员当前余额合计加本月已消耗等于已分配累计；透支记为 0 并标红。不把回收额再加一次 |
| 时区 | `timezone=Asia/Shanghai` 与 UTC 的跨日边界，日报不错位 |
| 倍率 | 验证 evc-admin 不再乘倍率，成本与中转站扣费一致 |
| 规则 | 通过的命中词带类别和来源；忽略词和补充说明一起下发；不允许审批自己的词 |
| 离职 | 禁用后客户端请求立即失败、key 失效、设备令牌失效、席位腾出 |

用 pytest + 独立测试库；每个测试用事务回滚或 truncate，不污染开发库。

---

## 9. 部署与配置

- 与 token4u 同机 Docker Compose，但 Postgres 用独立 database 和账号；Caddy 负责 HTTPS。
- 环境变量（示例，仅列名称，不写值）：

```
EVC_DATABASE_URL
EVC_JWT_SECRET
EVC_RELAY_BASE_URL          # https://.../api/v1
EVC_RELAY_GATEWAY_URL
EVC_RELAY_ADMIN_KEY         # 受限管理密钥，仅服务端
EVC_RELAY_GROUP_ID / GROUP_NAME
EVC_RELAY_TIMEZONE=Asia/Shanghai
```

- 备份：`usage_snapshots`、`quota_allocations`、`audit_logs` 是核心账本，每日备份并保留至少 12 个月。
- 日志：审计表只追加；应用日志脱敏（token、密钥、邮箱）。

---

## 10. 风险与待确认事项

| 风险 / 问题 | 影响 | 处理 |
|---|---|---|
| 客户是预充值到中转站，还是付给你统一结算 | 决定用预分配还是后结算 | M3 前确认；第一版建议预分配 |
| 商户是否需要自己登录中转站后台 | 若需要则必须做方案 A 租户钱包 | 当前假设“只通过客户端使用” |
| 自助注册是否真的能关 | 关不掉则员工可绕过公司归属 | M0 实测 |
| 本地视频历史和命中导出 | 中转站和 evc-admin 都不管审核结果本身 | 本期不做，留在客户端 |
| 客户端写死 token4u.cc | 连不上自托管 IP，什么都测不了 | M0 第一优先级 |
| 建号自动分组、口播临时凭证 | 这两处已经或必须留在中转站 fork 里 | 上游同步时单独保住，不随「零改动」丢掉 |
| 上游同步 | evc-admin 独立仓库，不受 `sync-upstream-release.sh` 影响 | 已规避 |

---

## 11. 总验收清单

- [ ] 中转站扣费主链路不改；建号只分配专属分组；口播临时凭证接口还在
- [ ] 三个角色权限符合矩阵，公司管理员只能动自己公司
- [ ] 成员通过邀请链接设密码，邮件里没有初始密码
- [ ] 额度分配幂等；看板只对余额和已消耗
- [ ] 命中词按类别和来源增量下发，并与内置词、忽略词一起生效
- [ ] 看板数字与中转站一致，且不用实时打中转站
- [ ] 离职时禁用账号、吊销密钥、解绑设备、腾出席位一次完成
- [ ] 客户端地址可配置，能连自托管中转站
