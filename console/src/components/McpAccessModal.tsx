import { useState, type ReactNode } from 'react';
import { api, type McpGrant } from '../lib/api';
import { copyText } from '../lib/clipboard';
import { useApi } from '../lib/hooks';
import {
  codeCommand, codeProjectConfig, codexConfig, desktopConfig, type DesktopOs, gatewayEnv, geminiConfig, loginHelperCommands, type McpServerRef,
  stdioBridge, TOKEN_ENV, TOKEN_PLACEHOLDER, tokenEnvCommand, vscodeConfig,
} from '../lib/mcpconfig';
import Async from './Async';
import Modal from './Modal';

type Target = 'desktop' | 'code' | 'codex' | 'vscode' | 'gemini' | 'stdio' | 'gateway';
const TARGETS: { key: Target; name: string; sub: string }[] = [
  { key: 'code', name: 'Claude Code', sub: '명령 한 줄' },
  { key: 'desktop', name: 'Claude Desktop', sub: '설정 파일 · 중계' },
  { key: 'codex', name: 'Codex · ChatGPT', sub: 'config.toml' },
  { key: 'vscode', name: 'VS Code', sub: '.vscode/mcp.json' },
  { key: 'gemini', name: 'Gemini CLI', sub: 'settings.json' },
  { key: 'stdio', name: '기타 클라이언트', sub: 'stdio 중계' },
  { key: 'gateway', name: '게이트웨이', sub: '스크립트 · 빌더' },
];
// 통합 서버(/mcp/hub) — 플랫폼 MCP 전부를 한 서버로. 데스크톱 도구는 서버마다 중계 프로세스가 하나라,
// 열댓 개를 따로 등록하면 시작할 때 일부가 일찍 죽었다(실측). 기본은 이것 하나다.
const HUB = 'paas';

type Catalog = { servers: { name: string; url: string; description: string }[]; projects: { name: string; url: string }[];
                 base_url: string; gateway_base_url: string };

/**
 * MCP 접속 — **내 토큰**으로 플랫폼의 MCP 서버에 붙을 설정을 클라이언트마다 만든다.
 *
 * 토큰은 사람에게 속한다(과제에 묶이지 않는다) — 한 토큰으로 문서·온톨로지·코드·저장소 서버와 고른 과제의 기획 MCP를
 * 함께 등록한다. 원문은 발급 직후 **한 번만** 보인다(서버는 해시만 둔다).
 *
 * SkillHub의 MCP 연결설정을 본떠: 클라이언트를 고르면 그 방법만 보이고, 토큰을 발급하기 전에도 설정을 자리표시자로
 * 미리 보이며(발급하면 채운다), 토큰은 저장소가 아니라 사용자 설정·환경변수·입력 프롬프트에 머물게 하고, 안 될 때
 * 볼 것을 그 자리에 둔다.
 */
