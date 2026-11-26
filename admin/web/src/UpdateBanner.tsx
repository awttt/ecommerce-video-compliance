import { Alert, Button, Progress } from "antd";

import { updateVisible, type UpdateStatus } from "./updateMenu";

export function UpdateBanner({
  info,
  starting,
  onStart,
}: {
  info: UpdateStatus | null;
  starting: boolean;
  onStart: () => void;
}) {
  if (!updateVisible(info) || !info) return null;
  const percent = (info.total ?? 0) > 0 ? Math.min(99, Math.round(((info.received ?? 0) / (info.total ?? 1)) * 100)) : 0;
  let text = `发现新版本 ${info.latest}，当前是 ${info.current}。更新会下载安装包并覆盖本机程序，已保存的审核数据会保留。`;
  if (!info.can_apply) text = `发现新版本 ${info.latest}。只有 Windows 安装版可以在这里直接更新。`;
  if (info.phase === "downloading") text = info.message || "正在下载安装包";
  if (info.phase === "installing") text = info.message || "正在安装，程序会关闭并在完成后重新打开";
  if (info.phase === "error") text = info.message || "更新失败，请稍后再试";
  return (
    <Alert
      type={info.phase === "error" ? "error" : "info"}
      showIcon
      message={text}
      action={
        info.can_apply && info.phase !== "installing" ? (
          <Button size="small" type="primary" loading={starting || info.phase === "downloading"} onClick={onStart}>
            {info.phase === "error" ? "重试" : "立即更新"}
          </Button>
        ) : null
      }
      description={info.phase === "downloading" ? <Progress percent={percent} size="small" /> : null}
    />
  );
}
