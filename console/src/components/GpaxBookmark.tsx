import { useEffect, useRef } from 'react';
import { agentCode } from '../lib/agentCode';

// 통합 북마크 「GPAX」 — 데이터 수집과 스마트워크가 같은 것을 보인다(lib/agentCode.ts).
// React는 javascript: href를 경고한다 — DOM에 직접 넣는다.
export default function GpaxBookmark() {
  const link = useRef<HTMLAnchorElement>(null);
  useEffect(() => { link.current?.setAttribute('href', `javascript:${encodeURIComponent(agentCode())}`); }, []);
  return (
    <a ref={link} className="mono" onClick={(e) => e.preventDefault()} title="북마크바로 끌어다 놓으세요"
       style={{ padding: '2px 6px', border: '1px solid var(--border)', borderRadius: 4, cursor: 'grab' }}>
      GPAX
    </a>
  );
}