export default function McpAccessModal({ onClose }: { onClose: () => void }) {
  const tokens = useApi(() => api.listMcpTokens());
  const catalog = useApi(() => api.myMcpServers());
  const grants = useApi(() => api.listMcpGrants());
  const [label, setLabel] = useState('Claude Code — 내 PC');
  const [bundle, setBundle] = useState(true);
  const [skip, setSkip] = useState<Set<string>>(new Set());          // 뺄 공용 서버
  const [plans, setPlans] = useState<Set<string>>(new Set());
  const [issued, setIssued] = useState<{ token: string; servers: McpServerRef[]; gateway: string } | null>(null);
  const [target, setTarget] = useState<Target>('code');
  const [os, setOs] = useState<DesktopOs>(navigator.userAgent.includes('Mac') ? 'mac' : 'windows');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const pick = (c: Catalog): McpServerRef[] => (bundle
    ? c.servers.filter((s) => s.name === HUB)
    : [
      ...c.servers.filter((s) => s.name !== HUB && !skip.has(s.name)),
      ...c.projects.filter((p) => plans.has(p.name) && p.url).map((p) => ({ name: `paas-plan-${p.name}`, url: p.url })),
    ]);

  const issue = async () => {
    if (!catalog.data) return;
    setBusy(true);
    setError('');
    try {
      const out = await api.createMcpToken(label.trim() || 'MCP 토큰');
      setIssued({ token: out.token, servers: pick(catalog.data), gateway: out.gateway_base_url || catalog.data.gateway_base_url });
      tokens.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: number) => {
    if (!window.confirm('이 토큰을 폐기할까요? 그 토큰을 넣어 둔 도구는 더 이상 붙지 못합니다.')) return;
    try {
      await api.revokeMcpToken(id);
      tokens.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const dropGrant = async (g: McpGrant) => {
    if (!window.confirm(`「${g.client_name}」 연결을 끊을까요? 그 도구는 다시 로그인해야 붙습니다.`)) return;
    try {
      await api.revokeMcpGrant(g.id);
      grants.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggle = (set: Set<string>, value: string, update: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value); else next.add(value);
    update(next);
  };

  return (
    <Modal title="MCP 연결설정 — 내 토큰" onClose={onClose} wide>
      <p style={{ fontSize: 13, marginTop: 0 }}>
        <b>내 이름으로</b> 토큰을 발급해 AI 도구(Claude Code·Desktop, Codex, VS Code…)에 플랫폼의 MCP 서버를 등록합니다.
        토큰은 과제가 아니라 나에게 속하고, 내가 접근할 수 있는 조직·과제만 열립니다. MCP와 게이트웨이에서만 통하며
        배포·터미널·계정 관리에는 쓸 수 없습니다. 90일 뒤 저절로 만료됩니다.
      </p>
      <Async state={catalog}>
        {(c: Catalog) => {
          const servers = issued?.servers ?? pick(c);
          const token = issued?.token ?? TOKEN_PLACEHOLDER;
          const hub = c.servers.filter((s) => s.name === HUB);
          return (
            <>
              {grants.data?.enabled && hub.length > 0 && c.base_url && <LoginConnect base={c.base_url} os={os} onOs={setOs} />}
              <h3 style={{ fontSize: 14, margin: '12px 0 6px' }}>① 연결할 서버와 토큰</h3>
              {issued ? (
                <p style={{ fontSize: 12, margin: '4px 0' }}>
                  <span className="status ok">발급됨</span> 「{label}」 — 아래 설정에 실제 토큰을 채웠습니다.{' '}
                  <b>이 창을 닫으면 토큰을 다시 볼 수 없습니다</b> — 잃으면 새로 발급하고 쓰던 것을 폐기하세요.{' '}
                  <button className="small secondary" onClick={() => setIssued(null)}>다른 토큰 발급</button>
                </p>
              ) : (
                <>
                  <div className="row" style={{ gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
                    <label className="field" style={{ flex: '1 1 260px' }}>
                      토큰 이름 (어느 PC·도구에 넣는지 — 폐기할 때 고릅니다)
                      <input value={label} onChange={(e) => setLabel(e.target.value)} style={{ width: '100%' }} />
                    </label>
                    <label style={{ fontSize: 13 }}>
                      <input type="radio" checked={bundle} onChange={() => setBundle(true)} /> 하나로 묶기(권장)
                    </label>
                    <label style={{ fontSize: 13 }}>
                      <input type="radio" checked={!bundle} onChange={() => setBundle(false)} /> 서버별로
                    </label>
                    <button disabled={busy || !c.base_url} onClick={() => void issue()}>{busy ? '발급 중...' : '토큰 발급'}</button>
                  </div>
                  {bundle ? (
                    <p className="mutedtext" style={{ fontSize: 12, margin: '4px 0' }}>
                      <span className="mono">paas</span> 서버 하나에 문서·온톨로지·저장소 목록·API 카탈로그·코드·기획(과제 이름을
                      인자로)·업무 앱 찾기·MCP 도구·운영(관리자)이 모두 들어 있습니다.
                    </p>
                  ) : (
                    <ServerPicker c={c} skip={skip} plans={plans}
                      onSkip={(n) => toggle(skip, n, setSkip)} onPlan={(n) => toggle(plans, n, setPlans)} />
                  )}
                  {!c.base_url && (
                    <p className="error" style={{ fontSize: 12 }}>
                      서버의 공개 주소가 설정돼 있지 않아 MCP 주소를 만들 수 없습니다(PAAS_PLATFORM_PUBLIC_URL).
                    </p>
                  )}
                </>
              )}

              <h3 style={{ fontSize: 14, margin: '16px 0 6px' }}>② 클라이언트에 등록</h3>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                {TARGETS.map((t) => (
                  <button key={t.key} className={target === t.key ? 'small' : 'small secondary'} onClick={() => setTarget(t.key)}
                          style={{ textAlign: 'left', lineHeight: 1.2 }}>
                    {t.name}<div style={{ fontSize: 10, opacity: 0.75 }}>{t.sub}</div>
                  </button>
                ))}
                {(target === 'desktop' || target === 'codex') && (
                  <select value={os} onChange={(e) => setOs(e.target.value as DesktopOs)} style={{ marginLeft: 8 }}>
                    <option value="windows">Windows</option>
                    <option value="mac">macOS</option>
                  </select>
                )}
              </div>
              {!issued && (
                <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 6px' }}>
                  미리 보기입니다 — <span className="mono">{TOKEN_PLACEHOLDER}</span> 자리는 ①에서 토큰을 발급하면 채워집니다.
                </p>
              )}
              <ClientGuide target={target} servers={servers} token={token} os={os}
                           gateway={issued?.gateway ?? c.gateway_base_url} />
              <Troubleshoot target={target} />
              {c.base_url && <AgentHandoff base={c.base_url} token={issued?.token} />}
            </>
          );
        }}
      </Async>

      <h3 style={{ fontSize: 14, marginTop: 18 }}>내 토큰</h3>
      <Async state={tokens} empty="발급한 토큰이 없습니다.">
        {(list) => (
          <table style={{ fontSize: 12 }}>
            <thead><tr><th>이름</th><th>발급</th><th>만료</th><th>마지막 사용</th><th /></tr></thead>
            <tbody>
              {list.map((t) => (
                <tr key={t.id}>
                  <td>{t.label || '(이름 없음)'}</td>
                  <td>{new Date(t.created_at).toLocaleDateString()}</td>
                  <td className={t.is_expired ? 'error' : ''}>
                    {t.is_expired ? '만료됨' : new Date(t.expires_at).toLocaleDateString()}
                  </td>
                  <td>{t.last_used_at ? new Date(t.last_used_at).toLocaleString() : '-'}</td>
                  <td><button className="small secondary" onClick={() => void revoke(t.id)}>폐기</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {(grants.data?.grants.length ?? 0) > 0 && (
        <>
          <h3 style={{ fontSize: 14, marginTop: 18 }}>승인된 앱 <span className="mutedtext" style={{ fontWeight: 400, fontSize: 12 }}>— GPAX 로그인으로 연결한 도구</span></h3>
          <table style={{ fontSize: 12 }}>
            <thead><tr><th>앱</th><th>연결한 곳</th><th>승인</th><th>만료</th><th>마지막 사용</th><th /></tr></thead>
            <tbody>
              {grants.data!.grants.map((g) => (
                <tr key={g.id}>
                  <td>{g.client_name}</td>
                  <td className="mono">{new URL(g.resource).pathname}</td>
                  <td>{new Date(g.created_at).toLocaleDateString()}</td>
                  <td className={g.is_expired ? 'error' : ''}>
                    {g.is_expired ? '만료됨' : new Date(g.expires_at).toLocaleDateString()}
                  </td>
                  <td>{g.last_used_at ? new Date(g.last_used_at).toLocaleString() : '-'}</td>
                  <td><button className="small secondary" onClick={() => void dropGrant(g)}>끊기</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row" style={{ marginTop: 10 }}>
        <button className="secondary" onClick={onClose}>닫기</button>
      </div>
    </Modal>
  );
}

/**
 * 토큰 없이 GPAX 로그인으로 — 로그인 도우미(paas-mcp.mjs)가 브라우저 로그인·승인과 갱신을 맡고, Claude Code에는
 * headersHelper로 헤더만 건넨다. Claude Code가 스스로 OAuth를 하려면 토큰 주소가 https여야 해서(http면 거부) 이렇게 간다.
 */
function LoginConnect({ base, os, onOs }: { base: string; os: DesktopOs; onOs: (os: DesktopOs) => void }) {
  return (
    <div style={{ border: '1px solid var(--border, #2c2e44)', borderRadius: 8, padding: '10px 12px', margin: '8px 0 4px' }}>
      <h3 style={{ fontSize: 14, margin: '0 0 4px' }}>
        토큰 없이 GPAX 로그인으로 연결 <span className="status">Claude Code</span>
      </h3>
      <p style={{ fontSize: 12, margin: '0 0 6px' }}>
        로그인 도우미를 받아 한 번 로그인하면, 브라우저에 <b>GPAX 로그인 → 승인</b> 화면이 열리고 Claude Code에 <span className="mono">paas</span>
        서버가 등록됩니다. 토큰을 복사해 붙여 넣지 않고, 연결할 때마다 1시간짜리 토큰을 도우미가 만들어 건넵니다(90일 동안 저절로 갱신).
        아래 「승인된 앱」에서 언제든 끊을 수 있습니다.
      </p>
      <div className="row" style={{ gap: 6, marginBottom: 6 }}>
        <select value={os} onChange={(e) => onOs(e.target.value as DesktopOs)}>
          <option value="windows">Windows (PowerShell)</option>
          <option value="mac">macOS · Linux</option>
        </select>
      </div>
      <CodeBlock title="받기 → 로그인 → Claude Code에 등록" text={loginHelperCommands(base, os)} />
      <p className="mutedtext" style={{ fontSize: 12, margin: '6px 0 0' }}>
        Node 18 이상이 필요합니다. 등록한 뒤 Claude Code를 새로 열면 붙습니다(<span className="mono">/mcp</span>에서 확인).
        같은 이름(<span className="mono">paas</span>)으로 사용자 범위에 등록된 것이 있으면 바꿉니다.
        상태 보기 <span className="mono">… status</span>, 끊기 <span className="mono">… logout</span>.
        VS Code·Cursor·Codex는 아래 ①~②의 토큰 방식을 쓰세요.
      </p>
    </div>
  );
}

function ServerPicker({ c, skip, plans, onSkip, onPlan }: {
  c: Catalog; skip: Set<string>; plans: Set<string>; onSkip: (n: string) => void; onPlan: (n: string) => void;
}) {
  return (
    <>
      <div style={{ fontSize: 13, margin: '8px 0 4px' }}>등록할 서버</div>
      <div style={{ maxHeight: 160, overflow: 'auto', fontSize: 12 }}>
        {c.servers.filter((s) => s.name !== HUB).map((s) => (
          <label key={s.name} style={{ display: 'block' }} title={s.description}>
            <input type="checkbox" checked={!skip.has(s.name)} onChange={() => onSkip(s.name)} />{' '}
            <span className="mono">{s.name}</span> <span className="mutedtext">{s.description}</span>
          </label>
        ))}
      </div>
      {c.projects.length > 0 && (
        <>
          <div style={{ fontSize: 13, margin: '8px 0 4px' }}>
            기획 MCP를 붙일 과제 <span className="mutedtext">(작업 지시·산출물 — 과제마다 서버가 하나)</span>
          </div>
          <div style={{ maxHeight: 120, overflow: 'auto', fontSize: 12 }}>
            {c.projects.map((p) => (
              <label key={p.name} style={{ display: 'inline-block', marginRight: 12 }}>
                <input type="checkbox" checked={plans.has(p.name)} onChange={() => onPlan(p.name)} /> {p.name}
              </label>
            ))}
          </div>
        </>
      )}
    </>
  );
}

/** 단계 하나 — 번호 · 제목 · 내용. */
function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div style={{ margin: '8px 0' }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
        <span className="status info" style={{ marginRight: 6 }}>{n}</span>{title}
      </div>
      <div style={{ fontSize: 12, paddingLeft: 4 }}>{children}</div>
    </div>
  );
}

/** 붙여 넣을 것 — 제목(있으면 위에)·내용·복사(덩어리 오른쪽 위 모서리). 평문 http에서도 되는 길로 복사하고 결과를
 *  버튼 글자로 잠깐 알린다 — 버튼이 덩어리 위에 혼자 한 줄을 차지하지 않게. */
function CodeBlock({ title, text }: { title?: string; text: string }) {
  const [done, setDone] = useState<'' | 'ok' | 'fail'>('');
  const copy = async () => {
    setDone((await copyText(text)) ? 'ok' : 'fail');
    window.setTimeout(() => setDone(''), 2000);
  };
  return (
    <div style={{ margin: '4px 0 8px' }}>
      {title && <div className="mono mutedtext" style={{ fontSize: 11, marginBottom: 2 }}>{title}</div>}
      <div style={{ position: 'relative' }}>
        <pre className="mono" style={{ background: '#090d16', padding: '10px 64px 10px 10px', borderRadius: 8, fontSize: 12,
                                       margin: 0, maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap',
                                       wordBreak: 'break-all' }}>{text}</pre>
        <button className="small secondary" onClick={() => void copy()}
                title={done === 'fail' ? '복사하지 못했습니다 — 직접 선택해 복사하세요' : undefined}
                style={{ position: 'absolute', top: 6, right: 6 }}>
          {done === 'ok' ? '복사됨' : done === 'fail' ? '직접 복사' : '복사'}
        </button>
      </div>
    </div>
  );
}

function ClientGuide({ target, servers, token, os, gateway }: {
  target: Target; servers: McpServerRef[]; token: string; os: DesktopOs; gateway: string;
}) {
  const name = servers[0]?.name ?? HUB;
  if (target === 'code') {
    return (
      <>
        <Step n={1} title="사용자 범위로 등록 — 붙여 넣을 것은 명령 한 줄">
          <CodeBlock text={codeCommand(servers, token)} />
          <span className="mutedtext">
            토큰은 내 사용자 설정(<span className="mono">~/.claude.json</span>)에만 남고 저장소에는 들어가지 않습니다. 어느
            프로젝트에서 열어도 보입니다(예전처럼 한 프로젝트의 <span className="mono">.mcp.json</span>에 넣으면 그 프로젝트
            밖에서는 보이지 않습니다).
          </span>
        </Step>
        <Step n={2} title="연결 확인">
          <span className="mono">claude mcp list</span>에 <span className="mono">{name}</span>이 「✓ Connected」로 나오면 됩니다.
          세션 안에서는 <span className="mono">/mcp</span>. 지우려면 <span className="mono">claude mcp remove {name} -s user</span>.
        </Step>
        <details style={{ fontSize: 12 }}>
          <summary style={{ cursor: 'pointer' }}>프로젝트에 두고 팀이 함께 쓰려면 — .mcp.json에 토큰 없이</summary>
          <p className="mutedtext" style={{ margin: '4px 0' }}>
            파일에는 환경변수 이름만 적어 커밋해도 비밀이 새지 않습니다. 사람마다 자기 토큰을 <span className="mono">{TOKEN_ENV}</span>에
            넣습니다(Claude Code가 <span className="mono">{'${…}'}</span>를 펼칩니다).
          </p>
          <CodeBlock title="내 PC — 토큰을 환경변수로(새 터미널부터)" text={tokenEnvCommand(token, os)} />
          <CodeBlock title=".mcp.json (프로젝트 루트)" text={codeProjectConfig(servers)} />
        </details>
      </>
    );
  }
  if (target === 'desktop') {
    return (
      <>
        <Step n={1} title="설정 파일 열기">
          Claude Desktop → 설정 → 개발자 → 「설정 편집」으로{' '}
          <span className="mono">
            {os === 'windows' ? '%APPDATA%\\Claude\\claude_desktop_config.json' : '~/Library/Application Support/Claude/claude_desktop_config.json'}
          </span>을 엽니다.
          {os === 'windows' && (
            <> Microsoft Store로 설치했으면 파일이 <span className="mono">%LOCALAPPDATA%\Packages\Claude_…\LocalCache\Roaming\Claude\</span>
              아래에 있습니다 — 「설정 편집」이 여는 파일이 맞는 파일입니다.</>
          )}
        </Step>
        <Step n={2} title="mcpServers에 붙여 넣기">
          <CodeBlock title="claude_desktop_config.json" text={desktopConfig(servers, token, os)} />
          <span className="mutedtext">
            이미 <span className="mono">mcpServers</span>가 있으면 그 안에 항목만 더합니다. PC의 <span className="mono">mcp-remote</span>가
            중계하므로 Node.js가 있어야 합니다(<span className="mono">npx -v</span>가 나오면 됩니다). 토큰은 이 파일의
            환경변수 칸에만 들어갑니다.
          </span>
        </Step>
        <Step n={3} title="다시 실행">Claude Desktop을 트레이(메뉴 막대)에서 <b>완전히 종료</b>한 뒤 다시 엽니다.</Step>
      </>
    );
  }
  if (target === 'codex') {
    return (
      <>
        <Step n={1} title="토큰을 환경변수로(새 터미널·앱부터)">
          <CodeBlock text={tokenEnvCommand(token, os)} />
        </Step>
        <Step n={2} title="~/.codex/config.toml에 등록 — Codex CLI와 ChatGPT Desktop이 같은 파일">
          <CodeBlock title="~/.codex/config.toml" text={codexConfig(servers, token)} />
          <span className="mutedtext">설정 파일에는 환경변수 이름만 남습니다 — 파일이 새도 토큰은 새지 않습니다.</span>
        </Step>
        <details style={{ fontSize: 12 }}>
          <summary style={{ cursor: 'pointer' }}>환경변수를 둘 수 없다면 — 헤더에 토큰을 직접</summary>
          <CodeBlock title="~/.codex/config.toml" text={codexConfig(servers, token, true)} />
          <p className="mutedtext" style={{ margin: 0 }}>
            ChatGPT Desktop 화면에서 넣으려면: 설정 → 플러그인 → MCP 서버 추가 → 유형 「스트리밍 가능한 HTTP」, 이름{' '}
            <span className="mono">{name}</span>, URL <span className="mono">{servers[0]?.url}</span>, 헤더{' '}
            <span className="mono">Authorization: Bearer {token}</span>(실행 명령·인자·환경 변수 칸은 비웁니다).
          </p>
        </details>
      </>
    );
  }
  if (target === 'vscode') {
    return (
      <>
        <Step n={1} title=".vscode/mcp.json에 등록 — 토큰은 파일에 넣지 않는다">
          <CodeBlock title=".vscode/mcp.json" text={vscodeConfig(servers)} />
        </Step>
        <Step n={2} title="처음 연결할 때 토큰 입력">
          VS Code가 「paas MCP 토큰」을 묻습니다 — 아래 토큰을 붙여 넣으면 VS Code가 비밀 저장소에 둡니다.
          <CodeBlock title="토큰" text={token} />
        </Step>
      </>
    );
  }
  if (target === 'gemini') {
    return (
      <Step n={1} title="~/.gemini/settings.json(사용자 설정)에 등록">
        <CodeBlock title="~/.gemini/settings.json" text={geminiConfig(servers, token)} />
        <span className="mutedtext">이미 <span className="mono">mcpServers</span>가 있으면 그 안에 항목만 더합니다.</span>
      </Step>
    );
  }
  if (target === 'stdio') {
    return (
      <Step n={1} title="stdio만 받는 클라이언트 — mcp-remote로 중계(Node 필요)">
        <CodeBlock text={stdioBridge(servers, token)} />
        <span className="mutedtext">
          Streamable HTTP를 받는 클라이언트면 중계 없이 주소(<span className="mono">{servers[0]?.url}</span>)와 헤더{' '}
          <span className="mono">Authorization: Bearer {token}</span>만 등록하면 됩니다.
        </span>
      </Step>
    );
  }
  return (
    <Step n={1} title="게이트웨이(/proxy · /a2a — LLM·도구·A2A)를 부르는 스크립트·외주 빌더의 환경변수">
      <CodeBlock text={gatewayEnv(gateway, token)} />
      <span className="mutedtext">MCP 토큰과 같은 토큰입니다.</span>
    </Step>
  );
}

/**
 * ③ 에이전트에게 전달 — 링크 하나(로그인 없이 읽히는 연결 안내 md — 비밀이 없다)와 에이전트에게 보낼 한 줄, md 내려받기.
 * 안내는 토큰을 환경변수(PAAS_MCP_TOKEN)로만 가리킨다. 발급한 토큰을 파일에 넣는 것은 사람이 고를 때만 — 그 파일은
 * 자격증명이라 저장소·공유 드라이브에 두면 안 된다(맨 위에 그렇게 적는다).
 */
function AgentHandoff({ base, token }: { base: string; token?: string }) {
  const link = `${base}/api/v1/mcp/agent-setup.md`;
  const [withToken, setWithToken] = useState(false);
  const [error, setError] = useState('');
  const message = `다음 안내를 읽고 paas MCP를 연결해 줘: ${link}\n`
    + `토큰은 환경변수 ${TOKEN_ENV}에 ${token ? '넣어 두었어' : '넣어 둘게'}.`;
  const download = async () => {
    setError('');
    try {
      const res = await fetch(link);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      let md = await res.text();
      if (withToken && token) {
        const [head, ...rest] = md.split('\n');
        md = [head, '',
          '> ⚠️ **이 파일에는 토큰이 들어 있습니다** — 저장소·공유 드라이브·메신저에 두지 말고, 설정을 마치면 지우세요.',
          '', '## 0. 토큰', '',
          `아래 값을 환경변수 \`${TOKEN_ENV}\`에 넣은 뒤 1부터 진행한다.`, '',
          '```', `Windows:       setx ${TOKEN_ENV} "${token}"`, `macOS·Linux:   export ${TOKEN_ENV}="${token}"`, '```',
          ...rest].join('\n');
      }
      const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
      const a = Object.assign(document.createElement('a'), { href: url, download: 'paas-mcp-agent-setup.md' });
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(`내려받지 못했습니다: ${(e as Error).message}`);
    }
  };
  return (
    <>
      <h3 style={{ fontSize: 14, margin: '16px 0 6px' }}>③ 에이전트에게 전달</h3>
      <p className="mutedtext" style={{ fontSize: 12, margin: '0 0 4px' }}>
        에이전트가 읽고 스스로 연결하는 안내입니다 — 연결 명령(클라이언트별)·도구 목록·쓰는 규칙·안 될 때. <b>토큰은 들어 있지
        않습니다</b>(환경변수 <span className="mono">{TOKEN_ENV}</span>로만 가리킵니다) — 링크를 그대로 건네도 됩니다.
      </p>
      <CodeBlock title="링크(로그인 없이 읽힘)" text={link} />
      <CodeBlock title="에이전트에게 보낼 말" text={message} />
      <div className="row" style={{ gap: 12, alignItems: 'center', fontSize: 12 }}>
        <button className="small secondary" onClick={() => void download()}>md 내려받기</button>
        <label title={token ? undefined : '①에서 토큰을 발급하면 고를 수 있습니다'} style={{ opacity: token ? 1 : 0.5 }}>
          <input type="checkbox" disabled={!token} checked={withToken && !!token} onChange={(e) => setWithToken(e.target.checked)} />{' '}
          발급한 토큰을 파일에 넣기 <span className="mutedtext">(그 파일이 자격증명이 됩니다)</span>
        </label>
      </div>
      {error && <p className="error" style={{ fontSize: 12 }}>{error}</p>}
    </>
  );
}

/** 연결이 안 될 때 — 공통으로 볼 것과 그 클라이언트에서 볼 것. */
function Troubleshoot({ target }: { target: Target }) {
  const own: Record<Target, ReactNode[]> = {
    code: [<>다른 프로젝트에서 안 보이면 사용자 범위(<span className="mono">--scope user</span>)로 등록했는지 —{' '}
      <span className="mono">claude mcp list</span>의 범위를 봅니다.</>,
      <>세션을 연 뒤에 등록했으면 Claude Code를 다시 시작합니다.</>],
    desktop: [<>로그: <span className="mono">%LOCALAPPDATA%\Claude\logs\mcp-server-paas.log</span>(macOS{' '}
      <span className="mono">~/Library/Logs/Claude/mcp-server-paas.log</span>).</>,
      <><span className="mono">npx</span>를 찾지 못한다는 로그면 Node.js를 설치하고 PC를 다시 로그인합니다.</>,
      <>앱의 「커스텀 커넥터」로는 등록하지 않습니다 — 그 연결은 Anthropic 쪽에서 나가 사내 주소에 닿지 않습니다.</>],
    codex: [<>환경변수는 <b>새로 연</b> 터미널·앱부터 보입니다 — <span className="mono">setx</span> 뒤에는 Codex·ChatGPT를
      다시 엽니다.</>],
    vscode: [<>토큰을 잘못 넣었으면 명령 팔레트의 「MCP: 서버 다시 시작」 또는 입력값 지우기로 다시 묻게 합니다.</>],
    gemini: [<>설정을 고친 뒤 Gemini CLI를 다시 시작합니다.</>],
    stdio: [<>플랫폼 주소가 <span className="mono">http://</span>면 <span className="mono">--allow-http</span>가 있어야 중계가 접속합니다.</>],
    gateway: [<>401이면 <span className="mono">PAAS_API_KEY</span>에 이 토큰이 들어갔는지 봅니다.</>],
  };
  return (
    <details style={{ fontSize: 12, marginTop: 8 }}>
      <summary style={{ cursor: 'pointer' }}>연결이 안 될 때</summary>
      <ul style={{ paddingLeft: 18, margin: '4px 0' }}>
        <li><b>401</b> — 토큰이 만료(90일)·폐기됐거나 <span className="mono">Bearer</span> 뒤 공백 한 칸이 빠졌습니다. 아래 「내 토큰」의 만료일을 보고, 필요하면 새로 발급해 바꿉니다.</li>
        <li><b>403</b> — 그 과제·조직의 권한이 없습니다(기획 MCP는 과제마다 권한을 봅니다).</li>
        <li><b>연결 거부·시간 초과</b> — 사내망(또는 VPN)에서 플랫폼 주소에 닿는지 봅니다. claude.ai·ChatGPT 웹의 커넥터는 벤더 클라우드가 접속하는 구조라 사내망에 닿지 못합니다 — 사내 PC에서 도는 앱·CLI만 됩니다.</li>
        <li><b>도구가 보이지 않음</b> — 클라이언트를 다시 시작합니다. 「내 토큰」의 마지막 사용 시각이 바뀌면 서버까지는 닿은 것입니다.</li>
        {own[target].map((x, i) => <li key={i}>{x}</li>)}
      </ul>
    </details>
  );
}
