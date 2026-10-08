import { useEffect, useMemo, useRef, useState } from 'react';

/**
 * 지식 그래프 뷰 — Obsidian·TheBrain의 그래프 뷰 스타일(힘기반 배치).
 *
 * 앞서 이 화면은 계층 배치(dagre)였다. 그 선택의 이유는 "같은 질문에 같은 그림"이었는데,
 * 지식 그래프를 **둘러보는** 일에는 다른 성질이 더 중요하다는 것이 Obsidian·TheBrain이
 * 보여 준 바다: 가까운 것이 가까이 모이고(군집이 눈에 보인다), 손으로 끌면 따라오고,
 * 한 노드에 마우스를 올리면 그 이웃만 남는다. 그 셋이 "이 문서는 어떤 이웃과 묶여 있나"를
 * 계층도보다 빨리 답한다.
 *
 * **의존성을 더하지 않는다.** 노드가 수십 개(이웃 확장 상한 30)라 d3-force를 들여올 이유가
 * 없다 — 밀기(반발)·당기기(연결)·중심 끌기 세 힘과 감쇠로 충분하고, 그게 전부 40줄이다.
 *
 * 시뮬레이션은 **잦아들면 멈춘다**(alpha 감쇠). 끝없이 떠는 화면은 읽을 수 없고 배터리만
 * 쓴다 — 노드를 더하거나 끌면 다시 깨어난다.
 *
 * 글자 크기는 확대와 **무관하게** 고정한다(화면 좌표에 그린다). 전환 현황에서 겪은 것과 같은
 * 이유다: SVG를 통째로 확대하면 글자가 8px이 되거나 19px이 된다.
 */
export interface GraphNode {
  id: string;
  label: string;
  kind: string;
  /** 크기에 쓰는 값(문서 수 등). 없으면 연결 수로 정한다. */
  weight?: number;
  /** 아직 이웃을 펼치지 않은 노드 — 테두리를 비워 "더 있다"를 표시한다. */
  unexpanded?: boolean;
}

export interface GraphLink {
  source: string;
  target: string;
  label?: string;
}

interface Body {
  x: number;
  y: number;
  vx: number;
  vy: number;
  pinned: boolean;
}

const CHARGE = 2600;     // 서로 밀어내는 힘
const SPRING = 0.015;    // 연결된 노드를 당기는 힘
const REST = 120;        // 연결의 자연 길이
const CENTER = 0.004;    // 가운데로 모으는 힘(흩어져 날아가지 않게)
const DAMP = 0.86;       // 속도 감쇠 — 이게 없으면 영원히 흔들린다
const ALPHA_DECAY = 0.985;
const MIN_ALPHA = 0.02;

