export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString('ko-KR');
}

/** 서버 시각 → Date. 시간대 표시가 없으면 UTC로 읽는다 — 서버는 UTC로 저장하고, DB에 따라(SQLite) 시간대 없이
 *  돌아온다. 그대로 new Date()에 넣으면 브라우저 시간대로 읽혀 9시간 어긋난다. */
export function serverTime(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso.replace(' ', 'T')}Z`);
}

/** KST(Asia/Seoul) 시각 — 「11-13 16:40:05 KST」. 브라우저 시간대와 상관없이 늘 KST로 보인다. */
export function fmtKst(at: Date | number): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    second: '2-digit', hourCycle: 'h23',
  }).formatToParts(typeof at === 'number' ? new Date(at) : at);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')} KST`;
}

/** 걸린 시간 — 「42초」「3분 05초」「1시간 02분」. */
export function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}분 ${String(s % 60).padStart(2, '0')}초`;
  return `${Math.floor(m / 60)}시간 ${String(m % 60).padStart(2, '0')}분`;
}

export function fmtBytes(n: number | undefined): string {
  if (n === undefined) return '-';
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GiB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(0)} MiB`;
  return `${n} B`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 8);
}
