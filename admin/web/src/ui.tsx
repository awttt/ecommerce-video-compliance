import { Tag, Typography } from "antd";
import type { ReactNode } from "react";

const CATEGORY: Record<string, { color: string; background: string }> = {
  价格: { color: "#a24e3c", background: "#faefeb" },
  规格: { color: "#8f6a20", background: "#f8f2e5" },
  品牌: { color: "#3b6a8c", background: "#ebf2f7" },
  夸大宣传: { color: "#7a4e78", background: "#f6eef5" },
  解析失败: { color: "#cf1322", background: "#fff1f0" },
};

export function CategoryTag({ value }: { value: string }) {
  const tone = CATEGORY[value];
  if (!tone) return <Tag>{value}</Tag>;
  return (
    <Tag style={{ color: tone.color, background: tone.background, borderColor: "transparent", marginInlineEnd: 0 }}>
      {value}
    </Tag>
  );
}

export function SourceTag({ value }: { value: string }) {
  const tone = value === "口播"
    ? { color: "#6b5a8c", background: "#f1edf8" }
    : { color: "#3b6a8c", background: "#ebf2f7" };
  return (
    <Tag style={{ color: tone.color, background: tone.background, borderColor: "transparent", marginInlineEnd: 0 }}>
      {value || "—"}
    </Tag>
  );
}

const STATUS: Record<string, string> = {
  active: "success",
  disabled: "default",
  invited: "processing",
  pending: "warning",
  approved: "success",
  rejected: "error",
  success: "success",
  failed: "error",
  processing: "processing",
  queued: "default",
  running: "processing",
  paused: "warning",
  cancelled: "default",
  done: "success",
  在营: "success",
  已停用: "default",
  正常: "success",
  待审: "warning",
  已通过: "success",
  已驳回: "error",
  排队: "default",
  识别中: "processing",
  已暂停: "warning",
  已取消: "default",
  已完成: "success",
  成功: "success",
  失败: "error",
  处理中: "processing",
};

export function StatusTag({ status, label }: { status: string; label: string }) {
  return <Tag color={STATUS[status] || STATUS[label] || "default"}>{label}</Tag>;
}

export function PageHead({
  title,
  description,
  extra,
}: {
  title: string;
  description?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        <Typography.Title level={4}>{title}</Typography.Title>
        {description ? (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {description}
          </Typography.Paragraph>
        ) : null}
      </div>
      <div className="page-extra">{extra}</div>
    </div>
  );
}