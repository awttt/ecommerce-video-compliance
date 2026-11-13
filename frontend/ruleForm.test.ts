import assert from "node:assert/strict";
import test from "node:test";

import { draftsFromPhrases, phrasesFromDrafts, RULE_GROUPS, groupKey } from "./src/ruleForm.ts";

test("phrases split into source and category boxes", () => {
  const drafts = draftsFromPhrases([
    { text: "X折", category: "价格", source: "口播" },
    { text: "到手", category: "价格" },
    { text: "logo", category: "品牌", source: "画面" },
    { text: "个", category: "规格", source: "口播" },
  ]);
  assert.equal(drafts[groupKey("口播", "价格")], "X折\n到手");
  assert.equal(drafts[groupKey("口播", "规格")], "个");
  assert.equal(drafts[groupKey("画面", "品牌")], "logo");
  assert.equal(drafts[groupKey("画面", "价格")], "");
  assert.equal(RULE_GROUPS.length, 6);
});

test("saving compacts spaces and keeps the group", () => {
  const drafts = draftsFromPhrases([]);
  drafts[groupKey("口播", "规格")] = "拍 1 发 X\n304 不锈钢\n拍1发X";
  drafts[groupKey("画面", "品牌")] = "品牌 slogan\n®";
  const parsed = phrasesFromDrafts(drafts);
  assert.equal(parsed.error, "");
  assert.deepEqual(parsed.phrases, [
    { text: "拍1发X", category: "规格", source: "口播" },
    { text: "304不锈钢", category: "规格", source: "口播" },
    { text: "品牌slogan", category: "品牌", source: "画面" },
    { text: "®", category: "品牌", source: "画面" },
  ]);
});

test("a line longer than 40 characters is rejected", () => {
  const drafts = draftsFromPhrases([]);
  drafts[groupKey("口播", "价格")] = "价".repeat(41);
  const parsed = phrasesFromDrafts(drafts);
  assert.equal(parsed.phrases.length, 0);
  assert.match(parsed.error, /40/);
});
