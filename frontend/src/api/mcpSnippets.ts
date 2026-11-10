export type McpClient = 'claude-code' | 'codex' | 'cursor'
export interface McpSnippetOptions { endpoint: string; serverName: string; tokenEnv: string }
export interface McpSnippet { title: string; fileName: string; content: string; instructions: string }

export const mcpClients: McpClient[] = ['claude-code', 'codex', 'cursor']

export function buildMcpSnippet(client: McpClient, options: McpSnippetOptions): McpSnippet {
  const { endpoint, serverName, tokenEnv } = options
  if (client === 'codex') return {
    title: 'Codex', fileName: '~/.codex/config.toml',
    content: `[mcp_servers.${serverName}]\nurl = ${JSON.stringify(endpoint)}\nbearer_token_env_var = ${JSON.stringify(tokenEnv)}`,
    instructions: `将配置追加到 Codex 配置文件。启动 Codex 的环境需要包含 ${tokenEnv}。`,
  }
  if (client === 'cursor') return {
    title: 'Cursor', fileName: '.cursor/mcp.json',
    content: JSON.stringify({ mcpServers: { [serverName]: { url: endpoint, headers: { Authorization: `Bearer \${env:${tokenEnv}}` } } } }, null, 2),
    instructions: `将配置放入项目的 .cursor/mcp.json。启动 Cursor 的环境需要包含 ${tokenEnv}。`,
  }
  return {
    title: 'Claude Code', fileName: '.mcp.json',
    content: JSON.stringify({ mcpServers: { [serverName]: { type: 'http', url: endpoint, headers: { Authorization: `Bearer \${${tokenEnv}}` } } } }, null, 2),
    instructions: `将配置放入项目的 .mcp.json。启动 Claude Code 的环境需要包含 ${tokenEnv}。`,
  }
}
