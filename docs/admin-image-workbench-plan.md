# 管理端生图工作台实施方案（bot创建）

日期：2026-11-24 · 分支：`cursor/admin-image-workbench-d62e` · 基线：`f0d9462`（evc-admin）· 目标 PR base：`evc-admin`

## 0. 结论

功能做在管理端 `admin/admin_app` + `admin/web`。不改桌面 `app/`，不改根目录 `frontend/`，不整分支搬用桌面实现。桌面参考里的组名精确匹配、尺寸矩阵、请求体构造和浅色卡片布局可以复用，路由、会话、权限和数据表按管理端重做。

菜单位置（最新要求）：管理端侧栏「工具」分组里，已有「视频审核工具」。**「生图工作台」排在它的正下方**。没有桌面工具地址时，视频工具这一项仍不显示，生图留在同一个「工具」分组。不放进「工作台」或「业务管理」，也不进桌面导航。

## 1. 已接上的本地改动

基线就是 `f0d94627fa38a05bbe5692532d82cca5645e557c`，下面四项直接接上，没有覆盖仓库里其他文件：

- `auth_relay.available_groups`：用当前用户令牌读 `GET /groups/available`。原补丁调用只返回 dict 的 `_authed`，列表会被收成空对象，因此改成保留 list，并认 `items` / `list` / `groups`。
- `tenant_members.relay_image_api_key`：只加列。已有审核钥 `relay_api_key` 不动。
- 本机库表 `image_jobs`：不设跨库外键。
- 权限 `image.workbench`：平台管理员、公司管理员、普通用户都有。历史和图片仍只属于当前用户，不因为角色更高就跨用户或跨租户查看。

## 2. 授权和密钥

- 分组只按名称「生图专用-bot创建」精确匹配当前用户的可用分组。没有这个组就明确不可用。
- 不回落审核组，不回落普通组，不用固定编号代替授权校验。
- 生图钥名称「生图专用」，幂等键 `evc-admin-image-key:{email}:{group_id}`。列回密钥时按这个名称复用，避免拿到「电商视频合规」。分组编号来自 `EVC_ADMIN_IMAGE_GROUP_ID`，默认 16。
- 不读 `EVC_RELAY_GROUP_NAME`，不写审核钥，不用管理密钥，也不接收前端传来的 baseURL / group / key。
- 新用户不自动开通。

## 3. 能力和价格

候选模型可配置，默认四个：`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2.6-flare`、`gpt-image-2.6-sunburst`。`ready` 只在专组返回的模型清单里出现时为真。清单缺省时，只有上游实测过的 flare 可选，并且 `ready` 仍为假。

上游已验证、本站网关待验的组合只有：`gpt-image-2.5-flare`，省略 quality，n=1，尺寸精确为 `2048x1152` 或 `3840x2160`。

`auto` / `1024x1024` / `1536x1024` / `1024x1536` 可选，标注「本站网关待验 · 未经上游实测」，`verified` 为假。指定 quality、其他尺寸默认禁用。2K/4K 的 n>1 或指定 quality 拒绝。参考图、edits、异步生成首版禁用。占位图是带 MOCK 字样的 SVG，按请求尺寸声明宽高，不做本地放大。

展示预估 ¥0.10/张，4 张 ¥0.40。真实扣费以网关按 ImageCount 的实账为准。不把本站价格当成上游成本，也不用美元账单换算人民币利润。

## 4. 真实调用

默认关闭。同时满足下面四项才同步 POST `https://token4u.cc/v1/images/generations`：

1. `EVC_ADMIN_IMAGE_LIVE=1`
2. `EVC_ADMIN_IMAGE_CONTRACT_OK=1`
3. 当前用户通过专组名称校验，并使用自己的生图钥
4. 请求是上面的已核验组合

网关地址不是 `https://token4u.cc/v1` 时直接拒绝。其他组合即使开关打开也不发起真实调用。本环境不配置这两个开关，也不发起真实生成。

## 5. 接口

前缀 `/v1/image-workbench`，全部要求 `image.workbench`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 能力、价格、分组是否可用、开关状态。不返回分组编号和密钥 |
| POST | `/generate` | `confirm: true`。多传 group / key / baseURL / async 则 422 |
| GET | `/jobs` | 只看当前用户，可按描述或模型搜索，并按方向筛选 |
| DELETE | `/jobs/{id}` | 只能删自己的 |
| GET | `/jobs/{id}/files/{name}` | 只能看自己的，`Cache-Control: private, no-store` |
| POST | `/references` | 固定 403，说明首版禁用 |

## 6. 明确不做

不合并、不部署、不打标签、不推 `main` 或 `evc-admin`。不登录生产服务器，不重配已有分组、渠道、账号或审核用户。不把云端预览说成父级电脑上正在跑的 8780。
