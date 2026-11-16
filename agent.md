# 给 Agent 的项目说明

本地电商视频合规检测。用户上传视频后，程序抽帧并用选中的画面模型看画面，同时用腾讯云录音文件识别看口播，标出价格、规格、品牌、口播夸大宣传和时间区间，可导出，也可把命中片段剪掉后重新拼接。只跑在本机，不部署云端。界面、提示词和导出字段用中文；代码标识符用英文。

先读本文件，再按下面的地图改代码。用户向的说明在 `README.md`。不要另起一个项目。

## 不能破坏的约定

- 只报告画面里看得见的文字，或口播里连续说出来的原话。禁止根据外观、常识或上下文推测。解析器会丢掉判定理由里带「推测 / 猜测 / 常识 / 应该是 / 看起来像 / 估计是 / 可能是」的命中。自定义规则的补充说明不能放宽这条。
- 类别有「价格」「规格」「品牌」，口播另有「夸大宣传」。来源只有「画面」「口播」。两者即使原文相同也不合并。夸大宣传只看口播转写里的连续原话，边界在 `app/core/exaggeration.py`：绝对化用语和疗效承诺。不判断真假。「最后」「最近」「最终」「最开始」以及「最大号」「最小号」不算。画面不报这一类。解析器会丢掉对不上这些说法的夸大宣传命中。
- 导出固定 8 列，顺序不要改：时间点、类别、来源、原文、位置、置信度、判定理由、备注。JSON 在 `结果` 数组里。口播位置固定为「口播内容」。
- 口播原文必须是某一句转写里的连续子串，对不上就丢掉。画面命中的时间是抽帧区间，不是模型自己编的时间。
- 画面模型只有两个：`glm-5.3-flash`（GLM 5.3 Flash，`PICTURE_MODEL`，默认）和 `deepseek-flash`（DeepSeek 4.1 Flash，`DEEPSEEK_FLASH_MODEL`）。登录后的请求用设置里选中的那个，基地址固定为 `https://token4u.cc/v1`。`chat_url()` 只在基地址后面补 `/chat/completions`；基地址已经以该路径结尾就原样使用，不要自己再插入 `/v1`。加载设置时，空的模型名和其他名字改成 `glm-5.3-flash`；`deepseek*`（例如 `deepseek-v4.1-flash`、`deepseek-v4.1-flash-1017`）改成 `deepseek-flash`。不要改用户已保存的基地址。
- 视觉请求只在 user 消息里放 `image_url.url` 的 data URL，不要带 `detail`。`glm-5.3-flash` 的 `thinking.type` 只能是 `enabled`，传 `disabled` 会 400。思考开关关闭时带 `reasoning_effort: low` 和 `max_tokens: 2048`；打开时用 `high` 和 `4096`。不要省略 `reasoning_effort`，上游默认是 `max`。使用 `response_format: json_object`；若 400 且错误提到 `response_format`，去掉该字段重试一次。408/409/429/500/502/503/504 和网络超时可重试。401/403 表示密钥无效。请求里不要带 temperature。低细节只把抽帧长边压到不超过 640，不传给模型。
- 服务只监听 `127.0.0.1` 或 `localhost`。不要改成对外网卡。
- 密钥只放在 gitignore 的 `.env` 和 `data/app.db`。不要写进任务记录、日志、测试、提交或聊天回复。`GET /api/settings` 只返回打码后的密钥。PUT 时空的密钥字段表示「不修改」。`clear_api_key` / `clear_tencent_secret` 只清本机保存的值。
- 有效密钥的优先级：画面识别用数据库里的 `api_key`，否则用 `DEEPSEEK_API_KEY`（环境变量名保持不变）。腾讯云必须 SecretId 和 SecretKey 成对出现，数据库一对优先，否则用 `TENCENTCLOUD_SECRET_ID` + `TENCENTCLOUD_SECRET_KEY`。地域用设置里的 `tencent_region`，否则 `TENCENTCLOUD_REGION`，否则 `ap-shanghai`。基地址没有环境变量，只在数据库设置里。登录后的请求改用账号密钥和 `https://token4u.cc/v1`。这把自动密钥绑定分组「电商审核专属-bot创建」（`EVC_RELAY_GROUP_NAME`，编号用 `GET /api/v1/groups/available` 按名称解析，对不上再用仍在该列表中的 `EVC_RELAY_GROUP_ID`）。它是专属分组，未分配给账号时要明确报错，不能改绑或删除已有密钥，也不能改用其他分组。
- `.env.example` 保持空值。`load_dotenv()` 用 `setdefault`，已经存在的环境变量优先。

