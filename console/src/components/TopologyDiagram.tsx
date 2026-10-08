import type { ComponentStatus, RedirectRuleSummary, ServerConfigOut, ServerConfigSite } from '../lib/types';

/**
 * 서버구성 — **입구 한 장 + 경로 목록**. 아래로 쌓이고 폭은 고정이다.
 *
 * 예전에는 프록시(위) → 사이트들(한 줄) → 런타임(아래)의 부채꼴이었다. 사이트가 늘면
 * 폭이 `사이트 수 × 188px`로 자라서(10개면 1,900px) 가로 스크롤만 남고, 그 상태에서는
 * 어느 경로가 어디로 가는지 한 화면에서 비교할 수 없다. 같은 실패를 code 레벨
 * 다이어그램에서 겪었고(lib/c4layout.layoutFlow 주석), 거기서 쓴 처방이 "흐름대로 접는 것"이다.
 *
 * 여기서는 한 걸음 더 간다 — **부채꼴을 버린다.** 선이 전부 똑같기 때문이다(모든 사이트가
 * 같은 프록시에서 오고 같은 런타임으로 간다). 선이 아무것도 구별해 주지 않으면 노드-링크는
 * 자리만 차지한다. 이 화면이 답해야 하는 질문은 "어떤 **경로**가 어디로 꽂혀 있고 떠 있나"고,
 * 그건 목록의 모양이다(Traefik·ingress 대시보드가 라우트를 표로 보여 주는 이유와 같다).
 *
 *  - 맨 위: 입구 한 줄(브라우저 → 프록시 → 런타임)과 개수. **크기가 고정**이다.
 *  - 그 아래: 도메인별로 묶은 경로 행. 행마다 경로 → 프로젝트(상태) → 업스트림 host:port.
 *    복합 프로젝트는 그 행 안에서 컴포넌트로 들여쓴다(api/* 와 나머지).
 *  - 목록은 세로로 스크롤한다(폭은 panel을 넘지 않는다).
 *
 * 색은 상태만 말하고, 상태 **글자**를 함께 둔다(색만으로 뜻을 나르지 않는다).
 */

// StatusPill의 CLASS_MAP과 동일한 상태→색상 분류를 재사용한다.
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

function upstream(site: ServerConfigSite): string {
  return site.internal_port ? `${site.internal_host ?? '127.0.0.1'}:${site.internal_port}` : '';
}

function redirectTooltip(redirects: RedirectRuleSummary[]): string {
  return redirects
    .map((r) => `${r.from_path} → ${r.to_path} (${r.kind}${r.kind === 'redirect' ? ' ' + r.status_code : ''})`)
    .join('\n');
}

