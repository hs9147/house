import { useState } from 'react';
import { api } from '../lib/api';
import { copyText } from '../lib/clipboard';
import { useApi } from '../lib/hooks';
import { codeConfig, desktopConfig, type DesktopOs, gatewayEnv, type McpServerRef } from '../lib/mcpconfig';
import Async from './Async';
import Modal from './Modal';

type Target = 'desktop' | 'code' | 'gateway';
const TARGETS: [Target, string][] = [['desktop', 'Claude Desktop'], ['code', 'Claude Code'], ['gateway', '게이트웨이']];
// 통합 서버(/mcp/hub) — 플랫폼 MCP 전부를 한 서버로. 데스크톱 도구는 서버마다 중계 프로세스가 하나라,
// 열댓 개를 따로 등록하면 시작할 때 일부가 일찍 죽었다(실측). 기본은 이것 하나다.
const HUB = 'paas';

/**
 * MCP 접속 — **내 토큰**으로 플랫폼의 MCP 서버에 붙을 설정을 만든다.
 *
 * 토큰은 사람에게 속한다(과제에 묶이지 않는다) — 한 토큰으로 문서·온톨로지·코드·저장소 서버와,
 * 고른 과제의 기획 MCP를 함께 등록한다. 접근 권한은 그 사람의 조직으로 판정된다. 원문은 발급 직후
 * **한 번만** 보인다(서버는 해시만 둔다) — 그래서 붙여 넣을 설정 전체를 그 자리에서 보여 준다.
 *
 * initialProject: 과제 행의 「에이전트 접속」에서 열었으면 그 과제의 기획 MCP를 미리 골라 둔다.
 */
export default function McpAccessModal({ initialProject, onClose }: {
  initialProject?: string;
  onClose: () => void;
}) {
  const tokens = useApi(() => api.listMcpTokens());
  const catalog = useApi(() => api.myMcpServers());
  const [label, setLabel] = useState('Claude Desktop — 내 PC');
  const [bundle, setBundle] = useState(!initialProject);  // 과제 행에서 열면 그 과제의 기획 MCP를 따로
  const [skip, setSkip] = useState<Set<string>>(new Set());          // 뺄 공용 서버
  const [plans, setPlans] = useState<Set<string>>(new Set(initialProject ? [initialProject] : []));
  const [issued, setIssued] = useState<{ token: string; servers: McpServerRef[]; gateway: string } | null>(null);
  const [target, setTarget] = useState<Target>('desktop');
  const [os, setOs] = useState<DesktopOs>(navigator.userAgent.includes('Mac') ? 'mac' : 'windows');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const issue = async () => {
    if (!catalog.data) return;
    setBusy(true);
    setError('');
    try {
      const out = await api.createMcpToken(label.trim() || 'MCP 토큰');
      const servers: McpServerRef[] = bundle
        ? catalog.data.servers.filter((s) => s.name === HUB)
        : [
          ...catalog.data.servers.filter((s) => s.name !== HUB && !skip.has(s.name)),
          ...catalog.data.projects.filter((p) => plans.has(p.name) && p.url)
            .map((p) => ({ name: `paas-plan-${p.name}`, url: p.url })),
        ];
      setIssued({ token: out.token, servers, gateway: out.gateway_base_url || catalog.data.gateway_base_url });
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

  const text = !issued ? '' : target === 'desktop' ? desktopConfig(issued.servers, issued.token, os)
    : target === 'code' ? codeConfig(issued.servers, issued.token) : gatewayEnv(issued.gateway, issued.token);

  // 평문 http에서는 navigator.clipboard가 없다 — 되는 길로 복사하고 실제 결과를 알린다.
  const copy = async () => {
    window.alert((await copyText(text))
      ? '복사했습니다. 아래 안내대로 붙여 넣으세요.'
      : '복사하지 못했습니다 — 위 내용을 직접 선택해 복사하세요.');
  };

  const toggle = (set: Set<string>, value: string, update: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value); else next.add(value);
    update(next);
  };

  return (
    <Modal title="MCP 접속 — 내 토큰" onClose={onClose}>
      {!issued ? (
        <>
          <p style={{ fontSize: 13, marginTop: 0 }}>
            <b>내 이름으로</b> 토큰을 발급해 Claude Desktop·Claude Code 같은 도구에 플랫폼의 MCP 서버를
            등록합니다. 토큰은 과제가 아니라 나에게 속하고, 내가 접근할 수 있는 조직·과제만 열립니다.
            MCP와 게이트웨이에서만 통하며 배포·터미널·계정 관리에는 쓸 수 없습니다.
          </p>
          <Async state={catalog}>
            {(c) => (
              <>
                <label className="field">
                  토큰 이름 (어느 PC·도구에 넣는지 — 폐기할 때 고릅니다)
                  <input value={label} onChange={(e) => setLabel(e.target.value)} style={{ width: '100%' }} />
                </label>
                <div className="row" style={{ gap: 12, fontSize: 13, margin: '8px 0 4px' }}>
                  <label>
                    <input type="radio" checked={bundle} onChange={() => setBundle(true)} /> 하나로 묶기(권장)
                  </label>
                  <label>
                    <input type="radio" checked={!bundle} onChange={() => setBundle(false)} /> 서버별로
                  </label>
                </div>
                {bundle ? (
                  <p className="mutedtext" style={{ fontSize: 12 }}>
                    <span className="mono">paas</span> 서버 하나에 문서·온톨로지·저장소 목록·API 카탈로그·코드·기획
                    (과제 이름을 인자로)·업무 앱 찾기·MCP 모듈·운영(관리자)이 모두 들어 있습니다. 데스크톱 도구는 서버마다 중계 프로세스가
                    하나라, 따로 등록하면 시작할 때 일부가 실패할 수 있습니다.
                  </p>
                ) : (
                <>
                <div style={{ fontSize: 13, margin: '8px 0 4px' }}>등록할 서버</div>
                <div style={{ maxHeight: 160, overflow: 'auto', fontSize: 12 }}>
                  {c.servers.filter((s) => s.name !== HUB).map((s) => (
                    <label key={s.name} style={{ display: 'block' }} title={s.description}>
                      <input type="checkbox" checked={!skip.has(s.name)}
                        onChange={() => toggle(skip, s.name, setSkip)} />{' '}
                      <span className="mono">{s.name}</span>{' '}
                      <span className="mutedtext">{s.description}</span>
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
                          <input type="checkbox" checked={plans.has(p.name)}
                            onChange={() => toggle(plans, p.name, setPlans)} /> {p.name}
                        </label>
                      ))}
                    </div>
                  </>
                )}
                </>
                )}
                {!c.base_url && (
                  <p className="error" style={{ fontSize: 12 }}>
                    서버의 공개 주소가 설정돼 있지 않아 MCP 주소를 만들 수 없습니다(PAAS_PLATFORM_PUBLIC_URL).
                  </p>
                )}
                <div className="row" style={{ marginTop: 10 }}>
                  <button disabled={busy || !c.base_url} onClick={() => void issue()}>
                    {busy ? '발급 중...' : '토큰 발급'}
                  </button>
                </div>
              </>
            )}
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
        </>
      ) : (
        <>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
            ⚠️ 이 화면을 닫으면 토큰을 다시 볼 수 없습니다 — 잃으면 새로 발급하고 쓰던 것을 폐기하세요.
          </p>
          <div className="row" style={{ gap: 6, marginBottom: 6 }}>
            {TARGETS.map(([key, name]) => (
              <button key={key} className={target === key ? 'small' : 'small secondary'}
                onClick={() => setTarget(key)}>{name}</button>
            ))}
            {target === 'desktop' && (
              <select value={os} onChange={(e) => setOs(e.target.value as DesktopOs)} style={{ marginLeft: 8 }}>
                <option value="windows">Windows</option>
                <option value="mac">macOS</option>
              </select>
            )}
          </div>
          <pre className="mono" style={{
            background: '#090d16', padding: 12, borderRadius: 8, fontSize: 12,
            maxHeight: 260, overflow: 'auto', whiteSpace: 'pre',
          }}>{text}</pre>
          <Guide target={target} os={os} />
          <div className="row">
            <button onClick={() => void copy()}>설정 복사</button>
            <button className="secondary" onClick={onClose}>닫기</button>
          </div>
        </>
      )}
      {error && <p className="error">{error}</p>}
    </Modal>
  );
}

