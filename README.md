# 电商视频素材合规检测

本地桌面工具。上传电商视频后，按可配置帧率抽帧，调用 DeepSeek V4.1 Flash（接口模型名 `deepseek-flash`）读取画面里的价格、规格、品牌。开启口播检测时，同时用 FFmpeg 抽出音频，调用腾讯云录音文件识别，再用同一套规则判断口播。画面和口播并行处理，相邻命中收成时间区间。

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
│   │   ├── deepseek.py         # deepseek-flash 画面与口播文本判定
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
├── scripts/build_windows.ps1
└── tests/
```

## 识别规则

模型只看单帧画面，或只看腾讯云转写出来的口播句子。提示词禁止根据常识或外观推测。解析时会丢掉这些结果：

- 类别不是价格、规格、品牌
- 原文为空、只写「无」，或长得不像画面文字
- 判定理由里出现「推测」「猜测」「常识」「应该是」

相邻帧上的同一处原文会合成一条。中间空了一帧就分成两段，避免把闪一下的价格拉成整段。画面和口播即使原文相同也分成两条，用来源区分。导出的每一条是：

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

设置里的「审核规则」可以关掉某一类、增加必须命中的原文、忽略指定原文，或写一段补充说明。补充说明不能放宽「禁止推测」。口播里的额外命中词还会在本地按原句精确匹配；画面仍只相信模型在这一帧里读到的文字。忽略词和原文完全一致，或原文只是忽略词里的一段时，这条不报告。

## 环境

- Windows 10/11，或用于开发的 Linux / macOS
- Python 3.11+
- Node.js 18+（只在构建界面时需要）
- FFmpeg 5+，并且 `ffmpeg`、`ffprobe` 在 PATH 中
- 能访问 `https://api.deepseek.com`
- 口播检测还要能访问 `https://asr.tencentcloudapi.com`，并准备腾讯云 SecretId / SecretKey
- Windows 桌面窗口使用系统 WebView2（Win10/11 一般已经带了）

DeepSeek 控制台创建 API Key。视觉请求使用 Chat Completions，图片用 base64。默认关闭思考模式，避免每帧都先输出长推理。

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

## 数据放在哪

- 开发（直接 `python run.py`）：项目下的 `data/`
- 打包后的 exe：`%APPDATA%\EcommerceVideoCompliance`
- 也可以用环境变量 `EVC_DATA_DIR` 指定

里面有 `app.db`、上传视频副本、导出文件和 `logs/app.log`。DeepSeek API Key 和腾讯云密钥明文存在本机数据库，不要把 `data/` 或 `.env` 交给别人。

`.env` 示例：

```text
DEEPSEEK_API_KEY=sk-...
TENCENTCLOUD_SECRET_ID=AKID...
TENCENTCLOUD_SECRET_KEY=...
TENCENTCLOUD_REGION=ap-shanghai
EVC_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
EVC_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
```

设置里保存的密钥优先于环境变量。界面上只显示末四位。

## 使用

1. 打开设置，填写 DeepSeek API Key。模型保持 `deepseek-flash`，接口地址保持 `https://api.deepseek.com`。
2. 需要口播检测时，打开「口播检测」，填写腾讯云 SecretId、SecretKey。地域默认 `ap-shanghai`，引擎默认 `16k_zh`。可以随时关掉口播，只检测画面。
3. 拖入视频。抽帧可在 0.5–2 FPS 之间调，并发默认 4。
4. 进度会同时显示抽帧、画面识别和口播阶段。画面检测与音频识别并行。
5. 点击表格或时间轴，视频跳到对应时间。口播条目在时间轴下半部分。
6. 导出 JSON 或 Excel。文件同时写在数据目录的 `exports/`。
7. 任务结束后，中间 jpg 和临时音频会删除。视频副本和结果留下，方便回看历史。没有腾讯云密钥或口播识别失败时，画面结果仍然保留，并在任务上显示原因。

画面长边默认压到 1280 像素。DeepSeek 还会把图片缩到大约 1300×1300 的像素量，再放大本地帧不会增加识别细节，只会变慢。价格字很小的时候不要改成「低细节」。

## Windows 打包

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

产物在 `dist\VideoCompliance\VideoCompliance.exe`。把 `ffmpeg.exe` 和 `ffprobe.exe` 放在 exe 旁边，或者保证它们在 PATH 里。程序会先找环境变量 `EVC_FFMPEG` / `EVC_FFPROBE`，再找 exe 同目录，最后找 PATH。

打包是窗口程序，没有黑色控制台。出错时看 `%APPDATA%\EcommerceVideoCompliance\logs\app.log`。开发时用 `python run.py` 可以直接看到报错。

## 剪辑预留

`app/core/editor.py` 的 `plan_cuts` 和 `render_clean_video` 是「一键生成无违规视频」的入口。画面命中向两侧加边距。口播只切被引用的那几个字：句尾再覆盖大约一个字的尾音；这几个字后面有停顿时，切在停顿上，避免把上一句的最后一个字留在下一句开头。整句时间不放进删除段。然后用 FFmpeg 把剩下的画面和音轨重新编码拼接。

检测完成后点「剪辑」，会按上面的规则把删除段摆到时间轴上，这一步不编码。先点选一段，再用「设为起点」「设为终点」或 0.1 秒按钮微调；放大后也可以拖把手。点「生成成片」才重新编码。预览可以听原片、循环这一段，或按保留段跳过。接口是 `POST /api/jobs/{id}/edit/plan`、`PUT /api/jobs/{id}/cuts`、`POST /api/jobs/{id}/cut/render` 和 `GET /api/jobs/{id}/strip/{index}`。`POST /api/jobs/{id}/cut` 仍会按边距直接出片。一次上传多个视频用 `POST /api/jobs/batch`，排队识别，同一时间只处理一个。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

不访问 DeepSeek 或腾讯云。有 FFmpeg 时会生成一个很短的测试视频，跑通抽帧、模拟画面识别、模拟口播、聚合、导出、清理和实验剪辑。

## 口播检测

音频用 FFmpeg 抽成 16kHz 单声道。本地文件通过录音文件识别的 `CreateRecTask`（`SourceType=1`）上传，再轮询 `DescribeTaskStatus` 取带时间戳的句子。单段音频超过约 4.5MB 时按时间切开，时间戳加回视频里的起点。口播文本交给 `deepseek-flash` 做文本判定，原文对不上句子的会被丢掉。

腾讯云密钥不会写进任务记录，日志里的 SecretId 和签名会打码。海外账号可以把 `TENCENT_ASR_HOST` 设成对应域名。

## 限制

- 不读外挂字幕。口播只看腾讯云转写结果，转写错了，判定也会跟着错。
- 小于抽帧间隔的闪现文字可能漏掉。字太小、反光、被挡住时，会标成「疑似」或者不报。
- 模型仍可能看错。导出结果需要人工看一眼再决定能不能发。
- 同一时刻只识别一个视频。可以一次选多个，其余在队列里等，最多排 20 个。
- 预览对 H.264 MP4 最稳。mkv 等封装即使窗口播不了，识别仍然进行。
- 剪辑会重新编码，长视频要花时间，画质也不是无损剪切。
