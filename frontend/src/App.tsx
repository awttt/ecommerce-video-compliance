import {
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
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
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  App as AntApp,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useRef, useState } from "react";
import {
  cancelJob,
  createJobs,
  cutJob,
  deleteJob,
  downloadExport,
  getJob,
  getSettings,
  listJobs,
  openDir,
  saveSettings,
} from "./api";
import { CATEGORY_COLOR, TERMINAL, type Finding, type Job, type PhraseRule, type Settings } from "./types";
import { VideoPane } from "./VideoPane";

const { Dragger } = Upload;
const { Text, Paragraph } = Typography;

function statusTag(status: string) {
  if (status === "done") return <Tag color="success">完成</Tag>;
  if (status === "failed") return <Tag color="error">失败</Tag>;
  if (status === "cancelled") return <Tag>已取消</Tag>;
  if (status === "queued") return <Tag color="processing">排队</Tag>;
  if (status === "running") return <Tag color="processing">识别中</Tag>;
  return <Tag>{status}</Tag>;
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
  const [settingsOpen, setSettingsOpen] = useState(false);
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
  const seekRef = useRef<(seconds: number) => void>(() => undefined);
  const defaultsApplied = useRef(false);
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
    refreshSettings().catch((error: Error) => message.error(error.message));
    refreshHistory().catch(() => undefined);
  }, [message]);

  useEffect(() => {
    if (!settings || defaultsApplied.current) return;
    defaultsApplied.current = true;
    setFps(settings.fps);
    setConcurrency(settings.concurrency);
  }, [settings]);

  useEffect(() => {
    if (!settings) return;
    form.setFieldsValue({
      base_url: settings.base_url,
      model: settings.model,
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
      } catch {
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

  function seekTo(finding: Finding) {
    setActiveKey(findingKey(finding));
    seekRef.current(finding.start_sec);
  }

  const columns: ColumnsType<Finding> = [
    { title: "时间点", dataIndex: "时间点", width: 130 },
    {
      title: "类别",
      dataIndex: "类别",
      width: 80,
      render: (value: string) => <Tag color={CATEGORY_COLOR[value]}>{value}</Tag>,
    },
    {
      title: "来源",
      dataIndex: "来源",
      width: 80,
      render: (value: string) => <Tag color={value === "口播" ? "purple" : "default"}>{value || "画面"}</Tag>,
    },
    { title: "原文", dataIndex: "原文", width: 160 },
    { title: "位置", dataIndex: "位置", width: 180 },
    {
      title: "置信度",
      dataIndex: "置信度",
      width: 90,
      render: (value: string) => <Tag color={value === "确定" ? "green" : "gold"}>{value}</Tag>,
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

  return (
    <>
      <header className="app-header">
        <div className="brand">
          <strong>电商视频素材合规检测</strong>
          <span>本地识别画面和口播中的价格、规格、品牌，并标出时间区间</span>
        </div>
        <Space>
          <Button icon={<HistoryOutlined />} onClick={() => { setHistoryOpen(true); refreshHistory().catch(() => undefined); }}>
            历史
          </Button>
          <Button onClick={() => setRulesOpen(true)}>审核规则</Button>
          <Button icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)}>
            设置
          </Button>
        </Space>
      </header>
      <div className="page">
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={8}>
            <Card title="上传视频">
              {settings?.mock_mode ? (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="模拟识别已打开，不会调用 DeepSeek。" />
              ) : null}
              {!settings?.has_api_key && !settings?.mock_mode ? (
                <Alert type="info" showIcon style={{ marginBottom: 12 }} message="请先在设置里填写 DeepSeek API Key。" />
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
                <p>
                  <CloudUploadOutlined style={{ fontSize: 28, color: "#1f6f5b" }} />
                </p>
                <p>拖拽一个或多个视频到这里，或点击选择</p>
                <p className="hint">支持 mp4 / mov / mkv / avi / webm。一次最多 20 个，中文文件名可以直接用。</p>
              </Dragger>
              {picked.length > 0 ? (
                <div style={{ marginTop: 12 }}>
                  {picked.map((file) => (
                    <div key={`${file.name}-${file.size}-${file.lastModified}`} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
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
                style={{ marginTop: 12 }}
                loading={uploading}
                disabled={picked.length === 0}
                onClick={() => startBatch().catch(() => undefined)}
              >
                {picked.length > 1 ? `开始识别（${picked.length} 个）` : "开始识别"}
              </Button>
              <div style={{ marginTop: 16 }}>
                <Text>抽帧密度 {fps} FPS</Text>
                <Slider min={0.5} max={2} step={0.1} value={fps} onChange={setFps} disabled={uploading} />
                <Text>并发请求 {concurrency}</Text>
                <Slider min={1} max={8} step={1} value={concurrency} onChange={setConcurrency} disabled={uploading} />
              </div>
              {batch.length > 0 ? (
                <div style={{ marginTop: 16 }}>
                  <Text type="secondary">本批队列</Text>
                  {batch.map((item) => (
                    <div
                      key={item.id}
                      style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 8, cursor: "pointer" }}
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
                多个视频会排队识别，同一时间只处理一个。只报告画面里看得见的文字，以及口播里明确说出的内容，不根据外观推测。结束后会删除中间图片和临时音频。
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
                  <Text>
                    {job.message}
                    {job.frames_estimated
                      ? ` · 抽帧 ${job.frames_extracted}/${job.frames_estimated} · 已识别 ${job.frames_recognized}`
                      : ""}
                    {job.failed_frames ? ` · 失败 ${job.failed_frames} 帧` : ""}
                  </Text>
                  {job.speech_message && job.speech_message !== job.asr_error ? (
                    <div>
                      <Text type="secondary">口播：{job.speech_message}</Text>
                    </div>
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
                      onReady={(seek) => {
                        seekRef.current = seek;
                      }}
                    />
                    {job.preview_hint ? <div className="hint">{job.preview_hint}</div> : null}
                    {job.duration_sec ? (
                      <>
                        <div
                          className="timeline"
                          onClick={(event) => {
                            const rect = event.currentTarget.getBoundingClientRect();
                            const ratio = (event.clientX - rect.left) / rect.width;
                            seekRef.current(Math.max(0, ratio * (job.duration_sec || 0)));
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
                                  background: CATEGORY_COLOR[finding.类别] || "#5c6b7a",
                                }}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  seekTo(finding);
                                }}
                              />
                            </Tooltip>
                          ))}
                        </div>
                        <div className="legend">
                          {Object.entries(CATEGORY_COLOR).map(([name, color]) => (
                            <span key={name}>
                              <i style={{ background: color }} />
                              {name}
                            </span>
                          ))}
                        </div>
                      </>
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
                    <Space wrap style={{ marginTop: 12 }}>
                      <Button icon={<DownloadOutlined />} disabled={job.status !== "done"} onClick={() => downloadExport(job.id, "json").catch((error: Error) => message.error(error.message))}>
                        导出 JSON
                      </Button>
                      <Button icon={<DownloadOutlined />} disabled={job.status !== "done"} onClick={() => downloadExport(job.id, "xlsx").catch((error: Error) => message.error(error.message))}>
                        导出 Excel
                      </Button>
                      <Button icon={<FolderOpenOutlined />} onClick={() => openDir("exports").then((result) => message.success(`已打开 ${result.path}`)).catch((error: Error) => message.error(error.message))}>
                        打开导出目录
                      </Button>
                      <Button
                        icon={<ScissorOutlined />}
                        disabled={job.status !== "done" || job.findings.length === 0}
                        onClick={() => {
                          Modal.confirm({
                            title: "按命中时间段生成新视频？",
                            content: `画面命中会向两侧各扩展 ${padding} 秒。口播按整句切除，并切到句间停顿，避免把上一句的最后一个字留在下一句开头。然后重新编码拼接。`,
                            okText: "开始剪辑",
                            cancelText: "取消",
                            onOk: async () => {
                              const next = await cutJob(job.id, padding);
                              setJob(next);
                              setSource("output");
                              message.success("剪辑视频已生成");
                            },
                          });
                        }}
                      >
                        实验：剪掉命中片段
                      </Button>
                      <InputNumber min={0} max={2} step={0.1} value={padding} onChange={(value) => setPadding(Number(value) || 0)} addonAfter="画面边距" />
                      {running ? (
                        <Button danger onClick={() => cancelJob(job.id).then(setJob).catch((error: Error) => message.error(error.message))}>
                          取消
                        </Button>
                      ) : null}
                    </Space>
                  </div>
                </>
              ) : (
                <div className="video-empty">上传视频后，这里会显示进度、时间轴和命中列表。</div>
              )}
            </Card>
          </Col>
        </Row>
      </div>

      <Drawer title="设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={460}>
        <Paragraph type="secondary">
          API Key 只保存在本机数据库。当前来源：{settings?.api_key_source === "saved" ? "本机已保存" : settings?.api_key_source === "env" ? "环境变量" : "未配置"}
          {settings?.api_key_masked ? `（${settings.api_key_masked}）` : ""}
        </Paragraph>
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values: Record<string, unknown>) => {
            const payload = { ...values };
            if (!payload.api_key) delete payload.api_key;
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
          <Form.Item label="DeepSeek API Key" name="api_key">
            <Input.Password placeholder="留空表示不修改" autoComplete="off" />
          </Form.Item>
          <Form.Item label="接口地址" name="base_url" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="模型" name="model" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
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
          <div key={`${item.category}-${item.text}`} style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
            <Text>
              <Tag color={CATEGORY_COLOR[item.category]}>{item.category}</Tag>
              {item.text}
            </Text>
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
            style={{ marginBottom: 8, cursor: "pointer" }}
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
    </>
  );
}
