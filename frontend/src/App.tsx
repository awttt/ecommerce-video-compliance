import {
  CaretRightOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  DownloadOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  HomeOutlined,
  PauseOutlined,
  ScissorOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Select,
  Slider,
  Space,
  Steps,
  Switch,
  Spin,
  Tooltip,
  Typography,
  Upload,
  App as AntApp,
} from "antd";
import { useEffect, useRef, useState } from "react";
import { SharedRulesPanel } from "./SharedRules";
import {
  cancelJob,
  clearQueue,
  createJobs,
  deleteAllJobs,
  deleteJob,
  downloadBatchExport,
  downloadExport,
  getHealth,
  getJob,
  getQueue,
  getSettings,
  listJobs,
  pauseQueue,
  resumeQueue,
  retryFailedFrames,
  retrySpeech,
  openDir,
  planEdit,
  renderEdit,
  saveCuts,
  saveSettings,
} from "./api";
import { LoginPage, RegisterPage } from "./AuthPages";
import { mcpAgentPrompt, mcpEndpoint } from "./mcpPrompt";
import { showTestMenu } from "./testMenu";
import { CutTimeline } from "./CutTimeline";
import {
  directoryEntries,
  folderImportMessage,
  isVideoName,
  MAX_PICKED,
  mergePicked,
  pickedKey,
  videosFromEntries,
  type PickedVideo,
} from "./dropVideos";
import { batchElapsed, jobElapsed } from "./elapsed";
import { FindingsTable, type FindingsTableHandle } from "./FindingsTable";
import { findingKey } from "./findingsView";
import { draftsFromPhrases, groupKey, phrasesFromDrafts, RULE_GROUPS } from "./ruleForm";
import { CATEGORY_COLOR, TERMINAL, type CutSpan, type Finding, type Job, type Settings } from "./types";
import { VideoPane, type PlayerControls } from "./VideoPane";

const { Dragger } = Upload;
const { Text, Paragraph } = Typography;

const DEFAULT_CONCURRENCY = 10;
const MAX_CONCURRENCY = 20;

function boundedConcurrency(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_CONCURRENCY;
  return Math.min(MAX_CONCURRENCY, Math.max(1, Math.round(value)));
}

const MODEL_OPTIONS = [
  { value: "glm-5.3-flash", label: "GLM 5.3 Flash" },
  { value: "deepseek-flash", label: "DeepSeek 4.1 Flash" },
  { value: "qwen3.8-flash", label: "Qwen 3.8 Flash" },
];

/** 剪辑入口和 JSON 导出先不露出，功能代码保留。 */
const SHOW_JSON_EXPORT = false;
const SHOW_CUT_BUTTON = false;

const ASR_ENGINE_OPTIONS = [
  { value: "16k_zh", label: "通用引擎 16k_zh（免费资源包）" },
  { value: "16k_zh_en_2.0", label: "大模型 2.0（16k_zh_en_2.0）" },
];

export function BrandMark() {
  return <img className="brand-logo" src="/logo.png" alt="" />;
}

function statusTag(status: string) {
  if (status === "done") return <span className="pill pill--confirmed"><i className="status-dot" />完成</span>;
  if (status === "failed") return <span className="pill pill--error"><i className="status-dot" />失败</span>;
  if (status === "cancelled") return <span className="pill pill--neutral">已取消</span>;
  if (status === "paused") return <span className="pill pill--neutral">已暂停</span>;
  if (status === "queued") return <span className="pill pill--active"><i className="status-dot" />排队</span>;
  if (status === "running") return <span className="pill pill--active"><i className="status-dot" />识别中</span>;
  return <span className="pill pill--neutral">{status}</span>;
}

function coversDuration(cuts: CutSpan[], duration: number | null): boolean {
  if (!duration || cuts.length === 0) return false;
  const ordered = [...cuts].sort((left, right) => left.start_sec - right.start_sec);
  let cursor = 0;
  for (const cut of ordered) {
    if (cut.start_sec - cursor >= 0.12) return false;
    cursor = Math.max(cursor, cut.end_sec);
  }
  return duration - cursor < 0.12;
}

function sameCuts(left: CutSpan[], right: CutSpan[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((cut, index) => cut.start_sec === right[index]?.start_sec && cut.end_sec === right[index]?.end_sec);
}

function previewMime(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "webm") return "video/webm";
  if (ext === "ogg" || ext === "ogv") return "video/ogg";
  return "video/mp4";
}

function canRetryFrames(job: Job): boolean {
  return (job.status === "failed" || job.status === "done") && job.failed_frames > 0;
}

function retryNotice(job: Job): string {
  if (job.error === "interrupted") return "程序退出时任务未完成";
  if (job.status === "failed") return job.error || job.message || `部分帧识别失败（${job.failed_frames} 帧）`;
  if (job.message.includes("部分帧识别失败")) return job.message;
  return `部分帧识别失败（${job.failed_frames} 帧）`;
}

function speechRetryNotice(job: Job): string {
  if (job.speech_retry === "transcribe") {
    return job.asr_error || job.speech_message || "口播转写失败，可只重试转写";
  }
  if (job.speech_message.includes("已按词表保留")) return job.speech_message;
  return job.speech_message || job.asr_error || "口播文字判定失败，可只重试判定";
}

function stepIndex(job: Job | null): number {
  if (!job) return 0;
  if (job.stage === "done" || job.status === "done") return 3;
  if (job.stage === "aggregating") return 2;
  if (job.frames_recognized > 0 || job.stage === "recognizing") return 1;
  return 0;
}

function mcpStatus(enabled: boolean, live: boolean | null): { type: "success" | "info" | "warning"; text: string } {
  if (live === null) return { type: "info", text: "还没确认这次运行有没有开放给 agent。" };
  if (live) return { type: "success", text: "这次运行已经开放给 agent，可以直接连接。" };
  if (enabled) return { type: "warning", text: "开关已保存。重新启动程序后，agent 才能连上。" };
  return { type: "info", text: "这次运行没有开放给 agent。打开开关并重新启动程序后再生效。" };
}

function McpDrawer({
  enabled,
  live,
  saving,
  onToggle,
}: {
  enabled: boolean;
  live: boolean | null;
  saving: boolean;
  onToggle: (checked: boolean) => Promise<void>;
}) {
  const { message } = AntApp.useApp();
  const url = mcpEndpoint(window.location);
  const prompt = mcpAgentPrompt(url);
  const status = mcpStatus(enabled, live);
  return (
    <>
      <Paragraph type="secondary">
        打开后，本机 agent 可以提交视频或扫描一个文件夹，跑完后拿回批量报告。开关保存在本机，重新启动程序后才生效。
      </Paragraph>
      <div className="mcp-switch-row">
        <Text>允许 agent 调用</Text>
        <Switch checked={enabled} loading={saving} onChange={(checked) => void onToggle(checked)} />
      </div>
      <Alert type={status.type} showIcon message={status.text} style={{ marginBottom: 16 }} />
      <Text strong>发给 agent 的提示语</Text>
      <Paragraph type="secondary" style={{ marginTop: 4 }}>
        复制下面整段，发给正在用的 agent，让它把这个地址配成 MCP。地址是 {url}。
      </Paragraph>
      <Input.TextArea className="mcp-prompt" readOnly rows={16} value={prompt} />
      <Button
        type="primary"
        style={{ marginTop: 12 }}
        onClick={() => {
          navigator.clipboard.writeText(prompt).then(
            () => message.success("已复制，发给 agent 即可"),
            () => message.error("复制失败，请手动选择提示语"),
          );
        }}
      >
        复制提示语
      </Button>
    </>
  );
}

