/** 与后端 `_ALLOWED` 一致。文件夹里其它文件直接跳过。 */
export const VIDEO_EXTENSIONS = [
  ".mp4",
  ".mov",
  ".mkv",
  ".avi",
  ".webm",
  ".m4v",
  ".flv",
  ".wmv",
  ".mpeg",
  ".mpg",
  ".ts",
] as const;

export const MAX_PICKED = 20;

const MAX_DEPTH = 32;
const SKIP_DIRS = new Set(["__MACOSX"]);

export type PickedVideo = {
  file: File;
  /** 列表里显示的名字。文件夹里的视频带相对路径，避免不同目录的同名文件看起来一样。 */
  label: string;
};

export type DropEntry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  fullPath?: string;
  file?: (success: (file: File) => void, error?: (err: unknown) => void) => void;
  createReader?: () => {
    readEntries: (success: (entries: DropEntry[]) => void, error?: (err: unknown) => void) => void;
  };
};

export function isVideoName(name: string): boolean {
  const lower = name.toLowerCase();
  return VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export function pickedKey(item: PickedVideo): string {
  return `${item.label}\0${item.file.size}\0${item.file.lastModified}`;
}

export function mergePicked(current: PickedVideo[], incoming: PickedVideo[], limit = MAX_PICKED) {
  const seen = new Set(current.map(pickedKey));
  const next = current.slice();
  let added = 0;
  let duplicate = 0;
  let overflow = 0;
  for (const item of incoming) {
    const key = pickedKey(item);
    if (seen.has(key)) {
      duplicate += 1;
      continue;
    }
    if (next.length >= limit) {
      overflow += 1;
      continue;
    }
    seen.add(key);
    next.push(item);
    added += 1;
  }
  return { next, added, duplicate, overflow };
}

export function folderImportMessage(
  found: number,
  merged: { added: number; duplicate: number; overflow: number },
): { type: "success" | "info" | "warning"; text: string } {
  if (found <= 0) return { type: "warning", text: "文件夹里没有支持的视频" };
  if (merged.added === 0 && merged.overflow === 0) return { type: "info", text: "这些视频已经在列表里" };
  if (merged.added === 0) {
    return { type: "warning", text: `一次最多 ${MAX_PICKED} 个，还有 ${merged.overflow} 个没有加入` };
  }
  const parts = [`已加入 ${merged.added} 个视频`];
  if (merged.duplicate > 0) parts.push(`${merged.duplicate} 个已在列表中`);
  if (merged.overflow > 0) parts.push(`一次最多 ${MAX_PICKED} 个，还有 ${merged.overflow} 个没有加入`);
  return { type: merged.overflow > 0 ? "warning" : "success", text: parts.join("，") };
}

function readChildren(entry: DropEntry): Promise<DropEntry[]> {
  if (!entry.createReader) return Promise.resolve([]);
  let reader: ReturnType<NonNullable<DropEntry["createReader"]>>;
  try {
    reader = entry.createReader();
  } catch {
    return Promise.resolve([]);
  }
  const all: DropEntry[] = [];
  const pull = (turn: number): Promise<DropEntry[]> => {
    if (turn > 10000) return Promise.resolve(all);
    return new Promise<DropEntry[]>((resolve) => {
      try {
        reader.readEntries(resolve, () => resolve([]));
      } catch {
        resolve([]);
      }
    }).then((batch) => {
      if (batch.length === 0) return all;
      all.push(...batch);
      return pull(turn + 1);
    });
  };
  return pull(0);
}

function readEntryFile(entry: DropEntry): Promise<File | null> {
  if (!entry.file) return Promise.resolve(null);
  return new Promise((resolve) => {
    try {
      entry.file?.((file) => resolve(file), () => resolve(null));
    } catch {
      resolve(null);
    }
  });
}

/** 递归读取拖入的目录。`readEntries` 每次只给一批，要一直读到空。 */
export async function videosFromEntries(entries: DropEntry[]): Promise<PickedVideo[]> {
  const found: PickedVideo[] = [];

  async function walk(entry: DropEntry, path: string, depth: number): Promise<void> {
    if (depth > MAX_DEPTH) return;
    const name = entry.name || "";
    if (!name || name === "." || name === "..") return;
    const nextPath = path ? `${path}/${name}` : name;
    if (entry.isDirectory) {
      if (name.startsWith(".") || SKIP_DIRS.has(name)) return;
      const children = await readChildren(entry);
      await Promise.all(children.map((child) => walk(child, nextPath, depth + 1)));
      return;
    }
    if (!entry.isFile || name.startsWith(".") || !isVideoName(name)) return;
    const file = await readEntryFile(entry);
    if (!file || !isVideoName(file.name)) return;
    const label = (entry.fullPath || nextPath).replace(/^\/+/, "") || file.name;
    found.push({ file, label });
  }

  await Promise.all(entries.map((entry) => walk(entry, "", 0)));
  found.sort((left, right) => left.label.localeCompare(right.label, "zh-CN", { numeric: true }));
  return found;
}

export function directoryEntries(items: DataTransferItemList | undefined | null): DropEntry[] {
  if (!items) return [];
  const entries: DropEntry[] = [];
  for (const item of Array.from(items)) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry?.();
    if (entry?.isDirectory) entries.push(entry as unknown as DropEntry);
  }
  return entries;
}
