# 御镜视频素材审核系统

本地桌面工具。上传电商视频后，按可配置帧率抽帧，调用 `glm-5.3-flash` 读取画面里的价格、规格、品牌。开启口播检测时，同时用 FFmpeg 抽出音频，调用腾讯云录音文件识别，再用同一套规则判断口播。口播转写里的绝对化用语和疗效承诺会另标为夸大宣传，不判断这句话是不是真的。画面和口播并行处理，相邻命中收成时间区间。

程序只跑在本机：FastAPI 监听 `127.0.0.1`，窗口用 pywebview，历史放在 SQLite。不部署云端服务。

## 目录

```text
ecommerce-video-compliance/
├── run.py                      # 桌面入口
├── requirements.txt
├── app/
│   ├── desktop.py              # pywebview + uvicorn
│   ├── cli.py                  # 命令行跑同一条流水线
│   ├── factory.py              # FastAPI
│   ├── api/routes.py
│   ├── services/manager.py     # 任务、进度、历史
│   ├── db.py
│   ├── core/
│   │   ├── ffmpeg.py           # 探测、抽帧、提取音频
│   │   ├── deepseek.py         # glm-5.3-flash 画面与口播文本判定
│   │   ├── tencent_asr.py      # 腾讯云录音文件识别
│   │   ├── speech.py           # 提取音频、识别、判定
│   │   ├── pipeline.py         # 抽帧和识别并行
│   │   ├── aggregate.py        # 相邻命中收成时间区间，画面和口播不合并
│   │   ├── parsing.py          # 丢掉推测和空原文
│   │   ├── rules.py            # 自定义类别、命中词、忽略词
│   │   └── editor.py           # 按命中区间剪辑；口播按引用的字和停顿切
│   └── exporting.py
├── frontend/                   # React + Ant Design + Video.js
├── packaging/evc.spec          # PyInstaller
├── packaging/installer.iss     # Inno Setup 安装包
├── scripts/build_windows.ps1
├── scripts/build_installer.ps1
└── tests/
```

## 识别规则

模型只看单帧画面，或只看腾讯云转写出来的口播句子。提示词禁止根据常识或外观推测。解析时会丢掉这些结果：

- 类别不是价格、规格、品牌；口播还可以是夸大宣传，但原文必须落在绝对化用语或疗效承诺上，画面不报这一类
- 原文为空、只写「无」，或长得不像画面文字
- 判定理由里出现「推测」「猜测」「常识」「应该是」

画面上同一处价格若只是读全或读漏，会收成一条。例如「5.55元」和「5.55元3把」，以及把小数点看成冒号的「5:55元」，原文留更完整、并且出现不止一次的读法。同一原文空了不到大约 2 秒也接上，避免一帧一帧重复。空得更久就分成两段，避免把闪一下的价格拉成整段。口播仍按原文完全一致来合并，中间空帧不合。画面和口播即使原文相同也分成两条，用来源区分。导出的每一条是：

```json
{
  "时间点": "00:12-00:15",
  "类别": "价格",
  "来源": "口播",
  "原文": "这款只要九十九元",
  "位置": "口播内容",
  "置信度": "确定",
  "判定理由": "口播中明确提及价格",
  "备注": ""
}
```

来源只有「画面」或「口播」。口播原文必须是识别句子里连续出现的原话，位置固定为「口播内容」。JSON 文件里这些条目在 `结果` 数组中。Excel 的「合规结果」工作表是这 8 列。

时间精度取决于抽帧 FPS。1 FPS 时，连续三帧覆盖 `00:12-00:15`。

「审核规则」按口播和画面、再按价格、规格、品牌分组，每组一行一个词，可以直接改。补充说明不能放宽「禁止推测」。口播词在原句里连续出现就会在本地补上，词里的 X 表示数字，例如 X折 对应「5折」。画面词只写进画面提示，仍要这一帧里看得见。忽略词和原文完全一致，或原文只是忽略词里的一段时，这条不报告。