## 改哪里

| 用户要改的行为 | 先看 |
| --- | --- |
| 画面或口播提示词、禁止推测 | `app/core/prompts.py` |
| 丢掉不合法命中、口播原文必须命中原句、词级时间 | `app/core/parsing.py` |
| 自定义类别开关、额外命中词、忽略词 | `app/core/rules.py`，结构在 `app/schemas.py` 的 `DetectionRules`，界面在 `frontend/src/App.tsx` 的审核规则抽屉 |
| 画面模型请求体、重试、模拟识别 | `app/core/deepseek.py` |
| 腾讯云录音文件识别、分片、时间戳 | `app/core/tencent_asr.py`，句子结构在 `app/core/transcript.py` |
| 抽音频、口播进度、和画面并行 | `app/core/speech.py`，调度在 `app/services/manager.py` |
| 抽帧、画面进度 | `app/core/pipeline.py`，`app/core/ffmpeg.py` |
| 相邻命中收成一条 | `app/core/aggregate.py`。合并键是类别 + 来源 + 原文。画面价格会把同一处的不同读法收成一档，例如「5.55元」和「5.55元3把」。画面间隙不超过 `PICTURE_GAP`（2 秒）就接上；口播间隙超过 `MERGE_EPSILON`（0.08 秒）就断开 |
| 自动剪辑 | `app/core/editor.py` 的 `plan_cuts`，调用在 `manager.cut` |
| 任务队列、设置、取消、导出 | `app/services/manager.py` |
| HTTP 接口 | `app/api/routes.py`。字段定义在 `app/schemas.py` |
| 历史和迁移 | `app/db.py`。加列走 `_ensure_columns`，不要靠删库 |
| 导出列 | `app/exporting.py` 和 `Finding.export_dict` |
| 上传、时间轴、设置、批量队列 | `frontend/src/App.tsx`，`frontend/src/api.ts`，`frontend/src/types.ts` |
| 预览播放器 | `frontend/src/VideoPane.tsx` |

界面构建产物由 `app/factory.py` 用 StaticFiles 托管 `frontend/dist`。改 Python 要重启进程。改前端要在 `frontend/` 执行 `npm run build`（`tsc --noEmit && vite build`），不必为纯静态资源重启。开发时 Vite 在 5173，把 `/api` 代理到 `127.0.0.1:8765`。

## 一条视频怎么走

1. `POST /api/jobs` 或 `POST /api/jobs/batch` 把文件放到 `data/videos/{job_id}/`。
2. 同一时间只跑一个任务。其余保持 `queued`，最多 20 个。`EVC_INLINE_JOBS=1` 时在请求里同步跑完，测试靠这个。
3. 画面：FFmpeg 按 FPS（0.5–2）抽 JPEG，边抽边发给当前选中的画面模型。口播：抽出 16 kHz 单声道，走腾讯云「录音文件识别」（`CreateRecTask` / `DescribeTaskStatus`，SourceType 1）。缺腾讯云密钥时跳过口播，任务仍然成功，`asr_error` 写明已跳过。
4. 两边并行。总进度大约是画面 70% + 口播 30%，完成前不超过 99。画面先结束时提示「画面已完成，正在等待口播结果」。
5. `aggregate()` 收成 `Finding`。口播命中额外带 `sentence_start`、`sentence_end`、`next_start`，只给剪辑用，不进导出。
6. 临时帧在 `data/frames/{job_id}`，临时音频在 `data/audio/{job_id}`。任务结束就删，除非 `EVC_KEEP_FRAMES=1`。
7. `POST /api/jobs/{id}/cut` 生成 `clean.mp4`。预览在原片和剪辑结果之间切换。

进度用约 700 ms 轮询 `GET /api/jobs/{id}`，也有 SSE `/events`。取消要停掉抽帧进程、音频进程，并关掉画面模型和 ASR 客户端。画面识别失败的帧号记在任务的 `failed_frames_json`。全部帧都失败时任务失败；只有一部分失败时任务完成，并写明失败帧数和最后一次错误。`POST /api/jobs/{id}/retry-frames` 重试这些帧：记下了帧号就只重抽这些帧，并和已有画面命中合在一起；没记下帧号就重跑全部画面。口播已经成功就保留，口播也失败则一起重跑。前面还有任务时排到队尾。

