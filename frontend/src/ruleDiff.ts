/** 对照公司当前生效的规则，拆出新增、修改、删除。和后台的对齐方式一致：同一格里改字算修改，只调顺序不算。 */

export interface DiffPhrase {
  id?: string;
  text: string;
  category: string;
  source: string;
}

export interface DiffIgnore {
  id?: string;
  text: string;
}

export interface RuleChange {
  op: "add" | "update" | "delete";
  target: "phrase" | "ignore" | "category" | "note";
  item_id: string;
  before: Record<string, string> | null;
  after: Record<string, string> | null;
  summary: string;
}

export interface DiffBaseline {
  categories: string[];
  phrases: DiffPhrase[];
  ignore_phrases: Array<string | DiffIgnore>;
  note: string;
}

export interface DiffDraft {
  categories: string[];
  phrases: { text: string; category: string; source: string }[];
  ignore_phrases: string[];
  note: string;
}

const GROUPS: Array<[string, string]> = [
  ["口播", "价格"],
  ["口播", "规格"],
  ["口播", "品牌"],
  ["画面", "价格"],
  ["画面", "规格"],
  ["画面", "品牌"],
];

function compact(value: string): string {
  return value.replace(/\s+/g, "");
}

function lcsPairs(oldTexts: string[], newTexts: string[]): Array<[number, number]> {
  const rows = oldTexts.length;
  const cols = newTexts.length;
  const scores = Array.from({ length: rows + 1 }, () => new Array<number>(cols + 1).fill(0));
  for (let i = 1; i <= rows; i += 1) {
    for (let j = 1; j <= cols; j += 1) {
      if (oldTexts[i - 1] === newTexts[j - 1]) scores[i][j] = scores[i - 1][j - 1] + 1;
      else scores[i][j] = scores[i - 1][j] >= scores[i][j - 1] ? scores[i - 1][j] : scores[i][j - 1];
    }
  }
  const pairs: Array<[number, number]> = [];
  let i = rows;
  let j = cols;
  while (i > 0 && j > 0) {
    if (oldTexts[i - 1] === newTexts[j - 1] && scores[i][j] === scores[i - 1][j - 1] + 1) {
      pairs.push([i - 1, j - 1]);
      i -= 1;
      j -= 1;
    } else if (scores[i - 1][j] >= scores[i][j - 1]) {
      i -= 1;
    } else {
      j -= 1;
    }
  }
  pairs.reverse();
  return pairs;
}

function leftovers<T extends { text: string }>(oldItems: T[], newTexts: string[]): { old: T[]; next: string[] } {
  const pairs = lcsPairs(
    oldItems.map((item) => item.text),
    newTexts,
  );
  const matchedOld = new Set(pairs.map(([left]) => left));
  const matchedNew = new Set(pairs.map(([, right]) => right));
  return {
    old: oldItems.filter((_, index) => !matchedOld.has(index)),
    next: newTexts.filter((_, index) => !matchedNew.has(index)),
  };
}

function phraseChanges(oldItems: DiffPhrase[], newTexts: string[], source: string, category: string): RuleChange[] {
  const { old, next } = leftovers(oldItems, newTexts);
  const pair = Math.min(old.length, next.length);
  const changes: RuleChange[] = [];
  for (let index = 0; index < pair; index += 1) {
    const previous = old[index];
    const text = next[index];
    if (previous.text === text) continue;
    changes.push({
      op: "update",
      target: "phrase",
      item_id: previous.id || "",
      before: { text: previous.text, category, source },
      after: { text, category, source },
      summary: `修改${source}${category}「${previous.text}」为「${text}」`,
    });
  }
  for (const previous of old.slice(pair)) {
    changes.push({
      op: "delete",
      target: "phrase",
      item_id: previous.id || "",
      before: { text: previous.text, category, source },
      after: null,
      summary: `删除${source}${category}「${previous.text}」`,
    });
  }
  for (const text of next.slice(pair)) {
    changes.push({
      op: "add",
      target: "phrase",
      item_id: "",
      before: null,
      after: { text, category, source },
      summary: `新增${source}${category}「${text}」`,
    });
  }
  return changes;
}

function ignoreChanges(oldItems: DiffIgnore[], newTexts: string[]): RuleChange[] {
  const { old, next } = leftovers(oldItems, newTexts);
  const pair = Math.min(old.length, next.length);
  const changes: RuleChange[] = [];
  for (let index = 0; index < pair; index += 1) {
    const previous = old[index];
    const text = next[index];
    if (previous.text === text) continue;
    changes.push({
      op: "update",
      target: "ignore",
      item_id: previous.id || "",
      before: { text: previous.text },
      after: { text },
      summary: `修改忽略词「${previous.text}」为「${text}」`,
    });
  }
  for (const previous of old.slice(pair)) {
    changes.push({
      op: "delete",
      target: "ignore",
      item_id: previous.id || "",
      before: { text: previous.text },
      after: null,
      summary: `删除忽略词「${previous.text}」`,
    });
  }
  for (const text of next.slice(pair)) {
    changes.push({
      op: "add",
      target: "ignore",
      item_id: "",
      before: null,
      after: { text },
      summary: `新增忽略词「${text}」`,
    });
  }
  return changes;
}

function ignoreItems(items: Array<string | DiffIgnore>): DiffIgnore[] {
  return items.map((item) => (typeof item === "string" ? { text: compact(item) } : { id: item.id, text: compact(item.text || "") }));
}

export function diffCompanyRules(baseline: DiffBaseline, draft: DiffDraft): RuleChange[] {
  const changes: RuleChange[] = [];
  for (const [source, category] of GROUPS) {
    const oldItems = baseline.phrases.filter((item) => (item.source === "画面" ? "画面" : "口播") === source && item.category === category);
    const newTexts = draft.phrases
      .filter((item) => (item.source === "画面" ? "画面" : "口播") === source && item.category === category)
      .map((item) => compact(item.text))
      .filter(Boolean);
    changes.push(...phraseChanges(oldItems, newTexts, source, category));
  }
  const oldIgnore = ignoreItems(baseline.ignore_phrases).filter((item) => item.text);
  const newIgnore = draft.ignore_phrases.map((item) => compact(item)).filter(Boolean);
  changes.push(...ignoreChanges(oldIgnore, newIgnore));
  const enabled = new Set(baseline.categories);
  const wanted = new Set(draft.categories);
  for (const name of draft.categories) {
    if (enabled.has(name)) continue;
    changes.push({
      op: "add",
      target: "category",
      item_id: "",
      before: null,
      after: { name },
      summary: `启用类别「${name}」`,
    });
  }
  for (const name of baseline.categories) {
    if (wanted.has(name)) continue;
    changes.push({
      op: "delete",
      target: "category",
      item_id: "",
      before: { name },
      after: null,
      summary: `停用类别「${name}」`,
    });
  }
  const beforeNote = (baseline.note || "").trim();
  const afterNote = (draft.note || "").trim();
  if (beforeNote !== afterNote) {
    changes.push({
      op: "update",
      target: "note",
      item_id: "",
      before: { text: beforeNote },
      after: { text: afterNote },
      summary: "修改补充说明",
    });
  }
  return changes;
}