function Guide({ target, os }: { target: Target; os: DesktopOs }) {
  if (target === 'desktop') {
    return (
      <ol style={{ fontSize: 12, paddingLeft: 18 }}>
        <li>PC에 Node.js가 있어야 합니다(<span className="mono">npx -v</span>가 나오면 됩니다).</li>
        <li>
          Claude Desktop → 설정 → 개발자 → 「설정 편집」으로{' '}
          <span className="mono">
            {os === 'windows' ? '%APPDATA%\\Claude\\claude_desktop_config.json' : '~/Library/Application Support/Claude/claude_desktop_config.json'}
          </span>
          을 엽니다. 이미 <span className="mono">mcpServers</span>가 있으면 그 안에 항목만 더합니다.
          {os === 'windows' && (
            <> Microsoft Store로 설치했으면 파일이{' '}
              <span className="mono">%LOCALAPPDATA%\Packages\Claude_…\LocalCache\Roaming\Claude\</span> 아래에
              있습니다 — 「설정 편집」이 여는 파일이 맞는 파일입니다.</>
          )}
        </li>
        <li>Claude Desktop을 트레이(메뉴 막대)에서 완전히 종료한 뒤 다시 엽니다.</li>
        <li>
          연결되지 않으면 로그를 봅니다:{' '}
          <span className="mono">
            {os === 'windows' ? '%LOCALAPPDATA%\\Claude\\logs\\mcp-server-paas.log' : '~/Library/Logs/Claude/mcp-server-paas.log'}
          </span>
        </li>
        <li className="mutedtext">
          앱의 「커스텀 커넥터」로는 등록하지 않습니다 — 그 연결은 Anthropic 쪽에서 나가서 사내 주소에 닿지
          않고, 토큰 헤더도 받지 않습니다. 여기 설정은 PC의 mcp-remote가 중계합니다.
        </li>
      </ol>
    );
  }
  if (target === 'code') {
    return (
      <p style={{ fontSize: 12 }}>
        프로젝트의 <span className="mono">.mcp.json</span>(또는 사용자 설정)의 <span className="mono">mcpServers</span>에
        넣습니다. Claude Code는 원격 서버를 그대로 받습니다.
      </p>
    );
  }
  return (
    <p style={{ fontSize: 12 }}>
      게이트웨이(/proxy·/a2a — LLM·모듈·A2A)를 부르는 스크립트·외주 빌더가 쓰는 환경변수입니다. 같은 토큰입니다.
    </p>
  );
}
