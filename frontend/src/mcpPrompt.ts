/** 本机 MCP 地址，以及发给 agent 的配置说明。 */

export interface PageLocation {
  protocol: string;
  hostname: string;
  port: string;
  origin: string;
}

export function mcpEndpoint(location: PageLocation): string {
  const host = location.hostname;
  const local = host === "127.0.0.1" || host === "localhost";
  if (!local) return "http://127.0.0.1:8765/mcp";
  if (location.port === "5173") return `http://${host}:8765/mcp`;
  return `${location.origin}/mcp`;
}

export function mcpAgentPrompt(url: string): string {
  return `请把本机的「御镜视频审核系统」加进 MCP，服务名用 evc。不要改检测程序的代码，也不要再启动一个检测进程。

连接地址：${url}
传输方式：Streamable HTTP

Grok 可执行：
grok mcp add --scope user --transport http evc ${url}

或在 ~/.grok/config.toml 写入：

[mcp_servers.evc]
url = "${url}"

Cursor 的 mcp.json 写成：

{
  "mcpServers": {
    "evc": { "url": "${url}" }
  }
}

配好之后告诉我已经连上。之后我指定一个文件夹时，调用 scan_folder，等这批视频全部检测完，把批量合规报告直接回在对话里，并给出 Excel 路径。如果调用中断，用当时的 batch_id 再调 batch_report，不要重新扫描。`;
}
