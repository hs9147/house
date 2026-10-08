import { useState, type ReactNode } from 'react';

/**
 * 같은 정보를 **그림과 표로 함께** 둘 때 쓰는 탭.
 *
 * 둘을 나란히 쌓아 두면 화면이 두 배로 길어지고, 사람은 둘이 같은 값인지 확인하느라 스크롤을
 * 왕복한다. 그런데 어느 하나를 버릴 수도 없다 — 그림은 모양을 보여 주고(어디서 떨어졌나,
 * 무엇이 무엇과 이어지나), 표는 값을 준다(정확히 몇 건인가, 복사해 쓸 수 있나). 접근성
 * 지침이 차트에 표 대안을 함께 두라고 하는 이유도 그것이다.
 *
 * 그래서 **같은 자리에서 바꿔 본다.** 기본은 그림이다(모양이 먼저 읽힌다). 표는 한 번
 * 누르면 나오고, 그 선택은 그 패널에만 남는다 — 화면 전체의 모드가 되면 다른 패널의
 * 그림까지 사라진다.
 */
export default function Tabs({ tabs, initial = 0 }: {
  tabs: { key: string; label: string; content: ReactNode }[];
  initial?: number;
}) {
  const [at, setAt] = useState(initial);
  const current = tabs[Math.min(at, tabs.length - 1)];
  return (
    <>
      <div className="row viewtabs" role="tablist">
        {tabs.map((t, i) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={i === at}
            className={i === at ? 'small' : 'small secondary'}
            onClick={() => setAt(i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel">{current?.content}</div>
    </>
  );
}
