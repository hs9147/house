import { useRef, useState } from 'react';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';

export default function Storage() {
  const storesState = useApi(() => api.listStorageStores());
  const [selected, setSelected] = useState('');
  const [path, setPath] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState('');
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);

  const stores = storesState.data ?? [];
  const active = stores.find((s) => s.name === selected) ?? stores[0];

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
                    // 읽기 전용만 표시하면 나머지가 무엇인지는 없는 표시로 읽어야 한다 —
                    // 둘 다 적어 상태를 눈으로 바로 알 수 있게 한다.
                    <option key={s.name} value={s.name}>
                      {s.name} {s.read_only ? '(읽기 전용)' : '(읽기/쓰기)'}
                    </option>
                  ))}
                </select>
              </div>

              <p className="mono mutedtext" style={{ fontSize: 12, marginBottom: 12 }}>
                {active.root}
                {!active.exists && ' — 이 경로에 디렉터리가 없습니다'}
              </p>

              {error && <p className="error">{error}</p>}
              {done && <p className="mutedtext" style={{ fontSize: 12 }}>{done}</p>}

              {active.read_only ? (
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  사내 문서 폴더는 읽으러 붙인 것이라 업로드가 열려 있지 않습니다.
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
