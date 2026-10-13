/**
 * MCP 클라이언트 설정 만들기 — 발급한 토큰 하나로 고른 서버들을 붙여 넣을 모양으로.
 *
 * 토큰은 **사람에게** 속한다(과제에 묶이지 않는다) — 한 토큰으로 여러 서버를 등록한다.
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

export function desktopConfig(servers: McpServerRef[], token: string, os: DesktopOs = 'windows'): string {
  const mcpServers: Record<string, unknown> = {};
  for (const s of servers) {
    const remote = ['-y', 'mcp-remote', s.url,
      ...(s.url.startsWith('http://') ? ['--allow-http'] : []),
      '--header', `Authorization:\${${AUTH_ENV}}`];
    mcpServers[s.name] = {
      command: os === 'windows' ? 'cmd' : 'npx',
      args: os === 'windows' ? ['/c', 'npx', ...remote] : remote,
      env: { [AUTH_ENV]: `Bearer ${token}` },
    };
  }
  return JSON.stringify({ mcpServers }, null, 2);
}

/** Claude Code(.mcp.json·~/.claude.json) — 원격 서버를 그대로 받는다. */
export function codeConfig(servers: McpServerRef[], token: string): string {
  const mcpServers: Record<string, unknown> = {};
  for (const s of servers) {
    mcpServers[s.name] = { type: 'http', url: s.url, headers: { Authorization: `Bearer ${token}` } };
  }
  return JSON.stringify({ mcpServers }, null, 2);
}

/** 게이트웨이(/proxy·/a2a)를 부르는 스크립트·빌더용 — 같은 토큰을 쓴다. */
export function gatewayEnv(gatewayBaseUrl: string, token: string): string {
  return `PAAS_GATEWAY_BASE_URL=${gatewayBaseUrl}\nPAAS_API_KEY=${token}`;
}
