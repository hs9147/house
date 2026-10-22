import { useOutletContext } from 'react-router-dom';
import C4Diagram from '../../components/C4Diagram';
import type { ProjectContext } from '../ProjectDetail';

// 에이전트 기획 화면(pages/AgentPlanning의 STAGES)과 같은 단계 표기 — 그림이 어느 단계 문서에서 왔는지 밝힌다.
const STAGE_LABELS: Record<string, string> = {
  spec: '① 기획서', architecture: '② 아키텍처 설계', solution: '③ 솔루션 구성', principles: '④ 개발원칙',
  tasks: '⑤ 작업 지시',
};

/**
 * 모델 — 에이전트 기획의 「단계별 시각화(C4 모델)」를 그대로 프로젝트 화면에서 본다. 기획 세션 없이 기본
 * 브랜치의 기획 산출물(docs/agent-planning/*.md)로 그린다 — 단계 확정은 매번 기본 브랜치로 반영되므로 그것이
 * 이 프로젝트의 최신 확정 모델이다. Code 레벨은 리포의 코드 구조를 그린다.
 */
export default function ModelTab() {
  const { project } = useOutletContext<ProjectContext>();
  return (
    <div className="panel">
      <h3 style={{ margin: 0 }}>🗺️ 단계별 시각화 (C4 모델)</h3>
      <p className="mutedtext" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        기본 브랜치(<span className="mono">{project.branch}</span>)의 기획 산출물로 그립니다 — 에이전트 기획에서 단계를
        확정하면 여기에 반영됩니다.
      </p>
      <C4Diagram key={project.id} projectId={project.id} reloadKey="" stage="" draft=""
                 stageLabels={STAGE_LABELS} />
    </div>
  );
}