## 环境

- Windows 10/11，或用于开发的 Linux / macOS
- Python 3.11+
- Node.js 18+（只在构建界面时需要）
- FFmpeg 5+，并且 `ffmpeg`、`ffprobe` 在 PATH 中
- 能访问 `https://token4u.cc`（登录后的画面请求发到 `https://token4u.cc/v1`，模型可选 `glm-5.3-flash`、`deepseek-flash` 或 `qwen3.8-flash`）
- 口播检测还要能访问 `https://asr.tencentcloudapi.com`。登录后的临时凭证由 token4u 签发，本机不保存腾讯云密钥
- Windows 桌面窗口使用系统 WebView2（Win10/11 一般已经带了）

登录 token4u.cc 账号后，程序用该账号的密钥调用画面模型。注册或登录时会列出这个账号的可用分组（`GET /api/v1/groups/available`），按名称「电商审核专属-bot创建」解析出分组编号，再创建或复用一把名为「电商视频合规」的密钥并绑到该分组。这是专属分组，余额计费，费率 1.8 倍，含口播费用。管理员没有把分组分配给账号时，页面只提示联系管理员开通。程序不会改用其他分组，也不会删除已有密钥。已经绑在别的分组上的同名密钥会留在原处，程序另用新分组里的密钥。

画面模型选 GLM 5.3 Flash 时，画面帧和口播批次都只请求 `glm-5.3-flash`，不再分到 `gpt-6-luna`。DeepSeek 和 Qwen 仍请求各自的模型。登录后的请求仍用同一把分组密钥和 `https://token4u.cc/v1`。

分组名称可以用 `EVC_RELAY_GROUP_NAME` 覆盖，默认就是「电商审核专属-bot创建」。名称对不上时，如果 `EVC_RELAY_GROUP_ID` 填了编号，并且该编号也在当前账号的可用分组里，才改用这个编号。编号还不确定时留空。查询可用分组失败、又配置了编号时，会直接用该编号创建；中转站拒绝绑定则同样报错。

视觉请求使用 Chat Completions，图片用 base64 data URL，不带 `detail`。`glm-5.3-flash` 不能关闭思考；思考开关关闭时用 `reasoning_effort: low`，避免默认的 `max`。`qwen3.8-flash` 会把思考过程放在 `reasoning_content`，程序只读取 `content`。它的输出上限是另外两个模型的两倍，避免思考过程把结果截断。请求带应用自己的 User-Agent。系统提示会写上本机当天日期，减少把已经过去的日期说成未来日期。

## 开发运行

Windows PowerShell：

```powershell
cd ecommerce-video-compliance
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
python run.py
```

只开服务、用浏览器访问：

```powershell
python run.py --no-window --browser
```

界面热更新（先让上面的服务停在 8765 端口，或改用 `--no-window`）：

```powershell
cd frontend
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。Vite 会把 `/api` 转到 `8765`。

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cd frontend && npm install && npm run build && cd ..
python run.py --no-window --browser
```

没有图形界面依赖时，`--no-window` 一样可以完成上传、识别和导出。

命令行：

```bash
python -m app.cli 你的视频.mp4 --fps 1 --concurrency 4
python -m app.cli 你的视频.mp4 --mock
python -m app.cli 你的视频.mp4 --no-asr
```

`--mock` 不调用模型，只走抽帧、音频提取、模拟识别、聚合和清理。`--no-asr` 只关闭这一次的口播检测。

## 给本机 agent 调用

默认关闭，不影响界面和原来的接口。登录后打开右上角的「MCP」抽屉，打开开关，再重新启动程序。也可以在启动前设置 `EVC_MCP=1`。地址是 `http://127.0.0.1:8765/mcp`。抽屉里有一段可复制的提示语，发给 agent 让它完成配置。程序要开着，并且已经登录。

agent 可以提交一个本地视频，或指定一个文件夹。文件夹只看这一层里的视频，排队跑完后返回批量报告，并在 `data/exports/` 写下 Excel。调用如果中断，用当时的 `batch_id` 再取同一次报告。

