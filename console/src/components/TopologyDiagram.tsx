import type { ComponentStatus, RedirectRuleSummary, ServerConfigOut, ServerConfigSite } from '../lib/types';

// StatusPill의 CLASS_MAP과 동일한 상태→색상 분류를 SVG stroke/fill에 맞게 재사용한다.
const STATUS_CLASS: Record<string, string> = {
  running: 'ok',
  building: 'warn',
  failed: 'bad',
  stopped: 'dim',
  partial: 'warn',
};

function statusClass(status: string): string {
  return STATUS_CLASS[status.split(' ')[0]] ?? 'dim';
}

const CLASS_FILL: Record<string, string> = {
  ok: 'var(--green)',
  warn: 'var(--yellow)',
  bad: 'var(--red)',
  dim: 'var(--muted)',
  info: 'var(--accent)',
};

const NODE_W = 160;
const NODE_H = 40;
const SUB_H = 26;
const GAP_X = 28;
const PROXY_Y = 20;
const SITE_Y = 120;
const COMPOSITE_H = SUB_H * 2 + 30;
const RUNTIME_Y = SITE_Y + COMPOSITE_H + 60;

function siteHeight(site: ServerConfigSite): number {
  return site.components && site.components.length > 0 ? COMPOSITE_H : NODE_H;
}

export default function TopologyDiagram({ cfg }: { cfg: ServerConfigOut }) {
  const sites = cfg.sites;
  if (sites.length === 0) {
    return <p className="mutedtext">표시할 사이트가 없습니다.</p>;
  }
  const width = sites.length * (NODE_W + GAP_X) - GAP_X;
  const height = RUNTIME_Y + NODE_H + 20;
  const centerX = width / 2 - NODE_W / 2;

  return (
    <div className="topo-wrap">
      <svg width={width} height={height} role="img" aria-label="서버 구성 토폴로지">
        {sites.map((s, i) => {
          const x = i * (NODE_W + GAP_X);
          const midX = x + NODE_W / 2;
          const bottomY = SITE_Y + siteHeight(s);
          // in_proxy === false: 프록시 설정(web.config)에 라우팅이 없음 = 미연결.
          // null(추적 안 하는 백엔드)이면 기존처럼 실선으로 둔다.
          const disconnected = s.in_proxy === false;
          return (
            <g key={`edge-${s.project_id}-${s.profile}`}>
              <path
                className="topo-edge"
                strokeDasharray={disconnected ? '4 4' : undefined}
                opacity={disconnected ? 0.35 : undefined}
                d={`M ${centerX + NODE_W / 2} ${PROXY_Y + NODE_H} C ${centerX + NODE_W / 2} ${PROXY_Y + NODE_H + 30}, ${midX} ${SITE_Y - 30}, ${midX} ${SITE_Y}`}
              />
              <path
                className="topo-edge"
                d={`M ${midX} ${bottomY} C ${midX} ${bottomY + 30}, ${centerX + NODE_W / 2} ${RUNTIME_Y - 30}, ${centerX + NODE_W / 2} ${RUNTIME_Y}`}
              />
            </g>
          );
        })}

        <TopoBox x={centerX} y={PROXY_Y} w={NODE_W} h={NODE_H} cls="info">
          <text x={centerX + NODE_W / 2} y={PROXY_Y + NODE_H / 2 + 4} textAnchor="middle" className="topo-label-title">
            프록시 · {cfg.proxy_backend}
          </text>
        </TopoBox>

        {sites.map((s, i) => {
          const x = i * (NODE_W + GAP_X);
          return <SiteNode key={`${s.project_id}-${s.profile}`} x={x} site={s} />;
        })}

        <TopoBox x={centerX} y={RUNTIME_Y} w={NODE_W} h={NODE_H} cls="info">
          <text x={centerX + NODE_W / 2} y={RUNTIME_Y + NODE_H / 2 + 4} textAnchor="middle" className="topo-label-title">
            런타임 · {cfg.runtime_backend}
          </text>
        </TopoBox>
      </svg>
      <div className="row topo-legend">
        {(['running', 'building', 'stopped', 'failed'] as const).map((s) => (
          <span key={s} className="row" style={{ gap: 4 }}>
            <span className="topo-dot" style={{ background: CLASS_FILL[statusClass(s)] }} />
            <span className="mutedtext" style={{ fontSize: 11 }}>{s}</span>
          </span>
        ))}
        <span className="row" style={{ gap: 4 }}>
          <span className="topo-dot" style={{ background: 'var(--accent)' }} />
          <span className="mutedtext" style={{ fontSize: 11 }}>숫자 배지 = redirect/rewrite 규칙 수(마우스 올리면 상세)</span>
        </span>
        <span className="row" style={{ gap: 4 }}>
          <svg width={22} height={8}><line x1={0} y1={4} x2={22} y2={4} stroke="var(--muted)" strokeWidth={2} strokeDasharray="4 4" /></svg>
          <span className="mutedtext" style={{ fontSize: 11 }}>점선 = 프록시(web.config)에 미구성 — 미연결</span>
        </span>
      </div>
    </div>
  );
}

