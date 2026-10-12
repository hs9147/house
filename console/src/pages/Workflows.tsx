import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import { api } from '../lib/api';
import { isAdmin } from '../lib/auth';
import { useApi } from '../lib/hooks';
import type { WorkflowOut } from '../lib/types';

/**
 * 워크플로 관리 — **조직 단위** 목록. 한 조직이 여러 개를 갖는다.
 *
 * 만들 때는 이름만 받는다(스펙은 빈 상태). 구성은 상세 화면의 대화로 하고, 여기서 템플릿을
 * 심어 주면 사람이 그것을 읽지 않고 지나간다 — 실행되는 것이 무엇인지 모르는 워크플로가
 * 가장 위험하다.
 */
export default function Workflows() {
  const navigate = useNavigate();
  const [org, setOrg] = useState<number | ''>('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  // 관리자는 모든 조직, 사용자는 소속 조직에만 만든다 — 서버도 같은 기준으로 막는다.
  const orgs = useApi<{ id: number; name: string }[]>(
    () => (isAdmin() ? api.listOrgs() : api.smartworkOrgs()), []);
  const list = useApi(() => api.listWorkflows(), []);

  const create = async () => {
    if (org === '' || !name.trim()) return;
    setError('');
    try {
      const made = await api.createWorkflow(Number(org), name.trim());
      setName('');
      navigate(`/workflows/${made.id}`);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (row: WorkflowOut) => {
    if (!window.confirm(`'${row.name}'을 지웁니까? 실행 이력도 함께 사라집니다.`)) return;
    setError('');
    try {
      await api.deleteWorkflow(row.id);
      list.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>워크플로 관리</h2>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          조직마다 여러 개를 둡니다. 단계는 플랫폼이 가진 자원(파일 저장소·문서 검색·모듈
          도구·LLM)과 <b>사람의 작업</b>을 엮은 것이고, 구성은 대화로 합니다 — 적은 것은
          검증을 통과해야 저장되고, 저장된 것만 실행됩니다.
        </p>
        <Async state={orgs}>
          {(rows: { id: number; name: string }[]) => (
            <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <select value={org} onChange={(e) => setOrg(e.target.value === '' ? '' : Number(e.target.value))}>
                <option value="">조직 선택</option>
                {rows.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
              </select>
              <input placeholder="워크플로 이름 — 예: 계약 검토"
                     value={name} style={{ minWidth: 220 }}
                     onChange={(e) => setName(e.target.value)}
                     onKeyDown={(e) => { if (e.key === 'Enter') create(); }} />
              <button className="small" disabled={org === '' || !name.trim()} onClick={create}>
                만들기
              </button>
            </div>
          )}
        </Async>
        {error && <p className="error">{error}</p>}
      </div>

      <Async state={list}>
        {(rows: WorkflowOut[]) => (
          <div className="panel">
            {rows.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12, margin: 0 }}>
                아직 워크플로가 없습니다.
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>조직</th><th>이름</th><th>단계</th><th>사람 단계</th>
                    <th>판</th><th>수정</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td>{row.org_name}</td>
                      <td>
                        <a href={`#/workflows/${row.id}`}
                           onClick={(e) => { e.preventDefault(); navigate(`/workflows/${row.id}`); }}>
                          {row.name}
                        </a>
                        {row.description && (
                          <div className="mutedtext" style={{ fontSize: 11 }}>{row.description}</div>
                        )}
                      </td>
                      <td className="mono">{row.summary.node_count}</td>
                      <td className="mono">{row.summary.human_steps}</td>
                      <td className="mono">v{row.version}</td>
                      <td className="mutedtext" style={{ fontSize: 11 }}>
                        {new Date(row.updated_at).toLocaleString('ko-KR')}
                      </td>
                      <td>
                        <button className="small secondary" onClick={() => remove(row)}>삭제</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </Async>
    </>
  );
}
