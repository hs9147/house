import { describe, expect, it } from 'vitest';
import {
  codeCommand, codeLoginCommand, codeProjectConfig, codexConfig, desktopConfig, gatewayEnv, geminiConfig, stdioBridge,
  tokenEnvCommand, vscodeConfig,
} from '../lib/mcpconfig';

const servers = [
  { name: 'paas-docs', url: 'http://gpax.lge.com/paas/api/v1/mcp/docs' },
  { name: 'paas-plan-supplier-pool', url: 'https://paas.example.com/paas/api/v1/plan/projects/3/mcp' },
];

describe('desktopConfig', () => {
  it('bridges each server through mcp-remote with the token in an env var', () => {
    const cfg = JSON.parse(desktopConfig(servers, 'tok123', 'windows')).mcpServers;
    expect(Object.keys(cfg)).toEqual(['paas-docs', 'paas-plan-supplier-pool']);
    expect(cfg['paas-docs']).toEqual({
      command: 'cmd',
      args: ['/c', 'npx', '-y', 'mcp-remote', 'http://gpax.lge.com/paas/api/v1/mcp/docs',
        '--allow-http', '--header', 'Authorization:${PAAS_AUTH}'],
      env: { PAAS_AUTH: 'Bearer tok123' },
    });
    // https 서버에는 --allow-http를 붙이지 않는다
    expect(cfg['paas-plan-supplier-pool'].args).not.toContain('--allow-http');
  });

  it('runs npx directly on macOS', () => {
    const cfg = JSON.parse(desktopConfig(servers, 'tok', 'mac')).mcpServers;
    expect(cfg['paas-docs'].command).toBe('npx');
    expect(cfg['paas-docs'].args.slice(0, 2)).toEqual(['-y', 'mcp-remote']);
  });
});

describe('gatewayEnv', () => {
  it('게이트웨이 스크립트의 환경변수', () => {
    expect(gatewayEnv('http://x/paas/api/v1', 'tok')).toBe('PAAS_GATEWAY_BASE_URL=http://x/paas/api/v1\nPAAS_API_KEY=tok');
  });
});

describe('토큰을 저장소에 남기지 않는 설정', () => {
  it('Claude Code는 사용자 범위 명령으로 — 서버마다 한 줄', () => {
    const cmd = codeCommand(servers, 'tok').split('\n');
    expect(cmd).toHaveLength(2);
    expect(cmd[0]).toBe('claude mcp add --transport http --scope user paas-docs '
      + 'http://gpax.lge.com/paas/api/v1/mcp/docs --header "Authorization: Bearer tok"');
  });

  it('GPAX 로그인으로 연결할 때는 헤더 없이 주소만', () => {
    const cmd = codeLoginCommand(servers.slice(0, 1));
    expect(cmd).toBe('claude mcp add --transport http --scope user paas-docs http://gpax.lge.com/paas/api/v1/mcp/docs');
    expect(cmd).not.toContain('Authorization');
  });

  it('프로젝트 .mcp.json에는 환경변수 이름만', () => {
    const cfg = JSON.parse(codeProjectConfig(servers)).mcpServers;
    expect(cfg['paas-docs'].headers).toEqual({ Authorization: 'Bearer ${PAAS_MCP_TOKEN}' });
    expect(codeProjectConfig(servers)).not.toContain('tok');
  });

  it('Codex는 기본이 환경변수, 원하면 헤더에 직접', () => {
    expect(codexConfig(servers.slice(0, 1), 'tok')).toBe(
      '[mcp_servers.paas-docs]\nurl = "http://gpax.lge.com/paas/api/v1/mcp/docs"\nbearer_token_env_var = "PAAS_MCP_TOKEN"');
    expect(codexConfig(servers.slice(0, 1), 'tok', true)).toContain('http_headers = { Authorization = "Bearer tok" }');
  });

  it('VS Code는 처음 연결할 때 묻는 입력으로 받는다', () => {
    const cfg = JSON.parse(vscodeConfig(servers));
    expect(cfg.inputs[0]).toMatchObject({ id: 'paas-mcp-token', password: true });
    expect(cfg.servers['paas-docs'].headers.Authorization).toBe('Bearer ${input:paas-mcp-token}');
  });

  it('Gemini·stdio 브리지·환경변수 명령', () => {
    expect(JSON.parse(geminiConfig(servers, 'tok')).mcpServers['paas-docs'].httpUrl).toBe(servers[0].url);
    expect(stdioBridge(servers.slice(0, 1), 'tok')).toBe(
      'npx -y mcp-remote http://gpax.lge.com/paas/api/v1/mcp/docs --allow-http --header "Authorization: Bearer tok"');
    expect(tokenEnvCommand('tok', 'windows')).toBe('setx PAAS_MCP_TOKEN "tok"');
  });
});