程序重新打开时，正在跑或暂停的任务记为失败；还在排队的任务会留在队列里。如果退出前队列是暂停的（存在暂停中的任务），重新打开后仍然暂停。

## 审核规则

保存在设置 JSON 的 `rules` 里，创建任务时抄进该任务的 options，中途改设置不影响已经排队的任务。

- `categories`：启用的预设类别。至少要有一个类别，或一条额外命中词。
- `extra_phrases`：`{text, category, source}`。`source` 是「口播」或「画面」，缺省为口播。口播词在原句里连续出现就在本地补一条命中；词里的 `X` 表示数字（`X折` 命中「5折」「五折」，`X.X贵` 命中「3.5贵」「三点五贵」），原文记实际说出的那一段。画面词只写进画面提示，仍要模型在这一帧里看见，不在本地给口播补命中。同一条按来源、类别、原文去重。最多 300 条，单条 1 到 40 字。
- `ignore_phrases`：命中原文与忽略词完全一致，或原文只是忽略词里的一段时丢掉。更长的句子里夹着忽略词，不因此整句丢掉。
- `note`：最多 400 字，追加在系统提示末尾。提示词最后仍写明禁止推测优先。

默认价格、规格、品牌、夸大宣传全开，并带一套内置词：口播的价格、规格、品牌，以及画面品牌（logo、商标等）。夸大宣传没有词表，转写后由语言模型按 `exaggeration.py` 的边界判断。忽略词和补充说明默认是空的。已经保存过的额外词不会被盖掉。额外词还是空、且没有 `rules_initialized` 时，加载设置会补上这套词并写下这个标记；之后再把词清空，就会保持清空。没有 `exaggeration_initialized` 时，会把「夸大宣传」补进启用类别并写下这个标记；用户后来关掉就保持关掉。

## 自动剪辑

`plan_cuts(duration, findings, padding, silences=...)` 返回要删的区间和要留的区间。

- 画面：`padding` 向两侧对称扩展。界面上的边距只按这个语义给画面用，范围 0–2 秒。
- 口播：初始删除段用被引用的那几个字的起止（`start_sec`–`end_sec`），不把 `sentence_start`–`sentence_end` 整句放进去。句首只收很短的一段（不超过 0.05 秒，并且不超过画面边距）。句尾再覆盖大约一个字（约 0.26 秒）。画面边距不拉长口播。若 FFmpeg `silencedetect` 在这几个字的结尾附近找到停顿，切在停顿上，使下一句开头不带上一句的最后一个字。下一句的时间戳若落在这个停顿之后，不要切进下一句的起音。两句时间戳几乎贴在一起且没有停顿时，仍向后多切大约一个字。
- 整句时间只在选中删除段时显示，方便把边缘再扩到没说中的半句。手改后的秒数是绝对时间，不再套画面边距，也不再加口播句尾。没有句子边界的旧命中同样按自己的 `start_sec`–`end_sec` 做句尾补偿。
- 保留段短于 0.12 秒就丢掉。全部被切掉则报错，不生成空视频。
- 编码是 libx264 veryfast、crf 22、yuv420p、aac，并加 `+faststart`。音轨编码失败就退回只有画面。剪辑前会探测静音；探测失败就只用时间戳，不要让静音探测失败把整个剪辑打挂。

## 预览

媒体地址是 `/api/jobs/{id}/media` 和 `/api/jobs/{id}/output`，路径里没有扩展名。video.js 在没有 `type` 时会用扩展名拼 `video/`，然后报「无法找到此视频兼容的源」。`VideoPane` 必须收到 MIME。`App.tsx` 的 `previewMime()` 按文件名给 webm/ogg，其余按 `video/mp4`；剪辑结果永远是 mp4。

`GET` 这些地址返回 200 和 `Accept-Ranges`。不要用 `HEAD` 判断文件是否存在：HEAD 没有单独注册，可能落到静态托管并返回 404。输出接口的 Content-Type 固定 `video/mp4`。

## 接口

路由前缀 `/api`。任务 id 是 32 位小写十六进制。

