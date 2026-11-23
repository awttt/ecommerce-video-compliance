import assert from "node:assert/strict";
import test from "node:test";
import { assignableMemberIds, chunkIds } from "./src/quotaSelection.ts";

test("selects everyone except admins and keeps the list order", () => {
  assert.deepEqual(
    assignableMemberIds([
      { id: 1, role: "tenant_admin" },
      { id: 2, role: "member" },
      { id: 3, role: "platform_admin" },
      { id: 4, role: "member" },
      { id: 2, role: "member" },
    ]),
    [2, 4],
  );
});

test("splits a full-company selection into batches of 20", () => {
  const ids = Array.from({ length: 25 }, (_, index) => index + 1);
  const chunks = chunkIds(ids);
  assert.deepEqual(chunks.map((item) => item.length), [20, 5]);
  assert.deepEqual(chunks.flat(), ids);
  assert.deepEqual(chunkIds([]), []);
});
