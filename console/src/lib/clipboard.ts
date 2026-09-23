// 클립보드 복사 — **평문 http에서도 되게** 한다.
//
// `navigator.clipboard`는 보안 컨텍스트(https·localhost)에서만 있다. 사내는 평문 http로
// 접속하므로 그것만 쓰면 복사가 조용히 안 되고, 화면은 "복사됨"이라고 말한다(실제로 그랬다 —
// components/GitBrowseButton.tsx에도 같은 기록이 있다). 그래서 두 가지를 순서대로 시도하고
// **성공 여부를 돌려준다** — 호출측이 거짓말하지 않게 하는 것이 이 함수의 요점이다.

/** 복사에 성공하면 true. 두 방법 다 안 되면 false(호출측이 "직접 복사하세요"를 말해야 한다). */
export async function copyText(text: string): Promise<boolean> {
  // 1. 표준 경로 — https·localhost에서만 존재한다.
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 권한 거부·포커스 없음 등 — 아래 폴백으로 넘어간다.
    }
  }
  // 2. 폴백 — 화면 밖 textarea를 선택해 execCommand로 복사한다. 낡은 API지만 평문 http에서
  //    동작하는 유일한 길이다. 실패해도 예외를 던지지 않으므로 반환값으로 판정한다.
  try {
    const area = document.createElement('textarea');
    area.value = text;
    // 화면에 보이지 않게 두되 선택은 가능해야 한다 — display:none이면 선택이 안 된다.
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    area.setAttribute('readonly', 'true');
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, text.length);  // iOS Safari는 select()만으로는 범위가 안 잡힌다
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
