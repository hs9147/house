/**
 * "자동배포" 표시 — 기본 브랜치에 push·merge가 들어오면 git 웹훅이 재배포를 건다
 * (app/api/webhooks.py의 git_push). 실행 중인 배포본에만 의미가 있어 running일 때만 켠다.
 *
 * 문구를 여기 한 곳에 둔 이유: 개요·서버구성 두 화면에 같은 표시가 나가는데, 설명이
 * 갈라지면 어느 쪽이 실제 동작인지 알 수 없게 된다.
 */
export default function AutoDeployMark({ status, branch }: { status: string; branch?: string }) {
  if (status !== 'running') return null;
  const target = branch ? `${branch} 브랜치` : '기본 브랜치';
  return (
    <span title={`${target}에 push·merge가 들어오면 자동으로 다시 배포됩니다`} aria-label="자동배포 켜짐">
      ✓
    </span>
  );
}
