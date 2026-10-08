import { useEffect, useRef, useState } from 'react';

/**
 * 차트 기본요소 — 의존성 없이 인라인 SVG로.
 *
 * **왜 라이브러리를 넣지 않는가.** 필요한 형태가 막대·스택막대·히트맵 셋이고(종류·관계
 * 분포는 범주 비교, 퍼널은 순서 있는 단계, 저장소×종류는 격자), 그 셋은 SVG 몇 줄이다.
 * 차트 라이브러리는 번들과 테마를 함께 들고 오고, 콘솔은 이미 자기 다크 테마가 있다.
 * 노드-링크가 필요한 자리(스키마 수준 그래프)는 이미 들어 있는 React Flow를 쓴다.
 *
 * **색은 역할로 고른다**(규칙은 dataviz 지침).
 *   - 순서 있는 단계(퍼널) → 한 색의 순서 램프. 검증 통과: `#cde2fb,#9ec5f4,#6da7ec,
 *     #3987e5,#1c5cab` (dark, surface #1a1b2a, --ordinal: 단조·간격·대비·단일색 전부 PASS).
 *   - 이름만 다른 범주(노드 종류·관계 종류·저장소) → **한 색**으로 전부 그린다. 길이가 이미
 *     크기를 말하므로 색으로 또 말하면 채널을 낭비하고 색맹 검사도 깨진다.
 *     슬롯1 blue `#3987e5` (dark 검증 PASS: 밝기대·채도·대비).
 *   - 상태(전환됨·노드 없음·추출 실패) → 상태 색. good `#0ca30c` · warning `#fab219` ·
 *     critical `#d03b3b`. 이 셋은 범주 팔레트의 밝기대 밖에 있고(검증기도 그렇게 말한다),
 *     그래서 **색만으로 뜻을 나르지 않는다** — 범례에 기호와 이름을 함께 둔다.
 *   - 크기 격자(히트맵) → 같은 blue 램프. 어두운 배경이라 **큰 값이 밝다**(0에 가까울수록
 *     배경에 가깝게 가라앉는다).
 */
export const VIZ = {
  surface: '#1a1b2a',      // --panel
  grid: '#2c2c3a',
  axis: '#3a3b4d',
  ink: '#e8e8f0',
  muted: 'rgba(255,255,255,0.45)',
  series: '#3987e5',       // 단일 시리즈(범주 비교)
  ordinal: ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#1c5cab'],
  status: { good: '#0ca30c', warning: '#fab219', critical: '#d03b3b' },
  // 히트맵 램프(0 → 최대). 어두운 표면에서 큰 값이 밝아지도록 램프를 뒤집어 쓴다.
  heat: ['#232438', '#1c5cab', '#2a78d6', '#3987e5', '#6da7ec', '#9ec5f4', '#cde2fb'],
} as const;

const BAR = 20;        // 막대 두께 상한(24px 이하)
const GAP = 2;         // 표면 간격 — 맞닿은 마크를 가르는 유일한 수단(테두리를 두르지 않는다)
const RADIUS = 4;      // 데이터 끝만 둥글게, 기준선 쪽은 각지게

