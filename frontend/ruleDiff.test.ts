import assert from "node:assert/strict";
import test from "node:test";

import { diffCompanyRules, type DiffBaseline } from "./src/ruleDiff.ts";

function baseline(): DiffBaseline {
  return {
    categories: ["价格", "规格", "品牌", "夸大宣传"],
    phrases: [
      { id: "a", text: "到手", category: "价格", source: "口播" },
      { id: "b", text: "5折", category: "价格", source: "口播" },
      { id: "c", text: "logo", category: "品牌", source: "画面" },
    ],
    ignore_phrases: [{ id: "i", text: "包邮" }],
    note: "",
  };
}

test("改一个字是修改，其余不动", () => {
  const changes = diffCompanyRules(baseline(), {
    categories: ["价格", "规格", "品牌", "夸大宣传"],
    phrases: [
      { text: "到手价", category: "价格", source: "口播" },
      { text: "5折", category: "价格", source: "口播" },
      { text: "logo", category: "品牌", source: "画面" },
    ],
    ignore_phrases: ["包邮"],
    note: "",
  });
  assert.deepEqual(
    changes.map((item) => item.summary),
    ["修改口播价格「到手」为「到手价」"],
  );
  assert.equal(changes[0].item_id, "a");
  assert.equal(changes[0].op, "update");
});

test("新增和删除各是一条，调换顺序不算", () => {
  const changes = diffCompanyRules(baseline(), {
    categories: ["价格", "规格", "品牌", "夸大宣传"],
    phrases: [
      { text: "5折", category: "价格", source: "口播" },
      { text: "到手", category: "价格", source: "口播" },
      { text: "全网最低", category: "价格", source: "口播" },
      { text: "logo", category: "品牌", source: "画面" },
    ],
    ignore_phrases: [],
    note: "只补充",
  });
  assert.deepEqual(
    changes.map((item) => `${item.op}:${item.summary}`),
    ["add:新增口播价格「全网最低」", "delete:删除忽略词「包邮」", "update:修改补充说明"],
  );
});

test("停用类别单独成一条", () => {
  const changes = diffCompanyRules(baseline(), {
    categories: ["价格", "品牌", "夸大宣传"],
    phrases: [
      { text: "到手", category: "价格", source: "口播" },
      { text: "5折", category: "价格", source: "口播" },
      { text: "logo", category: "品牌", source: "画面" },
    ],
    ignore_phrases: ["包邮"],
    note: "",
  });
  assert.deepEqual(
    changes.map((item) => item.summary),
    ["停用类别「规格」"],
  );
});
