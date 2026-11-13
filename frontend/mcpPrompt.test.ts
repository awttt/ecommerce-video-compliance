import assert from "node:assert/strict";
import test from "node:test";

import { mcpAgentPrompt, mcpEndpoint } from "./src/mcpPrompt.ts";

test("dev server points MCP at the backend port", () => {
  assert.equal(
    mcpEndpoint({ protocol: "http:", hostname: "127.0.0.1", port: "5173", origin: "http://127.0.0.1:5173" }),
    "http://127.0.0.1:8765/mcp",
  );
});

test("the desktop app uses its own origin", () => {
  assert.equal(
    mcpEndpoint({ protocol: "http:", hostname: "127.0.0.1", port: "8765", origin: "http://127.0.0.1:8765" }),
    "http://127.0.0.1:8765/mcp",
  );
});

test("the prompt tells the agent the address and the folder tool", () => {
  const prompt = mcpAgentPrompt("http://127.0.0.1:8765/mcp");
  assert.match(prompt, /grok mcp add --scope user --transport http evc http:\/\/127\.0\.0\.1:8765\/mcp/);
  assert.match(prompt, /scan_folder/);
  assert.match(prompt, /batch_report/);
  assert.doesNotMatch(prompt, /sk-/);
});
