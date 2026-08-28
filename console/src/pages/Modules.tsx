import { useState, useEffect } from 'react';
import Async from '../components/Async';
import Modal from '../components/Modal';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { fmtDate } from '../lib/format';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { ApiSearchResult, OrgOut } from '../lib/types';

const TYPE_HINTS: Record<string, string> = {
  external_api: '{"url": "https://...", "api_key": "..."}',
  internal_api: '{"target_project": "다른-프로젝트명"}',
  database: '{"dsn": "postgresql://user:pw@host/db"}',
  mcp: '{"url": "https://mcp.example.com", "api_key": "..."}',
  llm: '{"url": "https://api.example.com/v1", "api_key": "...", "model": "gpt-4o"}',
};

export default function Modules() {
  const state = useApi(() => api.listModules());
  const [showCreate, setShowCreate] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [showMcpSearch, setShowMcpSearch] = useState(false);
  const [showReport, setShowReport] = useState(false);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>모듈 레지스트리</h2>
        <div className="spacer" />
        <button className="secondary" onClick={() => setShowReport(true)}>
          📊 모듈 사용이력 리포트
        </button>
        {isAdmin() && (
          <>
            <button className="secondary" onClick={() => setShowSearch(true)}>
              외부 API 검색
            </button>
            <button className="secondary" onClick={() => setShowMcpSearch(true)}>
              사내 MCP 검색
            </button>
          </>
        )}
        <button onClick={() => setShowCreate(true)}>+ 새 모듈</button>
      </div>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        api_key·dsn·secret 등 민감 필드는 암호화 저장되며 이후 마스킹(•••)으로만 표시됩니다.
      </p>
      <Async state={state} empty="등록된 모듈이 없습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>이름</th>
                <th>타입</th>
                <th>카테고리</th>
                <th>범위</th>
                <th>설정</th>
                {isAdmin() && <th>작업</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}</td>
                  <td>
                    <StatusPill value={m.type} />
                    <EgressBadge verdict={m.egress} />
                  </td>
                  <td className="mutedtext">{m.category || '—'}</td>
                  <td>
                    <StatusPill value={m.organization_id ? 'org' : 'global'} />
                  </td>
                  <td>
                    {m.type === 'external_api' && typeof m.config === 'object' && m.config !== null ? (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {Boolean((m.config as Record<string, unknown>).url) ? (
                          <span style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'rgba(96, 165, 250, 0.15)', color: '#60a5fa', border: '1px solid rgba(96, 165, 250, 0.3)' }}>
                            🌐 URL: {String((m.config as Record<string, unknown>).url)}
                          </span>
                        ) : null}
                        {Boolean((m.config as Record<string, unknown>).api_key || (m.config as Record<string, unknown>).client_id) ? (
                          <span style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'rgba(245, 158, 11, 0.15)', color: '#f59e0b', border: '1px solid rgba(245, 158, 11, 0.3)' }}>
                            🔑 Auth Key Configured
                          </span>
                        ) : null}
                        {Object.keys(m.config).filter(k => !['url', 'api_key', 'client_id', 'client_secret'].includes(k)).length > 0 && (
                          <span style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'rgba(16, 185, 129, 0.15)', color: '#10b981', border: '1px solid rgba(16, 185, 129, 0.3)' }}>
                            ⚙️ +{Object.keys(m.config).filter(k => !['url', 'api_key', 'client_id', 'client_secret'].includes(k)).length} Custom Envs
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className="mono">{JSON.stringify(m.config)}</span>
                    )}
                  </td>
                  {isAdmin() && (
                    <td>
                      {m.type === 'mcp' && <McpCheckButton moduleId={m.id} />}
                      <button
                        className="small danger"
                        onClick={async () => {
                          if (confirm(`정말로 모듈 '${m.name}'을(를) 삭제하시겠습니까?`)) {
                            try {
                              await api.deleteModule(m.id);
                              state.reload();
                            } catch (err) {
                              alert((err as Error).message);
                            }
                          }
                        }}
                      >
                        삭제
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {showCreate && (
        <CreateModuleModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            state.reload();
          }}
        />
      )}
      {showSearch && (
        <SearchApiModal
          onClose={() => setShowSearch(false)}
          onAdded={() => state.reload()}
        />
      )}
      {showMcpSearch && (
        <SearchMcpModal
          onClose={() => setShowMcpSearch(false)}
          onAdded={() => state.reload()}
        />
      )}
      {showReport && <PlatformReportModal onClose={() => setShowReport(false)} />}
    </div>
  );
}

