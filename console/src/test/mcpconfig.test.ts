import { describe, expect, it } from 'vitest';
import { codeConfig, desktopConfig, gatewayEnv } from '../lib/mcpconfig';

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

describe('codeConfig / gatewayEnv', () => {
  it('passes remote servers straight to Claude Code', () => {
    const cfg = JSON.parse(codeConfig(servers, 'tok')).mcpServers;
    expect(cfg['paas-docs']).toEqual({
      type: 'http', url: 'http://gpax.lge.com/paas/api/v1/mcp/docs', headers: { Authorization: 'Bearer tok' },
    });
    expect(gatewayEnv('http://x/paas/api/v1', 'tok')).toBe('PAAS_GATEWAY_BASE_URL=http://x/paas/api/v1\nPAAS_API_KEY=tok');
  });
});
