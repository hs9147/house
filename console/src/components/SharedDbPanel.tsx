import { useState } from 'react';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { ProjectOut, ProjectSharedDb, SharedDbTable } from '../lib/types';
import Async from './Async';

const PROFILE: Record<string, string> = { release: '운영', development: '개발' };
const PRIVILEGE: Record<string, string> = { read: '읽기', write: '읽기·쓰기' };

/**
 * 공용 DB — 자료 DB(paas_data)에서 이 과제의 스키마와, 읽을 수 있는 표·준 권한(services/shareddb).
 *
 * 같은 공유 단위(기본은 자기 부서 — 조직 관리에서 관리자가 넓힌다)의 과제는 서로의 운영 표를 기본으로
 * 읽는다. 단위 밖의 과제는 그 스키마에 들어가지도 못하고, **관리자가 표 단위로**
 * 준다(읽기/쓰기, 만료일) — 플랫폼이 그 기록대로 DB 권한을 맞추고, 만료되면 거둔다. 비밀번호는
 * 보이지 않는다(앱은 배포 때 환경변수로 받는다).
 */
export default function SharedDbPanel({ projectId }: { projectId: number }) {
  const state = useApi(() => api.projectSharedDb(projectId), [projectId]);
  const admin = isAdmin();
  const projects = useApi(() => (admin ? api.listProjects() : Promise.resolve([] as ProjectOut[])), [admin]);
  const [granting, setGranting] = useState<SharedDbTable | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (state.data && !state.data.configured) return null;  // 공용 DB를 쓰지 않는 설치본

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      setGranting(null);
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
            배포할 때 이 과제의 스키마가 운영·개발 따로 만들어집니다.
          </p>
        ) : (
          <>
            <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
              앱은 <span className="mono">{s.env_key}</span>로 접속 정보를 받고, 표 이름만 쓰면 자기 스키마에
              만들어집니다.{' '}
              {s.schemas.length > 0
                ? s.schemas.map((x) => `${PROFILE[x.profile] ?? x.profile} ${x.schema}`).join(' · ')
                : '아직 만들어진 스키마가 없습니다 — 배포하면 만들어집니다.'}
            </p>
            {s.error && <p className="error">표를 읽지 못했습니다: {s.error}</p>}

            <h3 style={{ fontSize: 14 }}>이 과제의 표</h3>
            {s.tables.own.length === 0 ? (
              <p className="mutedtext">아직 표가 없습니다 — 앱의 마이그레이션이 만들면 보입니다.</p>
            ) : (
              <table>
                <tbody>
                  {s.tables.own.map((t) => (
                    <tr key={t.table}>
                      <td className="mono">{t.schema}.{t.table}</td>
                      <td className="mutedtext" style={{ fontSize: 12 }}>{t.comment || '(설명 없음)'}</td>
                      <td>
                        {admin && (
                          <button className="small secondary" disabled={busy}
                            onClick={() => setGranting(t)}>다른 과제에 주기</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {granting && (
              <GrantForm table={granting} projects={(projects.data ?? []).filter((p) => p.id !== projectId)}
                busy={busy} onCancel={() => setGranting(null)}
                onSubmit={(body) => void act(() => api.createDataGrant(body))} />
            )}

            {s.given.length > 0 && (
              <>
                <h3 style={{ fontSize: 14 }}>다른 과제에 준 권한</h3>
                <table>
                  <tbody>
                    {s.given.map((g) => (
                      <tr key={g.id}>
                        <td className="mono">{g.schema}.{g.table}</td>
                        <td>{g.project_name}</td>
                        <td>{PRIVILEGE[g.privilege] ?? g.privilege}</td>
                        <td className="mutedtext" style={{ fontSize: 12 }}>
                          {g.expires_at ? `${new Date(g.expires_at).toLocaleDateString()}까지` : '만료 없음'}
                        </td>
                        <td>
                          {admin && (
                            <button className="small secondary" disabled={busy} onClick={() => {
                              if (window.confirm(`${g.project_name}의 ${g.schema}.${g.table} 권한을 거둘까요?`)) {
                                void act(() => api.deleteDataGrant(g.id));
                              }
                            }}>회수</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {(s.tables.unit.length > 0 || s.tables.granted.length > 0) && (
              <>
                <h3 style={{ fontSize: 14 }}>읽을 수 있는 다른 표</h3>
                <table>
                  <tbody>
                    {[...s.tables.unit.map((t) => ({ ...t, how: '같은 공유 단위(읽기)' })),
                      ...s.tables.granted.map((t) => ({ ...t, how: `권한 받음(${PRIVILEGE[t.privilege ?? 'read']})` }))]
                      .map((t) => (
                        <tr key={`${t.schema}.${t.table}`}>
                          <td className="mono">{t.schema}.{t.table}</td>
                          <td>{t.project_name}</td>
                          <td>{t.how}</td>
                          <td className="mutedtext" style={{ fontSize: 12 }}>{t.comment}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        ))}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function GrantForm({ table, projects, busy, onCancel, onSubmit }: {
  table: SharedDbTable;
  projects: ProjectOut[];
  busy: boolean;
  onCancel: () => void;
  onSubmit: (body: { schema_name: string; table: string; project_id: number; privilege: string;
    expires_at: string | null }) => void;
}) {
  const [projectId, setProjectId] = useState<number | ''>('');
  const [privilege, setPrivilege] = useState('read');
  const [until, setUntil] = useState('');
  return (
    <div className="row" style={{ gap: 8, flexWrap: 'wrap', margin: '8px 0', fontSize: 13 }}>
      <span className="mono">{table.schema}.{table.table}</span> →
      <select value={projectId} onChange={(e) => setProjectId(e.target.value ? Number(e.target.value) : '')}>
        <option value="">과제 선택</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
      <select value={privilege} onChange={(e) => setPrivilege(e.target.value)}>
        <option value="read">읽기</option>
        <option value="write">읽기·쓰기</option>
      </select>
      <label>만료 <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} /></label>
      <button className="small" disabled={busy || projectId === ''} onClick={() => projectId !== '' && onSubmit({
        schema_name: table.schema, table: table.table, project_id: projectId, privilege,
        // 만료일 하루가 끝날 때까지 — 그날 자정(현지)에 거둔다.
        expires_at: until ? new Date(`${until}T23:59:59`).toISOString() : null,
      })}>주기</button>
      <button className="small secondary" onClick={onCancel}>취소</button>
      <span className="mutedtext" style={{ fontSize: 11 }}>
        같은 공유 단위 과제는 이미 읽습니다. 쓰기는 운영 배포에만 적용되고, 개발 배포는 읽기만 합니다.
      </span>
    </div>
  );
}
