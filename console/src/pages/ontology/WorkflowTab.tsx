import { useEffect, useMemo, useState } from 'react';
import ForceGraph, { type GraphLink, type GraphNode } from '../../components/ForceGraph';
import { api } from '../../lib/api';
import { useApi } from '../../lib/hooks';
import type { WorkflowLink, WorkflowLinks } from '../../lib/types';
import { VIZ, fmt } from '../../lib/viz';

// 저장소 쪽 색은 정보 조회 탭(ExploreTab)과 같다 — 같은 노드는 어느 탭에서나 같은 색이다.
// 워크플로 쪽은 한 가지 색(잉크)에 표식으로 가른다: 색은 "어느 쪽이냐", 표식은 "무엇이냐".
const KINDS: { key: string; label: string; color: string; mark: string }[] = [
  { key: 'workflow', label: '워크플로', color: VIZ.ink, mark: '◎' },
  { key: 'entity', label: '대상', color: VIZ.ink, mark: '◆' },
  { key: 'state', label: '상태', color: VIZ.ink, mark: '●' },
  { key: 'constraint', label: '규칙', color: VIZ.ink, mark: '▲' },
  { key: 'step', label: '단계', color: VIZ.ink, mark: '▪' },
  { key: 'store', label: '저장소', color: VIZ.muted, mark: '▣' },
  { key: 'document', label: '문서', color: VIZ.ordinal[4], mark: '▣' },
  { key: 'section', label: '절', color: VIZ.ordinal[3], mark: '▦' },
  { key: 'term', label: '용어', color: VIZ.ordinal[2], mark: '◆' },
  { key: 'table', label: '표', color: VIZ.ordinal[1], mark: '▤' },
];
const kindOf = (kind: string) => KINDS.find((k) => k.key === kind);

const REL: Record<string, string> = {
  uses: '사용', read: '읽음', matches: '같은 이름', mentions: '많이 나옴', grounded_in: '근거',
};
const SOURCE: Record<string, string> = {
  spec: '설계', run: '실행 기록', name: '이름 일치', index: '전문 색인', llm: 'LLM 제안',
};

/**
 * 워크플로 연결 — 부서 워크플로의 낱말(대상·상태·규칙)과 단계가 저장소 온톨로지의 어디에 닿는지.
 * 문서 온톨로지는 문서에서만 나오고, 이 선들은 워크플로 쪽에서 긋는다(services/workflowlinks).
 * 근거가 확실한 것(설계·실행·이름·색인)은 저장할 때마다 다시 계산되고, LLM 제안은 사람이 확인해야 쓰인다.
 */
