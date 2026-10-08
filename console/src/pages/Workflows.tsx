import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Async from '../components/Async';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { OrgOut, WorkflowConstraintOut, WorkflowOut } from '../lib/types';

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
  const [rule, setRule] = useState('');
  const orgs = useApi(() => api.listOrgs(), []);
  const list = useApi(() => api.listWorkflows(), []);
  // 조직을 골랐을 때만 그 조직의 업무 제약을 읽는다.
  const rules = useApi(
    () => (org === '' ? Promise.resolve([]) : api.listWorkflowConstraints(Number(org))),
    [org]);

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

  const addRule = async () => {
    if (org === '' || !rule.trim()) return;
    setError('');
    try {
      await api.addWorkflowConstraint(Number(org), rule.trim());
      setRule('');
      rules.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removeRule = async (id: number) => {
    setError('');
    try {
      await api.deleteWorkflowConstraint(id);
      rules.reload();
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
          {(rows: OrgOut[]) => (
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

      {org !== '' && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>업무 제약사항 (이 조직의 워크플로 전부에 적용)</h3>
          <p className="mutedtext" style={{ fontSize: 12 }}>
            여기 적는 것은 <b>업무 규칙</b>입니다 — 선급금 한도, 평가 순서, 결재선 같은 것.
            구성 대화와 평가 프롬프트에 그대로 실려 LLM을 묶습니다(하네싱). 에이전트 기획의
            공통 제약사항과는 <b>다른 목록</b>입니다: 그쪽은 에이전트를 <b>개발</b>할 때의
            제한(프록시 구조·외부 솔루션 금지)이고, 워크플로가 지킬 규칙이 아닙니다.
          </p>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <input
              placeholder="예: 선급금 30% 초과는 법무팀 합의 또는 임원 승인이 필요하다"
              value={rule}
              style={{ flex: '1 1 420px' }}
              onChange={(e) => setRule(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addRule(); }}
            />
            <button className="small" disabled={!rule.trim()} onClick={addRule}>추가</button>
          </div>
          <Async state={rules} empty="등록된 업무 제약사항이 없습니다.">
            {(rows: WorkflowConstraintOut[]) => (
              <ul style={{ fontSize: 13, margin: '8px 0 0', paddingLeft: 18 }}>
                {rows.map((r) => (
                  <li key={r.id} style={{ marginBottom: 4 }}>
                    {r.text}
                    <button className="small secondary" style={{ marginLeft: 8 }}
                            onClick={() => removeRule(r.id)}>
                      삭제
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </div>
      )}

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
