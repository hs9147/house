import { useState } from 'react';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { ProjectSharedDb } from '../lib/types';
import Async from './Async';

const PROFILE: Record<string, string> = { release: '운영', development: '개발' };

/**
 * 공용 DB — 같은 서버의 PostgreSQL에 플랫폼이 만들어 준 이 앱의 DB(services/shareddb).
 *
 * 비밀번호는 보이지 않는다 — 앱은 배포 때 환경변수로 받는다. 공용 자료 DB는 모든 앱이 읽기만
 * 하고, 그 자료를 채울 앱에만 관리자가 쓰기를 준다(운영 계정에만 — 개발 중인 코드가 모든 앱이
 * 읽는 기준 정보를 바꾸지 않게).
 */
export default function SharedDbPanel({ projectId }: { projectId: number }) {
  const state = useApi(() => api.projectSharedDb(projectId), [projectId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (state.data && !state.data.configured) return null;  // 공용 DB를 쓰지 않는 설치본

  const toggle = async (on: boolean) => {
    const verb = on ? '쓰기를 줍니다' : '다시 읽기만 하게 합니다';
    if (!window.confirm(`이 앱의 운영 계정에 공용 자료 DB ${verb}. 모든 앱이 읽는 자료입니다 — 진행할까요?`)) return;
    setBusy(true);
    setError('');
    try {
      await api.setCommonWrite(projectId, on);
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>공용 DB</h2>
      <Async state={state}>
        {(s: ProjectSharedDb) => (!s.bound ? (
          <p className="mutedtext">
            「공용 DB」(shared-db)를 바인딩하지 않았습니다 — 에이전트 기획의 솔루션 구성에서 바인딩하면,
            배포할 때 이 앱 전용 DB가 운영·개발 따로 만들어집니다.
          </p>
        ) : (
          <>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
              앱은 <span className="mono">{s.env_key}</span>로 접속 정보를 받습니다. 공용 자료는 같은 계정으로
              DB 이름만 <span className="mono">{s.common_db}</span>로 바꿔 읽습니다.
            </p>
            {s.databases.length === 0 ? (
              <p className="mutedtext">아직 만들어진 DB가 없습니다 — 배포하면 만들어집니다.</p>
            ) : (
              <table>
                <thead><tr><th>프로필</th><th>DB</th><th>공용 자료</th><th /></tr></thead>
                <tbody>
                  {s.databases.map((d) => (
                    <tr key={d.db_name}>
                      <td>{PROFILE[d.profile] ?? d.profile}</td>
                      <td className="mono">{d.db_name}</td>
                      <td>{d.common_write ? '읽기·쓰기' : '읽기만'}</td>
                      <td>
                        {isAdmin() && d.profile === 'release' && (
                          <button className="small secondary" disabled={busy}
                            onClick={() => void toggle(!d.common_write)}>
                            {d.common_write ? '쓰기 거두기' : '쓰기 주기'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        ))}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
