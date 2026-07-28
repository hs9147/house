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
  const modules = useApi(() => api.listModules());
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);

  const stores = (modules.data ?? []).filter((m) => m.type === 'file_storage');
  const active = selected || stores[0]?.name || '';
  const listing = useApi(
    () => (active ? api.listStorageFiles(active) : Promise.resolve(null)),
    [active],
  );

  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !active) return;
    setBusy(true);
    setError('');
    try {
      await api.uploadStorageFile(active, file, path.trim() || undefined);
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
    setError('');
    try {
      const blob = await api.downloadStorageFile(active, target);
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
    if (!confirm(`${target} 파일을 삭제할까요? 되돌릴 수 없습니다.`)) return;
    setError('');
    try {
      await api.deleteStorageFile(active, target);
      listing.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>파일 관리</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        file_storage 모듈의 저장소입니다. 실제 디렉터리는 플랫폼 내부 사정이고, 바인딩된 앱과
        이 화면 모두 아래 창구 URL로만 접근합니다.
      </p>

      <Async state={modules} empty="모듈이 없습니다.">
        {() =>
          stores.length === 0 ? (
            <p className="mutedtext">file_storage 타입 모듈이 없습니다. 모듈 화면에서 먼저 등록하세요.</p>
          ) : (
            <>
              <div className="row" style={{ marginBottom: 12 }}>
                <select value={active} onChange={(e) => setSelected(e.target.value)}>
                  {stores.map((m) => (
                    <option key={m.id} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>

              {error && <p className="error">{error}</p>}

              <Async state={listing}>
                {(data) =>
                  data && (
                    <>
                      <p className="mono" style={{ fontSize: 12, marginBottom: 12 }}>
                        {data.url}
                      </p>
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
                                  <button className="secondary small" onClick={() => remove(f.path)}>
                                    삭제
                                  </button>
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
            </>
          )
        }
      </Async>
    </div>
  );
}
