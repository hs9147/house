/**
 * "자동배포" 표시 — 그 프로필의 자동배포가 켜져 있으면(Project.auto_deploy_*), 기본 브랜치에 push·merge가
 * 들어올 때 git 웹훅이 그 프로필을 다시 배포한다(app/api/webhooks.py의 git_push). 예전에는 「떠 있으면 ✓」라
 * 웹훅이 실제로 배포하지 않는 프로필에도 ✓가 떴다.
 *
 * 문구를 여기 한 곳에 둔 이유: 개요·서버구성 두 화면에 같은 표시가 나가는데, 설명이
 * 갈라지면 어느 쪽이 실제 동작인지 알 수 없게 된다.
 */
export function autoDeployTitle(branch?: string): string {
  return `${branch ? `${branch} 브랜치` : '기본 브랜치'}에 push·merge가 들어오면 자동으로 다시 배포됩니다`;
}

export default function AutoDeployMark({ enabled, branch }: { enabled?: boolean; branch?: string }) {
  if (!enabled) return null;
  return (
    <span title={autoDeployTitle(branch)} aria-label="자동배포 켜짐">
      ✓
    </span>
  );
}
