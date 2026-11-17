/**
 * 识别结果列表的筛选、分页和进度条定位。
 *
 * 类别用接口里的「类别」字段（Finding.category）。流水线写入的值是
 * 「价格」「规格」「品牌」「夸大宣传」，解析失败的帧是「解析失败」。
 * 筛选按这个字段匹配，不从原文里再猜一次，避免一条命中被同时算进两类。
 * price / spec / specification / brand 只作为别名，和解析器保持一致。
 *
 * 分页的 pageSize 必须由调用方自己记住。Ant Design Table 收到 pagination.pageSize
 * 后会把它当成受控值；选择器改条数只会改内部状态，下一次渲染又被写死的值盖回去。
 */

export const FILTER_ALL = "全部";
export const PROBLEM_CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"] as const;
export const CATEGORY_FILTERS = [FILTER_ALL, ...PROBLEM_CATEGORIES] as const;
export const DEFAULT_PAGE_SIZE = 8;
export const PAGE_SIZE_OPTIONS = [8, 10, 20, 50, 100] as const;

const CATEGORY_ALIASES: Record<string, string> = {
  价格: "价格",
  price: "价格",
  规格: "规格",
  spec: "规格",
  specification: "规格",
  品牌: "品牌",
  brand: "品牌",
  夸大宣传: "夸大宣传",
};

export interface Categorized {
  类别: string;
}

export function findingKey(finding: { start_sec: number; 来源: string; 类别: string; 原文: string }): string {
  return `${finding.start_sec}-${finding.来源}-${finding.类别}-${finding.原文}`;
}

export function canonicalCategory(category: string): string {
  const text = category.trim();
  return CATEGORY_ALIASES[text] ?? CATEGORY_ALIASES[text.toLowerCase()] ?? text;
}

export function filterFindings<T extends Categorized>(items: readonly T[], category: string): readonly T[] {
  if (!category || category === FILTER_ALL) return items;
  const wanted = canonicalCategory(category);
  return items.filter((item) => canonicalCategory(item.类别) === wanted);
}

export function selectCategory(category: string): { filter: string; page: number } {
  const known = (CATEGORY_FILTERS as readonly string[]).includes(category);
  return { filter: known ? category : FILTER_ALL, page: 1 };
}

export function normalizePageSize(pageSize: number): number {
  if (!Number.isFinite(pageSize) || pageSize < 1) return DEFAULT_PAGE_SIZE;
  return Math.floor(pageSize);
}

export function pageCount(total: number, pageSize: number): number {
  const size = normalizePageSize(pageSize);
  if (total <= 0) return 1;
  return Math.ceil(total / size);
}

export function clampPage(page: number, total: number, pageSize: number): number {
  const pages = pageCount(total, pageSize);
  const current = Number.isFinite(page) ? Math.trunc(page) : 1;
  if (current < 1) return 1;
  return Math.min(current, pages);
}

export function pageForIndex(index: number, pageSize: number): number {
  if (!Number.isFinite(index) || index < 0) return 1;
  return Math.floor(index / normalizePageSize(pageSize)) + 1;
}

export function pageSlice<T>(items: readonly T[], page: number, pageSize: number): T[] {
  const size = normalizePageSize(pageSize);
  const current = clampPage(page, items.length, size);
  const start = (current - 1) * size;
  return items.slice(start, start + size);
}

export function applyPageChange(
  current: number,
  pageSize: number,
  total: number,
): { page: number; pageSize: number; pages: number } {
  const size = normalizePageSize(pageSize);
  return { page: clampPage(current, total, size), pageSize: size, pages: pageCount(total, size) };
}

export interface LocateResult {
  filter: string;
  page: number;
  clearedFilter: boolean;
  index: number;
}

export function locateFinding<T extends Categorized>(
  items: readonly T[],
  finding: T,
  filter: string,
  pageSize: number,
): LocateResult {
  if (items.indexOf(finding) < 0) {
    const visible = filterFindings(items, filter);
    return { filter, page: clampPage(1, visible.length, pageSize), clearedFilter: false, index: -1 };
  }
  const clearedFilter = filter !== FILTER_ALL && canonicalCategory(filter) !== canonicalCategory(finding.类别);
  const nextFilter = clearedFilter ? FILTER_ALL : filter;
  const visible = filterFindings(items, nextFilter);
  const index = visible.indexOf(finding);
  return {
    filter: nextFilter,
    page: pageForIndex(index, pageSize),
    clearedFilter,
    index,
  };
}

export function findingsEmptyText(running: boolean, filter: string): string {
  if (running) return "识别进行中，命中会陆续出现";
  if (filter && filter !== FILTER_ALL) return `没有「${filter}」类问题`;
  return "没有发现画面或口播中的价格、规格、品牌或夸大宣传";
}

export function lastMatchingRow<T extends { getAttribute(name: string): string | null }>(
  rows: ArrayLike<T>,
  key: string,
): T | null {
  let found: T | null = null;
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (row?.getAttribute("data-row-key") === key) found = row;
  }
  return found;
}
