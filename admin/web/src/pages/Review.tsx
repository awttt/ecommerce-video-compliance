import { InboxOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Col, Row, Select, Space, Table, Tooltip, Typography, Upload } from "antd";
import { useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api, download } from "../api";
import type { Job, Member, User } from "../types";
import { CategoryTag, PageHead, SourceTag, StatusTag } from "../ui";

export function Review() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [memberId, setMemberId] = useState<number | null>(null);
  const [videoUrl, setVideoUrl] = useState("");
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api<Job[]>("/v1/jobs") });
  const queue = useQuery({ queryKey: ["queue"], queryFn: () => api<{ paused: boolean }>("/v1/queue") });
  const members = useQuery({
    queryKey: ["members", "review"],
    queryFn: () => api<Member[]>("/v1/members"),
    enabled: user.role !== "member",
  });
  const rows = jobs.data ?? [];
  const selected = rows.find((item) => item.id === selectedId) ?? rows.find((item) => item.status === "done") ?? rows[0];
  const detail = useQuery({
    queryKey: ["job", selected?.id],
    queryFn: () => api<Job>(`/v1/jobs/${selected?.id}`),
    enabled: Boolean(selected),
  });

  useEffect(() => {
    if (!detail.data?.has_file) {
      setVideoUrl("");
      return;
    }
    let url = "";
    let alive = true;
    void fetch(`/v1/jobs/${detail.data.id}/media`, {
      headers: { Authorization: `Bearer ${sessionStorage.getItem("evc-admin-access") ?? ""}` },
      credentials: "include",
    })
      .then((response) => (response.ok ? response.blob() : null))
      .then((blob) => {
        if (!alive || !blob) return;
        url = URL.createObjectURL(blob);
        setVideoUrl(url);
      })
      .catch(() => setVideoUrl(""));
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [detail.data?.id, detail.data?.has_file]);

  const toggle = useMutation({
    mutationFn: () => api(`/v1/queue/${queue.data?.paused ? "resume" : "pause"}`, { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["queue"] });
      message.success(queue.data?.paused ? "队列已开始" : "队列已暂停");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "操作失败"),
  });

  const scope = user.role === "platform_admin"
    ? "平台可看全部公司。识别仍用该成员的中转站额度，口播成本已含在扣费里。"
    : user.role === "tenant_admin"
      ? "只看本公司的视频和命中。识别仍用该成员的中转站额度。"
      : "只看自己的视频。识别仍用自己的中转站额度。";

  return (
    <>
      <PageHead
        title="视频审核"
        description={scope}
        extra={
          <Space>
            <Button
              disabled={!detail.data}
              onClick={() => {
                if (!detail.data) return;
                void download(`/v1/jobs/${detail.data.id}/export`, `${detail.data.filename.replace(/\.[^.]+$/, "")}_合规结果.xlsx`).catch((error) => {
                  message.error(error instanceof ApiError ? error.message : "导出失败");
                });
              }}
            >
              导出 Excel
            </Button>
            {user.role === "member" ? null : (
              <Tooltip title="开始和暂停是同一个按钮，只切换登记队列，不在这里抽帧或调用模型。">
                <Button loading={toggle.isPending} onClick={() => toggle.mutate()}>
                  {queue.data?.paused ? "开始" : "暂停"}
                </Button>
              </Tooltip>
            )}
          </Space>
        }
      />
      <Alert
        style={{ marginBottom: 16 }}
        type="info"
        showIcon
        message="和桌面工具是同一套结果"
        description="命中列仍是时间点、类别、来源、原文、位置、置信度、判定理由、备注。画面和口播即使原文相同也不合并。已经排队的任务用当时抄下的规则，后来审批通过的词不影响它。这里只登记视频，不代理算力。"
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card title="上传" extra="mp4 / mov / mkv 等，单文件最大 2GB">
            {user.role === "member" ? null : (
              <Select
                style={{ width: "100%", marginBottom: 12 }}
                placeholder="选择成员"
                value={memberId ?? undefined}
                onChange={setMemberId}
                options={(members.data ?? [])
                  .filter((item) => item.status === "active")
                  .map((item) => ({ value: item.id, label: `${item.tenant_name} · ${item.name}` }))}
              />
            )}
            <Upload.Dragger
              multiple={false}
              showUploadList={false}
              accept=".mp4,.mov,.mkv,.avi,.webm,.m4v,.flv,.wmv,.mpeg,.mpg,.ts"
              customRequest={async (options) => {
                const form = new FormData();
                form.append("file", options.file as File);
                if (user.role !== "member") {
                  if (!memberId) {
                    message.warning("先选择成员");
                    options.onError?.(new Error("先选择成员"));
                    return;
                  }
                  form.append("member_id", String(memberId));
                }
                try {
                  const job = await api<Job>("/v1/jobs", { method: "POST", body: form });
                  options.onSuccess?.(job);
                  setSelectedId(job.id);
                  message.success("已登记。画面和口播识别在御镜桌面客户端完成。");
                  void client.invalidateQueries({ queryKey: ["jobs"] });
                } catch (error) {
                  options.onError?.(error as Error);
                  message.error(error instanceof ApiError ? error.message : "上传失败");
                }
              }}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">把视频拖到这里，或点击选择</p>
              <p className="ant-upload-hint">画面模型 GLM 5.3 Flash · 口播引擎 16k_zh。后台不调用模型。</p>
            </Upload.Dragger>
            <Table
              style={{ marginTop: 16 }}
              size="middle"
              rowKey="id"
              pagination={false}
              dataSource={rows}
              rowClassName={(row) => (row.id === selected?.id ? "row-selected" : "")}
              onRow={(row) => ({ onClick: () => setSelectedId(row.id), style: { cursor: "pointer" } })}
              columns={[
                {
                  title: "视频",
                  render: (_value, row) => (
                    <div>
                      <b>{row.filename}</b>
                      <div>
                        <Typography.Text type="secondary">{row.duration_label || "—"}</Typography.Text>
                      </div>
                    </div>
                  ),
                },
                { title: "公司 / 成员", render: (_value, row) => `${row.tenant_name} · ${row.member_name}` },
                {
                  title: "状态",
                  render: (_value, row) => (
                    <StatusTag
                      status={row.status}
                      label={row.status === "running" ? `${row.status_label} ${row.frame_done}/${row.frame_total}` : row.status_label}
                    />
                  ),
                },
                { title: "命中", dataIndex: "hit_count", width: 70, render: (value: number, row) => (row.status === "done" ? value : "—") },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={detail.data?.filename || "预览"} extra="原片在这里播放，剪辑仍在桌面客户端">
            <div className="video-stage">
              {videoUrl ? <video src={videoUrl} controls /> : <span>{detail.data?.has_file ? "正在加载视频" : "演示记录，没有视频文件"}</span>}
            </div>
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              {detail.data?.message || "选择左侧一条视频"}
            </Typography.Paragraph>
            <div style={{ marginTop: 12 }}>
              <Tooltip title="剪辑在御镜桌面客户端完成，按画面边距和口播字边界处理。">
                <Button disabled>生成剪辑</Button>
              </Tooltip>
            </div>
          </Card>
        </Col>
      </Row>
      <Card title="命中" extra={detail.data?.filename} style={{ marginTop: 16 }}>
        <Table
          rowKey={(_row, index) => String(index)}
          pagination={false}
          dataSource={detail.data?.findings ?? []}
          scroll={{ x: 960 }}
          locale={{ emptyText: "这条视频还没有命中" }}
          columns={[
            { title: "时间点", dataIndex: "时间点", width: 120 },
            { title: "类别", dataIndex: "类别", width: 110, render: (value: string) => <CategoryTag value={value} /> },
            { title: "来源", dataIndex: "来源", width: 90, render: (value: string) => <SourceTag value={value} /> },
            { title: "原文", dataIndex: "原文" },
            { title: "位置", dataIndex: "位置", width: 110 },
            { title: "置信度", dataIndex: "置信度", width: 90 },
            { title: "判定理由", dataIndex: "判定理由" },
            { title: "备注", dataIndex: "备注" },
          ]}
        />
      </Card>
    </>
  );
}