function TopoBox({
  x, y, w, h, cls, children,
}: {
  x: number; y: number; w: number; h: number; cls: string; children: React.ReactNode;
}) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={8} className={`topo-node ${cls}`} />
      {children}
    </g>
  );
}

function redirectTooltip(redirects: RedirectRuleSummary[]): string {
  return redirects
    .map((r) => `${r.from_path} → ${r.to_path} (${r.kind}${r.kind === 'redirect' ? ' ' + r.status_code : ''})`)
    .join('\n');
}

// URL redirect/rewrite 규칙이 있는 사이트의 우상단에 개수 배지를 얹는다 — hover 시
// SVG 네이티브 <title> 툴팁으로 실제 규칙(from → to)을 보여준다(별도 의존성 없음).
function RedirectBadge({ x, y, redirects }: { x: number; y: number; redirects: RedirectRuleSummary[] }) {
  if (redirects.length === 0) return null;
  return (
    <g>
      <circle cx={x} cy={y} r={9} className="topo-redirect-badge" />
      <text x={x} y={y + 3} className="topo-redirect-badge-text">{redirects.length}</text>
      <title>{redirectTooltip(redirects)}</title>
    </g>
  );
}

function SiteNode({ x, site }: { x: number; site: ServerConfigSite }) {
  if (site.components && site.components.length > 0) {
    return (
      <g>
        <rect x={x} y={SITE_Y} width={NODE_W} height={COMPOSITE_H} rx={8} className="topo-node topo-node-outer" />
        <text x={x + NODE_W / 2} y={SITE_Y + 16} textAnchor="middle" className="topo-label-title">
          {site.project_name}
        </text>
        <RedirectBadge x={x + NODE_W - 12} y={SITE_Y + 12} redirects={site.redirects} />
        {site.components.map((c: ComponentStatus, idx) => {
          const cy = SITE_Y + 22 + idx * (SUB_H + 4);
          const route = c.name === 'backend' ? '/api/*' : '/*';
          return (
            <g key={c.name}>
              <rect
                x={x + 8} y={cy} width={NODE_W - 16} height={SUB_H} rx={6}
                className={`topo-node ${statusClass(c.status)}`}
              />
              <text x={x + 14} y={cy + SUB_H / 2 + 4} className="topo-sublabel">
                {route} → {c.name}
              </text>
              <text
                x={x + NODE_W - 14} y={cy + SUB_H / 2 + 4} textAnchor="end"
                className="topo-sublabel" style={{ fill: CLASS_FILL[statusClass(c.status)] }}
              >
                {c.status}
              </text>
            </g>
          );
        })}
      </g>
    );
  }
  return (
    <g>
      <rect x={x} y={SITE_Y} width={NODE_W} height={NODE_H} rx={8} className={`topo-node ${statusClass(site.status)}`} />
      <text x={x + NODE_W / 2} y={SITE_Y + 16} textAnchor="middle" className="topo-label-title">
        {site.project_name}
      </text>
      <RedirectBadge x={x + NODE_W - 12} y={SITE_Y + 12} redirects={site.redirects} />
      <text
        x={x + NODE_W / 2} y={SITE_Y + 32} textAnchor="middle"
        className="topo-sublabel" style={{ fill: CLASS_FILL[statusClass(site.status)] }}
      >
        {site.status}
      </text>
    </g>
  );
}