export default function OntologyWorkflowTab() {
  const flows = useApi(() => api.listWorkflows());
  const [picked, setPicked] = useState<number | null>(null);
  const [data, setData] = useState<WorkflowLinks | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [selected, setSelected] = useState<string>();

  useEffect(() => {
    if (picked == null && flows.data?.length) setPicked(flows.data[0].id);
  }, [flows.data, picked]);

  useEffect(() => {
    if (picked == null) return;
    let alive = true;
    setData(null);
    setNote('');
    api.workflowLinks(picked).then((d) => { if (alive) setData(d); },
      (e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [picked]);

  const act = async (label: string, fn: () => Promise<WorkflowLinks>) => {
    if (picked == null) return;
    setBusy(label);
    setError('');
    try {
      const out = await fn();
      setData(out);
      if (out.suggested !== undefined) setNote(out.note || `제안 ${out.suggested}건 — 아래 목록에서 확인하거나 거절하세요.`);
      else if (out.counts) {
        setNote(`다시 계산했습니다 — ${Object.entries(out.counts).map(([k, v]) => `${SOURCE[k] ?? k} ${v}`).join(' · ') || '연결 없음'}`
          + (out.skipped?.length ? ` (건너뜀: ${out.skipped.join(', ')})` : ''));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const review = async (link: WorkflowLink, accept: boolean) => {
    if (picked == null) return;
    try {
      if (accept) await api.confirmWorkflowLink(picked, link.id, true);
      else await api.deleteWorkflowLink(picked, link.id);
      setData(await api.workflowLinks(picked));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const nodes: GraphNode[] = useMemo(() => (data?.graph.nodes ?? []).map((n) => ({
    id: n.id, label: n.label, kind: n.kind, fixed: n.kind === 'workflow',
    weight: n.kind === 'workflow' ? 30 : undefined,
  })), [data]);
  const links: GraphLink[] = useMemo(() => (data?.graph.links ?? []).map((l) => ({
    source: l.source, target: l.target,
    label: l.label ? `${REL[l.label] ?? l.label}${l.confirmed === false ? '(제안)' : ''}` : '',
  })), [data]);

  const groups = useMemo(() => {
    const by = new Map<string, WorkflowLink[]>();
    (data?.links ?? []).forEach((l) => {
      const key = `${l.subject_kind}|${l.subject}`;
      by.set(key, [...(by.get(key) ?? []), l]);
    });
    return [...by.entries()];
  }, [data]);
  const pending = (data?.links ?? []).filter((l) => !l.confirmed);

  return (
    <>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>워크플로 연결</h3>
        <p className="mutedtext" style={{ fontSize: 12 }}>
          부서 워크플로의 대상·상태·규칙과 단계가 저장소 온톨로지의 어느 문서·용어·조문·표에 닿는지 봅니다. 설계(단계가
          쓰는 저장소)·실행 기록(실제로 읽은 문서)·이름 일치·전문 색인으로 긋는 선은 워크플로를 저장하거나 저장소가
          재색인될 때 다시 계산됩니다. 이어지지 않은 낱말은 「LLM 제안」으로 짝을 받아 확인할 수 있습니다(제안은 확인해야
          스마트워크가 근거로 씁니다). 스마트워크에서 이 워크플로를 고르면 검색이 이 저장소부터, 이 근거부터 갑니다.
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <select value={picked ?? ''} onChange={(e) => setPicked(Number(e.target.value))}>
            {(flows.data ?? []).map((w) => <option key={w.id} value={w.id}>{w.org_name} · {w.name}</option>)}
          </select>
          <button className="small secondary" disabled={!picked || !!busy}
            onClick={() => void act('refresh', () => api.refreshWorkflowLinks(picked!))}>
            {busy === 'refresh' ? '계산 중…' : '다시 계산'}
          </button>
          <button className="small" disabled={!picked || !!busy}
            onClick={() => void act('suggest', () => api.suggestWorkflowLinks(picked!))}>
            {busy === 'suggest' ? 'LLM이 고르는 중…' : 'LLM 제안'}
          </button>
          {data && <span className="mutedtext" style={{ fontSize: 12 }}>
            저장소 {data.stores.join(', ') || '없음'} · 연결 {fmt(data.links.length)}
            {pending.length > 0 && ` · 확인 기다리는 제안 ${pending.length}`}
          </span>}
        </div>
        {flows.data && flows.data.length === 0 && (
          <p className="mutedtext" style={{ fontSize: 12 }}>볼 수 있는 워크플로가 없습니다 — 워크플로 관리에서 만드세요.</p>
        )}
        {note && <p className="mutedtext" style={{ fontSize: 12 }}>{note}</p>}
        {error && <p className="error">{error}</p>}
      </div>

      {data && (
        <div className="panel">
          <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginBottom: 6 }}>
            {KINDS.map((k) => (
              <span key={k.key} style={{ fontSize: 12, color: VIZ.muted }}>
                <span aria-hidden style={{ color: k.color, marginRight: 4 }}>{k.mark}</span>{k.label}
              </span>
            ))}
          </div>
          {nodes.length > 1
            ? <ForceGraph nodes={nodes} links={links} selected={selected} height={460}
                colorOf={(kind) => kindOf(kind)?.color ?? VIZ.series} onSelect={setSelected} />
            : <p className="mutedtext" style={{ fontSize: 12 }}>
                이 워크플로에는 아직 대상·규칙·저장소 단계가 없습니다 — 워크플로 관리에서 대화로 업무를 읽어 내고 저장하세요.
              </p>}
        </div>
      )}

      {data && data.unground_rules.length > 0 && (
        <div className="panel">
          <h4 style={{ margin: '0 0 6px' }}>근거 미확인 규칙 {data.unground_rules.length}</h4>
          <p className="mutedtext" style={{ fontSize: 12, marginTop: 0 }}>
            근거 조문·문서가 이어지지 않은 규칙입니다. 규칙에 「제N조」나 「문서명」을 적거나, 「LLM 제안」으로 근거 후보를
            받아 확인하세요.
          </p>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
            {data.unground_rules.map((r) => <li key={r}>{r}</li>)}
          </ul>
        </div>
      )}

      {data && groups.length > 0 && (
        <div className="panel">
          <h4 style={{ margin: '0 0 6px' }}>연결 목록</h4>
          <div style={{ maxHeight: '60vh', overflow: 'auto' }}>
            <table style={{ fontSize: 12 }}>
              <thead><tr><th>워크플로 쪽</th><th>관계</th><th>저장소 쪽</th><th>근거</th><th /></tr></thead>
              <tbody>
                {groups.map(([, rows]) => rows.map((l, i) => (
                  <tr key={l.id} style={l.confirmed ? undefined : { background: 'rgba(250, 178, 25, 0.08)' }}>
                    <td style={{ overflowWrap: 'anywhere' }}>
                      {i === 0 && <>
                        <span aria-hidden style={{ marginRight: 4 }}>{kindOf(l.subject_kind)?.mark}</span>
                        <span className="mutedtext">{kindOf(l.subject_kind)?.label}</span> {l.subject}
                      </>}
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>{REL[l.rel] ?? l.rel}</td>
                    <td style={{ overflowWrap: 'anywhere' }}>
                      <span aria-hidden style={{ color: kindOf(l.target_kind)?.color, marginRight: 4 }}>
                        {kindOf(l.target_kind)?.mark}
                      </span>
                      {l.target}
                      <div className="mono mutedtext" style={{ fontSize: 10 }}>
                        {l.store}{l.path && l.path !== l.target ? ` / ${l.path}` : ''}
                      </div>
                    </td>
                    <td>
                      {SOURCE[l.source] ?? l.source}
                      {l.detail && <div className="mutedtext" style={{ fontSize: 11 }}>{l.detail}</div>}
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {!l.confirmed && <>
                        <button className="small" onClick={() => void review(l, true)}>확인</button>{' '}
                        <button className="small secondary" onClick={() => void review(l, false)}>거절</button>
                      </>}
                      {l.confirmed && l.source === 'llm' && (
                        <button className="small secondary" onClick={() => void review(l, false)}>빼기</button>
                      )}
                    </td>
                  </tr>
                ))).flat()}
              </tbody>
            </table>
          </div>
          <p className="mutedtext" style={{ fontSize: 11, marginBottom: 0 }}>
            설계·실행 기록·이름 일치·전문 색인 연결은 다시 계산할 때 원래대로 돌아옵니다 — 빼고 싶은 것은 워크플로의 낱말이나
            규칙을 고치세요. {groups.length > 0 && `대상·상태·규칙·단계 ${groups.length}개`}
          </p>
        </div>
      )}
    </>
  );
}
