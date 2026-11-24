import assert from "node:assert/strict";
import test from "node:test";

import { IMAGE_WORKBENCH_PATH, TOOL_GROUP, VIDEO_TOOLS_PATH, toolMenuEntries } from "./src/toolNav.ts";

test("image workbench sits directly under the admin video tool", () => {
  for (const role of ["platform_admin", "tenant_admin", "member"] as const) {
    const items = toolMenuEntries(true, role);
    assert.deepEqual(
      items.map((item) => item.key),
      [VIDEO_TOOLS_PATH, IMAGE_WORKBENCH_PATH],
    );
    assert.equal(items[0].label, "视频审核工具");
    assert.equal(items[1].label, "生图工作台");
    assert.ok(items.every((item) => item.group === TOOL_GROUP));
    assert.equal(items.some((item) => item.group === "工作台" || item.group === "业务管理"), false);
  }
});

test("standalone admin keeps image generation in the tool group", () => {
  const items = toolMenuEntries(false, "member");
  assert.deepEqual(items.map((item) => item.key), [IMAGE_WORKBENCH_PATH]);
  assert.equal(items[0].group, TOOL_GROUP);
});