- `GET /health`
- `GET /settings`，`PUT /settings`
- `POST /jobs` 单文件字段名 `file`。`POST /jobs/batch` 字段名 `files`，一次最多 20 个。非法文件记在 `errors` 里，不要因此丢掉同批里的合法文件。
- `GET /jobs`，`GET /jobs/{id}`，`GET /jobs/{id}/events`
- `POST /jobs/{id}/cancel`，`DELETE /jobs/{id}`
- `POST /jobs/{id}/retry-frames` 重试这个任务里识别失败的画面帧。任务还在排队、运行或暂停时返回 409。原视频不在了返回 404。
- `GET /jobs/{id}/media`，`GET /jobs/{id}/output`，`GET /jobs/{id}/strip/{index}`（时间轴缩略图 JPEG）
- `GET /jobs/{id}/export?fmt=json|xlsx`
- `POST /jobs/export`，正文 `{ids}`，至少 2 个、最多 20 个。已完成的视频合成一个 Excel：结果表在原来的 8 列前加「视频」，未完成的只记在「任务信息」里。单视频导出的 8 列不变
- `POST /jobs/{id}/cut`，正文 `{padding_sec}`
- `POST /system/open-dir`，打开 `exports` 或 `data`

允许的上传扩展名：mp4、mov、mkv、avi、webm、m4v、flv、wmv、mpeg、mpg、ts。单文件最大 2GB。队列满返回 409。

命中在 API 里用中文别名（`时间点`、`类别`、`来源` 等），同时带 `start_sec` / `end_sec` 供定位。`Finding` 和 `RawHit` 用关键字构造，不要改成位置参数。

## 数据与配置

SQLite 在 `data/app.db`，WAL 模式。设置是 `settings` 表里 key 为 `config` 的一行 JSON。打包后的数据目录在系统用户目录（Windows 为 `%APPDATA%\EcommerceVideoCompliance`），开发时是项目下的 `data/`。`EVC_DATA_DIR` 可覆盖。

其他环境变量：`EVC_FFMPEG`、`EVC_FFPROBE`、`EVC_MOCK=1`、`EVC_INLINE_JOBS=1`、`EVC_KEEP_FRAMES=1`、`EVC_LOG_LEVEL`、`TENCENT_ASR_HOST`。日志在 `data/logs/app.log`，写入前会打码 sk-、Bearer、AKID、Credential、Signature。

`data/`、`.env`、`.deps/`、`frontend/node_modules/`、`frontend/dist/` 都在 `.gitignore`。不要提交它们。

## 本机怎么跑

项目路径：`/home/awt/Documents/workspace/ecommerce-video-compliance`。这台 Linux 没有 `python3-venv` / ensurepip，依赖装在项目内的 `.deps`（已 gitignore）。

```bash
cd /home/awt/Documents/workspace/ecommerce-video-compliance
PYTHONPATH=".deps:$PYTHONPATH" python3 -m pytest -q
PYTHONPATH=".deps:$PYTHONPATH" python3 run.py --no-window --browser
```

默认端口 8765。`--no-window` 会一直占着进程。改后端后先停掉旧进程再启动，避免端口冲突。pytest 会自己改 `EVC_DATA_DIR`，并挡住 `.env`，不要在测试里读真实密钥。`asyncio_default_fixture_loop_scope` 是 `function`。需要真实短视频的测试在没有 libx264 时会跳过。

命令行走同一条流水线：`python3 -m app.cli 视频.mp4 --mock --no-asr`。Windows 桌面窗口用 pywebview；这台机器没有桌面 WebView，用浏览器即可。打包入口是 `packaging/evc.spec` 和 `scripts/build_windows.ps1`。

## 改完要核对

- 后端行为：`PYTHONPATH=".deps:$PYTHONPATH" python3 -m pytest -q`。现有覆盖包括提示词禁推测、口播原文必须在句中、画面和口播不合并、剪辑边界、设置不泄漏密钥、模拟流水线、批量上传。
- 改了界面：`cd frontend && npm run build`。预览相关改动要确认 `sources[].type` 仍在。这台机器没有 Chrome / Firefox，无法做点击级浏览器验收时，要在回复里说明只做了构建和接口检查。
- 不要为了修一个边界把对称边距套回口播，也不要为了消掉句尾一个字去切掉下一句的第一个字。
- 不要把模拟识别、缺密钥跳过口播、或单任务队列改成静默失败。缺密钥和坏视频都要在任务的 `message` / `error` 里用中文写清楚。
