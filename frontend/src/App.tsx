import {
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  ScissorOutlined,
  SettingOutlined,
  WalletOutlined,
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
  Popover,
  Spin,
  Table,
  Tooltip,
  Typography,
  Upload,
  App as AntApp,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useRef, useState } from "react";
import {
  accountLogout,
  accountRefresh,
  cancelJob,
  createJobs,
  deleteJob,
  downloadExport,
  getJob,
  getSettings,
  listJobs,
  openDir,
  planEdit,
  renderEdit,
  saveCuts,
  saveSettings,
} from "./api";
import { formatBalance, LoginPage, RegisterPage } from "./AuthPages";
import { CutTimeline } from "./CutTimeline";
import { RechargePage } from "./RechargePage";
import { CATEGORY_COLOR, TERMINAL, type CutSpan, type Finding, type Job, type PhraseRule, type Settings } from "./types";
import { VideoPane, type PlayerControls } from "./VideoPane";

const { Dragger } = Upload;
const { Text, Paragraph } = Typography;

const CATEGORY_PILL: Record<string, string> = {
  价格: "pill--price",
  规格: "pill--spec",
  品牌: "pill--brand",
};

/** 取景框 + 扫描线：品牌标记，用于顶栏与登录页 */
export function BrandMark({ size = 21 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 9V7a3 3 0 0 1 3-3h2M15 4h2a3 3 0 0 1 3 3v2M20 15v2a3 3 0 0 1-3 3h-2M9 20H7a3 3 0 0 1-3-3v-2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path d="M7.5 12h9" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function statusTag(status: string) {
  if (status === "done") return <span className="pill pill--confirmed"><i className="status-dot" />完成</span>;
  if (status === "failed") return <span className="pill pill--error"><i className="status-dot" />失败</span>;
  if (status === "cancelled") return <span className="pill pill--neutral">已取消</span>;
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

function stepIndex(job: Job | null): number {
  if (!job) return 0;
  if (job.stage === "done" || job.status === "done") return 3;
  if (job.stage === "aggregating") return 2;
  if (job.frames_recognized > 0 || job.stage === "recognizing") return 1;
  return 0;
}

export function Shell() {
  const { message } = AntApp.useApp();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [authPage, setAuthPage] = useState<"login" | "register">("login");
  const [refreshingAccount, setRefreshingAccount] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [bootError, setBootError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [workspace, setWorkspace] = useState<"detect" | "recharge">("detect");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [fps, setFps] = useState(1);
  const [concurrency, setConcurrency] = useState(4);
  const [padding, setPadding] = useState(0.2);
  const [picked, setPicked] = useState<File[]>([]);
  const [batch, setBatch] = useState<Job[]>([]);
  const [uploading, setUploading] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [ruleCategories, setRuleCategories] = useState<string[]>(["价格", "规格", "品牌"]);
  const [extraPhrases, setExtraPhrases] = useState<PhraseRule[]>([]);
  const [draftPhrase, setDraftPhrase] = useState("");
  const [draftCategory, setDraftCategory] = useState("价格");
  const [ignoreText, setIgnoreText] = useState("");
  const [ruleNote, setRuleNote] = useState("");
  const [source, setSource] = useState<"media" | "output">("media");
  const [activeKey, setActiveKey] = useState("");
  const [editorOn, setEditorOn] = useState(false);
  const [cuts, setCuts] = useState<CutSpan[]>([]);
  const [previewMode, setPreviewMode] = useState<"original" | "segment" | "keep">("keep");
  const [selectedCut, setSelectedCut] = useState<CutSpan | null>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [editAction, setEditAction] = useState<"plan" | "render" | "reset" | null>(null);
  const [fileAction, setFileAction] = useState<"json" | "xlsx" | "dir" | null>(null);
  const [cancellingJob, setCancellingJob] = useState(false);
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

  useEffect(() => {
    refreshSettings().catch((error: Error) => {
      setBootError(error.message);
      message.error(error.message);
    });
  }, [message]);

  const loggedIn = Boolean(settings?.account_logged_in);
  useEffect(() => {
    if (!loggedIn) return;
    refreshHistory().catch(() => undefined);
  }, [loggedIn]);

  useEffect(() => {
    if (!settings || defaultsApplied.current) return;
    defaultsApplied.current = true;
    setFps(settings.fps);
    setConcurrency(settings.concurrency);
  }, [settings]);

  useEffect(() => {
    if (!settings) return;
    form.setFieldsValue({
      fps: settings.fps,
      concurrency: settings.concurrency,
      max_width: settings.max_width,
      jpeg_q: settings.jpeg_q,
      detail: settings.detail,
      thinking: settings.thinking,
      timeout_sec: settings.timeout_sec,
      mock_mode: settings.mock_mode,
      asr_enabled: settings.asr_enabled,
      tencent_region: settings.tencent_region,
      asr_engine: settings.asr_engine,
    });
  }, [form, settings]);

  useEffect(() => {
    if (!settings?.rules) return;
    setRuleCategories(settings.rules.categories);
    setExtraPhrases(settings.rules.extra_phrases);
    setIgnoreText(settings.rules.ignore_phrases.join("\n"));
    setRuleNote(settings.rules.note);
  }, [settings]);

  useEffect(() => {
    if (!job) {
      seenJob.current = null;
      return;
    }
    if (seenJob.current === job.id) return;
    seenJob.current = job.id;
    window.clearTimeout(saveTimer.current);
    setPreviewMode("keep");
    setSelectedCut(null);
    setCurrentTime(0);
    lastTime.current = 0;
    setEditorOn(Boolean(job.has_edit_plan));
    setCuts(job.cuts ?? []);
  }, [job]);

  const jobId = job?.id;
  const jobStatus = job?.status;
  useEffect(() => {
    if (!jobId || (jobStatus != null && TERMINAL.includes(jobStatus))) return undefined;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      try {
        const next = await getJob(jobId);
        if (stop) return;
        setJob(next);
        if (!TERMINAL.includes(next.status)) window.setTimeout(tick, 700);
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
  }, [jobId, jobStatus]);

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
  const mediaUrl =
    job == null ? null : source === "output" && job.output_ready ? `/api/jobs/${job.id}/output` : `/api/jobs/${job.id}/media`;
  const mediaMime = previewMime(source === "output" ? "clean.mp4" : job?.filename || "");

  async function startBatch() {
    if (picked.length === 0) return;
    const formData = new FormData();
    picked.forEach((file) => formData.append("files", file));
    formData.append("fps", String(fps));
    formData.append("concurrency", String(concurrency));
    setUploading(true);
    try {
      const created = await createJobs(formData);
      setPicked([]);
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

  function findingKey(finding: Finding) {
    return `${finding.start_sec}-${finding.来源}-${finding.类别}-${finding.原文}`;
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

  async function runFileAction(action: "json" | "xlsx" | "dir", task: () => Promise<void>) {
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

  const columns: ColumnsType<Finding> = [
    { title: "时间点", dataIndex: "时间点", width: 130 },
    {
      title: "类别",
      dataIndex: "类别",
      width: 80,
      render: (value: string) => <span className={`pill ${CATEGORY_PILL[value] ?? "pill--neutral"}`}>{value}</span>,
    },
    {
      title: "来源",
      dataIndex: "来源",
      width: 80,
      render: (value: string) => <span className={`pill ${value === "口播" ? "pill--speech" : "pill--neutral"}`}>{value || "画面"}</span>,
    },
    { title: "原文", dataIndex: "原文", width: 160 },
    { title: "位置", dataIndex: "位置", width: 180 },
    {
      title: "置信度",
      dataIndex: "置信度",
      width: 90,
      render: (value: string) => <span className={`pill ${value === "确定" ? "pill--confirmed" : "pill--pending"}`}>{value}</span>,
    },
    {
      title: "判定理由",
      dataIndex: "判定理由",
      ellipsis: { showTitle: false },
      render: (value: string) => (
        <Tooltip title={value}>
          <span>{value}</span>
        </Tooltip>
      ),
    },
    { title: "备注", dataIndex: "备注", width: 160, ellipsis: true },
  ];

  async function refreshAccount() {
    if (refreshingAccount || loggingOut) return;
    setRefreshingAccount(true);
    try {
      const result = await accountRefresh();
      setSettings(result.settings);
      message.success(result.message || "已更新余额");
    } catch (error) {
      const text = error instanceof Error ? error.message : "刷新失败";
      message.error(text);
      if (text.includes("请重新登录") || text.includes("请先登录") || text.includes("还没有登录")) {
        refreshSettings().catch(() => undefined);
      }
    } finally {
      setRefreshingAccount(false);
    }
  }

  async function logout() {
    if (refreshingAccount || loggingOut) return;
    setLoggingOut(true);
    try {
      const result = await accountLogout();
      setSettings(result.settings);
      setJob(null);
      setBatch([]);
      setPicked([]);
      setHistory([]);
      setWorkspace("detect");
      setAuthPage("login");
      message.success(result.message || "已退出登录");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "退出失败");
    } finally {
      setLoggingOut(false);
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
        onRegistered={(next) => {
          setSettings(next);
          setAuthPage("login");
        }}
        onGoLogin={() => setAuthPage("login")}
      />
    ) : (
      <LoginPage onLoggedIn={setSettings} onGoRegister={() => setAuthPage("register")} />
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <button className="brand" type="button" onClick={() => setWorkspace("detect")}>
          <span className="brand-mark"><BrandMark /></span>
          <span className="brand-text">
            <strong>电商视频素材合规检测</strong>
            <span>本地识别画面和口播中的价格、规格、品牌，并标出时间区间</span>
          </span>
        </button>
        <nav className="app-nav">
          <Button type="text" icon={<HistoryOutlined />} onClick={() => { setHistoryOpen(true); refreshHistory().catch(() => undefined); }}>
            历史
          </Button>
          <Button type="text" onClick={() => setRulesOpen(true)}>审核规则</Button>
          <Button type="text" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)}>设置</Button>
          <Button
            type="text"
            icon={<WalletOutlined />}
            className={workspace === "recharge" ? "is-active" : ""}
            onClick={() => setWorkspace("recharge")}
          >
            充值
          </Button>
        </nav>
        <div className="app-account">
          <Popover
            content={
              <Space direction="vertical" size={4}>
                <span>冻结 {formatBalance(settings.account_frozen_balance)}</span>
                <span>今日消耗 {formatBalance(settings.account_today_cost)}</span>
                <span>累计消耗 {formatBalance(settings.account_total_cost)}</span>
              </Space>
            }
          >
            <span className="account-chip">
              {settings.account_email} · 余额 {formatBalance(settings.account_balance)}
            </span>
          </Popover>
          <Button type="text" size="small" loading={refreshingAccount} disabled={loggingOut} onClick={() => void refreshAccount()}>
            刷新余额
          </Button>
          <Button type="text" size="small" loading={loggingOut} disabled={refreshingAccount} onClick={() => void logout()}>
            退出登录
          </Button>
        </div>
      </header>
      <div className="page">
        {workspace === "recharge" ? (
          <RechargePage settings={settings} onSettings={setSettings} />
        ) : (
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={8}>
            <Card title="上传视频">
              {settings?.mock_mode ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="模拟识别已打开，不会调用 DeepSeek。" />
              ) : null}
              {!settings.has_api_key && !settings.mock_mode ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="账号已登录，还没有拿到画面识别密钥。请点右上角的刷新余额。"
                />
              ) : null}
              <Dragger
                multiple
                showUploadList={false}
                disabled={uploading}
                accept="video/*,.mp4,.mov,.mkv,.avi,.webm,.m4v,.flv"
                beforeUpload={(file) => {
                  setPicked((current) => {
                    const exists = current.some(
                      (item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified,
                    );
                    return exists ? current : [...current, file];
                  });
                  return Upload.LIST_IGNORE;
                }}
              >
                <div className="upload-icon"><CloudUploadOutlined /></div>
                <p className="upload-title">拖拽视频到这里，或点击选择</p>
                <p className="upload-sub">支持 mp4 / mov / mkv / avi / webm · 可一次选多个，最多 20 个</p>
              </Dragger>
              {picked.length > 0 ? (
                <div style={{ marginTop: 12 }}>
                  {picked.map((file) => (
                    <div className="file-row" key={`${file.name}-${file.size}-${file.lastModified}`}>
                      <Text ellipsis>{file.name}</Text>
                      <Button
                        size="small"
                        type="text"
                        disabled={uploading}
                        onClick={() => setPicked((current) => current.filter((item) => item !== file))}
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
              <div className="slider-row">
                <Text>抽帧密度 {fps} FPS</Text>
                <Slider min={0.5} max={2} step={0.1} value={fps} onChange={setFps} disabled={uploading} />
                <Text>并发请求 {concurrency}</Text>
                <Slider min={1} max={8} step={1} value={concurrency} onChange={setConcurrency} disabled={uploading} />
              </div>
              {batch.length > 0 ? (
                <div style={{ marginTop: 20 }}>
                  <span className="section-label">本批队列</span>
                  {batch.map((item) => (
                    <div
                      className="queue-row"
                      key={item.id}
                      onClick={() => {
                        if (item.id !== job?.id) setSource("media");
                        setJob(item);
                      }}
                    >
                      <Text ellipsis>{item.filename}</Text>
                      <Space size={4}>
                        {statusTag(item.status)}
                        {!TERMINAL.includes(item.status) ? <Text type="secondary">{Math.round(item.progress)}%</Text> : null}
                      </Space>
                    </div>
                  ))}
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
                  </div>
                  {job.speech_message && job.speech_message !== job.asr_error ? (
                    <div className="job-meta is-muted">口播：{job.speech_message}</div>
                  ) : null}
                  {job.mock ? (
                    <Alert style={{ marginTop: 12 }} type="warning" showIcon message="这次是模拟识别，原文不是模型读出来的。" />
                  ) : null}
                  {job.error && job.status === "failed" ? (
                    <Alert style={{ marginTop: 12 }} type="error" showIcon message={job.error} />
                  ) : null}
                  {job.asr_error && job.status !== "failed" ? (
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
                      emptyText="还没有可预览的视频"
                      onTime={handleTime}
                      onReady={(player) => {
                        seekRef.current = player.seek;
                        if (pendingSeek.current == null) return;
                        const sec = pendingSeek.current;
                        pendingSeek.current = null;
                        player.seek(sec, { play: false });
                      }}
                    />
                    {job.preview_hint ? <div className="hint">{job.preview_hint}</div> : null}
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
                        onSelectFinding={seekTo}
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
                                seekTo(finding);
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
                    <Table
                      size="small"
                      rowKey={(record) => findingKey(record)}
                      columns={columns}
                      dataSource={job.findings}
                      pagination={{ pageSize: 8, hideOnSinglePage: true }}
                      rowClassName="click-row"
                      onRow={(record) => ({ onClick: () => seekTo(record) })}
                      locale={{ emptyText: running ? "识别进行中，命中会陆续出现" : "没有发现画面或口播中的价格、规格或品牌" }}
                    />
                    <div className="toolbar">
                      <Button
                        icon={<DownloadOutlined />}
                        loading={fileAction === "json"}
                        disabled={job.status !== "done" || fileAction !== null}
                        onClick={() => void runFileAction("json", async () => { await downloadExport(job.id, "json"); })}
                      >
                        导出 JSON
                      </Button>
                      <Button
                        icon={<DownloadOutlined />}
                        loading={fileAction === "xlsx"}
                        disabled={job.status !== "done" || fileAction !== null}
                        onClick={() => void runFileAction("xlsx", async () => { await downloadExport(job.id, "xlsx"); })}
                      >
                        导出 Excel
                      </Button>
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
                      <Button
                        icon={<ScissorOutlined />}
                        loading={editAction === "plan"}
                        disabled={editBusy || job.status !== "done" || job.findings.length === 0}
                        onClick={() => enterEditor().catch((error: Error) => message.error(error.message))}
                      >
                        剪辑
                      </Button>
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
                      ) : null}
                    </div>
                  </div>
                </>
              ) : (
                <div className="video-empty">
                  <div className="empty-state">
                    <span className="empty-mark"><BrandMark size={24} /></span>
                    <span className="empty-title">还没有检测结果</span>
                    <span className="empty-sub">上传视频后，这里会显示进度、时间轴和命中列表</span>
                  </div>
                </div>
              )}
            </Card>
          </Col>
        </Row>
        )}
      </div>

      <Drawer title="设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={460}>
        <Paragraph type="secondary">
          当前账号 {settings.account_email}。画面识别使用这个账号的密钥，请求地址固定为 https://token4u.cc/v1，模型固定为 deepseek-v4.1-flash-1017。口播继续用腾讯云。
        </Paragraph>
        <Paragraph type="secondary">
          API Key 只保存在本机数据库。当前来源：
          {settings?.api_key_source === "account"
            ? "登录账号"
            : settings?.api_key_source === "saved"
              ? "本机已保存"
              : settings?.api_key_source === "env"
                ? "环境变量"
                : "未配置"}
          {settings?.api_key_masked ? `（${settings.api_key_masked}）` : ""}
        </Paragraph>
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values: Record<string, unknown>) => {
            const payload = { ...values };
            if (!payload.tencent_secret_id) delete payload.tencent_secret_id;
            if (!payload.tencent_secret_key) delete payload.tencent_secret_key;
            try {
              setSettings(await saveSettings(payload));
              message.success("已保存");
            } catch (error) {
              message.error(error instanceof Error ? error.message : "保存失败");
            }
          }}
        >
          <Form.Item label="默认抽帧 FPS" name="fps" rules={[{ required: true }]}>
            <InputNumber min={0.5} max={2} step={0.1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="默认并发" name="concurrency" rules={[{ required: true }]}>
            <InputNumber min={1} max={12} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="画面长边像素" name="max_width" rules={[{ required: true }]}>
            <InputNumber min={480} max={1920} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="JPEG 质量（2 最清晰）" name="jpeg_q" rules={[{ required: true }]}>
            <InputNumber min={2} max={15} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="图像细节" name="detail">
            <Select
              options={[
                { value: "original", label: "原图（适合小字价格）" },
                { value: "low", label: "低细节（更快，可能漏小字）" },
              ]}
            />
          </Form.Item>
          <Form.Item label="思考模式" name="thinking" valuePropName="checked">
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>
          <Form.Item label="单帧超时（秒）" name="timeout_sec">
            <InputNumber min={15} max={180} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="模拟识别" name="mock_mode" valuePropName="checked" extra="打开后不调用 API，只用于看流程。">
            <Switch />
          </Form.Item>
          <Form.Item label="口播检测" name="asr_enabled" valuePropName="checked" extra="关闭后只检测画面，不再提取音频。">
            <Switch checkedChildren="开" unCheckedChildren="关" />
          </Form.Item>
          <Paragraph type="secondary">
            腾讯云密钥只保存在本机。当前来源：{settings?.tencent_secret_source === "saved" ? "本机已保存" : settings?.tencent_secret_source === "env" ? "环境变量" : "未配置"}
            {settings?.tencent_secret_id_masked ? `（SecretId ${settings.tencent_secret_id_masked}）` : ""}
          </Paragraph>
          <Form.Item label="腾讯云 SecretId" name="tencent_secret_id">
            <Input.Password placeholder="留空表示不修改" autoComplete="off" />
          </Form.Item>
          <Form.Item label="腾讯云 SecretKey" name="tencent_secret_key">
            <Input.Password placeholder="留空表示不修改" autoComplete="off" />
          </Form.Item>
          <Form.Item label="腾讯云地域" name="tencent_region" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="录音识别引擎" name="asr_engine" rules={[{ required: true }]} extra="普通话录音文件识别常用 16k_zh。">
            <Input />
          </Form.Item>
          <Space wrap>
            <Button type="primary" htmlType="submit">保存</Button>
            <Button
              onClick={async () => {
                setSettings(await saveSettings({ clear_api_key: true }));
                message.success("已清除本机保存的 Key");
              }}
            >
              清除 Key
            </Button>
            <Button
              onClick={async () => {
                setSettings(await saveSettings({ clear_tencent_secret: true }));
                message.success("已清除本机保存的腾讯云密钥");
              }}
            >
              清除腾讯云密钥
            </Button>
          </Space>
        </Form>
      </Drawer>

      <Drawer title="审核规则" open={rulesOpen} onClose={() => setRulesOpen(false)} width={480}>
        <Paragraph type="secondary">
          规则只保存在本机。关闭某个类别后，那一类不再报告。额外命中词只要在口播里原样出现就会报告；画面仍要模型在这一帧里看见同样的文字。忽略词和原文完全一致时不报告。补充说明不能放宽「禁止推测」。
        </Paragraph>
        <div style={{ marginBottom: 12 }}>
          <Text>启用类别</Text>
          <div>
            <Checkbox.Group
              options={["价格", "规格", "品牌"]}
              value={ruleCategories}
              onChange={(values) => setRuleCategories(values.map(String))}
            />
          </div>
        </div>
        <Text>额外命中词</Text>
        <Space.Compact style={{ width: "100%", marginTop: 8 }}>
          <Select
            value={draftCategory}
            style={{ width: 96 }}
            onChange={setDraftCategory}
            options={[
              { value: "价格", label: "价格" },
              { value: "规格", label: "规格" },
              { value: "品牌", label: "品牌" },
            ]}
          />
          <Input
            value={draftPhrase}
            placeholder="必须原样出现的文字"
            onChange={(event) => setDraftPhrase(event.target.value)}
            onPressEnter={() => {
              const text = draftPhrase.trim();
              if (!text) return;
              setExtraPhrases((current) =>
                current.some((item) => item.text === text && item.category === draftCategory)
                  ? current
                  : [...current, { text, category: draftCategory }],
              );
              setDraftPhrase("");
            }}
          />
          <Button
            onClick={() => {
              const text = draftPhrase.trim();
              if (!text) return;
              setExtraPhrases((current) =>
                current.some((item) => item.text === text && item.category === draftCategory)
                  ? current
                  : [...current, { text, category: draftCategory }],
              );
              setDraftPhrase("");
            }}
          >
            添加
          </Button>
        </Space.Compact>
        {extraPhrases.map((item) => (
          <div className="file-row" key={`${item.category}-${item.text}`}>
            <span className="phrase-item">
              <span className={`pill ${CATEGORY_PILL[item.category] ?? "pill--neutral"}`}>{item.category}</span>
              <span>{item.text}</span>
            </span>
            <Button size="small" type="text" onClick={() => setExtraPhrases((current) => current.filter((phrase) => phrase !== item))}>
              移除
            </Button>
          </div>
        ))}
        <div style={{ marginTop: 16 }}>
          <Text>忽略词</Text>
          <Input.TextArea
            style={{ marginTop: 8 }}
            rows={4}
            value={ignoreText}
            placeholder={"每行一个。例如：\n包邮"}
            onChange={(event) => setIgnoreText(event.target.value)}
          />
        </div>
        <div style={{ marginTop: 16 }}>
          <Text>补充说明</Text>
          <Input.TextArea
            style={{ marginTop: 8 }}
            rows={4}
            maxLength={400}
            value={ruleNote}
            placeholder="例如：把「全网最低」也当成价格。不要要求模型猜测画面里没有的字。"
            onChange={(event) => setRuleNote(event.target.value)}
          />
        </div>
        <Button
          type="primary"
          style={{ marginTop: 16 }}
          onClick={async () => {
            if (ruleCategories.length === 0 && extraPhrases.length === 0) {
              message.error("至少启用一个类别，或添加一条额外命中词");
              return;
            }
            try {
              const saved = await saveSettings({
                rules: {
                  categories: ruleCategories,
                  extra_phrases: extraPhrases,
                  ignore_phrases: ignoreText
                    .split(/\n+/)
                    .map((item) => item.trim())
                    .filter(Boolean),
                  note: ruleNote.trim(),
                },
              });
              setSettings(saved);
              message.success("审核规则已保存，之后的识别会使用这套规则");
            } catch (error) {
              message.error(error instanceof Error ? error.message : "保存失败");
            }
          }}
        >
          保存规则
        </Button>
      </Drawer>

      <Drawer title="历史记录" open={historyOpen} onClose={() => setHistoryOpen(false)} width={460}>
        {history.length === 0 ? <Text type="secondary">还没有记录</Text> : null}
        {history.map((item) => (
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
        ))}
      </Drawer>
    </div>
  );
}
