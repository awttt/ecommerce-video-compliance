import assert from "node:assert/strict";
import test from "node:test";

import { showTestMenu } from "./src/testMenu.ts";

test("only the designated account sees the test menu", () => {
  assert.equal(showTestMenu("350944364@qq.com"), true);
  assert.equal(showTestMenu(" 350944364@QQ.com "), true);
  assert.equal(showTestMenu("other@qq.com"), false);
  assert.equal(showTestMenu(""), false);
  assert.equal(showTestMenu(null), false);
});
