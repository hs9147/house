import { useState } from 'react';
import Async from '../components/Async';
import Modal from '../components/Modal';
import StatusPill from '../components/StatusPill';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { ApiSearchResult, OrgOut } from '../lib/types';

const TYPE_HINTS: Record<string, string> = {
  external_api: '{"url": "https://...", "api_key": "..."}',
  internal_api: '{"target_project": "다른-프로젝트명"}',
  database: '{"dsn": "postgresql://user:pw@host/db"}',
  file_storage: '{"endpoint": "http://...", "bucket": "..."}',
  mcp: '{"url": "https://mcp.example.com", "api_key": "..."}',
};

export default function Modules() {
  const state = useApi(() => api.listModules());
  const [showCreate, setShowCreate] = useState(false);
  const [showSearch, setShowSearch] = useState(false);

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>모듈 레지스트리</h2>
        <div className="spacer" />
        {isAdmin() && (
          <button className="secondary" onClick={() => setShowSearch(true)}>
            외부 API 검색
          </button>
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
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}</td>
                  <td><StatusPill value={m.type} /></td>
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
    </div>
  );
}

function SearchApiModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [keyword, setKeyword] = useState('');
  const [results, setResults] = useState<ApiSearchResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Record<string, string>>({});

  const search = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyword.trim()) return;
    setBusy(true);
    setError('');
    try {
      const res = await api.searchApis(keyword.trim());
      setResults(res.results);
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
        공개 API 디렉터리를 키워드로 검색해 external_api 모듈로 추가합니다. 추가 후
        설정의 <span className="mono">url</span>·<span className="mono">api_key</span>는 새 모듈
        수정에서 채웁니다.
      </p>
      <form onSubmit={search} className="row" style={{ marginBottom: 12 }}>
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="예: payment, weather, calendar"
          style={{ flex: 1 }}
        />
        <button type="submit" disabled={busy || !keyword.trim()}>
          {busy ? '검색 중...' : '검색'}
        </button>
      </form>
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

function FileStorageConfigForm({
  config,
  onChange,
  storageMode,
  setStorageMode,
  zipFile,
  setZipFile,
}: {
  config: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
  storageMode: 'folder' | 'zip';
  setStorageMode: (mode: 'folder' | 'zip') => void;
  zipFile: File | null;
  setZipFile: (file: File | null) => void;
}) {
  const [rootPath, setRootPath] = useState<string>(String(config.endpoint ?? './data/storage'));
  const [subFolder, setSubFolder] = useState<string>(String(config.sub_folder ?? config.bucket ?? 'uploads'));

  const update = (root: string, sub: string) => {
    onChange({
      endpoint: root.trim(),
      sub_folder: sub.trim(),
      bucket: sub.trim(),
    });
  };

  const rootClean = rootPath.trim().replace(/[/\\]+$/, '');
  const subClean = subFolder.trim().replace(/^[/\\]+/, '');
  const fullPath = subClean ? `${rootClean}/${subClean}` : rootClean;
  const zipFolderName = zipFile ? zipFile.name.replace(/\.[^/.]+$/, '').trim() : '';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 4 }}>
      {/* 방식 선택 라디오 */}
      <div className="panel" style={{ padding: 10, margin: 0, backgroundColor: 'rgba(255,255,255,0.03)' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#9ca3af', marginBottom: 8, display: 'block' }}>
          저장소 구성 방식
        </span>
        <div className="row" style={{ gap: 16 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="radio"
              name="storageMode"
              checked={storageMode === 'folder'}
              onChange={() => setStorageMode('folder')}
            />
            📂 하위 폴더 직접 지정
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="radio"
              name="storageMode"
              checked={storageMode === 'zip'}
              onChange={() => setStorageMode('zip')}
            />
            📦 ZIP 파일 업로드 (파일명 폴더 자동 생성)
          </label>
        </div>
      </div>

      {storageMode === 'folder' ? (
        <>
          <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#38bdf8', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              💾 Root 경로 (환경변수 PAAS_STORAGE_ROOT 반영)
            </div>
            <label className="field">
              저장소 Root 경로
              <input
                value={rootPath}
                onChange={(e) => {
                  setRootPath(e.target.value);
                  update(e.target.value, subFolder);
                }}
                placeholder="./data/storage 또는 /mnt/nas/storage"
              />
            </label>
          </div>

          <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#f59e0b', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              📂 하위 폴더 지정
            </div>
            <label className="field">
              Root 경로 하위 폴더 명칭 <span style={{ color: '#ef4444' }}>*</span>
              <input
                value={subFolder}
                onChange={(e) => {
                  setSubFolder(e.target.value);
                  update(rootPath, e.target.value);
                }}
                placeholder="uploads 또는 shared-data"
                required={storageMode === 'folder'}
              />
            </label>
            <div style={{ marginTop: 8, padding: 8, borderRadius: 4, background: 'rgba(0,0,0,0.3)', fontSize: 12 }}>
              <span style={{ color: '#9ca3af' }}>최종 자동 결합 경로: </span>
              <span style={{ color: '#38bdf8', fontWeight: 600, fontFamily: 'monospace' }}>{fullPath}</span>
            </div>
          </div>
        </>
      ) : (
        <div className="panel" style={{ padding: 12, margin: 0, backgroundColor: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.08)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#10b981', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
            📦 ZIP 압축 파일 선택 (Root 경로에 파일명으로 폴더 자동 생성)
          </div>
          <input
            type="file"
            accept=".zip"
            onChange={(e) => setZipFile(e.target.files?.[0] ?? null)}
            required={storageMode === 'zip'}
          />
          {zipFile && (
            <div style={{ marginTop: 10, padding: 8, borderRadius: 4, background: 'rgba(16, 185, 129, 0.1)', border: '1px solid rgba(16, 185, 129, 0.3)', fontSize: 12 }}>
              <span style={{ color: '#a7f3d0' }}>생성될 폴더 경로: </span>
              <span style={{ color: '#10b981', fontWeight: 600, fontFamily: 'monospace' }}>
                {rootClean}/{zipFolderName}
              </span>
            </div>
          )}
        </div>
      )}
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
  const [storageMode, setStorageMode] = useState<'folder' | 'zip'>('folder');
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');

    if (type === 'file_storage' && storageMode === 'zip') {
      if (!zipFile) {
        setError('업로드할 ZIP 파일을 선택해 주세요.');
        setBusy(false);
        return;
      }
      try {
        await api.uploadFileStorageModule(
          zipFile,
          name.trim(),
          category.trim() || undefined,
          organizationId ? Number(organizationId) : undefined,
        );
        onCreated();
      } catch (err) {
        setError((err as Error).message);
        setBusy(false);
      }
      return;
    }

    let finalConfig: Record<string, unknown> = {};

    if (type === 'external_api' || type === 'file_storage') {
      finalConfig = configObj;
      if (type === 'external_api' && !finalConfig.url) {
        setError('API Endpoint URL은 필수 입력 항목입니다.');
        setBusy(false);
        return;
      }
      if (type === 'file_storage' && !finalConfig.sub_folder) {
        setError('하위 폴더 지정은 필수 항목입니다.');
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
              if (newType === 'file_storage') {
                setConfigObj({ endpoint: './data/storage', sub_folder: 'uploads' });
              } else {
                setConfigJson(TYPE_HINTS[newType] || '{}');
              }
            }}
          >
            <option value="external_api">external_api — 외부 API (환경변수 그룹 관리)</option>
            <option value="file_storage">file_storage — 파일 저장소 (Root/하위 폴더 지정)</option>
            <option value="internal_api">internal_api — 플랫폼 내 프로젝트</option>
            <option value="database">database — DB 연결</option>
            <option value="mcp">mcp — 외부 MCP 서버</option>
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
        ) : type === 'file_storage' ? (
          <FileStorageConfigForm
            config={configObj}
            onChange={setConfigObj}
            storageMode={storageMode}
            setStorageMode={setStorageMode}
            zipFile={zipFile}
            setZipFile={setZipFile}
          />
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
