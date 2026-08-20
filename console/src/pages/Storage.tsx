import { useRef, useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function Storage() {
  const storesState = useApi(() => api.listStorageStores());
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);

  const stores = storesState.data ?? [];
  const active = stores.find((s) => s.name === selected) ?? stores[0];
  const listing = useApi(
    () => (active ? api.listStorageFiles(active.name) : Promise.resolve(null)),
    [active?.name],
  );

  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !active) return;
    setBusy(true);
    setError('');
    try {
      await api.uploadStorageFile(active.name, file, path.trim() || undefined);
      setPath('');
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
      listing.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const download = async (target: string) => {
    if (!active) return;
    setError('');
    try {
      const blob = await api.downloadStorageFile(active.name, target);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = target.split('/').pop() || target;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const remove = async (target: string) => {
    if (!active) return;
    if (!confirm(`${target} 파일을 삭제할까요? 되돌릴 수 없습니다.`)) return;
    setError('');
    try {
      await api.deleteStorageFile(active.name, target);
      listing.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>파일 관리</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        저장소 목록은 서버 환경변수가 정합니다 — 내부 저장소는 <code>PAAS_STORAGE_ROOT</code>,
        사내 문서 폴더는 <code>PAAS_DOC_ROOTS</code>(읽기 전용). 같은 파일을 LLM이 본문으로
        검색하는 창구는 사내 MCP 서버 <code>paas-docs</code>입니다.
      </p>

      <Async state={storesState}>
        {() =>
          !active ? (
            <p className="mutedtext">열려 있는 저장소가 없습니다.</p>
          ) : (
            <>
              <div className="row" style={{ marginBottom: 12 }}>
                <select value={active.name} onChange={(e) => setSelected(e.target.value)}>
                  {stores.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}
                      {s.read_only ? ' (읽기 전용)' : ''}
                    </option>
                  ))}
                </select>
              </div>

              <p className="mono mutedtext" style={{ fontSize: 12, marginBottom: 12 }}>
                {active.root}
                {!active.exists && ' — 이 경로에 디렉터리가 없습니다'}
              </p>

              {error && <p className="error">{error}</p>}

              <Async state={listing}>
                {(data) =>
                  data && (
                    <>
                      {data.files.length === 0 ? (
                        <p className="mutedtext">저장된 파일이 없습니다.</p>
                      ) : (
                        <table style={{ marginBottom: 16 }}>
                          <thead>
                            <tr>
                              <th>경로</th>
                              <th>크기</th>
                              <th />
                            </tr>
                          </thead>
                          <tbody>
                            {data.files.map((f) => (
                              <tr key={f.path}>
                                <td className="mono">{f.path}</td>
                                <td>{humanSize(f.size)}</td>
                                <td>
                                  <button className="secondary small" onClick={() => download(f.path)}>
                                    다운로드
                                  </button>{' '}
                                  {!active.read_only && (
                                    <button className="secondary small" onClick={() => remove(f.path)}>
                                      삭제
                                    </button>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </>
                  )
                }
              </Async>

              {active.read_only ? (
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  사내 문서 폴더는 읽으러 붙인 것이라 업로드·삭제가 열려 있지 않습니다.
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