export default function ForceGraph({
  nodes, links, selected, colorOf, onSelect, onExpand, height = 560,
}: {
  nodes: GraphNode[];
  links: GraphLink[];
  selected?: string;
  colorOf: (kind: string) => string;
  onSelect?: (id: string) => void;
  onExpand?: (id: string) => void;
  height?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: height });
  const bodies = useRef(new Map<string, Body>());
  const alpha = useRef(1);
  const [, redraw] = useState(0);
  const [hover, setHover] = useState<string>('');
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ id: string | null; x: number; y: number } | null>(null);

  useEffect(() => {
    const target = box.current;
    if (!target) return;
    const observer = new ResizeObserver(() =>
      setSize({ w: Math.max(320, target.clientWidth), h: height }));
    setSize({ w: Math.max(320, target.clientWidth), h: height });
    observer.observe(target);
    return () => observer.disconnect();
  }, [height]);

  const degree = useMemo(() => {
    const count = new Map<string, number>();
    links.forEach((l) => {
      count.set(l.source, (count.get(l.source) ?? 0) + 1);
      count.set(l.target, (count.get(l.target) ?? 0) + 1);
    });
    return count;
  }, [links]);

  // 새 노드는 **고른 노드 근처**에 놓고 시작한다 — 화면 가운데에 쏟으면 펼친 것이 어디서
  // 나왔는지 알 수 없다(Obsidian이 확장한 노드 주변에서 번지는 것과 같은 효과).
  useEffect(() => {
    const map = bodies.current;
    const anchor = selected ? map.get(selected) : undefined;
    nodes.forEach((n, i) => {
      if (map.has(n.id)) return;
      const angle = (i / Math.max(1, nodes.length)) * Math.PI * 2;
      const radius = 60 + Math.random() * 40;
      map.set(n.id, {
        x: (anchor?.x ?? size.w / 2) + Math.cos(angle) * radius,
        y: (anchor?.y ?? size.h / 2) + Math.sin(angle) * radius,
        vx: 0, vy: 0, pinned: false,
      });
    });
    [...map.keys()].forEach((id) => {
      if (!nodes.some((n) => n.id === id)) map.delete(id);
    });
    alpha.current = 1;              // 노드가 바뀌면 다시 깨운다
  }, [nodes, selected, size.w, size.h]);

  // 시뮬레이션 — 잦아들면 멈춘다.
  useEffect(() => {
    let frame = 0;
    const step = () => {
      const map = bodies.current;
      if (alpha.current > MIN_ALPHA && map.size > 0) {
        const list = [...map.entries()];
        for (let i = 0; i < list.length; i += 1) {
          const [, a] = list[i];
          for (let j = i + 1; j < list.length; j += 1) {
            const [, b] = list[j];
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let dist = Math.hypot(dx, dy);
            if (dist < 1) {           // 완전히 겹치면 방향이 없다 — 살짝 흔든다
              dx = Math.random() - 0.5;
              dy = Math.random() - 0.5;
              dist = 1;
            }
            const push = (CHARGE / (dist * dist)) * alpha.current;
            a.vx += (dx / dist) * push;
            a.vy += (dy / dist) * push;
            b.vx -= (dx / dist) * push;
            b.vy -= (dy / dist) * push;
          }
        }
        links.forEach((l) => {
          const a = map.get(l.source);
          const b = map.get(l.target);
          if (!a || !b) return;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const dist = Math.max(1, Math.hypot(dx, dy));
          const pull = (dist - REST) * SPRING * alpha.current;
          a.vx += (dx / dist) * pull;
          a.vy += (dy / dist) * pull;
          b.vx -= (dx / dist) * pull;
          b.vy -= (dy / dist) * pull;
        });
        map.forEach((body) => {
          if (body.pinned) {
            body.vx = 0;
            body.vy = 0;
            return;
          }
          body.vx += (size.w / 2 - body.x) * CENTER * alpha.current;
          body.vy += (size.h / 2 - body.y) * CENTER * alpha.current;
          body.vx *= DAMP;
          body.vy *= DAMP;
          body.x += body.vx;
          body.y += body.vy;
        });
        alpha.current *= ALPHA_DECAY;
        redraw((n) => n + 1);
      }
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [links, size.w, size.h]);

  // 이웃 관계 — 마우스를 올리면 그 노드와 이웃만 남긴다(나머지는 흐린다).
  const neighbors = useMemo(() => {
    const focus = hover || selected || '';
    if (!focus) return null;
    const keep = new Set<string>([focus]);
    links.forEach((l) => {
      if (l.source === focus) keep.add(l.target);
      if (l.target === focus) keep.add(l.source);
    });
    return keep;
  }, [hover, selected, links]);

  const radius = (n: GraphNode) =>
    6 + Math.min(14, Math.sqrt(n.weight ?? degree.get(n.id) ?? 1) * 2.2);

  const toScreen = (x: number, y: number) => ({
    x: (x - size.w / 2) * view.k + size.w / 2 + view.x,
    y: (y - size.h / 2) * view.k + size.h / 2 + view.y,
  });

  const onPointerDown = (id: string | null) => (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { id, x: e.clientX, y: e.clientY };
    if (id) {
      const body = bodies.current.get(id);
      if (body) body.pinned = true;
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = (e.clientX - d.x) / view.k;
    const dy = (e.clientY - d.y) / view.k;
    d.x = e.clientX;
    d.y = e.clientY;
    if (d.id) {
      const body = bodies.current.get(d.id);
      if (body) {
        body.x += dx;
        body.y += dy;
        alpha.current = Math.max(alpha.current, 0.35);  // 끌면 주변이 다시 자리를 잡는다
      }
    } else {
      setView((v) => ({ ...v, x: v.x + dx * view.k, y: v.y + dy * view.k }));
    }
    redraw((n) => n + 1);
  };

  const onPointerUp = () => {
    const d = drag.current;
    if (d?.id) {
      // 놓으면 풀어 준다 — 끌어 둔 자리에 못 박히면 다음 확장이 그 노드를 비켜 간다.
      const body = bodies.current.get(d.id);
      if (body) body.pinned = false;
    }
    drag.current = null;
  };

  return (
    <div ref={box}>
      <svg
        width={size.w}
        height={size.h}
        style={{ touchAction: 'none', cursor: drag.current ? 'grabbing' : 'grab',
                 background: 'var(--bg)', borderRadius: 8 }}
        onPointerDown={onPointerDown(null)}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => { onPointerUp(); setHover(''); }}
        onWheel={(e) => {
          e.preventDefault();
          setView((v) => ({ ...v, k: Math.min(2.5, Math.max(0.35, v.k * (e.deltaY < 0 ? 1.1 : 0.9))) }));
        }}
        role="img"
        aria-label="지식 그래프"
      >
        {links.map((l, i) => {
          const a = bodies.current.get(l.source);
          const b = bodies.current.get(l.target);
          if (!a || !b) return null;
          const p = toScreen(a.x, a.y);
          const q = toScreen(b.x, b.y);
          const lit = !neighbors || (neighbors.has(l.source) && neighbors.has(l.target));
          return (
            <line
              key={`${l.source}|${l.target}|${i}`}
              x1={p.x} y1={p.y} x2={q.x} y2={q.y}
              stroke="var(--border)"
              strokeWidth={lit ? 1.6 : 1}
              opacity={lit ? 0.8 : 0.12}
            />
          );
        })}
        {nodes.map((n) => {
          const body = bodies.current.get(n.id);
          if (!body) return null;
          const p = toScreen(body.x, body.y);
          const r = radius(n) * Math.min(1.4, Math.max(0.7, view.k));
          const lit = !neighbors || neighbors.has(n.id);
          const color = colorOf(n.kind);
          return (
            <g
              key={n.id}
              opacity={lit ? 1 : 0.18}
              onPointerDown={(e) => { e.stopPropagation(); onPointerDown(n.id)(e); }}
              onPointerEnter={() => setHover(n.id)}
              onClick={(e) => {
                e.stopPropagation();
                onSelect?.(n.id);
                if (n.unexpanded) onExpand?.(n.id);
              }}
              style={{ cursor: 'pointer' }}
            >
              <circle
                cx={p.x} cy={p.y} r={r}
                fill={n.unexpanded ? 'var(--bg)' : color}
                stroke={n.id === selected ? 'var(--text)' : color}
                strokeWidth={n.id === selected ? 2.5 : 1.5}
              />
              {/* 글자는 확대와 무관하게 고정 크기다 — 확대하면 8px이 되거나 19px이 된다. */}
              {(lit || view.k > 1.2) && (
                <text
                  x={p.x} y={p.y + r + 12}
                  textAnchor="middle"
                  fontSize={11}
                  fill="var(--text)"
                  style={{ pointerEvents: 'none' }}
                >
                  {n.label.length > 22 ? `${n.label.slice(0, 21)}…` : n.label}
                </text>
              )}
              <title>{`${n.label} · ${n.kind}${n.weight ? ` · 문서 ${n.weight}건` : ''}`}</title>
            </g>
          );
        })}
      </svg>
      <p className="mutedtext" style={{ fontSize: 11, margin: '4px 0 0' }}>
        노드를 끌어 옮기고 휠로 확대합니다. 마우스를 올리면 그 이웃만 남고, 테두리만 있는
        노드를 누르면 한 걸음 더 펼칩니다.
      </p>
    </div>
  );
}
