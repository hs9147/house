import { useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { api } from '../lib/api';
import type { OntologyCleanse } from '../lib/types';

/**
 * 온톨로지 관리 — 탭 셋.  워크플로 연결: 부서 워크플로의 낱말·단계가 저장소의 어느 문서·용어·조문에 닿는지.
 *
 *  - **전환 현황**: 문서가 그래프로 얼마나 옮겨졌고 어디서 멈췄는지(집계·퍼널·행렬).
 *  - **정보 조회**: 옮겨진 것을 실제로 들여다본다(검색 → 이웃 → 요구 시 확장).
 *
 * 둘을 한 화면에 섞지 않는 이유는 질문이 다르기 때문이다. 현황은 "어디를 고쳐야 하나"이고
 * 조회는 "이것이 무엇과 이어져 있나"다 — 전자는 전체 분포라 집계가 맞고, 후자는 국소
 * 구조라 노드-링크가 맞는다(그래서 쓰는 표현도 다르다).
 */
// 순서가 뜻이다 — 쓰는 일(찾아 보기)이 먼저고, 고치는 일(전환 현황)이 그 다음이다.
// 그래서 기본 탭도 정보 조회다(/ontology).
const TABS: [string, string][] = [
  ['/ontology', '정보 조회'],
  ['/ontology/workflows', '워크플로 연결'],
  ['/ontology/conversion', '전환 현황'],
  ['/ontology/semantic', '내용 분석'],
];

export default function Ontology() {
  // 갱신·클린징이 끝나면 탭을 다시 그린다(탭마다 자기 데이터를 다시 읽는다).
  const [version, setVersion] = useState(0);
  return (
    <>
      <div className="tabs" style={{ alignItems: 'center' }}>
        {TABS.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === '/ontology'}>
            {label}
          </NavLink>
        ))}
        <div style={{ flex: 1 }} />
        <Maintenance onDone={() => setVersion((v) => v + 1)} />
      </div>
      <Outlet key={version} />
    </>
  );
}

const size = (bytes: number) => (bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)}MB` : `${Math.ceil(bytes / 1024)}KB`);

/** 클린징 미리보기를 사람이 읽을 줄들로 — 확인 창에 그대로 띄운다. */
function describe(c: OntologyCleanse): string[] {
  const lines = [
    ...c.orphan_index.map((i) => `· 색인 대상이 아닌 저장소의 색인: ${i.store}.db (${size(i.bytes)}) — 그 안의 내용 분석 결과도 함께`),
    ...c.orphan_ready.map((i) => `· 색인 대상이 아닌 저장소의 .ready: ${i.store} (${size(i.bytes)})`),
  ];
  for (const [store, s] of Object.entries(c.stores)) {
    const parts = [
      s.stale_nodes + s.stale_edges > 0 && `옛 노드·관계 ${s.stale_nodes + s.stale_edges}`,
      s.stale_semantic > 0 && `빠진 문서의 내용 그래프 ${s.stale_semantic}`,
      s.stale_ready > 0 && `빠진 문서의 .ready ${s.stale_ready}개(${size(s.stale_ready_bytes)})`,
    ].filter(Boolean);
    lines.push(`· ${store}: ${parts.join(', ')}`);
  }
  return lines;
}

/**
 * 전체 갱신 — 색인 대상 저장소를 차례로 재색인한다(저장소마다 남은 것이 없을 때까지). 그래프는 색인의 파생물이라
 * 이것이 곧 온톨로지 갱신이다. 클린징 — 색인 디렉터리의 찌꺼기(파생 데이터)를 미리 보여 주고, 확인하면 지운다.
 * 원본 문서는 어느 쪽도 건드리지 않는다.
 */
function Maintenance({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');

  const refreshAll = async () => {
    setNote('');
    try {
      const stores = (await api.ontologyOverview()).stores.map((s) => s.store);
      let indexed = 0;
      for (const store of stores) {
        for (let round = 0; round < 200; round += 1) {      // 한 번에 20초씩 — 남은 것이 없을 때까지
          setBusy(`갱신 중 · ${store}`);
          const r = await api.reindexOntology(store, false);
          indexed += r.indexed ?? 0;
          if (r.done) break;
        }
      }
      setNote(`전체 갱신 끝 — 저장소 ${stores.length}개, 다시 색인한 문서 ${indexed.toLocaleString()}건`);
      onDone();
    } catch (e) {
      setNote(`갱신 실패: ${(e as Error).message}`);
    } finally {
      setBusy('');
    }
  };

  const cleanse = async () => {
    setNote('');
    setBusy('찌꺼기를 세는 중…');
    try {
      const preview = await api.ontologyCleansePreview();
      if (preview.total === 0) {
        setNote('클린징 — 지울 것이 없습니다.');
        return;
      }
      const ok = window.confirm(['다음 파생 데이터를 지웁니다(원본 문서는 그대로입니다).', '', ...describe(preview)].join(String.fromCharCode(10)));
      if (!ok) return;
      setBusy('클린징 중…');
      const done = await api.ontologyCleanse();
      const failed = done.orphan_index.filter((i) => i.error);
      setNote(failed.length
        ? `클린징 — 일부를 지우지 못했습니다: ${failed.map((i) => `${i.store}(${i.error})`).join(', ')}`
        : `클린징 끝 — ${describe(done).length}건 정리`);
      onDone();
    } catch (e) {
      setNote(`클린징 실패: ${(e as Error).message}`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="row" style={{ gap: 6, alignItems: 'center' }}>
      {(busy || note) && <span className="mutedtext" style={{ fontSize: 12 }}>{busy || note}</span>}
      <button className="small secondary" disabled={!!busy} onClick={() => void refreshAll()}
        title="색인 대상 저장소를 모두 다시 색인합니다 — 바뀐 문서만 다시 읽습니다">전체 갱신</button>
      <button className="small secondary" disabled={!!busy} onClick={() => void cleanse()}
        title="색인 대상이 아닌 저장소의 색인·.ready, 옛 노드, 빠진 문서의 캐시를 미리 보여 주고 지웁니다">클린징</button>
    </div>
  );
}
