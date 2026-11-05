import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { DataDatabaseRow, OrgDatabasesRow } from '../lib/types';
import Async from './Async';

/**
 * 부서별 DB — 부서마다 쓰는 자료 DB를 여럿 고른다(관리자, services/shareddb). 앞의 것이 그 부서의 기본이다.
 *
 * **같은 DB를 고른 부서의 과제끼리 서로의 운영 표를 읽는다.** 한 부서만 고른 DB에는 그 부서만 접속한다. 과제는
 * 부서의 DB 중 하나에 자기 스키마를 두는데, 어느 DB인지는 기획 LLM이 DB의 용도 설명을 보고 고른다. 용도
 * 설명은 사람이 적지 않는다 — LLM이 실제 쓰임(표·표 설명·쓰는 과제)을 보고 적고 고친다. 자기 과제의 자료가
 * 있는 DB는 뺄 수 없다(서버가 거부한다).
 */
export default function SharedDbPolicyPanel() {
  const state = useApi(() => api.sharedDbDatabases());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [adding, setAdding] = useState<Record<number, string>>({});
  if (state.data && !state.data.configured) return null;  // 공용 DB를 쓰지 않는 설치본

  // 자료(과제 스키마·표)가 있는 DB는 강제 삭제다 — 되돌릴 수 없어 DB 이름을 직접 입력받는다
  const forceOk = (d: DataDatabaseRow, why: string) => window.prompt(
    `DB '${d.name}'을(를) 스키마·표까지 모두 지웁니다(강제 삭제).\n${why}\n\n붙어 있는 앱의 접속을 끊고 지우며 되돌릴 수`
    + ' 없습니다. 그 과제는 다시 배포하면 부서의 기본 DB에 빈 스키마를 받습니다.\n\n지우려면 DB 이름을 입력하세요.') === d.name;
  const remove = async (d: DataDatabaseRow) => {
    const users = d.organizations.length ? `쓰는 부서: ${d.organizations.join(', ')}` : '쓰는 부서 없음';
    let force = d.schemas > 0;
    if (force ? !forceOk(d, `과제 스키마 ${d.schemas}개 · ${users}`)
      : !window.confirm(`DB '${d.name}'을(를) 지웁니다.\n${users}\n\n서버에서 DB를 삭제하고 쓰던 부서에서도 뺍니다.`
        + ' 되돌릴 수 없습니다.')) return;
    setBusy(true);
    setError('');
    try {
      try {
        await api.deleteDatabase(d.name, force);
      } catch (e) {
        // 플랫폼 밖에서 만든 표가 있었다 — 강제로 지울지 다시 묻는다
        const msg = (e as Error).message;
        if (force || !msg.includes('강제 삭제') || !forceOk(d, msg)) throw e;
        force = true;
        await api.deleteDatabase(d.name, true);
      }
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async (row: OrgDatabasesRow, databases: string[]) => {
    setBusy(true);
    setError('');
    try {
      await api.setOrgDatabases(row.organization_id, databases);
      setAdding((a) => ({ ...a, [row.organization_id]: '' }));
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>부서별 DB</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        부서마다 쓰는 DB를 고릅니다(여럿, 앞의 것이 기본). 같은 DB를 고른 부서의 과제끼리 서로의 운영 표를 읽고, 한
        부서만 고른 DB에는 그 부서만 접속합니다. 과제가 어느 DB에 스키마를 둘지는 기획 LLM이 아래 용도 설명을 보고
        고릅니다 — 용도 설명은 LLM이 실제 쓰임을 보고 적고 고칩니다. 새 이름을 더하면 서버에 DB를 만듭니다.
      </p>
      <Async state={state}>
        {(s) => {
          const names = s.databases.map((d) => d.name);
          return (
            <>
              <table>
                <thead><tr><th>부서</th><th>쓰는 DB</th><th>스키마</th></tr></thead>
                <tbody>
                  {s.organizations.map((row) => {
                    const draft = adding[row.organization_id] ?? '';
                    return (
                      <tr key={row.organization_id}>
                        <td>{row.name}</td>
                        <td>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
                            {row.databases.map((d, i) => (
                              <span key={d} className="status info" title={i === 0 ? '이 부서의 기본 DB' : undefined}>
                                <span className="mono">{d}</span>{i === 0 ? ' (기본)' : ''}
                                {row.databases.length > 1 && (
                                  <button className="small secondary" style={{ marginLeft: 4, padding: '0 4px' }}
                                    disabled={busy} aria-label={`${d} 빼기`}
                                    onClick={() => void save(row, row.databases.filter((x) => x !== d))}>×</button>
                                )}
                              </span>
                            ))}
                            <input list="paas-db-names" value={draft} placeholder="DB 더하기" disabled={busy}
                              style={{ width: 130, fontSize: 12 }} aria-label="더할 DB"
                              onChange={(e) => setAdding((a) => ({ ...a, [row.organization_id]: e.target.value }))}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && draft.trim()) void save(row, [...row.databases, draft.trim()]);
                              }} />
                            <button className="small secondary" disabled={busy || !draft.trim()}
                              onClick={() => void save(row, [...row.databases, draft.trim()])}>더하기</button>
                          </div>
                        </td>
                        <td className="mono">{row.schemas}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <datalist id="paas-db-names">{names.map((n) => <option key={n} value={n} />)}</datalist>

              <h3 style={{ fontSize: 14, marginTop: 16 }}>DB 목록</h3>
              <table>
                <thead><tr><th>DB</th><th>용도 (LLM이 쓰임을 보고 적음)</th><th>쓰는 부서</th><th>스키마</th><th /></tr></thead>
                <tbody>
                  {s.databases.map((d) => (
                    <tr key={d.name}>
                      <td className="mono">{d.name}</td>
                      <td style={{ fontSize: 12 }}>
                        {d.description || <span className="mutedtext">(아직 없음 — 과제가 쓰기 시작하면 적습니다)</span>}
                        {d.described_at && (
                          <div className="mutedtext" style={{ fontSize: 11 }}>
                            {d.described_by === 'planning' ? '기획에서 고를 때' : '쓰임을 보고'} ·{' '}
                            {new Date(d.described_at).toLocaleString()}
                          </div>
                        )}
                      </td>
                      <td style={{ fontSize: 12 }}>{d.organizations.join(', ') || '—'}</td>
                      <td className="mono">{d.schemas}</td>
                      <td>
                        {/* 기본 DB는 지울 수 없다 — 버튼을 두지 않는다 */}
                        {d.name !== 'paas_data' && (
                          <button className="small secondary" disabled={busy}
                            title={d.schemas > 0 ? '스키마까지 지웁니다(강제 삭제 — DB 이름을 입력해 확인)' : 'DB를 지웁니다(확인 후)'}
                            onClick={() => void remove(d)}>{d.schemas > 0 ? '강제 삭제' : '삭제'}</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          );
        }}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
