import { parseUnifiedDiff } from '../lib/diff';

export default function DiffView({ diff }: { diff: string }) {
  const lines = parseUnifiedDiff(diff);
  return (
    <div className="diffview">
      {lines.map((line, i) => (
        <div key={i} className={`line ${line.kind}`}>
          {line.text || ' '}
        </div>
      ))}
    </div>
  );
}