const ACCOUNT_KEY_NOTICE = "当前还不能识别画面，请联系管理员处理。";

function accountKeyNotice(text: string): string {
  if (
    text.includes("请联系管理员") ||
    text.includes("画面识别密钥") ||
    text.includes("可用分组") ||
    text.includes("专属分组")
  ) {
    return ACCOUNT_KEY_NOTICE;
  }
  return "";
}

export function Shell() {
  const { message } = AntApp.useApp();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [keyNotice, setKeyNotice] = useState("");
  const [authPage, setAuthPage] = useState<"login" | "register">("login");
  const [bootError, setBootError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [retryingId, setRetryingId] = useState("");
  const [fps, setFps] = useState(1);
  const [concurrency, setConcurrency] = useState(DEFAULT_CONCURRENCY);
  const [padding, setPadding] = useState(0.2);
  const [picked, setPicked] = useState<PickedVideo[]>([]);
  const pickedRef = useRef<PickedVideo[]>([]);
  const directoryNamesRef = useRef<Set<string>>(new Set());
  const overflowRef = useRef(0);
  const overflowTimer = useRef<number | null>(null);
  const dropRef = useRef<HTMLDivElement>(null);
  const uploadingRef = useRef(false);
  const scanningRef = useRef(false);
  const [batch, setBatch] = useState<Job[]>([]);
  const [uploading, setUploading] = useState(false);
  const [scanning, setScanning] = useState(false);
  uploadingRef.current = uploading;
  scanningRef.current = scanning;
  const [rulesOpen, setRulesOpen] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [testOpen, setTestOpen] = useState(false);
  const [mcpSaving, setMcpSaving] = useState(false);
  const [mcpLive, setMcpLive] = useState<boolean | null>(null);
  const [ruleCategories, setRuleCategories] = useState<string[]>(["价格", "规格", "品牌", "夸大宣传"]);
  const [phraseDrafts, setPhraseDrafts] = useState<Record<string, string>>({});
  const [ignoreText, setIgnoreText] = useState("");
  const [ruleNote, setRuleNote] = useState("");
  const [source, setSource] = useState<"media" | "output">("media");
  const [activeKey, setActiveKey] = useState("");
  const findingsRef = useRef<FindingsTableHandle>(null);
  const [editorOn, setEditorOn] = useState(false);
  const [cuts, setCuts] = useState<CutSpan[]>([]);
  const [previewMode, setPreviewMode] = useState<"original" | "segment" | "keep">("keep");
  const [selectedCut, setSelectedCut] = useState<CutSpan | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [editAction, setEditAction] = useState<"plan" | "render" | "reset" | null>(null);
  const [fileAction, setFileAction] = useState<"json" | "xlsx" | "batch-xlsx" | "dir" | null>(null);
  const [cancellingJob, setCancellingJob] = useState(false);
  const [queuePaused, setQueuePaused] = useState(false);
  const [queueAction, setQueueAction] = useState<"pause" | "resume" | null>(null);
  const [clearingHistory, setClearingHistory] = useState(false);
  const [clearingQueue, setClearingQueue] = useState(false);
  const editBusy = editAction !== null;
  const seekRef = useRef<PlayerControls["seek"]>(() => undefined);
  const defaultsApplied = useRef(false);
  const seenJob = useRef<string | null>(null);
  const saveTimer = useRef(0);
  const saveToken = useRef(0);
  const cutsRef = useRef(cuts);
  const previewRef = useRef(previewMode);
  const selectedCutRef = useRef(selectedCut);
  const sourceRef = useRef(source);
  const scrubRef = useRef(false);
  const editorOnRef = useRef(editorOn);
  const lastTime = useRef(0);
  const pendingSeek = useRef<number | null>(null);
  cutsRef.current = cuts;
  previewRef.current = previewMode;
  selectedCutRef.current = selectedCut;
  sourceRef.current = source;
  editorOnRef.current = editorOn;
  const [form] = Form.useForm();

  async function refreshSettings() {
    const next = await getSettings();
    setSettings(next);
    return next;
  }

  async function refreshHistory() {
    setHistory(await listJobs());
  }

  async function retryFrames(id: string) {
    setRetryingId(`${id}:frames`);
    try {
      const next = await retryFailedFrames(id);
      setJob(next);
      await refreshHistory();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重试失败");
    } finally {
      setRetryingId("");
    }
  }

  async function retrySpeechPart(id: string, part: "transcribe" | "judge") {
    setRetryingId(`${id}:speech`);
    try {
      const next = await retrySpeech(id, part);
      setJob(next);
      await refreshHistory();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重试失败");
    } finally {
      setRetryingId("");
    }
  }

  useEffect(() => {
    refreshSettings().catch((error: Error) => {
      setBootError(error.message);
      message.error(error.message);
    });
  }, [message]);

  const loggedIn = Boolean(settings?.account_logged_in);
  useEffect(() => {
    if (!loggedIn) return undefined;
    let stop = false;
    listJobs()
      .then((jobs) => {
        if (stop) return;
        setHistory(jobs);
        const active = jobs
          .filter((item) => !TERMINAL.includes(item.status))
          .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
        if (active.length === 0) return;
        setBatch((current) => (current.length > 0 ? current : active));
        setJob(
          (current) => current ?? active.find((item) => item.status === "paused" || item.status === "running") ?? null,
        );
      })
      .catch(() => undefined);
    getQueue()
      .then((state) => {
        if (!stop) setQueuePaused(state.paused);
      })
      .catch(() => undefined);
    return () => {
      stop = true;
    };
  }, [loggedIn]);

  useEffect(() => {
    if (!settings || defaultsApplied.current) return;
    defaultsApplied.current = true;
    setFps(settings.fps);
    setConcurrency(boundedConcurrency(settings.concurrency));
  }, [settings]);

  useEffect(() => {
    if (!settings) return;
    form.setFieldsValue({
      fps: settings.fps,
      concurrency: boundedConcurrency(settings.concurrency),
      max_width: settings.max_width,
      jpeg_q: settings.jpeg_q,
      detail: settings.detail,
      thinking: settings.thinking,
      motion_enabled: settings.motion_enabled,
      timeout_sec: settings.timeout_sec,
      asr_enabled: settings.asr_enabled,
      model: settings.model,
      asr_engine: settings.asr_engine,
    });
  }, [form, settings]);

  function applyRuleForm(rules: Settings["rules"]) {
    setRuleCategories(rules.categories);
    setPhraseDrafts(draftsFromPhrases(rules.extra_phrases));
    setIgnoreText(rules.ignore_phrases.join("\n"));
    setRuleNote(rules.note);
  }

  useEffect(() => {
    if (!settings?.rules || rulesOpen) return;
    applyRuleForm(settings.rules);
  }, [settings, rulesOpen]);

  useEffect(() => {
    if (!mcpOpen) return;
    let cancelled = false;
    getHealth()
      .then((health) => {
        if (!cancelled) setMcpLive(Boolean(health.mcp));
      })
      .catch(() => {
        if (!cancelled) setMcpLive(null);
      });
    return () => {
      cancelled = true;
    };
  }, [mcpOpen]);

  useEffect(() => {
    if (!job) {
      seenJob.current = null;
      return;
    }
    if (seenJob.current === job.id) return;
    seenJob.current = job.id;
    window.clearTimeout(saveTimer.current);
    setActiveKey("");
    setPreviewMode("keep");
    setSelectedCut(null);
    setCurrentTime(0);
    lastTime.current = 0;
    setEditorOn(Boolean(job.has_edit_plan));
    setCuts(job.cuts ?? []);
  }, [job]);

  const jobId = job?.id;
  const jobStatus = job?.status;
  const previewStatus = job?.preview_status;
  const previewWaiting = previewStatus === "pending";
  useEffect(() => {
    const terminal = jobStatus != null && TERMINAL.includes(jobStatus);
    if (!jobId || (terminal && !previewWaiting)) return undefined;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      try {
        const next = await getJob(jobId);
        if (stop) return;
        setJob(next);
        const stillWaiting = next.preview_status === "pending";
        if (!TERMINAL.includes(next.status) || stillWaiting) window.setTimeout(tick, 700);
        else refreshHistory().catch(() => undefined);
      } catch (error) {
        const text = error instanceof Error ? error.message : "";
        if (text.includes("请先登录") || text.includes("请重新登录")) {
          refreshSettings().catch(() => undefined);
          return;
        }
        if (!stop) window.setTimeout(tick, 1500);
      }
    };
    const starter = window.setTimeout(tick, 400);
    return () => {
      stop = true;
      window.clearTimeout(starter);
    };
  }, [jobId, jobStatus, previewWaiting]);

  useEffect(() => {
    if (!job) return;
    setBatch((current) => current.map((item) => (item.id === job.id ? job : item)));
  }, [job]);

  const batchKey = batch.map((item) => `${item.id}:${item.status}`).join("|");
  useEffect(() => {
    const pending = batch.filter((item) => item.id !== jobId && !TERMINAL.includes(item.status));
    if (pending.length === 0) return undefined;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      const updated = await Promise.all(
        pending.map(async (item) => {
          try {
            return await getJob(item.id);
          } catch {
            return item;
          }
        }),
      );
      if (stop) return;
      setBatch((current) => current.map((item) => updated.find((next) => next.id === item.id) ?? item));
      if (updated.some((item) => !TERMINAL.includes(item.status))) window.setTimeout(tick, 800);
      else refreshHistory().catch(() => undefined);
    };
    const starter = window.setTimeout(tick, 800);
    return () => {
      stop = true;
      window.clearTimeout(starter);
    };
  }, [batchKey, jobId]);

  const running = job != null && !TERMINAL.includes(job.status);
  const hasActiveWork =
    running ||
    batch.some((item) => !TERMINAL.includes(item.status)) ||
    history.some((item) => !TERMINAL.includes(item.status));
  const [now, setNow] = useState(() => Date.now());
  const clockOn =
    running ||
    batch.some((item) => !TERMINAL.includes(item.status)) ||
    history.some((item) => !TERMINAL.includes(item.status));
  useEffect(() => {
    if (!clockOn) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [clockOn]);
  const currentElapsed = job ? jobElapsed(job, now) : null;
  const totalElapsed = batchElapsed(batch, now);
  const previewBlocked = job?.preview_status === "pending" || job?.preview_status === "failed";
  const mediaUrl =
    job == null
      ? null
      : source === "output" && job.output_ready
        ? `/api/jobs/${job.id}/output`
        : previewBlocked
          ? null
          : `/api/jobs/${job.id}/media`;
  const mediaMime =
    source === "output" || job?.preview_status === "ready" ? "video/mp4" : previewMime(job?.filename || "");
  const uploadReady = Boolean(settings?.account_logged_in);

  function commitPicked(next: PickedVideo[]) {
    pickedRef.current = next;
    setPicked(next);
  }

  function addIncoming(incoming: PickedVideo[]) {
    const merged = mergePicked(pickedRef.current, incoming);
    commitPicked(merged.next);
    return merged;
  }

  function warnOverflow(count: number) {
    if (count <= 0) return;
    overflowRef.current += count;
    if (overflowTimer.current != null) return;
    overflowTimer.current = window.setTimeout(() => {
      overflowTimer.current = null;
      const pending = overflowRef.current;
      overflowRef.current = 0;
      if (pending > 0) message.warning(`一次最多 ${MAX_PICKED} 个视频，还有 ${pending} 个没有加入`);
    }, 0);
  }

  useEffect(() => {
    const node = dropRef.current;
    if (!node || !uploadReady) return undefined;
    let cancelled = false;
    // 捕获阶段先于内部的文件输入。先记下文件夹，等这一轮把同批的普通文件收完，再递归读目录。
    const onDrop = (event: DragEvent) => {
      const directories = directoryEntries(event.dataTransfer?.items);
      if (directories.length === 0) return;
      event.preventDefault();
      directoryNamesRef.current = new Set(directories.map((entry) => entry.name));
      window.setTimeout(() => {
        directoryNamesRef.current = new Set();
        if (cancelled || uploadingRef.current || scanningRef.current) return;
        scanningRef.current = true;
        setScanning(true);
        void videosFromEntries(directories)
          .then((videos) => {
            if (cancelled) return;
            const merged = addIncoming(videos);
            const notice = folderImportMessage(videos.length, merged);
            message[notice.type](notice.text);
          })
          .catch(() => {
            if (!cancelled) message.error("读取文件夹失败");
          })
          .finally(() => {
            scanningRef.current = false;
            if (!cancelled) setScanning(false);
          });
      }, 0);
    };
    node.addEventListener("drop", onDrop, true);
    return () => {
      cancelled = true;
      scanningRef.current = false;
      node.removeEventListener("drop", onDrop, true);
    };
  }, [message, uploadReady]);

  async function changeQueue(pause: boolean) {
    if (queueAction) return;
    setQueueAction(pause ? "pause" : "resume");
    try {
      const next = pause ? await pauseQueue() : await resumeQueue();
      setQueuePaused(next.paused);
      const pendingIds = new Set(batch.filter((item) => !TERMINAL.includes(item.status)).map((item) => item.id));
      if (job && !TERMINAL.includes(job.status)) pendingIds.add(job.id);
      if (pendingIds.size > 0) {
        const updated = await Promise.all([...pendingIds].map((id) => getJob(id)));
        const byId = new Map(updated.map((item) => [item.id, item]));
        setBatch((current) => current.map((item) => byId.get(item.id) ?? item));
        if (job && byId.has(job.id)) setJob(byId.get(job.id) ?? job);
      }
      setNow(Date.now());
      refreshHistory().catch(() => undefined);
    } catch (error) {
      message.error(error instanceof Error ? error.message : pause ? "暂停失败" : "开始失败");
    } finally {
      setQueueAction(null);
    }
  }

  async function clearQueued() {
    if (clearingQueue) return;
    setClearingQueue(true);
    try {
      const result = await clearQueue();
      setBatch((current) => current.filter((item) => TERMINAL.includes(item.status)));
      setHistory((current) => current.filter((item) => TERMINAL.includes(item.status)));
      setJob((current) => (current && !TERMINAL.includes(current.status) ? null : current));
      message.success(result.deleted > 0 ? `已清空 ${result.deleted} 个排队视频` : "队列是空的");
      refreshHistory().catch(() => undefined);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "清空队列失败");
      throw error;
    } finally {
      setClearingQueue(false);
    }
  }

  async function clearHistory() {
    if (clearingHistory) return;
    setClearingHistory(true);
    try {
      const result = await deleteAllJobs();
      setJob(null);
      setBatch([]);
      setHistory([]);
      setSource("media");
      message.success(result.deleted > 0 ? `已删除 ${result.deleted} 条历史` : "没有可删除的历史");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除历史失败");
      throw error;
    } finally {
      setClearingHistory(false);
    }
  }

  async function startBatch() {
    if (picked.length === 0) return;
    const formData = new FormData();
    picked.forEach((item) => formData.append("files", item.file));
    formData.append("fps", String(fps));
    formData.append("concurrency", String(concurrency));
    setUploading(true);
    try {
      const created = await createJobs(formData);
      commitPicked([]);
      setBatch((current) => {
        const incoming = new Set(created.jobs.map((item) => item.id));
        const older = current.filter((item) => !incoming.has(item.id) && !TERMINAL.includes(item.status));
        return [...created.jobs, ...older];
      });
      setSource("media");
      setActiveKey("");
      if (created.jobs[0]) setJob(created.jobs[0]);
      if (created.errors.length > 0) {
        message.warning(created.errors.map((item) => `${item.filename || "文件"}：${item.detail}`).join("；"));
      }
      if (created.jobs.length > 0) {
        message.success(created.jobs.length > 1 ? `已加入队列，共 ${created.jobs.length} 个视频` : "已开始检测");
      }
      refreshHistory().catch(() => undefined);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  }

  function seekOriginal(sec: number, play = false) {
    if (play) {
      scrubRef.current = true;
      window.setTimeout(() => {
        scrubRef.current = false;
      }, 400);
    }
    if (sourceRef.current !== "media") {
      sourceRef.current = "media";
      pendingSeek.current = sec;
      setSource("media");
      return;
    }
    seekRef.current(sec, { play });
  }

  function rememberCuts(next: CutSpan[]) {
    setCuts(next);
    if (!job) return;
    const token = ++saveToken.current;
    const id = job.id;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      saveCuts(id, next)
        .then((saved) => {
          if (saveToken.current !== token) return;
          setCuts(saved.cuts);
          setJob(saved);
        })
        .catch((error: Error) => message.error(error.message));
    }, 500);
  }

  function handleTime(sec: number, playing: boolean) {
    const previous = lastTime.current;
    lastTime.current = sec;
    if (editorOnRef.current) setCurrentTime(sec);
    if (sourceRef.current !== "media" || !playing || scrubRef.current || !editorOnRef.current) return;
    if (sec < previous || sec - previous > 0.45) return;
    if (previewRef.current === "keep") {
      const hit = cutsRef.current.find((cut) => previous < cut.start_sec + 0.02 && sec >= cut.start_sec && sec < cut.end_sec - 0.03);
      if (!hit) return;
      seekRef.current(hit.end_sec, { play: true });
      return;
    }
    if (previewRef.current === "segment") {
      const cut = selectedCutRef.current;
      if (!cut || previous >= cut.end_sec || sec < cut.end_sec - 0.04) return;
      seekRef.current(cut.start_sec, { play: true });
    }
  }

  async function enterEditor() {
    if (!job || editAction !== null) return;
    if (job.has_edit_plan) {
      setEditorOn(true);
      setCuts(job.cuts);
      setSource("media");
      return;
    }
    setEditAction("plan");
    try {
      const next = await planEdit(job.id, padding);
      setJob(next);
      setCuts(next.cuts);
      setEditorOn(true);
      setSource("media");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "生成剪辑方案失败");
    } finally {
      setEditAction((prev) => (prev === "plan" ? null : prev));
    }
  }

  async function generateEdit() {
    if (!job || editAction !== null) return;
    const token = ++saveToken.current;
    window.clearTimeout(saveTimer.current);
    setEditAction("render");
    try {
      const saved = await saveCuts(job.id, cutsRef.current);
      if (saveToken.current !== token) return;
      const next = await renderEdit(saved.id);
      if (saveToken.current !== token) return;
      setJob(next);
      setCuts(next.cuts);
      setSource("output");
      message.success("剪辑视频已生成");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "生成成片失败");
    } finally {
      setEditAction((prev) => (prev === "render" ? null : prev));
    }
  }

  async function runFileAction(action: "json" | "xlsx" | "batch-xlsx" | "dir", task: () => Promise<void>) {
    if (fileAction !== null) return;
    setFileAction(action);
    try {
      await task();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setFileAction((prev) => (prev === action ? null : prev));
    }
  }

  function seekTo(finding: Finding) {
    setActiveKey(findingKey(finding));
    seekOriginal(finding.start_sec, true);
  }

  function revealFinding(finding: Finding) {
    findingsRef.current?.reveal(finding);
  }

  async function saveTestFlag(patch: Record<string, boolean>) {
    try {
      setSettings(await saveSettings(patch));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    }
  }

  if (!settings) {
    return (
      <div className="auth-page">
        <Card className="auth-card">
          {bootError ? <Alert type="error" showIcon message={bootError} /> : <div style={{ textAlign: "center" }}><Spin /></div>}
        </Card>
      </div>
    );
  }

  if (!settings.account_logged_in) {
    return authPage === "register" ? (
      <RegisterPage
        onRegistered={(next, notice) => {
          setSettings(next);
          setKeyNotice(accountKeyNotice(notice || ""));
          setAuthPage("login");
        }}
        onGoLogin={() => setAuthPage("login")}
      />
    ) : (
      <LoginPage
        onLoggedIn={(next, notice) => {
          setSettings(next);
          setKeyNotice(accountKeyNotice(notice || ""));
        }}
        onGoRegister={() => setAuthPage("register")}
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <nav className="app-nav">
          <Button type="text" icon={<HomeOutlined />} className="is-active">
            首页
          </Button>
          <Button type="text" icon={<HistoryOutlined />} onClick={() => { setHistoryOpen(true); refreshHistory().catch(() => undefined); }}>
            历史
          </Button>
          <Button
            type="text"
            onClick={() => {
              if (settings?.rules) applyRuleForm(settings.rules);
              setRulesOpen(true);
            }}
          >
            审核规则
          </Button>
          <Button type="text" onClick={() => setMcpOpen(true)}>MCP</Button>
          <Button type="text" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)}>设置</Button>
          {showTestMenu(settings.account_email) ? (
            <Button
              type="text"
              icon={<ExperimentOutlined />}
              className={testOpen ? "is-active" : ""}
              onClick={() => setTestOpen(true)}
            >
              测试
            </Button>
          ) : null}
        </nav>
      </header>
      <div className="page">
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={8}>
            <Card title="上传视频">
              {settings.mock_mode ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="模拟识别已打开，不会请求画面模型和大语言模型，也不会领取口播凭证。" />
              ) : null}
              {settings.fault_bad_json ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="测试：每一帧都会按非法 JSON 失败，任务会整段失败，不会请求模型。" />
              ) : null}
              {settings.fault_tencent ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="测试：口播会在调用腾讯云时失败，画面识别继续，不会真的请求腾讯云。" />
              ) : null}
              {settings.fault_partial_frames ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="测试：第 1、4、7… 帧会调用失败，其余帧照常识别。" />
              ) : null}
              {keyNotice ? (
                <Alert type="error" showIcon style={{ marginBottom: 12 }} message={keyNotice} />
              ) : null}
              {!keyNotice && !settings.has_api_key && !settings.mock_mode ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={ACCOUNT_KEY_NOTICE}
                />
              ) : null}
              <div className="slider-row" style={{ marginBottom: 12 }}>
                <Text>画面模型</Text>
                <Select
                  style={{ width: "100%" }}
                  value={MODEL_OPTIONS.some((item) => item.value === settings.model) ? settings.model : "glm-5.3-flash"}
                  options={MODEL_OPTIONS}
                  disabled={uploading}
                  onChange={(value) => {
                    void saveSettings({ model: value })
                      .then(setSettings)
                      .catch((error: Error) => message.error(error.message));
                  }}
                />
              </div>
              <div ref={dropRef}>
                <Dragger
                  multiple
                  showUploadList={false}
                  disabled={uploading || scanning}
                  accept="video/*,.mp4,.mov,.mkv,.avi,.webm,.m4v,.flv,.wmv,.mpeg,.mpg,.ts"
                  beforeUpload={(file) => {
                    if (uploadingRef.current || scanningRef.current) return Upload.LIST_IGNORE;
                    if (directoryNamesRef.current.has(file.name) || !isVideoName(file.name)) return Upload.LIST_IGNORE;
                    const merged = addIncoming([{ file, label: file.name }]);
                    warnOverflow(merged.overflow);
                    return Upload.LIST_IGNORE;
                  }}
                >
                  <div className="upload-icon"><CloudUploadOutlined /></div>
                  <p className="upload-title">{scanning ? "正在读取文件夹…" : "拖拽视频或文件夹到这里，或点击选择"}</p>
                  <p className="upload-sub">支持 mp4 / mov / mkv / avi / webm 等 · 文件夹会连同子目录里的视频一起加入 · 一次最多 {MAX_PICKED} 个</p>
                </Dragger>
              </div>
              {picked.length > 0 ? (
                <div className="picked-list">
                  {picked.map((item) => (
                    <div className="file-row" key={pickedKey(item)}>
                      <Text ellipsis={{ tooltip: item.label }}>{item.label}</Text>
                      <Button
                        size="small"
                        type="text"
                        disabled={uploading}
                        onClick={() => commitPicked(pickedRef.current.filter((current) => pickedKey(current) !== pickedKey(item)))}
                      >
                        移除
                      </Button>
                    </div>
                  ))}
                </div>
              ) : null}
              <Button
                type="primary"
                block
                className="primary-action"
                loading={uploading}
                disabled={picked.length === 0}
                onClick={() => startBatch().catch(() => undefined)}
              >
                {picked.length > 1 ? `开始识别（${picked.length} 个）` : "开始识别"}
              </Button>
              <div className="queue-actions">
                <Button
                  icon={queuePaused ? <CaretRightOutlined /> : <PauseOutlined />}
                  type={queuePaused ? "primary" : "default"}
                  disabled={queueAction !== null || (!queuePaused && !hasActiveWork)}
                  loading={queueAction !== null}
                  onClick={() => void changeQueue(!queuePaused)}
                >
                  {queuePaused ? "开始" : "暂停"}
                </Button>
              </div>
              <Popconfirm
                title="清空当前队列？"
                description="正在识别和还在排队的视频会停掉，本地副本一起删除。已经完成的记录保留。"
                okText="清空"
                cancelText="取消"
                okButtonProps={{ danger: true, loading: clearingQueue }}
                onConfirm={() => clearQueued()}
              >
                <Button
                  danger
                  block
                  className="queue-clear"
                  icon={<DeleteOutlined />}
                  disabled={!hasActiveWork || clearingQueue || queueAction !== null}
                  loading={clearingQueue}
                >
                  一键清空队列
                </Button>
              </Popconfirm>
              {queuePaused ? (
                <Paragraph className="hint">识别已暂停。点「开始」后从停下的位置继续，排队的视频也会接着跑。</Paragraph>
              ) : null}
              <div className="slider-row">
                <Text>抽帧密度 {fps} FPS</Text>
                <Slider min={0.5} max={2} step={0.1} value={fps} onChange={setFps} disabled={uploading} />
                <Text>并发请求 {concurrency}</Text>
                <Slider
                  min={1}
                  max={MAX_CONCURRENCY}
                  step={1}
                  value={boundedConcurrency(concurrency)}
                  onChange={setConcurrency}
                  disabled={uploading}
                />
              </div>
              {batch.length > 0 ? (
                <div style={{ marginTop: 20 }}>
                  <div className="section-head">
                    <span className="section-label">本批队列</span>
                    {totalElapsed ? <span className="elapsed">总耗时 {totalElapsed}</span> : null}
                  </div>
                  {batch.map((item) => {
                    const spent = jobElapsed(item, now);
                    return (
                      <div
                        className={`queue-row${item.id === job?.id ? " is-selected" : ""}`}
                        key={item.id}
                        role="button"
                        tabIndex={0}
                        aria-current={item.id === job?.id ? "true" : undefined}
                        onClick={() => {
                          if (item.id !== job?.id) setSource("media");
                          setJob(item);
                        }}
                        onKeyDown={(event) => {
                          if (event.key !== "Enter" && event.key !== " ") return;
                          event.preventDefault();
                          if (item.id !== job?.id) setSource("media");
                          setJob(item);
                        }}
                      >
                        <Text ellipsis>{item.filename}</Text>
                        <Space size={4}>
                          {statusTag(item.status)}
                          {spent ? <Text type="secondary">{spent}</Text> : null}
                          {!TERMINAL.includes(item.status) ? <Text type="secondary">{Math.round(item.progress)}%</Text> : null}
                        </Space>
                      </div>
                    );
                  })}
                </div>
              ) : null}
              <Paragraph className="hint">
                多个视频排队识别，同一时间只处理一个。只报告画面里看得见的文字和口播里明确说出的内容，不根据外观推测。结束后会删除中间图片和临时音频。
              </Paragraph>
            </Card>
          </Col>
          <Col xs={24} lg={16}>
            <Card
              title={job ? job.filename : "检测结果"}
              extra={job ? statusTag(job.status) : null}
            >
              {job ? (
                <>
                  <Steps
                    size="small"
                    current={stepIndex(job)}
                    items={[{ title: "抽帧" }, { title: "识别" }, { title: "汇总" }, { title: "完成" }]}
                  />
                  <Progress percent={Math.round(job.progress)} style={{ margin: "12px 0" }} />
                  <div className="job-meta">
                    {job.message}
                    {job.frames_estimated
                      ? ` · 抽帧 ${job.frames_extracted}/${job.frames_estimated} · 已识别 ${job.frames_recognized}`
                      : ""}
                    {job.failed_frames ? ` · 失败 ${job.failed_frames} 帧` : ""}
                    {currentElapsed ? ` · 总耗时 ${currentElapsed}` : ""}
                  </div>
                  {job.speech_message && job.speech_message !== job.asr_error ? (
                    <div className="job-meta is-muted">口播：{job.speech_message}</div>
                  ) : null}
                  {job.mock ? (
                    <Alert style={{ marginTop: 12 }} type="warning" showIcon message="这次是模拟识别，原文不是模型读出来的。" />
                  ) : null}
                  {canRetryFrames(job) ? (
                    <Alert
                      style={{ marginTop: 12 }}
                      type={job.status === "failed" ? "error" : "warning"}
                      showIcon
                      message={retryNotice(job)}
                      action={
                        <Button size="small" loading={retryingId === `${job.id}:frames`} onClick={() => retryFrames(job.id)}>
                          重试失败的 {job.failed_frames} 帧
                        </Button>
                      }
                    />
                  ) : job.error && job.status === "failed" ? (
                    <Alert
                      style={{ marginTop: 12 }}
                      type="error"
                      showIcon
                      message={job.error === "interrupted" ? "程序退出时任务未完成" : job.error}
                    />
                  ) : null}
                  {job.speech_retry ? (
                    <Alert
                      style={{ marginTop: 12 }}
                      type={job.status === "failed" ? "error" : "warning"}
                      showIcon
                      message={speechRetryNotice(job)}
                      action={
                        <Button
                          size="small"
                          loading={retryingId === `${job.id}:speech`}
                          onClick={() => retrySpeechPart(job.id, job.speech_retry as "transcribe" | "judge")}
                        >
                          {job.speech_retry === "transcribe" ? "重试口播转写" : "重试文字判定"}
                        </Button>
                      }
                    />
                  ) : job.asr_error && job.status !== "failed" ? (
                    <Alert style={{ marginTop: 12 }} type="warning" showIcon message={job.asr_error} />
                  ) : null}
                  <div style={{ marginTop: 12 }}>
                    {job.output_ready ? (
                      <Segmented
                        style={{ marginBottom: 8 }}
                        value={source}
                        onChange={(value) => setSource(value as "media" | "output")}
                        options={[
                          { label: "原视频", value: "media" },
                          { label: "剪辑结果", value: "output" },
                        ]}
                      />
                    ) : null}
                    <VideoPane
                      mediaUrl={mediaUrl}
                      mimeType={mediaMime}
                      emptyText={previewBlocked && job.preview_hint ? job.preview_hint : "还没有可预览的视频"}
                      onTime={handleTime}
                      onReady={(player) => {
                        seekRef.current = player.seek;
                        if (pendingSeek.current == null) return;
                        const sec = pendingSeek.current;
                        pendingSeek.current = null;
                        player.seek(sec, { play: false });
                      }}
                    />
                    {job.preview_hint && !previewBlocked ? <div className="hint">{job.preview_hint}</div> : null}
                    {editorOn && coversDuration(cuts, job.duration_sec) ? (
                      <Alert
                        style={{ marginTop: 12 }}
                        type="warning"
                        showIcon
                        message="删除段盖住了整段视频。选中一段后，用「设为起点」「设为终点」或 0.1 秒按钮把边缘往里收，留下要保留的画面后再生成。"
                      />
                    ) : null}
                    {editorOn && job.duration_sec ? (
                      <CutTimeline
                        jobId={job.id}
                        duration={job.duration_sec}
                        findings={job.findings}
                        cuts={cuts}
                        currentTime={currentTime}
                        activeKey={activeKey}
                        findingKey={findingKey}
                        onSeek={(sec) => seekOriginal(sec, false)}
                        onChange={rememberCuts}
                        onGesture={(active) => {
                          scrubRef.current = active;
                        }}
                        onSelectFinding={revealFinding}
                        onSelect={setSelectedCut}
                        onActivate={(cut) => {
                          if (previewRef.current === "segment") seekOriginal(cut.start_sec, true);
                        }}
                      />
                    ) : job.duration_sec ? (
                      <div
                        className="timeline"
                        onClick={(event) => {
                          const rect = event.currentTarget.getBoundingClientRect();
                          const ratio = (event.clientX - rect.left) / rect.width;
                          seekOriginal(Math.max(0, ratio * (job.duration_sec || 0)), true);
                        }}
                      >
                        {job.findings.map((finding) => (
                          <Tooltip key={findingKey(finding)} title={`${finding.时间点} ${finding.来源} ${finding.类别} ${finding.原文}`}>
                            <button
                              type="button"
                              className={`${finding.来源 === "口播" ? "seg speech" : "seg"}${activeKey === findingKey(finding) ? " active" : ""}`}
                              style={{
                                left: `${(finding.start_sec / (job.duration_sec || 1)) * 100}%`,
                                width: `${Math.max(((finding.end_sec - finding.start_sec) / (job.duration_sec || 1)) * 100, 0.8)}%`,
                                background: CATEGORY_COLOR[finding.类别] || "#8b968f",
                              }}
                              onClick={(event) => {
                                event.stopPropagation();
                                revealFinding(finding);
                              }}
                            />
                          </Tooltip>
                        ))}
                      </div>
                    ) : null}
                    {job.duration_sec ? (
                      <div className="legend">
                        {Object.entries(CATEGORY_COLOR).map(([name, color]) => (
                          <span key={name}>
                            <i style={{ background: color }} />
                            {name}
                          </span>
                        ))}
                        {editorOn ? (
                          <span>
                            <i style={{ background: "var(--danger)" }} />
                            删除
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    <FindingsTable
                      ref={findingsRef}
                      jobId={job.id}
                      findings={job.findings}
                      running={running}
                      activeKey={activeKey}
                      onOpen={seekTo}
                    />
                    <div className="toolbar">
                      {SHOW_JSON_EXPORT ? (
                        <Button
                          icon={<DownloadOutlined />}
                          loading={fileAction === "json"}
                          disabled={job.status !== "done" || fileAction !== null}
                          onClick={() => void runFileAction("json", async () => { await downloadExport(job.id, "json"); })}
                        >
                          导出 JSON
                        </Button>
                      ) : null}
                      <Button
                        icon={<DownloadOutlined />}
                        loading={fileAction === "xlsx"}
                        disabled={job.status !== "done" || fileAction !== null}
                        onClick={() => void runFileAction("xlsx", async () => { await downloadExport(job.id, "xlsx"); })}
                      >
                        导出 Excel
                      </Button>
                      {batch.length > 1 ? (
                        <Button
                          icon={<DownloadOutlined />}
                          loading={fileAction === "batch-xlsx"}
                          disabled={!batch.some((item) => item.status === "done") || fileAction !== null}
                          onClick={() => void runFileAction("batch-xlsx", async () => {
                            const ready = batch.filter((item) => item.status === "done").length;
                            await downloadBatchExport(batch.map((item) => item.id));
                            if (ready < batch.length) {
                              message.warning(`已导出 ${ready} 个已完成的视频，另外 ${batch.length - ready} 个还在处理`);
                            } else {
                              message.success(`已导出 ${ready} 个视频`);
                            }
                          })}
                        >
                          批量导出 Excel
                        </Button>
                      ) : null}
                      <Button
                        icon={<FolderOpenOutlined />}
                        loading={fileAction === "dir"}
                        disabled={fileAction !== null}
                        onClick={() => void runFileAction("dir", async () => {
                          const result = await openDir("exports");
                          message.success(`已打开 ${result.path}`);
                        })}
                      >
                        打开导出目录
                      </Button>
                      {SHOW_CUT_BUTTON ? (
                        <Button
                          icon={<ScissorOutlined />}
                          loading={editAction === "plan"}
                          disabled={editBusy || job.status !== "done" || job.findings.length === 0}
                          onClick={() => enterEditor().catch((error: Error) => message.error(error.message))}
                        >
                          剪辑
                        </Button>
                      ) : null}
                      <InputNumber min={0} max={2} step={0.1} value={padding} onChange={(value) => setPadding(Number(value) || 0)} addonAfter="画面边距" />
                      {editorOn ? (
                        <>
                          <Button
                            type={job.edit_rendered && sameCuts(cuts, job.cuts ?? []) ? "default" : "primary"}
                            loading={editAction === "render"}
                            disabled={editBusy || cuts.length === 0}
                            onClick={() => generateEdit()}
                          >
                            {job.edit_rendered && sameCuts(cuts, job.cuts ?? []) ? "生成成片" : "生成成片（有未生成的修改）"}
                          </Button>
                          <Button
                            loading={editAction === "reset"}
                            disabled={editBusy}
                            onClick={() => {
                              Modal.confirm({
                                title: "按命中重新生成删除段？",
                                content: `会覆盖时间轴上改过的区间。画面命中向两侧各扩展 ${padding} 秒。口播只标被引用的那几个字，句尾再留大约一个字。`,
                                okText: "重置",
                                cancelText: "取消",
                                onOk: async () => {
                                  if (editAction !== null) return;
                                  setEditAction("reset");
                                  const token = ++saveToken.current;
                                  window.clearTimeout(saveTimer.current);
                                  try {
                                    const next = await planEdit(job.id, padding);
                                    if (saveToken.current !== token) return;
                                    setJob(next);
                                    setCuts(next.cuts);
                                    setEditorOn(true);
                                    setSource("media");
                                  } catch (error) {
                                    message.error(error instanceof Error ? error.message : "重新生成删除段失败");
                                  } finally {
                                    setEditAction((prev) => (prev === "reset" ? null : prev));
                                  }
                                },
                              });
                            }}
                          >
                            重置为自动方案
                          </Button>
                          <Segmented
                            size="small"
                            value={previewMode}
                            onChange={(value) => {
                              const mode = value as "original" | "segment" | "keep";
                              setPreviewMode(mode);
                              if (mode === "segment" && selectedCut) seekOriginal(selectedCut.start_sec, true);
                            }}
                            options={[
                              { label: "原片", value: "original" },
                              { label: "这一段", value: "segment" },
                              { label: "保留结果", value: "keep" },
                            ]}
                          />
                        </>
                      ) : null}
                      {running ? (
                        <>
                        {queuePaused ? (
                          <Button
                            icon={<CaretRightOutlined />}
                            type="primary"
                            loading={queueAction === "resume"}
                            disabled={queueAction !== null}
                            onClick={() => void changeQueue(false)}
                          >
                            开始
                          </Button>
                        ) : (
                          <Button
                            icon={<PauseOutlined />}
                            loading={queueAction === "pause"}
                            disabled={queueAction !== null}
                            onClick={() => void changeQueue(true)}
                          >
                            暂停
                          </Button>
                        )}
                        <Button
                          danger
                          loading={cancellingJob}
                          disabled={cancellingJob}
                          onClick={() => {
                            if (cancellingJob) return;
                            setCancellingJob(true);
                            cancelJob(job.id)
                              .then(setJob)
                              .catch((error: Error) => message.error(error.message))
                              .finally(() => setCancellingJob(false));
                          }}
                        >
                          取消
                        </Button>
                        </>
                      ) : null}
                    </div>
                  </div>
                </>
              ) : (
                <div className="video-empty">
                  <div className="empty-state">
                    <span className="empty-mark"><BrandMark /></span>
                    <span className="empty-title">还没有检测结果</span>
                    <span className="empty-sub">上传视频后，这里会显示进度、时间轴和命中列表</span>
                  </div>
                </div>
              )}
            </Card>
          </Col>
        </Row>
      </div>

      <Drawer
        title="设置"
        rootClassName="settings-drawer"
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        width={460}
        footer={
          <Button type="primary" onClick={() => form.submit()}>
            保存
          </Button>
        }
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values: Record<string, unknown>) => {
            const payload = { ...values };
            try {
              setSettings(await saveSettings(payload));
              message.success("已保存");
            } catch (error) {
              message.error(error instanceof Error ? error.message : "保存失败");
            }
          }}
        >
          <Form.Item label="画面模型" name="model" rules={[{ required: true }]}>
            <Select options={MODEL_OPTIONS} />
          </Form.Item>
          <Form.Item label="默认抽帧 FPS" name="fps" rules={[{ required: true }]}>
            <InputNumber min={0.5} max={2} step={0.1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="默认并发" name="concurrency" rules={[{ required: true }]}>
            <InputNumber min={1} max={MAX_CONCURRENCY} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="画面长边像素" name="max_width" rules={[{ required: true }]}>
            <InputNumber min={480} max={1920} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="JPEG 质量（2 最清晰）" name="jpeg_q" rules={[{ required: true }]}>
            <InputNumber min={2} max={15} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item
            label="图像细节"
            name="detail"
            extra="低细节会把抽帧长边压到不超过 640。模型只接收图片，不接受 detail 参数。"
          >
            <Select
              options={[
                { value: "original", label: "原图（适合小字价格）" },
                { value: "low", label: "低细节（长边不超过 640）" },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="思考模式"
            name="thinking"
            valuePropName="checked"
            extra="三个模型都保持思考开启。关掉时用低强度推理，打开时用高强度。"
          >
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>
          <Form.Item
            label="闪帧检测"
            name="motion_enabled"
            valuePropName="checked"
            extra="找出只出现一两帧的后期贴字。镜头不动时比较小块亮度，镜头在动时再对齐前后帧并把那一块放大。关闭后只按原来的抽帧。只影响之后开始的检测，打开会多送一些帧。"
          >
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>
          <Form.Item label="单帧超时（秒）" name="timeout_sec">
            <InputNumber min={15} max={180} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="口播检测" name="asr_enabled" valuePropName="checked" extra="关闭后只检测画面，不再提取音频。口播凭证在登录后自动获取，不保存在本机。">
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>
          <Form.Item label="录音识别引擎" name="asr_engine" rules={[{ required: true }]} extra="免费的 10 小时包只抵扣通用引擎。大模型 2.0 需要对应的资源包。">
            <Select options={ASR_ENGINE_OPTIONS} />
          </Form.Item>
        </Form>
      </Drawer>

      <Drawer
        title="审核规则"
        rootClassName="rules-drawer"
        open={rulesOpen}
        onClose={() => setRulesOpen(false)}
        width={560}
        footer={
          <Button
            type="primary"
            onClick={async () => {
              const parsed = phrasesFromDrafts(phraseDrafts);
              if (parsed.error) {
                message.error(parsed.error);
                return;
              }
              if (ruleCategories.length === 0 && parsed.phrases.length === 0) {
                message.error("至少启用一个类别，或添加一条额外命中词");
                return;
              }
              try {
                const saved = await saveSettings({
                  rules: {
                    categories: ruleCategories,
                    extra_phrases: parsed.phrases,
                    ignore_phrases: ignoreText
                      .split(/\n+/)
                      .map((item) => item.trim())
                      .filter(Boolean),
                    note: ruleNote.trim(),
                  },
                });
                setSettings(saved);
                applyRuleForm(saved.rules);
                message.success("审核规则已保存，之后的识别会使用这套规则");
              } catch (error) {
                message.error(error instanceof Error ? error.message : "保存失败");
              }
            }}
          >
            保存规则
          </Button>
        }
      >
        <Paragraph type="secondary">
          规则只保存在本机。口播词按原话匹配，X 表示数字。画面词只写进画面提示，必须在这一帧里看得见。短词只要原话里连续出现就会命中。忽略词和原文完全一致，或原文只是忽略词里的一段时不报告。补充说明不能放宽「禁止推测」。夸大宣传只看口播转写，原话里出现绝对化用语或疗效承诺才标，不判断真假。
        </Paragraph>
        <div style={{ marginBottom: 12 }}>
          <Text>启用类别</Text>
          <div>
            <Checkbox.Group
              options={["价格", "规格", "品牌", "夸大宣传"]}
              value={ruleCategories}
              onChange={(values) => setRuleCategories(values.map(String))}
            />
          </div>
        </div>
        {RULE_GROUPS.map((group) => (
          <div className="rule-group" key={groupKey(group.source, group.category)}>
            <Text strong>{group.title}</Text>
            <Text type="secondary" className="rule-group-hint">{group.hint}</Text>
            <Input.TextArea
              rows={group.category === "品牌" && group.source === "口播" ? 4 : 6}
              value={phraseDrafts[groupKey(group.source, group.category)] ?? ""}
              placeholder="一行一个"
              onChange={(event) => {
                const value = event.target.value;
                setPhraseDrafts((current) => ({ ...current, [groupKey(group.source, group.category)]: value }));
              }}
            />
          </div>
        ))}
        <div className="rule-group">
          <Text strong>忽略词</Text>
          <Input.TextArea
            style={{ marginTop: 8 }}
            rows={4}
            value={ignoreText}
            placeholder={"每行一个。例如：\n包邮"}
            onChange={(event) => setIgnoreText(event.target.value)}
          />
        </div>
        <div className="rule-group">
          <Text strong>补充说明</Text>
          <Input.TextArea
            style={{ marginTop: 8 }}
            rows={4}
            maxLength={400}
            value={ruleNote}
            placeholder="例如：把「全网最低」也当成价格。不要要求模型猜测画面里没有的字。"
            onChange={(event) => setRuleNote(event.target.value)}
          />
        </div>
        <SharedRulesPanel />
      </Drawer>

      <Drawer title="测试" open={testOpen} onClose={() => setTestOpen(false)} width={460}>
        <Paragraph type="secondary">
          打开后，下一次识别按这里的失败场景走。非法 JSON 和腾讯云失败都不会真的发出那次请求。测完请关掉。
        </Paragraph>
        <div className="test-row">
          <div>
            <Text strong>模拟识别</Text>
            <p>不请求画面模型，也不向大语言模型领取口播凭证。登录和刷新余额仍会访问账号接口。</p>
          </div>
          <Switch checked={settings.mock_mode} onChange={(checked) => void saveTestFlag({ mock_mode: checked })} />
        </div>
        <div className="test-row">
          <div>
            <Text strong>接口返回非法 JSON</Text>
            <p>每一帧都按「模型没有返回可解析的 JSON」失败，任务会整段失败。口播文字判定如果执行到，也会得到同样的错误。和下面的部分帧同时打开时，以非法 JSON 为准。</p>
          </div>
          <Switch checked={settings.fault_bad_json} onChange={(checked) => void saveTestFlag({ fault_bad_json: checked })} />
        </div>
        <div className="test-row">
          <div>
            <Text strong>腾讯云接口调用失败</Text>
            <p>口播在提交识别时失败，提示无法连接腾讯云。画面识别继续，任务仍可以完成。</p>
          </div>
          <Switch checked={settings.fault_tencent} onChange={(checked) => void saveTestFlag({ fault_tencent: checked })} />
        </div>
        <div className="test-row">
          <div>
            <Text strong>部分帧调用失败</Text>
            <p>第 1、4、7… 帧按模型 HTTP 503 失败，其余帧照常识别。视频至少要有 2 帧，才能同时看到失败和成功。关掉后再点「重试失败的帧」，就会按正常方式重跑。</p>
          </div>
          <Switch
            checked={settings.fault_partial_frames}
            onChange={(checked) => void saveTestFlag({ fault_partial_frames: checked })}
          />
        </div>
      </Drawer>

      <Drawer title="MCP" open={mcpOpen} onClose={() => setMcpOpen(false)} width={560}>
        <McpDrawer
          enabled={Boolean(settings.mcp_enabled)}
          live={mcpLive}
          saving={mcpSaving}
          onToggle={async (checked) => {
            setMcpSaving(true);
            try {
              const saved = await saveSettings({ mcp_enabled: checked });
              setSettings(saved);
              message.success(checked ? "已打开。重新启动程序后，agent 才能连上。" : "已关闭。重新启动程序后停止接受 agent 调用。");
            } catch (error) {
              message.error(error instanceof Error ? error.message : "保存失败");
            } finally {
              setMcpSaving(false);
            }
          }}
        />
      </Drawer>

      <Drawer
        title="历史记录"
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        width={460}
        extra={
          <Popconfirm
            title="删除全部历史记录？"
            description="本地视频副本和导出文件会一起删掉。正在识别的任务会先停掉。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true, loading: clearingHistory }}
            onConfirm={() => clearHistory()}
          >
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              disabled={history.length === 0 || clearingHistory}
              loading={clearingHistory}
            >
              一键删除历史
            </Button>
          </Popconfirm>
        }
      >
        {history.length === 0 ? <Text type="secondary">还没有记录</Text> : null}
        {history.map((item) => {
          const spent = jobElapsed(item, now);
          return (
          <Card
            key={item.id}
            size="small"
            className="history-card"
            onClick={async () => {
              setJob(await getJob(item.id));
              setSource("media");
              setHistoryOpen(false);
            }}
          >
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <div>
                <div>{item.filename}</div>
                <Text type="secondary">
                  {new Date(item.created_at).toLocaleString()} · {item.hit_count} 条命中
                  {spent ? ` · 总耗时 ${spent}` : ""}
                </Text>
              </div>
              <Space>
                {statusTag(item.status)}
                <Popconfirm
                  title="删除这条记录和本地视频副本？"
                  onConfirm={async (event) => {
                    event?.stopPropagation();
                    await deleteJob(item.id);
                    if (job?.id === item.id) setJob(null);
                    await refreshHistory();
                  }}
                  onCancel={(event) => event?.stopPropagation()}
                >
                  <Button size="small" icon={<DeleteOutlined />} onClick={(event) => event.stopPropagation()} />
                </Popconfirm>
              </Space>
            </Space>
          </Card>
          );
        })}
      </Drawer>
    </div>
  );
}