/**
 * 아웃바운드 검증 배지 — 이 모듈로 나가는 **플랫폼 호출**에 내부 정보가 실리는지
 * (app/services/egress.py). 배포된 앱이 {PREFIX}_URL로 직접 부르는 것은 판정 범위가
 * 아니라서, 문구에도 "플랫폼이 보내는 것"이라고 못 박는다 — 없는 보증을 주면 안 된다.
 */
function EgressBadge({ verdict }: { verdict?: import('../lib/types').EgressVerdict }) {
  if (!verdict) return null;
  const chip = (bg: string, color: string, text: string, title: string) => (
    <span
      title={title}
      style={{
        fontSize: 11, padding: '2px 6px', borderRadius: 4, marginLeft: 6,
        background: bg, color, border: `1px solid ${color}55`,
      }}
    >
      {text}
    </span>
  );
  const sends = verdict.platform_sends.length
    ? `플랫폼이 보내는 것: ${verdict.platform_sends.join(', ')}`
    : '플랫폼이 대상에게 보내는 부가 정보 없음';

  if (verdict.scope === 'local') return null;
  if (verdict.scope === 'unknown') {
    return chip('rgba(148,163,184,0.15)', '#94a3b8', '주소 없음', verdict.findings.join('\n'));
  }
  if (verdict.scope === 'internal') {
    return chip('rgba(96,165,250,0.15)', '#60a5fa', '🏠 사내',
      `${verdict.host} — 사내 주소라 데이터가 망을 벗어나지 않습니다.\n${sends}`);
  }
  if (!verdict.secured) {
    return chip('rgba(245,158,11,0.15)', '#f59e0b', '⚠ 점검 필요',
      `${verdict.host}\n${verdict.findings.join('\n')}`);
  }
  return chip('rgba(16,185,129,0.15)', '#10b981', '🔒 Secured',
    `${verdict.host} — 플랫폼이 이 대상으로 보내는 호출에 내부 정보가 실리지 않음을 확인했습니다.\n`
    + `${sends}\n(배포된 앱이 직접 호출하는 것은 이 검증 범위가 아닙니다.)`);
}

function SearchMcpModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<import('../lib/types').McpDirectoryItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Record<string, string>>({});

  const doSearch = async (q: string) => {
    setBusy(true);
    setError('');
    try {
      const res = await api.searchMcpDirectory(q);
      setResults(res);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    doSearch('');
  }, []);

  const searchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    doSearch(query);
  };

  const importMcp = async (item: import('../lib/types').McpDirectoryItem) => {
    setError('');
    try {
      const created = await api.importMcpModule(item.id, item.url, item.category);
      setAdded((a) => ({ ...a, [item.id]: created.name }));
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <Modal title="사내 MCP 서버 검색 및 원클릭 등록" onClose={onClose}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <form className="row" onSubmit={searchSubmit}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="키워드 검색 (예: 운영, 저장소, 코드, 데이터베이스)..."
            style={{ flex: 1 }}
            autoFocus
          />
          <button type="submit" disabled={busy}>
            {busy ? '검색 중...' : '검색'}
          </button>
        </form>

        {error && <div className="error">{error}</div>}

        <div style={{ maxHeight: 400, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {results && results.length === 0 && (
            <div className="mutedtext" style={{ padding: 20, textAlign: 'center' }}>
              검색 조건에 맞는 사내 MCP 서버가 없습니다.
            </div>
          )}
          {results &&
            results.map((r) => (
              <div
                key={r.id}
                className="panel"
                style={{
                  padding: 10,
                  margin: 0,
                  backgroundColor: 'rgba(255,255,255,0.02)',
                  border: '1px solid rgba(255,255,255,0.08)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, color: '#38bdf8' }}>{r.name}</span>
                    <StatusPill value={r.category} />
                    <span style={{ fontSize: 11, color: '#9ca3af', fontFamily: 'monospace' }}>
                      by {r.vendor}
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: '#d1d5db', marginBottom: 4 }}>{r.description}</div>
                  <div style={{ fontSize: 11, color: '#6b7280', fontFamily: 'monospace' }}>{r.url || r.path}</div>
                </div>
                <div>
                  {added[r.id] ? (
                    <span style={{ color: '#10b981', fontSize: 12, fontWeight: 600 }}>
                      ✓ 추가됨 ({added[r.id]})
                    </span>
                  ) : r.url ? (
                    <button className="small" onClick={() => importMcp(r)}>
                      + 모듈로 등록
                    </button>
                  ) : (
                    <span
                      className="mutedtext"
                      style={{ fontSize: 11 }}
                      title="PAAS_MCP_INTERNAL_BASE_URL(없으면 백채널 주소)을 설정해야 등록할 수 있습니다 — 플랫폼이 자기 자신에게 닿는 주소입니다"
                    >
                      기준 주소 미설정
                    </span>
                  )}
                </div>
              </div>
            ))}
        </div>
      </div>
    </Modal>
  );
}

function SearchApiModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState('');   // '' = 전체(기본값)
  const [source, setSource] = useState('');       // '' = 전체 소스
  const [categories, setCategories] = useState<import('../lib/types').ApiCategory[]>([]);
  const [status, setStatus] = useState<import('../lib/types').ApiCatalogStatus | null>(null);
  const [results, setResults] = useState<ApiSearchResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Record<string, string>>({});

  const [notice, setNotice] = useState('');

  // 선택지는 카탈로그에서 받아 온다 — 화면에 적어 두면 실제로는 고를 수 없는 값이 남는다.
  const loadCategories = () => {
    api
      .listApiCategories()
      .then((res) => setCategories(res.categories))
      .catch(() => setCategories([]));   // 목록을 못 받아도 키워드 검색은 되어야 한다
    // 소스 선택지는 수집 현황에서 나온다 — 0건인 소스도 내려오므로, 공공데이터가
    // 비어 있으면 "왜 없는지"(주소 미설정인지 수집 실패인지)를 여기서 보여 줄 수 있다.
    api.apiCatalogStatus().then(setStatus).catch(() => setStatus(null));
  };
  useEffect(loadCategories, []);

  // 검색은 수집해 둔 표만 읽으므로, 아직 한 번도 안 받았으면 아무리 검색해도 비어 있다.
  // 하루 한 번 백그라운드로 돌지만 그 사이에 당겨 쓸 자리가 화면에 없으면
  // "비어 있습니다"만 보고 할 수 있는 일이 없다.
  const refresh = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const r = await api.refreshApiCatalog();
      setNotice(`수집 완료 — 추가 ${r.added} · 갱신 ${r.updated} · 그대로 ${r.unchanged}`
        + (r.removed ? ` · 사라짐 ${r.removed}` : ''));
      setError((r.warnings ?? []).join(' / '));
      loadCategories();   // 카테고리와 소스별 건수를 함께 다시 읽는다
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const search = async (e: React.FormEvent) => {
    e.preventDefault();
    // 셋 다 비면 서버가 빈 목록을 준다 — 미리 막는다(소스만 골라도 조건이다).
    if (!keyword.trim() && !category && !source) return;
    setBusy(true);
    setError('');
    try {
      setNotice('');
      const res = await api.searchApis(keyword.trim(), category, source);
      setResults(res.results);
      // 아직 수집하지 않았으면 그렇다고 말한다 — 그 사실을 감추면 결과가 없는 이유를
      // 알 수 없다.
      setError((res.warnings ?? []).join(' / '));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const add = async (r: ApiSearchResult) => {
    setError('');
    try {
      const url = r.spec_url || r.homepage;
      const created = await api.importApiModule(r.id, url, r.categories[0]);
      setAdded((a) => ({ ...a, [r.id]: created.name }));
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <Modal title="외부 API 검색 → 모듈 추가" onClose={onClose}>
      <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
        수집해 둔 공개 API 카탈로그를 키워드·카테고리로 검색해 external_api 모듈로
        추가합니다(검색은 밖으로 나가지 않습니다 — 수집은 하루 한 번, <b>수집</b> 버튼으로
        당겨 쓸 수 있습니다).
        카테고리 기본값은 전체이고, 카테고리가 없는 API는 <span className="mono">기타</span>로
        고릅니다. 키워드는 이름·설명뿐 아니라 <b>주소</b>에도 걸립니다 — 받아 둔 URL을
        그대로 붙여 넣어 그게 무슨 API였는지 되짚을 수 있습니다. 추가 후
        설정의 <span className="mono">url</span>·<span className="mono">api_key</span>는 새 모듈
        수정에서 채웁니다.
      </p>
      <form onSubmit={search} className="row" style={{ marginBottom: 12 }}>
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="예: payment, weather — 주소를 그대로 붙여 넣어도 됩니다"
          style={{ flex: 1 }}
        />
        <select
          value={source}
          onChange={(e) => setSource(e.target.value)}
          title="어느 카탈로그에서 찾을지 — 기본은 전체입니다"
        >
          <option value="">모든 소스</option>
          {Object.entries(status?.sources ?? {}).map(([key, s]) => (
            <option key={key} value={key}>
              {s.label} ({s.total})
            </option>
          ))}
        </select>
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          title="카테고리 조건 — 기본은 전체입니다"
        >
          <option value="">전체</option>
          {categories.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} ({c.count})
            </option>
          ))}
        </select>
        <button type="submit" disabled={busy || (!keyword.trim() && !category && !source)}>
          {busy ? '검색 중...' : '검색'}
        </button>
        <button
          type="button"
          className="secondary"
          onClick={refresh}
          disabled={busy}
          title="apis.guru·공공데이터에서 카탈로그를 지금 다시 받습니다(아웃바운드)"
        >
          수집
        </button>
      </form>
      {notice && <p className="mutedtext" style={{ fontSize: 12 }}>{notice}</p>}
      {/* 고른 소스가 0건이면 검색하기 전에 이유를 말해 준다 — 빈 결과만 보고
          "그런 API가 없다"로 읽으면 설정을 고칠 생각을 못 한다. */}
      {source && status?.sources[source]?.total === 0 && (
        <p className="mutedtext" style={{ fontSize: 12 }}>
          {status.sources[source].label}에서 받아 둔 것이 없습니다 —{' '}
          {status.sources[source].enabled
            ? '수집 버튼을 눌러 보세요.'
            : '서버에 이 소스의 주소가 설정돼 있지 않아 아예 부르지 않습니다(PAAS_PUBLIC_DATA_URL).'}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {results && results.length === 0 && (
        <p className="mutedtext">검색 결과가 없습니다.</p>
      )}
      {results && results.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>API</th>
              <th>카테고리</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr key={r.id}>
                <td>
                  <div>{r.title}</div>
                  <div className="mutedtext" style={{ fontSize: 12 }}>
                    {r.description || r.provider}
                  </div>
                </td>
                <td className="mutedtext" style={{ fontSize: 12 }}>
                  {r.categories.join(', ') || '—'}
                  <div>{status?.sources[r.source]?.label ?? r.source}</div>
                </td>
                <td>
                  {added[r.id] ? (
                    <span className="status ok">추가됨: {added[r.id]}</span>
                  ) : (
                    <button className="small" onClick={() => add(r)}>추가</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}

function GroupedApiConfigForm({
  config,
  onChange,
}: {
  config: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
}) {
  const [url, setUrl] = useState<string>(String(config.url ?? ''));
  const [apiKey, setApiKey] = useState<string>(String(config.api_key ?? ''));
  const [clientId, setClientId] = useState<string>(String(config.client_id ?? ''));
  const [clientSecret, setClientSecret] = useState<string>(String(config.client_secret ?? ''));
  const [customPairs, setCustomPairs] = useState<Array<{ key: string; value: string }>>(() => {
    const known = new Set(['url', 'api_key', 'client_id', 'client_secret']);
    return Object.entries(config)
      .filter(([k]) => !known.has(k))
      .map(([key, value]) => ({ key, value: String(value ?? '') }));
  });

  const update = (
    newUrl: string,
    newApiKey: string,
    newClientId: string,
    newClientSecret: string,
    pairs: Array<{ key: string; value: string }>,
  ) => {
    const res: Record<string, unknown> = {};
    if (newUrl.trim()) res.url = newUrl.trim();
    if (newApiKey.trim()) res.api_key = newApiKey.trim();
    if (newClientId.trim()) res.client_id = newClientId.trim();
    if (newClientSecret.trim()) res.client_secret = newClientSecret.trim();
    for (const p of pairs) {
      if (p.key.trim()) {
        res[p.key.trim()] = p.value.trim();
      }
    }
    onChange(res);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 4 }}>
      {/* 🌐 그룹 1: 기본 접속 정보 */}
      <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#60a5fa', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          🌐 기본 연결 정보 (Connection & Endpoint)
        </div>
        <label className="field">
          API Endpoint URL <span style={{ color: '#ef4444' }}>*</span>
          <input
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              update(e.target.value, apiKey, clientId, clientSecret, customPairs);
            }}
            placeholder="https://api.service.com/v1"
            required
          />
        </label>
      </div>

      {/* 🔑 그룹 2: 인증 & 보안 키 */}
      <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#f59e0b', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          🔑 인증 & 보안 키 (Authentication & Secret Keys)
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <label className="field">
            API Key / Secret Token (자동 암호화 보관)
            <input
              type="password"
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                update(url, e.target.value, clientId, clientSecret, customPairs);
              }}
              placeholder="paas_sec_... 또는 Bearer 토큰"
            />
          </label>
          <div className="row" style={{ gap: 10 }}>
            <label className="field" style={{ flex: 1 }}>
              Client ID / App Key
              <input
                value={clientId}
                onChange={(e) => {
                  setClientId(e.target.value);
                  update(url, apiKey, e.target.value, clientSecret, customPairs);
                }}
                placeholder="client_id_123"
              />
            </label>
            <label className="field" style={{ flex: 1 }}>
              Client Secret (자동 암호화 보관)
              <input
                type="password"
                value={clientSecret}
                onChange={(e) => {
                  setClientSecret(e.target.value);
                  update(url, apiKey, clientId, e.target.value, customPairs);
                }}
                placeholder="client_secret_456"
              />
            </label>
          </div>
        </div>
      </div>

      {/* ⚙️ 그룹 3: 추가 커스텀 환경변수 */}
      <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#10b981', display: 'flex', alignItems: 'center', gap: 6 }}>
            ⚙️ 추가 환경변수 & 헤더 (Custom Variables)
          </div>
          <button
            type="button"
            className="secondary small"
            onClick={() => {
              const next = [...customPairs, { key: '', value: '' }];
              setCustomPairs(next);
              update(url, apiKey, clientId, clientSecret, next);
            }}
          >
            + 환경변수 추가
          </button>
        </div>
        {customPairs.length === 0 ? (
          <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
            추가로 주입할 환경변수(예: TIMEOUT, VERSION 등)가 있으면 항목을 추가하세요.
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {customPairs.map((pair, idx) => (
              <div key={idx} className="row" style={{ gap: 8, alignItems: 'center' }}>
                <input
                  style={{ flex: 1 }}
                  placeholder="변수명 (예: TIMEOUT)"
                  value={pair.key}
                  onChange={(e) => {
                    const next = [...customPairs];
                    next[idx].key = e.target.value;
                    setCustomPairs(next);
                    update(url, apiKey, clientId, clientSecret, next);
                  }}
                />
                <input
                  style={{ flex: 1 }}
                  placeholder="값 (예: 30)"
                  value={pair.value}
                  onChange={(e) => {
                    const next = [...customPairs];
                    next[idx].value = e.target.value;
                    setCustomPairs(next);
                    update(url, apiKey, clientId, clientSecret, next);
                  }}
                />
                <button
                  type="button"
                  className="secondary small"
                  style={{ color: '#ef4444', padding: '4px 8px' }}
                  onClick={() => {
                    const next = customPairs.filter((_, i) => i !== idx);
                    setCustomPairs(next);
                    update(url, apiKey, clientId, clientSecret, next);
                  }}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function CreateModuleModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const orgs = useApi<OrgOut[]>(() => api.listOrgs());
  const [name, setName] = useState('');
  const [type, setType] = useState('external_api');
  const [category, setCategory] = useState('');
  const [organizationId, setOrganizationId] = useState('');
  const [configObj, setConfigObj] = useState<Record<string, unknown>>({ url: '' });
  const [configJson, setConfigJson] = useState(TYPE_HINTS.external_api);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');

    let finalConfig: Record<string, unknown> = {};

    if (type === 'external_api') {
      finalConfig = configObj;
      if (!finalConfig.url) {
        setError('API Endpoint URL은 필수 입력 항목입니다.');
        setBusy(false);
        return;
      }
    } else {
      try {
        finalConfig = JSON.parse(configJson);
      } catch {
        setError('설정이 올바른 JSON이 아닙니다.');
        setBusy(false);
        return;
      }
    }

    try {
      await api.createModule(
        name.trim(), type, finalConfig,
        category.trim() || undefined,
        organizationId ? Number(organizationId) : undefined,
      );
      onCreated();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal title="새 모듈 등록" onClose={onClose}>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <label className="field">
          모듈 이름 — 빈칸 없이 소문자·숫자·하이픈만 사용 (예: user-storage)
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            pattern="[a-z0-9][a-z0-9-]{1,40}"
            title="빈칸 없이 소문자·숫자·하이픈만 사용하세요 (예: user-storage)"
            placeholder="user-storage"
            required
            autoFocus
          />
        </label>
        <label className="field">
          타입
          <select
            value={type}
            onChange={(e) => {
              const newType = e.target.value;
              setType(newType);
              setConfigJson(TYPE_HINTS[newType] || '{}');
            }}
          >
            <option value="external_api">external_api — 외부 API (환경변수 그룹 관리)</option>
            <option value="internal_api">internal_api — 플랫폼 내 프로젝트</option>
            <option value="database">database — DB 연결</option>
            <option value="mcp">mcp — MCP 서버 (사내·외부 공통)</option>
            <option value="llm">llm — 배포 앱이 쓸 LLM 엔드포인트</option>
          </select>
        </label>

        <label className="field">
          카테고리 (선택 — 예: storage, media, docs)
          <input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="storage" />
        </label>

        {orgs.data && orgs.data.length > 0 && (
          <label className="field">
            조직 범위 (선택)
            <select value={organizationId} onChange={(e) => setOrganizationId(e.target.value)}>
              <option value="">전역 (모든 프로젝트)</option>
              {orgs.data.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        )}

        {/* 타입별 맞춤형 환경변수 및 폴더 지정 폼 */}
        {type === 'external_api' ? (
          <GroupedApiConfigForm config={configObj} onChange={setConfigObj} />
        ) : (
          <label className="field">
            설정 (JSON)
            <textarea
              className="mono"
              rows={4}
              value={configJson}
              onChange={(e) => setConfigJson(e.target.value)}
            />
          </label>
        )}

        {error && <p className="error">{error}</p>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 10 }}>
          <button type="button" className="secondary" onClick={onClose}>
            취소
          </button>
          <button type="submit" disabled={busy}>
            {busy ? '등록 중...' : '등록'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function PlatformReportModal({ onClose }: { onClose: () => void }) {
  const reportState = useApi(() => api.getPlatformModuleReport());

  return (
    <Modal title="📊 house 플랫폼 전역 모듈 사용이력 통합 리포트" onClose={onClose}>
      <div style={{ minWidth: 680, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
          house 플랫폼에 등록된 모든 모듈과 이를 바인딩하여 사용 중인 프로젝트 목록, 그리고 최근 모듈 관련 변경 이력을 종합 리포팅합니다.
        </p>

        <Async state={reportState}>
          {(report) => (
            <>
              {/* 요약 카드 */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 12 }}>
                <div style={{ padding: 14, borderRadius: 8, background: 'rgba(56, 189, 248, 0.08)', border: '1px solid rgba(56, 189, 248, 0.2)' }}>
                  <div style={{ fontSize: 12, color: '#888' }}>총 등록 모듈</div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: '#38bdf8', marginTop: 4 }}>
                    {report.total_modules}개
                  </div>
                </div>
                <div style={{ padding: 14, borderRadius: 8, background: 'rgba(16, 185, 129, 0.08)', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                  <div style={{ fontSize: 12, color: '#888' }}>총 바인딩 연결 건수</div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: '#10b981', marginTop: 4 }}>
                    {report.total_bindings}건
                  </div>
                </div>
              </div>

              {/* 전역 모듈 현황 테이블 */}
              <div style={{ marginTop: 8 }}>
                <h4 style={{ margin: '0 0 8px 0', fontSize: 14 }}>🧩 등록 모듈 및 프로젝트 바인딩 현황</h4>
                <div style={{ maxHeight: 260, overflowY: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>모듈명</th>
                        <th>타입</th>
                        <th>소속 조직</th>
                        <th>바인딩 프로젝트 ({report.total_bindings})</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.modules.map((m) => (
                        <tr key={m.module_id}>
                          <td style={{ fontWeight: 600 }}>{m.module_name}</td>
                          <td><StatusPill value={m.type} /></td>
                          <td>{m.organization_name || '전역(Global)'}</td>
                          <td>
                            {m.bound_projects.length === 0 ? (
                              <span className="mutedtext" style={{ fontSize: 11 }}>미바인딩</span>
                            ) : (
                              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                                {m.bound_projects.map((p) => (
                                  <span
                                    key={p}
                                    style={{
                                      fontSize: 11,
                                      padding: '1px 6px',
                                      borderRadius: 4,
                                      background: 'rgba(56, 189, 248, 0.15)',
                                      color: '#38bdf8',
                                      border: '1px solid rgba(56, 189, 248, 0.3)',
                                    }}
                                  >
                                    🏢 {p}
                                  </span>
                                ))}
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* 최근 모듈 감사 로그 */}
              <div style={{ marginTop: 8 }}>
                <h4 style={{ margin: '0 0 8px 0', fontSize: 14 }}>📜 최근 모듈 변경 이력 ({report.recent_history.length})</h4>
                <div style={{ maxHeight: 200, overflowY: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>일시</th>
                        <th>작업자</th>
                        <th>Action</th>
                        <th>대상</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.recent_history.map((h) => (
                        <tr key={h.id}>
                          <td className="mono" style={{ fontSize: 11 }}>{fmtDate(h.created_at)}</td>
                          <td className="mono" style={{ fontSize: 11 }}>{h.actor}</td>
                          <td><StatusPill value={h.action} /></td>
                          <td className="mono" style={{ fontSize: 11 }}>{h.target}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </Async>

        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
          <button type="button" onClick={onClose}>
            닫기
          </button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * mcp 모듈 연결 확인 — tools/list를 한 번 찔러 결과를 그 자리에 보여준다.
 *
 * 등록만으로는 동작을 알 수 없다. 주소가 틀렸거나 전송 방식이 안 맞으면 등록은 성공한
 * 채 조용히 죽어 있어서, 채팅에서 도구가 안 보일 때까지 아무도 모른다.
 */
type McpCheck = { ok: boolean; error: string | null; tool_count: number; can_issue_key?: boolean };

function McpCheckButton({ moduleId }: { moduleId: number }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<McpCheck | null>(null);

  const check = async () => {
    setBusy(true);
    try {
      setResult(await api.checkMcpModule(moduleId));
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message, tool_count: 0 });
    } finally {
      setBusy(false);
    }
  };

  // 사내 MCP 서버는 인증을 요구하는데, '사내 MCP 검색'으로 가져온 모듈에만 키가
  // 자동 발급된다. 그 전에 등록됐거나 주소를 직접 적어 만든 모듈은 키가 빈 채로 남는다.
  // 예전 안내는 지우고 다시 가져오라고 했지만, 바인딩된 프로젝트가 있으면 그럴 수 없다.
  const issueKey = async () => {
    setBusy(true);
    try {
      await api.issueMcpModuleKey(moduleId);
      setResult(await api.checkMcpModule(moduleId)); // 발급이 실제로 통했는지 바로 확인
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message, tool_count: 0 });
    } finally {
      setBusy(false);
    }
  };

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginRight: 6 }}>
      <button className="small secondary" disabled={busy} onClick={check}>
        {busy ? '확인 중…' : '연결 확인'}
      </button>
      {result && !result.ok && result.can_issue_key && (
        <button
          className="small"
          disabled={busy}
          onClick={issueKey}
          title="이 모듈에 전용 키를 발급해 넣습니다 (모듈을 지울 필요 없음)"
        >
          키 발급
        </button>
      )}
      {result && (
        <span
          style={{ fontSize: 11, color: result.ok ? '#10b981' : '#ef4444', maxWidth: 320 }}
          title={result.error ?? ''}
        >
          {result.ok ? `응답함 · 도구 ${result.tool_count}개` : `실패 — ${result.error ?? ''}`}
        </span>
      )}
    </span>
  );
}
