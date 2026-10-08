import { useRef, useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';

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
  const fileInput = useRef<HTMLInputElement>(null);

  const stores = storesState.data ?? [];
  const active = stores.find((s) => s.name === selected) ?? stores[0];

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
      // 목록을 보여주지 않으므로, 어디에 저장됐는지는 여기서 말해 주어야 한다.
      setDone(`저장했습니다: ${saved.path}`);
      setPath('');
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>파일 관리</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        저장소는 부서에 소속됩니다. 소속된 여러 부서의 저장소와 온톨로지를 함께 볼 수 있습니다.
        전 폴더를 가로질러 <b>읽는</b> 창구는 사내 MCP 서버 <code>paas-docs</code>이고,
        폴더 하나를 다루는 창구는 <code>paas-storage-{'{'}폴더{'}'}</code>입니다.
        삭제는 폴더 안 <code>.trash</code>로 옮기는 것이라 되돌릴 수 있습니다.
      </p>
      {isAdmin() && (
        <form className="row" onSubmit={createStore} style={{ marginBottom: 16 }}>
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
            // 경로를 어디서 정하는지 함께 적는다 — "없습니다"만 두면 서버에서 무엇을
            // 해야 하는지 알 방법이 없다.
            <p className="mutedtext">
              접근 가능한 저장소가 없습니다. 관리자에게 부서 소속을 확인하세요.
            </p>
          ) : (
            <>
              <div className="row" style={{ marginBottom: 12 }}>
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

              <p className="mono mutedtext" style={{ fontSize: 12, marginBottom: 12 }}>
                {active.root || `소속 부서 ID: ${active.organization_id ?? '미배정'}`}
                {!active.exists && ' — 이 경로에 디렉터리가 없습니다'}
              </p>
              {isAdmin() && (
                <div className="row" style={{ marginBottom: 12 }}>
                  <select value={active.organization_id ?? ''}
                          onChange={(e) => e.target.value && assignStore(Number(e.target.value), active.read_only)}>
                    <option value="">부서 미배정 — 관리자만 접근</option>
                    {(orgsState.data ?? []).map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
                  </select>
                  <label><input type="checkbox" checked={active.read_only}
                                onChange={(e) => active.organization_id && assignStore(active.organization_id, e.target.checked)} /> 읽기 전용</label>
                </div>
              )}

              {error && <p className="error">{error}</p>}
              {done && <p className="mutedtext" style={{ fontSize: 12 }}>{done}</p>}

              {active.read_only ? (
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  이 저장소는 읽기 전용이라 업로드가 막혀 있습니다.
                </p>
              ) : (
                <form className="row" onSubmit={upload}>
                  <input
                    ref={fileInput}
                    type="file"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                    required
                  />
                  <input
                    className="mono"
                    placeholder="저장 경로 (비우면 파일명 그대로)"
                    value={path}
                    onChange={(e) => setPath(e.target.value)}
                  />
                  <button disabled={busy || !file}>{busy ? '업로드 중...' : '업로드'}</button>
                </form>
              )}
            </>
          )
        }
      </Async>
    </div>
  );
}