export default function TopologyDiagram({ cfg }: { cfg: ServerConfigOut }) {
  const sites = cfg.sites;
  if (sites.length === 0) {
    return <p className="mutedtext">표시할 사이트가 없습니다.</p>;
  }
  const running = sites.filter((s) => statusClass(s.status) === 'ok').length;
  const unlinked = sites.filter((s) => s.in_proxy === false).length;

  // 도메인별로 묶는다 — 커스텀 도메인 배포가 섞이면 "어느 호스트의 경로인가"가 먼저다.
  const domains = [...new Set(sites.map((s) => s.domain))].sort();
  const byDomain = domains.map((domain) => ({
    domain,
    rows: sites
      .filter((s) => s.domain === domain)
      .sort((a, b) => a.path_prefix.localeCompare(b.path_prefix)),
  }));

  return (
    <div className="topo-wrap">
      {/* 입구 — 크기가 고정된 한 줄. 사이트가 늘어도 이 줄은 자라지 않는다. */}
      <div className="topo-entry">
        <span className="topo-entry-box">브라우저</span>
        <span className="topo-entry-arrow">→</span>
        <span className="topo-entry-box info">프록시 · {cfg.proxy_backend}</span>
        <span className="topo-entry-arrow">→</span>
        <span className="topo-entry-box">
          경로 {sites.length}개
          <span className="mutedtext" style={{ marginLeft: 6, fontSize: 11 }}>
            떠 있음 {running}
            {unlinked > 0 && ` · 미연결 ${unlinked}`}
          </span>
        </span>
        <span className="topo-entry-arrow">→</span>
        <span className="topo-entry-box info">런타임 · {cfg.runtime_backend}</span>
      </div>

      <div className="topo-routes">
        {byDomain.map(({ domain, rows }) => (
          <div key={domain} className="topo-domain">
            <div className="topo-domain-head mono">
              {domain}
              <span className="mutedtext" style={{ marginLeft: 6, fontSize: 11 }}>
                경로 {rows.length}
              </span>
            </div>
            {rows.map((site) => <RouteRow key={`${site.project_id}-${site.profile}`} site={site} />)}
          </div>
        ))}
      </div>

      <div className="row topo-legend">
        {(['running', 'building', 'stopped', 'failed'] as const).map((s) => (
          <span key={s} className="row" style={{ gap: 4 }}>
            <span className="topo-dot" style={{ background: CLASS_FILL[statusClass(s)] }} />
            <span className="mutedtext" style={{ fontSize: 11 }}>{s}</span>
          </span>
        ))}
        <span className="row" style={{ gap: 4 }}>
          <span className="topo-badge">2</span>
          <span className="mutedtext" style={{ fontSize: 11 }}>
            redirect/rewrite 규칙 수(마우스를 올리면 규칙)
          </span>
        </span>
        <span className="row" style={{ gap: 4 }}>
          <span className="topo-unlinked">미연결</span>
          <span className="mutedtext" style={{ fontSize: 11 }}>
            프록시(web.config)에 라우팅이 없음
          </span>
        </span>
      </div>
    </div>
  );
}

/** 경로 한 줄 — 경로 → 프로젝트(상태) → 업스트림. 복합이면 컴포넌트로 들여쓴다. */
function RouteRow({ site }: { site: ServerConfigSite }) {
  const cls = statusClass(site.status);
  const components: ComponentStatus[] = site.components ?? [];
  const target = upstream(site);
  return (
    <div className={`topo-route ${site.in_proxy === false ? 'unlinked' : ''}`}>
      <div className="topo-route-main">
        <span className="topo-path mono" title={site.path_prefix || '/'}>
          {site.path_prefix || '/'}
        </span>
        <span className="topo-arrow">→</span>
        <span className="topo-project">
          {site.project_name}
          <span className="mutedtext" style={{ marginLeft: 6, fontSize: 11 }}>{site.profile}</span>
        </span>
        <span className="topo-status" style={{ color: CLASS_FILL[cls] }}>
          <span className="topo-dot" style={{ background: CLASS_FILL[cls] }} />
          {site.status}
        </span>
        <span className="topo-upstream mono">
          {target || <span className="mutedtext">포트 없음</span>}
        </span>
        {site.redirects.length > 0 && (
          <span className="topo-badge" title={redirectTooltip(site.redirects)}>
            {site.redirects.length}
          </span>
        )}
        {site.in_proxy === false && (
          <span className="topo-unlinked" title="프록시 설정에 이 경로가 없습니다">미연결</span>
        )}
      </div>
      {components.length > 0 && (
        <div className="topo-components">
          {components.map((c) => {
            const sub = statusClass(c.status);
            // 라우팅 규약: 백엔드는 api/*, 나머지는 프론트엔드가 받는다(서버가 그렇게 구성한다).
            const route = c.name === 'backend' ? 'api/*' : '*';
            return (
              <div key={c.name} className="topo-component">
                <span className="topo-branch" aria-hidden>└</span>
                <span className="mono topo-path">{route}</span>
                <span className="topo-arrow">→</span>
                <span>{c.name}</span>
                <span className="topo-status" style={{ color: CLASS_FILL[sub] }}>
                  <span className="topo-dot" style={{ background: CLASS_FILL[sub] }} />
                  {c.status}
                </span>
                <span className="topo-upstream mono">
                  {c.internal_port ? `:${c.internal_port}` : <span className="mutedtext">포트 없음</span>}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