/** 값 끝만 둥근 수평 막대 — 기준선(왼쪽)은 각지게 둔다. */
function barPath(x: number, y: number, w: number, h: number): string {
  if (w <= RADIUS) return `M${x},${y}h${w}v${h}h${-w}z`;
  const r = Math.min(RADIUS, h / 2);
  return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${h - 2 * r}`
    + `a${r},${r} 0 0 1 ${-r},${r}h${-(w - r)}z`;
}

/**
 * 그릴 **실제 픽셀 폭**을 잰다.
 *
 * 처음에는 `viewBox` + `width="100%"`로 그렸다. 그러면 SVG가 칸에 맞춰 늘어나는데 **글자도
 * 함께 늘어난다** — 같은 fontSize 12가 폭 400px에서 8.5px, 900px에서 19px로 보였다(전환
 * 현황에서 바로 드러났다). 좌표를 픽셀로 쓰고 폭을 직접 재면 글자는 늘 12px이다.
 *
 * 폭을 모르는 첫 렌더에는 기준값으로 그린다(0으로 그리면 한 프레임 깜빡인다).
 */
function useWidth(fallback: number): [React.RefObject<HTMLDivElement>, number] {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const target = box.current;
    if (!target) return;
    const observer = new ResizeObserver(() => {
      // 패널 여백 안쪽만 쓴다. 1px 미만 변화는 무시한다(소수점 떨림으로 다시 그리지 않게).
      const next = Math.max(240, Math.round(target.clientWidth));
      setWidth((prev) => (Math.abs(prev - next) > 1 ? next : prev));
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, []);
  return [box, width];
}

export function fmt(n: number): string {
  return n.toLocaleString('ko-KR');
}

type BarRow = { label: string; value: number; hint?: string; color?: string };

/**
 * 수평 막대 목록 — 범주 비교의 기본형(긴 이름이 많으므로 가로로 둔다).
 * 시리즈가 하나라 범례는 두지 않는다(제목이 무엇을 그린 것인지 말한다).
 */
export function BarList({ rows, max, unit = '건', height = BAR }: {
  rows: BarRow[];
  max?: number;
  unit?: string;
  height?: number;
}) {
  const top = max ?? Math.max(1, ...rows.map((r) => r.value));
  const labelW = 150;
  const valueW = 72;
  const rowH = height + GAP * 3;
  const [box, width] = useWidth(560);
  const plotW = Math.max(60, width - labelW - valueW);
  return (
    <div ref={box}>
    <svg width={width} height={rows.length * rowH} role="img">
      {rows.map((r, i) => {
        const y = i * rowH + GAP;
        const w = Math.max(1, Math.round((r.value / top) * plotW));
        return (
          <g key={r.label}>
            <title>{`${r.label}: ${fmt(r.value)}${unit}${r.hint ? ` — ${r.hint}` : ''}`}</title>
            <text x={labelW - 8} y={y + height / 2 + 4} textAnchor="end"
                  fill={VIZ.muted} fontSize="12">{r.label}</text>
            <path d={barPath(labelW, y, w, height)} fill={r.color ?? VIZ.series} />
            {/* 값은 막대 끝에 직접 적는다 — 축을 읽게 만드는 대신 숫자를 바로 준다. */}
            <text x={labelW + w + 8} y={y + height / 2 + 4} fill={VIZ.ink}
                  fontSize="12" style={{ fontVariantNumeric: 'tabular-nums' }}>
              {fmt(r.value)}
            </text>
          </g>
        );
      })}
    </svg>
    </div>
  );
}

/**
 * 퍼널 — 단계마다 직전 단계의 부분집합이다. 순서 램프로 칠하고 전 단계 대비 비율을 적는다.
 * 떨어지는 폭이 곧 "어디서 멈췄나"이므로 그 숫자를 가장 크게 둔다.
 */
export function FunnelBars({ stages }: {
  stages: { stage: string; count: number; detail?: string }[];
}) {
  const top = Math.max(1, ...stages.map((s) => s.count));
  const labelW = 130;
  const rowH = 34;
  const [box, width] = useWidth(620);
  const plotW = Math.max(60, width - labelW - 150);
  return (
    <div ref={box}>
    <svg width={width} height={stages.length * rowH} role="img">
      {stages.map((s, i) => {
        const prev = i === 0 ? s.count : stages[i - 1].count;
        const drop = prev > 0 ? Math.round((1 - s.count / prev) * 100) : 0;
        const y = i * rowH + GAP;
        const w = Math.max(1, Math.round((s.count / top) * plotW));
        return (
          <g key={s.stage}>
            <title>{`${s.stage}: ${fmt(s.count)}건${s.detail ? ` — ${s.detail}` : ''}`}</title>
            <text x={labelW - 8} y={y + BAR / 2 + 4} textAnchor="end"
                  fill={VIZ.muted} fontSize="12">{s.stage}</text>
            <path d={barPath(labelW, y, w, BAR)}
                  fill={VIZ.ordinal[Math.min(i, VIZ.ordinal.length - 1)]} />
            <text x={labelW + w + 8} y={y + BAR / 2 + 4} fill={VIZ.ink} fontSize="12"
                  style={{ fontVariantNumeric: 'tabular-nums' }}>
              {fmt(s.count)}
              {i > 0 && drop > 0 && (
                <tspan fill={VIZ.status.warning}>{`  -${drop}%`}</tspan>
              )}
            </text>
          </g>
        );
      })}
    </svg>
    </div>
  );
}

type StackRow = { label: string; parts: { key: string; value: number; color: string }[] };

/**
 * 스택 막대 — 부분과 전체(저장소별 전환 상태). 맞닿은 조각은 **표면 간격 2px**로만 가른다.
 * 상태 색을 쓰므로 범례에 기호와 이름을 함께 둔다(색만으로 뜻을 나르지 않는다).
 */
export function StackedBars({ rows, legend }: {
  rows: StackRow[];
  legend: { key: string; label: string; color: string; mark: string }[];
}) {
  const totals = rows.map((r) => r.parts.reduce((sum, p) => sum + p.value, 0));
  const top = Math.max(1, ...totals);
  const labelW = 150;
  const rowH = BAR + GAP * 3;
  const [box, width] = useWidth(620);
  const plotW = Math.max(60, width - labelW - 80);
  return (
    <>
      <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginBottom: 6 }}>
        {legend.map((l) => (
          <span key={l.key} style={{ fontSize: 12, color: VIZ.muted }}>
            <span aria-hidden style={{ color: l.color, marginRight: 4 }}>{l.mark}</span>
            {l.label}
          </span>
        ))}
      </div>
      <svg ref={box as unknown as React.RefObject<SVGSVGElement>}
           width={width} height={rows.length * rowH} role="img">
        {rows.map((r, i) => {
          const y = i * rowH + GAP;
          let x = labelW;
          const total = totals[i];
          return (
            <g key={r.label}>
              <title>
                {`${r.label}: ` + r.parts.map((p) => `${p.key} ${fmt(p.value)}`).join(' · ')}
              </title>
              <text x={labelW - 8} y={y + BAR / 2 + 4} textAnchor="end"
                    fill={VIZ.muted} fontSize="12">{r.label}</text>
              {r.parts.map((p, j) => {
                if (p.value <= 0) return null;
                const w = Math.max(1, Math.round((p.value / top) * plotW));
                const last = j === r.parts.length - 1
                  || r.parts.slice(j + 1).every((q) => q.value <= 0);
                const seg = last
                  ? barPath(x, y, w, BAR)
                  : `M${x},${y}h${w}v${BAR}h${-w}z`;
                const node = <path key={p.key} d={seg} fill={p.color} />;
                x += w + GAP;  // 표면 간격: 조각 사이를 배경색이 가른다
                return node;
              })}
              <text x={width - 72} y={y + BAR / 2 + 4} fill={VIZ.ink} fontSize="12"
                    style={{ fontVariantNumeric: 'tabular-nums' }}>{fmt(total)}</text>
            </g>
          );
        })}
      </svg>
    </>
  );
}

/**
 * 히트맵 — 저장소 × 노드 종류처럼 격자 안의 크기를 본다. 노드-링크로는 읽히지 않는 밀도를
 * 행렬이 그대로 보여 준다(Ghoniem 2004 이후 알려진 대비).
 *
 * 셀에는 숫자를 모두 적지 않는다 — 마우스를 올리면 값을 말하고, 아래 표가 전부를 담는다.
 */
export function Heatmap({ rows, columns, value, unit = '건' }: {
  rows: string[];
  columns: string[];
  value: (row: string, col: string) => number;
  unit?: string;
}) {
  const [hover, setHover] = useState<{ row: string; col: string; n: number } | null>(null);
  const [box, width] = useWidth(620);
  const labelW = 150;
  const headH = 22;
  // 칸은 폭에 따라 늘어나도 좋다(크기를 나타내는 사각형이다) — 글자는 늘어나면 안 된다.
  const cell = Math.max(28, Math.min(56,
    Math.floor((width - labelW) / Math.max(1, columns.length))));
  const max = Math.max(1, ...rows.flatMap((r) => columns.map((c) => value(r, c))));
  const color = (n: number) => {
    if (n <= 0) return VIZ.heat[0];
    const idx = 1 + Math.round((n / max) * (VIZ.heat.length - 2));
    return VIZ.heat[Math.min(idx, VIZ.heat.length - 1)];
  };
  return (
    <div ref={box} style={{ position: 'relative' }}>
      <svg width={labelW + columns.length * cell} height={headH + rows.length * cell}
           role="img">
        {columns.map((c, j) => (
          <text key={c} x={labelW + j * cell + cell / 2} y={headH - 8}
                textAnchor="middle" fill={VIZ.muted} fontSize="11">{c}</text>
        ))}
        {rows.map((r, i) => (
          <g key={r}>
            <text x={labelW - 8} y={headH + i * cell + cell / 2 + 4} textAnchor="end"
                  fill={VIZ.muted} fontSize="12">{r}</text>
            {columns.map((c, j) => {
              const n = value(r, c);
              return (
                <rect
                  key={c}
                  x={labelW + j * cell + GAP / 2}
                  y={headH + i * cell + GAP / 2}
                  width={cell - GAP}
                  height={cell - GAP}
                  rx={2}
                  fill={color(n)}
                  onMouseEnter={() => setHover({ row: r, col: c, n })}
                  onMouseLeave={() => setHover(null)}
                >
                  <title>{`${r} · ${c}: ${fmt(n)}${unit}`}</title>
                </rect>
              );
            })}
          </g>
        ))}
      </svg>
      <div className="row" style={{ gap: 6, alignItems: 'center', marginTop: 4 }}>
        <span style={{ fontSize: 11, color: VIZ.muted }}>0</span>
        {VIZ.heat.map((c) => (
          <span key={c} style={{
            width: 18, height: 10, borderRadius: 2, background: c, display: 'inline-block',
          }} />
        ))}
        <span style={{ fontSize: 11, color: VIZ.muted }}>{fmt(max)}</span>
        {hover && (
          <span style={{ fontSize: 12, marginLeft: 10 }}>
            {hover.row} · {hover.col}: <b>{fmt(hover.n)}</b>{unit}
          </span>
        )}
      </div>
    </div>
  );
}

/** 단일 수치 타일 — 한 숫자가 요점일 때는 막대 하나를 그리지 않는다. */
export function StatTile({ label, value, sub }: {
  label: string; value: string; sub?: string;
}) {
  return (
    <div style={{
      border: '1px solid var(--border-soft)', borderRadius: 8, padding: '10px 14px',
      minWidth: 136,
    }}>
      <div style={{ fontSize: 12, color: VIZ.muted }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 600 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: VIZ.muted }}>{sub}</div>}
    </div>
  );
}
