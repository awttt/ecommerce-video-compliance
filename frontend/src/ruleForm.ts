import type { PhraseRule } from "./types";

export type RuleSource = "口播" | "画面";
export type RuleCategory = "价格" | "规格" | "品牌";

export interface RuleGroup {
  source: RuleSource;
  category: RuleCategory;
  title: string;
  hint: string;
}

export const RULE_GROUPS: RuleGroup[] = [
  {
    source: "口播",
    category: "价格",
    title: "口播 · 价格",
    hint: "一行一个。X 表示数字，例如 X折 能命中「5折」或「五折」，X.X贵 能命中「3.5贵」。",
  },
  {
    source: "口播",
    category: "规格",
    title: "口播 · 规格",
    hint: "一行一个。数量、尺寸、材质。拍1发X 里的 1 是数字 1，X 是后面的数量。",
  },
  {
    source: "口播",
    category: "品牌",
    title: "口播 · 品牌",
    hint: "一行一个。口播里连续说出来才命中。",
  },
  {
    source: "画面",
    category: "价格",
    title: "画面 · 价格",
    hint: "一行一个。只发给画面模型，必须在这一帧里看得见。",
  },
  {
    source: "画面",
    category: "规格",
    title: "画面 · 规格",
    hint: "一行一个。只发给画面模型，必须在这一帧里看得见。",
  },
  {
    source: "画面",
    category: "品牌",
    title: "画面 · 品牌",
    hint: "一行一个。Logo、商标、水印、包装标写在这里，必须在这一帧里看得见。",
  },
];

const MAX_PHRASES = 300;
const MAX_LENGTH = 40;

export function groupKey(source: string, category: string): string {
  return `${source}:${category}`;
}

export function draftsFromPhrases(phrases: PhraseRule[]): Record<string, string> {
  const buckets = new Map<string, string[]>();
  for (const group of RULE_GROUPS) {
    buckets.set(groupKey(group.source, group.category), []);
  }
  for (const item of phrases) {
    const source = item.source === "画面" ? "画面" : "口播";
    const key = groupKey(source, item.category);
    const lines = buckets.get(key);
    if (!lines || !item.text) continue;
    lines.push(item.text);
  }
  return Object.fromEntries([...buckets].map(([key, lines]) => [key, lines.join("\n")]));
}

export function phrasesFromDrafts(drafts: Record<string, string>): { phrases: PhraseRule[]; error: string } {
  const phrases: PhraseRule[] = [];
  const seen = new Set<string>();
  for (const group of RULE_GROUPS) {
    const raw = drafts[groupKey(group.source, group.category)] ?? "";
    for (const line of raw.split(/\n+/)) {
      const text = line.replace(/\s+/g, "");
      if (!text) continue;
      if ([...text].length > MAX_LENGTH) {
        return { phrases: [], error: `「${line.trim()}」超过 40 个字` };
      }
      const key = `${group.source}:${group.category}:${text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      phrases.push({ text, category: group.category, source: group.source });
    }
  }
  if (phrases.length > MAX_PHRASES) {
    return { phrases: [], error: "额外命中词最多 300 条" };
  }
  return { phrases, error: "" };
}
