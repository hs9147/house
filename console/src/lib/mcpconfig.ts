/**
 * MCP 클라이언트 설정 만들기 — 발급한 토큰 하나로 고른 서버들을 붙여 넣을 모양으로.
 *
 * 토큰은 **사람에게** 속한다(과제에 묶이지 않는다) — 한 토큰으로 여러 서버를 등록한다.
 *
 * **토큰을 저장소에 남기지 않는다.** 예전 Claude Code 안내는 토큰째 프로젝트의 .mcp.json에 넣게 해, 저장소에
 * 커밋될 수 있었고 그 프로젝트 밖에서는 MCP가 보이지 않았다. 그래서 클라이언트마다 토큰이 머무는 자리를
 * 사용자 설정(~/…) · 환경변수 · 입력 프롬프트로 둔다(SkillHub의 MCP 연결설정도 같은 방식이다).
 *
 * Claude Desktop은 설정 파일(claude_desktop_config.json)에 내 PC에서 실행할 명령만 받는다. 그래서
 * 원격 서버는 `mcp-remote`가 PC에서 중계한다 — PC가 사내망 안이라 플랫폼에 닿는다(앱의 「커스텀
 * 커넥터」는 Anthropic 쪽에서 접속해 사내 주소에 닿지 않고, 헤더 토큰도 받지 않는다).
 *  - 플랫폼이 평문 http면 `--allow-http`가 있어야 mcp-remote가 접속한다.
 *  - 헤더는 `Authorization:${PAAS_AUTH}`처럼 환경변수로 넘긴다 — 윈도우에서 인자 안의 공백
 *    (`Bearer 토큰`)이 깨지는 문제를 피하는 mcp-remote 안내의 방식이다.
 *  - 윈도우는 npx가 npx.cmd라 바로 실행되지 않는 경우가 있어 `cmd /c npx …`로 감싼다.
 */
export interface McpServerRef {
  name: string;
  url: string;
}

export type DesktopOs = 'windows' | 'mac';

const AUTH_ENV = 'PAAS_AUTH';
/** 토큰을 담아 두는 환경변수 — 설정 파일에는 이 이름만 적는다(Codex·Claude Code 공유 설정). */
export const TOKEN_ENV = 'PAAS_MCP_TOKEN';
/** 발급 전 미리 보기의 자리표시자 — 발급하면 실제 토큰으로 바뀐다. */
export const TOKEN_PLACEHOLDER = '<내 토큰>';

const bridgeArgs = (url: string) => ['-y', 'mcp-remote', url, ...(url.startsWith('http://') ? ['--allow-http'] : [])];

export function desktopConfig(servers: McpServerRef[], token: string, os: DesktopOs = 'windows'): string {
  const mcpServers: Record<string, unknown> = {};
  for (const s of servers) {
    const remote = [...bridgeArgs(s.url), '--header', `Authorization:\${${AUTH_ENV}}`];
    mcpServers[s.name] = {
      command: os === 'windows' ? 'cmd' : 'npx',
      args: os === 'windows' ? ['/c', 'npx', ...remote] : remote,
      env: { [AUTH_ENV]: `Bearer ${token}` },
    };
  }
  return JSON.stringify({ mcpServers }, null, 2);
}

/**
 * Claude Code — 사용자 범위(--scope user)로 등록하는 명령. 토큰은 내 사용자 설정(~/.claude.json)에만 남고 저장소에는
 * 들어가지 않으며, 어느 프로젝트에서 열어도 보인다. 서버마다 한 줄.
 */
export function codeCommand(servers: McpServerRef[], token: string): string {
  return servers.map((s) =>
    `claude mcp add --transport http --scope user ${s.name} ${s.url} --header "Authorization: Bearer ${token}"`).join('\n');
}

/**
 * Claude Code — 프로젝트에 두고 함께 쓰는 .mcp.json. 토큰 대신 환경변수 이름만 적는다(Claude Code가 ${…}를 펼친다)
 * — 커밋해도 비밀이 새지 않는다. 사람마다 자기 토큰을 PAAS_MCP_TOKEN에 넣는다.
 */
export function codeProjectConfig(servers: McpServerRef[]): string {
  const mcpServers: Record<string, unknown> = {};
  for (const s of servers) {
    mcpServers[s.name] = { type: 'http', url: s.url, headers: { Authorization: `Bearer \${${TOKEN_ENV}}` } };
  }
  return JSON.stringify({ mcpServers }, null, 2);
}

/**
 * Codex CLI·ChatGPT Desktop(~/.codex/config.toml — 둘이 같은 파일). 기본은 토큰을 환경변수로(bearer_token_env_var) —
 * 설정 파일이 새도 토큰은 새지 않는다. inline=true면 헤더에 토큰을 직접 적는다(환경변수를 둘 수 없을 때).
 */
export function codexConfig(servers: McpServerRef[], token: string, inline = false): string {
  return servers.map((s) => [
    `[mcp_servers.${s.name}]`,
    `url = "${s.url}"`,
    inline ? `http_headers = { Authorization = "Bearer ${token}" }` : `bearer_token_env_var = "${TOKEN_ENV}"`,
  ].join('\n')).join('\n\n');
}

/**
 * VS Code(.vscode/mcp.json) — 토큰은 처음 연결할 때 묻는 입력(password)으로 받는다. 파일에는 남지 않는다.
 */
export function vscodeConfig(servers: McpServerRef[]): string {
  const out: Record<string, unknown> = {};
  for (const s of servers) {
    out[s.name] = { type: 'http', url: s.url, headers: { Authorization: 'Bearer ${input:paas-mcp-token}' } };
  }
  return JSON.stringify({
    inputs: [{ type: 'promptString', id: 'paas-mcp-token', description: 'paas MCP 토큰', password: true }],
    servers: out,
  }, null, 2);
}

/** Gemini CLI(~/.gemini/settings.json — 사용자 설정). */
export function geminiConfig(servers: McpServerRef[], token: string): string {
  const mcpServers: Record<string, unknown> = {};
  for (const s of servers) {
    mcpServers[s.name] = { httpUrl: s.url, headers: { Authorization: `Bearer ${token}` } };
  }
  return JSON.stringify({ mcpServers }, null, 2);
}

/** stdio만 받는 클라이언트 — mcp-remote가 HTTP를 stdio로 바꿔 물린다(Node 필요). */
export function stdioBridge(servers: McpServerRef[], token: string): string {
  return servers.map((s) => `npx ${bridgeArgs(s.url).join(' ')} --header "Authorization: Bearer ${token}"`).join('\n');
}

/** 토큰을 환경변수에 넣는 명령 — 사용자 환경변수로 남긴다(새 터미널부터). */
export function tokenEnvCommand(token: string, os: DesktopOs): string {
  return os === 'windows' ? `setx ${TOKEN_ENV} "${token}"` : `echo 'export ${TOKEN_ENV}="${token}"' >> ~/.zshrc`;
}

/** 게이트웨이(/proxy·/a2a)를 부르는 스크립트·빌더용 — 같은 토큰을 쓴다. */
export function gatewayEnv(gatewayBaseUrl: string, token: string): string {
  return `PAAS_GATEWAY_BASE_URL=${gatewayBaseUrl}\nPAAS_API_KEY=${token}`;
}
