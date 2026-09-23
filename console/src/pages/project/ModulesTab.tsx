import { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import Async from '../../components/Async';
import StatusPill from '../../components/StatusPill';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { ModuleSummary } from '../../lib/types';
import type { ProjectContext } from '../ProjectDetail';

/**
 * 바인딩된 모듈 — **조회와 해제**만 한다.
 *
 * 수작업 바인딩 폼이 있었는데, 감사 기록을 보면 쓰이지 않았다: 바인딩 34건 중 32건이 기획
 * "솔루션 구성" 단계의 도구(`via: plan.solution`)가 만든 것이고, 손으로 만든 2건은 같은 날
 * 해제됐다(실험 한 번). 모듈을 고르고 env 접두사를 사람이 정하는 일은 그 단계에서 문서와 함께
 * 결정되는 것이고, 여기서 다시 하게 두면 두 곳이 서로 다른 구성을 만든다.
 *
 * **해제는 남긴다.** 기획 단계에는 해제 도구가 없어서(바인딩만 한다), 접두사를 잘못 정한
 * 바인딩을 되돌릴 길이 여기밖에 없다 — 그걸 없애면 잘못 주입되는 환경변수를 고칠 방법이
 * 사라진다. 실제로 그 2건을 되돌린 것도 이 화면이다.
 */
export default function ModulesTab() {
  const { project } = useOutletContext<ProjectContext>();
  const bound = useApi(() => api.projectModules(project.id), [project.id]);
  const [error, setError] = useState('');

  const unbind = async (m: ModuleSummary) => {
    if (!window.confirm(`'${m.agent_name}' 바인딩(${m.env_prefix})을 해제하시겠습니까?\n다음 배포부터 이 환경변수가 주입되지 않습니다.`)) return;
    setError('');
    try {
      await api.unbindModule(project.id, m.binding_id);
      bound.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="panel">
      <h2>바인딩된 모듈</h2>
      <p className="mutedtext" style={{ fontSize: 12 }}>
        바인딩은 에이전트 기획의 <b>솔루션 구성</b> 단계에서 문서와 함께 결정됩니다 — 여기서는
        무엇이 붙어 있는지 확인하고, 잘못된 것을 해제합니다. 바인딩된 모듈은 다음 배포부터
        규약된 환경변수로 주입되고, 이 목록은 가용 모듈 제약(외부 빌드 guardrail)에도 그대로
        쓰입니다.
      </p>
      <Async state={bound} empty="바인딩된 모듈이 없습니다 — 기획의 '솔루션 구성' 단계에서 붙습니다.">
        {(rows) => (
          <table>
            <thead>
              <tr>
                <th>모듈</th>
                <th>타입</th>
                <th>env 접두사</th>
                <th>능력</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.binding_id}>
                  <td>{m.agent_name}</td>
                  <td><StatusPill value={m.type} /></td>
                  <td className="mono">{m.env_prefix}</td>
                  <td className="mono">{m.skills.join(', ')}</td>
                  <td>
                    <button className="small danger" onClick={() => unbind(m)}>해제</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
