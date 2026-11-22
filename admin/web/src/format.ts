import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import relativeTime from "dayjs/plugin/relativeTime";

dayjs.extend(relativeTime);
dayjs.locale("zh-cn");

export function money(value: number): string {
  return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function when(value?: string | null): string {
  if (!value) return "—";
  return dayjs(value).format("MM-DD HH:mm");
}

export function fromNow(value?: string | null): string {
  if (!value) return "";
  return dayjs(value).fromNow();
}

export function tokenText(value: number): string {
  if (value >= 1_000_000) {
    const text = (value / 1_000_000).toFixed(1);
    return `${text.endsWith(".0") ? text.slice(0, -2) : text}M`;
  }
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`;
  return String(value);
}

export function deltaText(current: number, previous: number, label = "较上月"): string | null {
  if (!previous) return null;
  const rate = ((current - previous) / previous) * 100;
  const sign = rate >= 0 ? "+" : "";
  return `${label} ${sign}${rate.toFixed(1)}%`;
}
