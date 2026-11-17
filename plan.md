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
| 关键词提交 / 审批 / 增量下发 | 代理算力请求 |
| 用量快照与看板 | 商户自助登录中转站后台 |
| 三角色固定权限 + 审计 | 可配置权限编辑器 / Casbin |
| 客户端中转站地址配置化 | 共用电脑的本地视频历史隔离（第四期） |

### 1.3 核心映射关系

```
1 个公司成员 = 1 个中转站 user
tenant_members.relay_user_id  ←→  token4u users.id
中转站 usage_logs.user_id      ←→  业务侧的成员用量归因
```

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
keyword_entries      正式词库：id, tenant_id(NULL=全局), word, status, version, deleted_at
keyword_submissions  提交：id, tenant_id, member_id, word, scope, status, reviewer_id, reviewed_at, remark
quota_allocations    额度分配：id, tenant_id, member_id, amount, period, idempotency_key(UNIQUE), operator_id, created_at
usage_snapshots      用量快照：id, tenant_id, member_id, date, requests, input_tokens, output_tokens, cost, UNIQUE(tenant_id,member_id,date)
audit_logs           审计：id, tenant_id, actor_id, role, action, object, before, after, ip, created_at   （只追加）
```

约束：
- `tenant_members.role` 用枚举 + CHECK：`platform_admin / tenant_admin / member`。
- `quota_allocations.idempotency_key` 唯一索引，重试不会产生第二条。
- `usage_snapshots` 按 `(tenant_id, member_id, date)` 唯一，重复跑快照是 upsert。
- 所有删除都是软删除（`deleted_at`），界面不提供物理删除。

---

## 5. API 设计

### 5.1 认证与角色

```
POST /v1/auth/login          邮箱 + 密码 → access/refresh JWT（含 sub / role / tenant_id）
POST /v1/auth/refresh
POST /v1/auth/logout
```

JWT 的 `tenant_id` 是租户作用域的唯一来源。普通用户不登录后台，用 `member_tokens` 在客户端证明身份。

### 5.2 业务接口

| 模块 | 接口 | 权限 |
|---|---|---|
| 租户 | `GET/POST /v1/tenants`、`PUT /v1/tenants/:id/status` | 平台管理员 |
| 成员 | `GET /v1/members`、`POST /v1/members/invite`、`POST /v1/members/:id/disable`、`POST /v1/members/:id/password-reset` | 平台 / 公司管理员（本公司） |
| 额度 | `POST /v1/quotas/allocate`（带 idempotency_key）、`POST /v1/quotas/recycle`、`GET /v1/quotas/records` | 平台 / 公司管理员（不超公司池） |
| 关键词 | `GET /v1/keywords?since=<version>`（支持 ETag）、`POST /v1/keywords/submissions`、`GET /v1/keywords/submissions`、`POST /v1/keywords/:id/approve`、`/reject`、`DELETE`（软删） | 按 scope 与 tenant 判定 |
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

```
邀请 → 校验席位数 → POST /users(email,password,allowed_groups,balance)
     → 写 tenant_members.relay_user_id → 发送邮箱+初始密码
     → 客户端登录后自动建 key（relay.py:373）或手工填 api_key（manager.py:594-597）
```

### 6.2 额度分配

```
提交 → 校验公司池剩余 → POST /users/:id/balance(idempotency_key)
     → 写 quota_allocations → 写 audit_logs
```

对账等式（每日校验，看板顶部红/绿状态条）：

```
公司池总额 = 成员余额合计 + 本月已消耗 + 已回收
```

### 6.3 关键词

```
客户端提交 → 待审 → 审批（通过/驳回）→ 通过则版本号 +1
          → 客户端 GET /v1/keywords?since=<version> 拉增量（ETag）
          → 删除一律软删除，写审计
```

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

禁用中转站用户（`UpdateUser status=disabled`）→ 吊销 key → 解绑设备令牌。三件必须同时做。

---

## 7. 开发阶段

### M0 前置确认与环境准备（先做，否则后面都测不了）

- [ ] 中转站关闭后台模式与自助注册，确认邮箱验证策略
- [ ] 建专属分组，确认倍率 1.8，拿到分组 ID / 名称
- [ ] 生成受限管理密钥（只含 users.read / users.write / users.balance.write / usage.read），存环境变量
- [ ] 客户端 `app/core/relay.py:19-20` 的中转站地址配置化（支持自托管 IP + HTTPS 证书）
- [ ] 模型名配置化，确认 ASR 凭证接口在新地址可用

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
- [ ] 成员邀请与开通（调 `POST /users`，写 `relay_user_id`）
- [ ] 成员禁用（三件事一起做）、密码重置
- [ ] `member_tokens` 发放与吊销
- [ ] 后台页面：租户管理、成员管理

**验收**：新开一个成员后，客户端能用发放的邮箱密码登录并跑通一次请求；禁用后客户端立即不可用。

### M3 额度与用量

- [ ] 额度分配 / 回收，幂等键唯一索引
- [ ] 每日用量快照 cron（含 timezone 处理）
- [ ] 对账状态计算与看板红/绿提示
- [ ] 用量看板：按成员、按天、导出
- [ ] 审计日志查询页

**验收**：重复提交同一分配请求只产生一条 `quota_allocations`、只加一次余额；对账等式成立；看板数字与中转站后台一致。

### M4 关键词

- [ ] 词库表、提交表、版本号管理
- [ ] 客户端提交接口 + 增量拉取接口（ETag）
- [ ] 审批页：待审 / 通过 / 驳回，禁止审批自己的词
- [ ] 软删除与审计
- [ ] 全局词库与公司自定义词库的双轨作用域

**验收**：提交一条词 → 审批通过 → 客户端在 30 秒内用 `since` 拉到；驳回与删除不出现在增量结果里。

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
| 对账 | 构造余额/消耗/回收数据，验证等式；不平则状态条变红 |
| 时区 | `timezone=Asia/Shanghai` 与 UTC 的跨日边界，日报不错位 |
| 倍率 | 验证 evc-admin 不再乘倍率，成本与中转站扣费一致 |
| 词汇 | 通过/驳回/软删后的增量结果；不允许审批自己的词 |
| 离职 | 禁用后客户端请求立即失败、key 失效、设备令牌失效 |

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
| 本地视频历史的可见性 | 中转站管不了；共用电脑会串数据 | 按 Windows 账号分目录，第四期 |
| 客户端写死 token4u.cc | 连不上自托管 IP，什么都测不了 | M0 第一优先级 |
| 上游同步 | evc-admin 独立仓库，不受 `sync-upstream-release.sh` 影响 | 已规避 |

---

## 11. 总验收清单

- [ ] 中转站代码零改动
- [ ] 三个角色权限符合矩阵，公司管理员只能动自己公司
- [ ] 成员无需自助注册，由 evc-admin 代建
- [ ] 额度分配幂等、对账等式成立
- [ ] 关键词增量下发可用，违规词进不去
- [ ] 看板数字与中转站一致，且不用实时打中转站
- [ ] 离职回收三件事一次完成
- [ ] 客户端地址/模型配置化，可连自托管中转站