## 管理控制台

管理后台的页面和桌面程序打进同一个安装包，但它是另一个独立应用：

- 页面来自安装包里的 `admin/web/dist`，由本机 `127.0.0.1:8780` 提供；
- 所有 `/v1` 请求由本机原样转发到云端后台 `EVC_ADMIN_SERVER_URL`（默认 `https://admin.token4u.cc`），公司、成员、额度、用量和审计仍然只存在云端，本机不落库；
- 本机不复制后台的业务代码，也不需要它的数据库或管理密钥。

**程序启动直接进控制台**。工具端不单独开窗口，而是从控制台左侧菜单的「视频审核工具」在内容区用 iframe 打开；工具端自己的导航栏里不再有「管理控制台」入口，也不显示账号、余额和退出登录。

登录只有一套：

- 从控制台进来时带 `?sso=1`，本机壳用工具端已经登录的中转站令牌向云端换控制台会话，不会再让你输密码；
- 反过来在控制台输入账号密码登录成功后，本机壳会用同一组账号把工具端也登进去（含两步验证），所以嵌在下面的工具端不会再要求登录；
- 两边都失败时（工具端没登录、云端没升级、断网）会退回正常登录页，单独打开控制台也能用密码登录。

两边用不同的本机端口，浏览器的存储和 Cookie 互不可见，工具端的数据不会被控制台页面读到。

控制台只在窗口里显示入口；单独用 `python run.py --no-window --browser` 打开浏览器时，导航栏也会显示入口，因为控制台服务同样在本机起好了。控制台连不上云端或者没有网络时，只影响控制台页面，审核工具照常使用。页面没有构建过（没有 `admin/web/dist`）时入口不显示。

相关环境变量：

```text
# 云端后台地址。本机的 /v1 请求都转发到这里。
# EVC_ADMIN_SERVER_URL=https://admin.token4u.cc
# 控制台的本机端口，默认 8780。被占用时控制台入口关闭，工具端不受影响。
# EVC_CONSOLE_PORT=8780
# 设为 0 时完全不启动控制台。
# EVC_CONSOLE=0
```

命令行参数 `--no-console` 只关掉这一次的控制台入口。

## 数据放在哪

- 开发（直接 `python run.py`）：项目下的 `data/`
- 打包后的 Windows exe：`%APPDATA%\EcommerceVideoCompliance`
- 打包后的 macOS 应用：`~/Library/Application Support/EcommerceVideoCompliance`
- 也可以用环境变量 `EVC_DATA_DIR` 指定

里面有 `app.db`、上传视频副本、导出文件和 `logs/app.log`。画面识别密钥明文存在本机数据库，不要把 `data/` 或 `.env` 交给别人。登录后的口播凭证只留在内存里。

`.env` 示例：

```text
DEEPSEEK_API_KEY=sk-...
# 只在本机调试口播时填写。登录后的正式口播向 token4u 要临时凭证。
# TENCENTCLOUD_SECRET_ID=
# TENCENTCLOUD_SECRET_KEY=
# TENCENTCLOUD_REGION=ap-shanghai
EVC_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
EVC_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
# EVC_RELAY_GROUP_NAME=电商审核专属-bot创建
# EVC_RELAY_GROUP_ID=
```

设置里保存的密钥优先于环境变量。界面上只显示末四位。

## 使用

