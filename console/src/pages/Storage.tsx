import { useEffect, useMemo, useRef, useState } from 'react';
import Async from '../components/Async';
import Split from '../components/Split';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { StorageFile } from '../lib/types';
import StoragePreview, { fmtSize } from './storage/StoragePreview';

// 목록에 한 번에 그리는 파일 수 — 사내 문서 저장소는 파일이 수천 개다. 넘으면 찾기로 좁힌다.
const MAX_LIST = 300;

/**
 * 저장소 관리 — 부서 저장소를 고르고, 그 안의 파일을 찾아 미리 본다(왼쪽 목록 · 오른쪽 미리보기).
 * 관리자는 저장소를 등록하고 소속 부서·읽기 전용을 정한다.
 */
export default function Storage() {
  const storesState = useApi(() => api.listStorageStores());
  const orgsState = useApi(() => api.listOrgs());
  const [newName, setNewName] = useState('');
  const [newRoot, setNewRoot] = useState('');
  const [newOrg, setNewOrg] = useState('');
  const [newReadOnly, setNewReadOnly] = useState(false);
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState('');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [order, setOrder] = useState<'path' | 'recent'>('path');
  const [picked, setPicked] = useState<StorageFile | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const stores = storesState.data ?? [];
  const active = stores.find((s) => s.name === selected) ?? stores[0];
  const listing = useApi(() => (active ? api.listStorageFiles(active.name) : Promise.resolve(null)),
    [active?.name]);
  useEffect(() => { setPicked(null); setQuery(''); }, [active?.name]);

  const files = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = (listing.data?.files ?? []).filter((f) => !q || f.path.toLowerCase().includes(q));
    return order === 'recent' ? [...all].sort((a, b) => b.mtime - a.mtime) : all;
  }, [listing.data, query, order]);

  const createStore = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try {
      await api.createStorageStore({ name: newName.trim(), root: newRoot.trim(),
        organization_id: Number(newOrg), read_only: newReadOnly });
      setNewName(''); setNewRoot(''); setNewOrg('');
      storesState.reload();
    } catch (err) { setError((err as Error).message); }
  };

  const assignStore = async (organizationId: number, readOnly: boolean) => {
    if (!active) return;
    setError('');
    try {
      await api.updateStorageStore(active.name, { organization_id: organizationId, read_only: readOnly });
      storesState.reload();
    } catch (err) { setError((err as Error).message); }
  };

  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !active) return;
    setBusy(true);
    setError('');
    setDone('');
    try {
      const saved = await api.uploadStorageFile(active.name, file, path.trim() || undefined);
      setDone(`저장했습니다: ${saved.path}`);
      setPath('');
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
      // 올린 파일을 목록에서 바로 고른다 — 어디에 들어갔는지 눈으로 확인하게.
      const fresh = await api.listStorageFiles(active.name);
      listing.reload();
      setPicked(fresh.files.find((f) => f.path === saved.path) ?? null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const left = (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>저장소 관리</h2>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          저장소는 부서에 소속됩니다. 소속된 여러 부서의 저장소와 온톨로지를 함께 볼 수 있습니다.
          전 폴더를 가로질러 <b>읽는</b> 창구는 사내 MCP 서버 <code>paas-docs</code>이고,
          폴더 하나를 다루는 창구는 <code>paas-storage-{'{'}폴더{'}'}</code>입니다.
          삭제는 폴더 안 <code>.trash</code>로 옮기는 것이라 되돌릴 수 있습니다.
        </p>
        {isAdmin() && (
          <form className="row" onSubmit={createStore} style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <input placeholder="저장소 이름" value={newName} onChange={(e) => setNewName(e.target.value)} required />
            <input placeholder="서버의 절대 폴더 경로" value={newRoot} onChange={(e) => setNewRoot(e.target.value)} required />
            <select value={newOrg} onChange={(e) => setNewOrg(e.target.value)} required>
              <option value="">소속 부서</option>
              {(orgsState.data ?? []).map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
            </select>
            <label><input type="checkbox" checked={newReadOnly} onChange={(e) => setNewReadOnly(e.target.checked)} /> 읽기 전용</label>
            <button type="submit">저장소 등록</button>
          </form>
        )}

        <Async state={storesState}>
          {() =>
            !active ? (
              <p className="mutedtext">
                접근 가능한 저장소가 없습니다. 관리자에게 부서 소속을 확인하세요.
              </p>
            ) : (
              <>
                <div className="row" style={{ marginBottom: 8 }}>
                  <select value={active.name} onChange={(e) => setSelected(e.target.value)}>
                    {stores.map((s) => (
                      // 읽기 전용만 표시하면 나머지가 무엇인지는 없는 표시로 읽어야 한다 —
                      // 둘 다 적어 상태를 눈으로 바로 알 수 있게 한다.
                      <option key={s.name} value={s.name}>
                        {s.name} {s.read_only ? '(읽기 전용)' : '(읽기/쓰기)'}
                      </option>
                    ))}
                  </select>
                </div>
                <p className="mono mutedtext" style={{ fontSize: 12, marginBottom: 8 }}>
                  {active.root || `소속 부서 ID: ${active.organization_id ?? '미배정'}`}
                  {!active.exists && ' — 이 경로에 디렉터리가 없습니다'}
                </p>
                {isAdmin() && (
                  <div className="row" style={{ marginBottom: 8 }}>
                    <select value={active.organization_id ?? ''}
                            onChange={(e) => e.target.value && assignStore(Number(e.target.value), active.read_only)}>
                      <option value="">부서 미배정 — 관리자만 접근</option>
                      {(orgsState.data ?? []).map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
                    </select>
                    <label><input type="checkbox" checked={active.read_only}
                                  onChange={(e) => active.organization_id && assignStore(active.organization_id, e.target.checked)} /> 읽기 전용</label>
                  </div>
                )}
                {active.read_only ? (
                  <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                    이 저장소는 읽기 전용이라 업로드가 막혀 있습니다.
                  </p>
                ) : (
                  <form className="row" onSubmit={upload} style={{ flexWrap: 'wrap' }}>
                    <input ref={fileInput} type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required />
                    <input className="mono" placeholder="저장 경로 (비우면 파일명 그대로)" value={path}
                           onChange={(e) => setPath(e.target.value)} />
                    <button disabled={busy || !file}>{busy ? '업로드 중...' : '업로드'}</button>
                  </form>
                )}
                {error && <p className="error">{error}</p>}
                {done && <p className="mutedtext" style={{ fontSize: 12 }}>{done}</p>}
              </>
            )
          }
        </Async>
      </div>

      {active && (
        <div className="panel">
          <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }}>
            <h3 style={{ margin: 0 }}>파일</h3>
            <input placeholder="이름·경로 일부" value={query} onChange={(e) => setQuery(e.target.value)}
                   style={{ flex: 1, minWidth: 140 }} />
            <select value={order} onChange={(e) => setOrder(e.target.value as 'path' | 'recent')}>
              <option value="path">경로순</option>
              <option value="recent">최근 바뀐 순</option>
            </select>
            <button className="small secondary" onClick={() => listing.reload()}>새로 고침</button>
          </div>
          <Async state={listing}>
            {() => (
              <>
                <div className="mutedtext" style={{ fontSize: 11, marginBottom: 4 }}>
                  {query ? `찾은 ${files.length}개 / 전체 ${listing.data?.files.length ?? 0}개`
                    : `전체 ${files.length}개`}
                  {files.length > MAX_LIST && ` — 앞 ${MAX_LIST}개만 보입니다. 찾기로 좁히세요.`}
                </div>
                {files.length === 0 ? (
                  <p className="mutedtext" style={{ fontSize: 12 }}>
                    {query ? '맞는 파일이 없습니다.' : '파일이 없습니다.'}
                  </p>
                ) : (
                  <div style={{ maxHeight: '55vh', overflow: 'auto' }}>
                    <table style={{ fontSize: 12 }}>
                      <thead><tr><th>파일</th><th>크기</th><th>바뀐 때</th></tr></thead>
                      <tbody>
                        {files.slice(0, MAX_LIST).map((f) => (
                          <tr key={f.path} onClick={() => setPicked(f)}
                              style={{ cursor: 'pointer',
                                       background: picked?.path === f.path ? 'rgba(56, 189, 248, 0.10)' : undefined }}>
                            <td className="mono" style={{ overflowWrap: 'anywhere' }}>{f.path}</td>
                            <td style={{ whiteSpace: 'nowrap' }}>{fmtSize(f.size)}</td>
                            <td style={{ whiteSpace: 'nowrap' }}>{new Date(f.mtime * 1000).toLocaleDateString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </Async>
        </div>
      )}
    </>
  );

  return (
    <Split leftLabel="저장소" left={left} leftWidth={520}
           right={<StoragePreview store={active?.name ?? ''} file={picked} />} />
  );
}
