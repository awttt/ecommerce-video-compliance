const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_ROWS = 300;
const NAME_HEADERS = new Set(["姓名", "名字", "name"]);
const EMAIL_HEADERS = new Set(["邮箱", "邮箱号", "邮箱地址", "email", "e-mail"]);
const TITLE_HEADERS = new Set(["职位", "岗位", "职务", "title"]);

export type ImportStatus = "ready" | "sending" | "ok" | "fail" | "skipped";

export type ImportDraft = {
  key: string;
  title: string;
  name: string;
  email: string;
  status: ImportStatus;
  message: string;
  token: string;
};

export type TakenEmails = {
  members?: string[];
  invites?: string[];
};

export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}

export function inviteUrl(token: string, page?: { hostname: string; origin: string }): string {
  const hostname = page?.hostname ?? location.hostname;
  const origin = (page?.origin ?? location.origin).replace(/\/$/, "");
  const local = hostname === "localhost" || hostname === "127.0.0.1";
  const base = local ? "https://admin.token4u.cc" : origin;
  return `${base}/invite/${encodeURIComponent(token)}`;
}

function cellText(value: unknown): string {
  if (value == null) return "";
  return String(value).replace(/\u00a0/g, " ").trim();
}

function headerKind(value: unknown): "name" | "email" | "title" | "" {
  const text = cellText(value).toLowerCase().replace(/\s+/g, "");
  if (NAME_HEADERS.has(text)) return "name";
  if (EMAIL_HEADERS.has(text)) return "email";
  if (TITLE_HEADERS.has(text)) return "title";
  return "";
}

function draft(partial: { key: string; title: string; name: string; email: string }): ImportDraft {
  return {
    key: partial.key,
    title: partial.title,
    name: partial.name,
    email: normalizeEmail(partial.email),
    status: "ready",
    message: "",
    token: "",
  };
}

export function rowsFromGrid(grid: unknown[][]): ImportDraft[] {
  let headerAt = -1;
  let nameCol = -1;
  let emailCol = -1;
  let titleCol = -1;
  const scan = Math.min(grid.length, 10);
  for (let index = 0; index < scan; index += 1) {
    const row = grid[index] ?? [];
    let name = -1;
    let email = -1;
    let title = -1;
    row.forEach((cell, column) => {
      const kind = headerKind(cell);
      if (kind === "name" && name < 0) name = column;
      if (kind === "email" && email < 0) email = column;
      if (kind === "title" && title < 0) title = column;
    });
    if (name >= 0 && email >= 0) {
      headerAt = index;
      nameCol = name;
      emailCol = email;
      titleCol = title;
      break;
    }
  }
  if (headerAt < 0) throw new Error("表格需要有「姓名」和「邮箱」两列");

  const rows: ImportDraft[] = [];
  for (let index = headerAt + 1; index < grid.length; index += 1) {
    const row = grid[index] ?? [];
    if (row.every((cell) => cellText(cell) === "")) continue;
    rows.push(
      draft({
        key: `row-${index + 1}`,
        title: titleCol >= 0 ? cellText(row[titleCol]) : "",
        name: cellText(row[nameCol]),
        email: cellText(row[emailCol]),
      }),
    );
    if (rows.length > MAX_ROWS) throw new Error(`一次最多导入 ${MAX_ROWS} 人`);
  }
  if (rows.length === 0) throw new Error("表格里没有可邀请的人");
  return rows;
}

const TEMPLATE_HEADERS = ["职位", "姓名", "邮箱号"];
const TEMPLATE_NAME = "成员导入模板.xlsx";

async function templateWorkbook() {
  const XLSX = await import("xlsx");
  const book = XLSX.utils.book_new();
  const sheet = XLSX.utils.aoa_to_sheet([TEMPLATE_HEADERS]);
  sheet["!cols"] = [{ wch: 12 }, { wch: 16 }, { wch: 28 }];
  XLSX.utils.book_append_sheet(book, sheet, "成员");
  return { XLSX, book };
}

export async function importTemplateBytes(): Promise<Uint8Array> {
  const { XLSX, book } = await templateWorkbook();
  return XLSX.write(book, { type: "array", bookType: "xlsx" }) as Uint8Array;
}

export async function downloadImportTemplate(): Promise<void> {
  const { XLSX, book } = await templateWorkbook();
  XLSX.writeFile(book, TEMPLATE_NAME);
}

export async function rowsFromWorkbook(data: ArrayBuffer | Uint8Array): Promise<ImportDraft[]> {
  const XLSX = await import("xlsx");
  let book: ReturnType<typeof XLSX.read>;
  try {
    book = XLSX.read(data, { type: "array" });
  } catch {
    throw new Error("这个表格打不开，请另存为 xlsx 后再导入");
  }
  const sheetName = book.SheetNames[0];
  if (!sheetName) throw new Error("表格是空的");
  const grid = XLSX.utils.sheet_to_json(book.Sheets[sheetName], {
    header: 1,
    raw: false,
    defval: "",
  }) as unknown[][];
  return rowsFromGrid(grid);
}

export function issueField(issue: string): "name" | "email" | "" {
  if (!issue) return "";
  if (issue.includes("姓名")) return "name";
  return "email";
}

export function issuesFor(rows: ImportDraft[], taken: TakenEmails = {}): Record<string, string> {
  const members = new Set((taken.members ?? []).map(normalizeEmail).filter(Boolean));
  const invites = new Set((taken.invites ?? []).map(normalizeEmail).filter(Boolean));
  const counts = new Map<string, number>();
  for (const row of rows) {
    if (row.status === "ok") continue;
    const email = normalizeEmail(row.email);
    if (!email) continue;
    counts.set(email, (counts.get(email) ?? 0) + 1);
  }
  const invitedNow = new Set(
    rows.filter((row) => row.status === "ok").map((row) => normalizeEmail(row.email)).filter(Boolean),
  );
  const issues: Record<string, string> = {};
  for (const row of rows) {
    if (row.status === "ok") continue;
    const name = row.name.trim();
    const email = normalizeEmail(row.email);
    const nameLength = [...name].length;
    if (!name) issues[row.key] = "请填写姓名";
    else if (nameLength > 40) issues[row.key] = "姓名最多 40 个字";
    else if (!email) issues[row.key] = "请填写邮箱";
    else if (email.length > 120 || !EMAIL_RE.test(email)) issues[row.key] = "邮箱不正确";
    else if (members.has(email)) issues[row.key] = "这个邮箱已经开通";
    else if (invites.has(email)) issues[row.key] = "这个邮箱已有未使用的邀请";
    else if (invitedNow.has(email)) issues[row.key] = "这个邮箱刚刚已经导入过";
    else if ((counts.get(email) ?? 0) > 1) issues[row.key] = "这个邮箱在表里重复了";
  }
  return issues;
}