1. 登录 token4u.cc 账号。画面请求地址固定为 `https://token4u.cc/v1`，模型在 GLM 5.3 Flash 和 DeepSeek 4.1 Flash 之间选择。
2. 需要口播检测时，打开「口播检测」。程序用登录账号向 token4u 领取临时凭证，再由本机把音频直接发给腾讯云。引擎默认 `16k_zh`（录音文件识别通用引擎，走每月免费资源包）。设置里可以改成大模型 2.0 的 `16k_zh_en_2.0`。可以随时关掉口播，只检测画面。
3. 拖入视频。抽帧可在 0.5–2 FPS 之间调，并发在 1–20 之间，默认 10。
4. 进度会同时显示抽帧、画面识别和口播阶段。画面检测与音频识别并行。
5. 点击表格或时间轴，视频跳到对应时间。口播条目在时间轴下半部分。
6. 导出 JSON 或 Excel。一次识别多个视频时，可以批量导出成一个 Excel。文件同时写在数据目录的 `exports/`。
7. 任务结束后，中间 jpg 和临时音频会删除。视频副本和结果留下，方便回看历史。没有口播凭证或口播识别失败时，画面结果仍然保留，并在任务上显示原因。有画面帧识别失败时，结果上会出现「重试失败的 N 帧」，只重跑这些帧，口播不再跟着重跑。腾讯转写失败时出现「重试口播转写」。转写已经返回、文字判定失败时出现「重试文字判定」，这一次不会再调用腾讯云，也不会重跑画面。

画面长边默认压到 1280 像素。低细节会再把长边压到不超过 640。价格字很小的时候不要改成「低细节」。

## Windows 打包

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

产物在 `dist\VideoCompliance\VideoCompliance.exe`。把 `ffmpeg.exe` 和 `ffprobe.exe` 放在 exe 旁边，或者保证它们在 PATH 里。程序会先找环境变量 `EVC_FFMPEG` / `EVC_FFPROBE`，再找 exe 同目录，最后找 PATH。

打包是窗口程序，没有黑色控制台。出错时看 `%APPDATA%\EcommerceVideoCompliance\logs\app.log`。开发时用 `python run.py` 可以直接看到报错。

### 打成安装包

先生成上面的目录，再编译安装包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

