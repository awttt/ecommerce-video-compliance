import assert from "node:assert/strict";
import test from "node:test";

import { AUDIT_PAGE_SIZES, auditLogsPath, nextAuditPage } from "./src/auditPage.ts";

test("audit pages only offer 20, 50 and 100 and default to 20", () => {
  assert.deepEqual(AUDIT_PAGE_SIZES, [20, 50, 100]);
  assert.equal(auditLogsPath(1, 20), "/v1/audit-logs?page=1&page_size=20");
  assert.equal(auditLogsPath(0, 10), "/v1/audit-logs?page=1&page_size=20");
  assert.equal(auditLogsPath(3, 100), "/v1/audit-logs?page=3&page_size=100");
});

test("changing the page size returns to the first page", () => {
  assert.deepEqual(nextAuditPage(20, 3, 50, 80), { page: 1, pageSize: 50 });
  assert.deepEqual(nextAuditPage(20, 2, 100, 40), { page: 1, pageSize: 100 });
});

test("the same page size keeps a valid page and clamps the last page", () => {
  assert.deepEqual(nextAuditPage(20, 2, 20, 45), { page: 2, pageSize: 20 });
  assert.deepEqual(nextAuditPage(20, 9, 20, 45), { page: 3, pageSize: 20 });
  assert.deepEqual(nextAuditPage(50, 4, 50, 0), { page: 1, pageSize: 50 });
});
