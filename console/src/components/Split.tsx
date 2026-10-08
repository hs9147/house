import type { ReactNode } from 'react';

/**
 * 좌우 2단 틀 — **왼쪽은 사람이 읽고 쓰는 것, 오른쪽은 기계가 낸 것.**
 *
 * 콘솔 전체에 쓰는 규약이다.
 *   왼쪽(고정 폭): 대화·설명·입력 폼·검색 — 사람이 말을 거는 자리. 세로로 길어지면 이 칸
 *                  안에서만 스크롤해서, 오른쪽을 훑는 동안 입력창이 화면에서 사라지지 않는다.
 *   오른쪽(남은 폭): 표·그림·진행 기록 — 사람이 쓴 것에 대한 **답**. 넓은 쪽이 받아야 한다
 *                  (29단계 흐름도를 반 폭에 그리면 글자를 읽을 수 없다).
 *
 * **모든 화면에 쓰지 않는다.** 두 조건을 **함께** 만족할 때만 쓴다.
 *   1. 사람이 말을 거는 자리와 그 답이 **둘 다** 있다.
 *   2. 내용이 **한 화면을 넘는다.**
 * 한 화면에 들어가는 화면을 2단으로 가르면 양쪽이 다 좁아지고 빈 칸이 생긴다 — 그런 화면은
 * 지금처럼 가운데 한 단으로 둔다(감사 로그·계정·조직·배포 이력·파일 관리처럼 표 하나로
 * 끝나는 화면이 전부 여기 속한다).
 *
 * 좁은 화면(1180px 이하)에서는 한 단으로 쌓인다 — 왼쪽이 먼저다(할 일이 먼저 보여야 한다).
 * 접으면 오른쪽이 전체 폭을 쓴다(그림을 크게 보려고 접는다).
 */
export default function Split({
  left, right, collapsed = false, onToggle, leftLabel = '입력', leftWidth = 420,
}: {
  left: ReactNode;
  right: ReactNode;
  collapsed?: boolean;
  /** 접기를 쓸 화면만 넘긴다 — 없으면 접기 버튼이 나오지 않는다. */
  onToggle?: (collapsed: boolean) => void;
  leftLabel?: string;
  leftWidth?: number;
}) {
  if (collapsed) {
    return (
      <>
        {onToggle && (
          <div className="row" style={{ marginBottom: 8 }}>
            <button className="small secondary" onClick={() => onToggle(false)}>
              ← {leftLabel} 펼치기
            </button>
          </div>
        )}
        {right}
      </>
    );
  }
  return (
    <div className="split" style={{ ['--split-left' as string]: `${leftWidth}px` }}>
      <div className="split-left">
        {onToggle && (
          <div className="row" style={{ justifyContent: 'flex-end', marginBottom: 6 }}>
            <button className="small secondary" onClick={() => onToggle(true)}>
              {leftLabel} 접기 →
            </button>
          </div>
        )}
        {left}
      </div>
      <div className="split-right">{right}</div>
    </div>
  );
}
