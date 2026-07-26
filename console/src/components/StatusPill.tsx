const CLASS_MAP: Record<string, string> = {
  running: 'ok',
  sent: 'ok',
  applied: 'ok',
  building: 'warn',
  progressing: 'warn',
  pending: 'warn',
  proposed: 'info',
  development: 'info',
  release: 'ok',
  failed: 'bad',
  high: 'bad',
  medium: 'warn',
  low: 'info',
  stopped: 'dim',
  expired: 'dim',
  rejected: 'dim',
};

export default function StatusPill({ value }: { value: string }) {
  const base = value.split(' ')[0]; // "progressing (1/2)" 같은 형태 지원
  const cls = CLASS_MAP[base] ?? 'dim';
  return <span className={`status ${cls}`}>{value}</span>;
}
