import dayjs from "dayjs";

/** 消耗区间按业务时区切日，不跟浏览器本地时区。 */
export const BUSINESS_TIMEZONE = "Asia/Shanghai";

export type RangePreset = "today" | "week" | "month" | "lastMonth" | "days7" | "days30";

export function shanghaiToday(): dayjs.Dayjs {
  const text = new Intl.DateTimeFormat("en-CA", {
    timeZone: BUSINESS_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  return dayjs(text);
}

function iso(value: dayjs.Dayjs): string {
  return value.format("YYYY-MM-DD");
}

export function rangeFor(preset: RangePreset, today = shanghaiToday()): [string, string] {
  if (preset === "today") return [iso(today), iso(today)];
  if (preset === "week") {
    const weekday = today.day();
    const offset = weekday === 0 ? 6 : weekday - 1;
    return [iso(today.subtract(offset, "day")), iso(today)];
  }
  if (preset === "month") return [iso(today.startOf("month")), iso(today)];
  if (preset === "lastMonth") {
    const last = today.subtract(1, "month");
    return [iso(last.startOf("month")), iso(last.endOf("month"))];
  }
  if (preset === "days7") return [iso(today.subtract(6, "day")), iso(today)];
  return [iso(today.subtract(29, "day")), iso(today)];
}

export function consumptionTitle(preset: RangePreset | "custom"): string {
  switch (preset) {
    case "today":
      return "今日消耗";
    case "week":
      return "本周消耗";
    case "month":
      return "本月消耗";
    case "lastMonth":
      return "上月消耗";
    case "days7":
      return "近 7 日消耗";
    case "days30":
      return "近 30 日消耗";
    default:
      return "区间消耗";
  }
}
