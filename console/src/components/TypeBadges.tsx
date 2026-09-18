import StatusPill from './StatusPill';
import type { ProjectOut } from '../lib/types';

/**
 * 프로젝트 타입 표시 — 감지된 **배포 단위**를 배지로 나열한다.
 *
 * 타입 하나로는 백엔드+프론트엔드처럼 서로 다른 Dockerfile 템플릿·다른 내부 포트로
 * 빌드돼야 하는 구성을 표현할 수 없다(app/services/structure.py). `composite` 하나만
 * 찍으면 무엇과 무엇으로 이루어졌는지 화면에서 알 수 없어서, 컴포넌트별로 보여준다.
 *
 * 구조가 아직 감지되지 않은 프로젝트(structure=null)는 예전처럼 타입 하나만 찍는다 —
 * 기존 프로젝트는 다음 배포나 '구조 다시 감지'에서 채워진다.
 */
export default function TypeBadges({ project }: { project: ProjectOut }) {
  const components = project.structure?.components ?? [];
  if (components.length === 0) return <StatusPill value={project.type} />;

  return (
    <span className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
      {components.map((c) => (
        <span key={c.path} className="row" style={{ gap: 3, alignItems: 'center' }}>
          {/* 컴포넌트가 하나뿐이면 이름은 군더더기다(경로가 리포 루트라 "app"으로만 찍힌다) */}
          {components.length > 1 && (
            <span className="mutedtext" style={{ fontSize: 10 }} title={c.path}>{c.name}</span>
          )}
          {/* 판정 불가는 추측해 채우지 않는다 — 그래야 $link·시그니처를 손볼 자리가 보인다 */}
          <StatusPill value={c.type ?? '판정불가'} />
        </span>
      ))}
    </span>
  );
}