脚本会依次做：构建程序 → 缺 FFmpeg 时自动下载到 `dist\VideoCompliance\` → 编译 `packaging\installer.iss`。产物是单个 `dist\VideoCompliance-<版本>-Setup.exe`，用户双击安装即可。需要 [Inno Setup](https://jrsoftware.org/isdl.php)（6.6 或更新版本），没有时会提示安装命令。

安装行为：

- 安装时可以选择目录，按当前用户安装，不需要管理员权限。全新安装会从 D 到 Z 找第一个固定磁盘，默认放到该盘的 `VideoCompliance`。只有 C 盘时才用 `%LOCALAPPDATA%\Programs\VideoCompliance`，页面上可以改。升级会带出上次的目录，也可以改。
- `AppId` 固定，重复运行新版本安装包会覆盖所选目录里的旧版本，不会出现两份；安装前会自动关闭正在运行的程序。
- 旧版本的 `_internal` 目录会先删掉，避免残留的旧模块被加载。
- 数据在 `%APPDATA%\EcommerceVideoCompliance`，在安装目录之外，升级和卸载都不动它。
- 安装包自带 `ffmpeg.exe` 和 `ffprobe.exe`，用户不用自己装 FFmpeg。
- 安装完成页会询问是否删除安装包。点「删除」后，安装程序关闭时删掉这个安装包；点「保留」则留下。静默安装不询问，也不删除。
- 静默安装：`VideoCompliance-<版本>-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`。不写 `/DIR` 时用上面的默认目录。静默模式装完不会自动启动程序。
- Win10 缺少 WebView2 时，安装程序会提示并打开微软下载页。

程序图标和安装包图标都用 `packaging/app.ico`，由 `frontend/public/logo.png` 生成；换 logo 后重新生成这个 ico 即可。

版本号取自 `app/__init__.py` 的 `__version__`，发新版本前先改这里。没有代码签名证书时，用户首次运行安装包会看到 SmartScreen 提示，点「更多信息 → 仍要运行」可以继续。

GitHub Actions 的 `Windows package` 工作流会顺带编译安装包，并静默安装一次做冒烟检查，产物名为 `VideoCompliance-windows-setup`。

## macOS 打包

在 Mac 上执行，打出来的是本机架构的应用，不能用来生成 Windows exe。

```bash
scripts/build_mac.sh
```

产物是 `dist/VideoCompliance.app`，双击打开。脚本会做临时签名，方便在这台电脑上直接试。第一次若被系统拦住，再执行 `xattr -cr dist/VideoCompliance.app`，或在访达里按住 Control 点「打开」。

FFmpeg 用 `brew install ffmpeg` 即可。从访达打开时，程序会查找 `.app` 里面、`.app` 旁边，以及 `/opt/homebrew/bin` 和 `/usr/local/bin`。也可以设置 `EVC_FFMPEG` 和 `EVC_FFPROBE`。

打包后的数据和开发时的 `data/` 分开。要重新登录。默认仍听 `127.0.0.1:8765`，这个端口被占用时窗口会马上退出，日志在 `~/Library/Application Support/EcommerceVideoCompliance/logs/`。发给别人的应用没有开发者签名，对方的 Mac 可能拒绝打开。

## 剪辑预留

`app/core/editor.py` 的 `plan_cuts` 和 `render_clean_video` 是「一键生成无违规视频」的入口。画面命中向两侧加边距。口播只切被引用的那几个字：句尾再覆盖大约一个字的尾音；这几个字后面有停顿时，切在停顿上，避免把上一句的最后一个字留在下一句开头。整句时间不放进删除段。然后用 FFmpeg 把剩下的画面和音轨重新编码拼接。

检测完成后点「剪辑」，会按上面的规则把删除段摆到时间轴上，这一步不编码。先点选一段，再用「设为起点」「设为终点」或 0.1 秒按钮微调；放大后也可以拖把手。点「生成成片」才重新编码。预览可以听原片、循环这一段，或按保留段跳过。接口是 `POST /api/jobs/{id}/edit/plan`、`PUT /api/jobs/{id}/cuts`、`POST /api/jobs/{id}/cut/render` 和 `GET /api/jobs/{id}/strip/{index}`。`POST /api/jobs/{id}/cut` 仍会按边距直接出片。一次上传多个视频用 `POST /api/jobs/batch`，排队识别，同一时间只处理一个。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

不访问模型接口或腾讯云。有 FFmpeg 时会生成一个很短的测试视频，跑通抽帧、模拟画面识别、模拟口播、聚合、导出、清理和实验剪辑。

## 口播检测

音频用 FFmpeg 抽成 16kHz 单声道。本地文件通过录音文件识别的 `CreateRecTask`（`SourceType=1`）上传，再轮询 `DescribeTaskStatus` 取带时间戳的句子。单段音频超过约 4.5MB 时按时间切开，时间戳加回视频里的起点。口播文本交给当前选中的画面模型做文本判定，原文对不上句子的会被丢掉。

口播临时凭证不会写进任务记录。日志里的 SecretId 和签名会打码。海外账号可以把 `TENCENT_ASR_HOST` 设成对应域名。

## 限制

- 不读外挂字幕。口播只看腾讯云转写结果，转写错了，判定也会跟着错。
- 设置里的「闪帧检测」默认关闭。打开后，只出现一两帧就消失的后期贴字会单独抽出来，时间用这一帧自己的起止；镜头一直在动时，还会把变化周围的一块放大再看。关闭后只按原来的抽帧。短于一帧、没有写进成片像素的字仍然看不到。字太小、反光、被挡住时，会标成「疑似」或者不报。
- 模型仍可能看错。导出结果需要人工看一眼再决定能不能发。
- 同一时刻只识别一个视频。可以一次选多个，其余在队列里等，最多排 20 个。
- 窗口只直接播放 H.264、8 位、4:2:0 的 MP4，以及 VP8/VP9 的 WebM。其他 MP4（例如 H.265、10 位色、4:4:4）会另转一份预览再播，识别和剪辑仍用原片。
- 剪辑会重新编码，长视频要花时间，画质也不是无损剪切。
