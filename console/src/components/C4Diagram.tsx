import { useEffect, useMemo, useState } from 'react';
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import Async from './Async';
import CodeStructure from './CodeStructure';
import { codeLevel } from '../lib/codelevel';
import { api } from '../lib/api';
import { layoutElements, layoutFlow } from '../lib/c4layout';
import { useApi } from '../lib/hooks';
import type { C4Element, C4Level } from '../lib/types';

// C4 모델 시각화 — 확정 산출물에 실린 mermaid C4 블록이 원천이다(app/services/c4.py).
// 레벨은 위 버튼으로 고르고, 각 레벨은 **그 레벨 전체**를 한 장에 그린다. 확대·축소는
// 뷰포트로만 한다(React Flow의 Controls·휠·드래그·미니맵).
//
// 노드를 클릭해 한 단계 안으로 들어가는 '단위 확대'는 없앴다: 어느 단위 안에 있는지가
// 화면 상태로 숨고, 같은 레벨을 볼 때마다 경로가 달라져 그림을 비교할 수 없었다.
// code 레벨도 컴포넌트별이 아니라 리포 전체다 — 문서에 $link을 적지 않았다는 이유로
// 코드가 안 보이는 일이 없어야 한다.

type DocLevel = 'context' | 'container' | 'component';
type Level = DocLevel | 'code';

// 그릴 수 없는 레벨에는 **어느 단계에서 무엇을 써야 생기는지**를 알려 준다. 초안이든
// 확정본이든 사용자가 할 일은 같으므로(그 단계 문서에 그 블록을 싣는다) 두 경우를
// 가려내지 않는다. code 레벨은 문서가 원천이 아니라 여기 문구를 두지 않는다.
const LEVELS: { key: Level; label: string; origin: string }[] = [
  {
    key: 'context',
    label: 'System Context',
    origin: '① 기획서 단계 문서에 C4Context 블록(Person·System·System_Ext)을 실으면 그려집니다.',
  },
  {
    key: 'container',
    label: 'Container',
    origin: '② 아키텍처 설계 단계 문서에 C4Container 블록을 실으면 그려집니다 '
      + '(③ 솔루션 구성에서 실제 모듈·기술로 구체화).',
  },
  {
    key: 'component',
    label: 'Component',
    origin: '② 아키텍처 설계 단계 문서에 C4Component 블록을 실으면 그려집니다 '
      + '(③ 솔루션 구성에서 실제 모듈·기술로 구체화).',
  },
  { key: 'code', label: 'Code', origin: '' },
];

interface Props {
  sessionId: number;
  projectId: number;
  // 확정 상태가 바뀌면 다시 불러온다(확정 커밋 sha를 이어 붙인 값).
  reloadKey: string;
  // 지금 편집 중인 단계와 그 초안 — 확정을 기다리지 않고 그린다. 그림은 확정 여부를
  // 검토하기 위한 도구이고 확정은 그 검토의 결과이므로, 초안이 나온 즉시 보여야 한다.
  stage: string;
  draft: string;
  // stage → 기획 화면과 같은 단계 표기('① 기획서'). 그림이 어느 단계에서 왔는지 밝힌다.
  stageLabels: Record<string, string>;
}

// 초안은 타이핑마다 바뀐다 — 글자마다 요청을 보내지 않도록 잠잠해진 뒤에만 반영한다.
const DRAFT_DEBOUNCE_MS = 800;

/** value가 ms 동안 바뀌지 않았을 때만 따라가는 값. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

export default function C4Diagram({
  sessionId, projectId, reloadKey, stage, draft, stageLabels,
}: Props) {
  const settledDraft = useDebounced(draft, DRAFT_DEBOUNCE_MS);
  const model = useApi(
    () => api.planC4(sessionId, stage, settledDraft),
    [sessionId, reloadKey, stage, settledDraft],
  );
  const [level, setLevel] = useState<Level>('context');

  const levels = model.data?.levels ?? {};
  const current: C4Level | undefined = level === 'code' ? undefined : levels[level];

  // code 레벨은 문서가 아니라 리포가 원천이라 "그릴 수 있는가"가 산출물과 무관하다 —
  // 파싱할 코드 파일이 있으면 열린다. 실제 파일 수는 캔버스가 받아 본 뒤에야 알 수 있어
  // 여기서는 잠그지 않는다(잠가 두면 코드가 있는데도 못 여는 경우가 생긴다).
  const hintFor = (key: Level): string =>
    (key === 'code'
      ? '리포의 코드 구조를 정적 파싱해 그립니다 — 파일이 경계, 클래스·함수가 노드입니다.'
      : LEVELS.find((l) => l.key === key)?.origin ?? '');

  return (
    <>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {LEVELS.map((l) => {
          const available = l.key === 'code' ? true : !!levels[l.key as DocLevel];
          return (
            <button
              key={l.key}
              className={level === l.key ? 'primary small' : 'secondary small'}
              style={{ opacity: available ? 1 : 0.45 }}
              onClick={() => setLevel(l.key)}
              title={available ? '' : hintFor(l.key)}
            >
              {available ? '' : '🔒 '}{l.label}
            </button>
          );
        })}
        <div className="spacer" />
        <button className="secondary small" onClick={() => model.reload()}>새로고침</button>
      </div>

      <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
        {current ? (
          <>
            {stageLabels[current.stage] ?? current.stage}{' '}
            {current.confirmed ? '확정 산출물' : (
              <span style={{ color: 'var(--yellow)' }}>초안(미확정)</span>
            )}의 C4 블록
            {current.title && <> · {current.title}</>}
          </>
        ) : (
          hintFor(level)
        )}
      </p>

      <Async state={model}>
        {() => (level === 'code'
          ? <CodeLevel projectId={projectId} />
          : current && <LevelCanvas level={level} data={current} />)}
      </Async>
    </>
  );
}

// --- 레벨 하나의 캔버스 ---

interface CanvasProps {
  // React Flow를 다시 마운트할 key에만 쓰인다 — code 레벨도 같은 캔버스를 쓴다.
  level: Level;
  data: C4Level;
}

function LevelCanvas({ level, data }: CanvasProps) {
  const { nodes, edges } = useMemo(() => {
    const shown = data.elements;
    const aliases = new Set(shown.map((e) => e.alias));
    const relations = data.relations.filter(
      (r) => aliases.has(r.source) && aliases.has(r.target),
    );
    // code 레벨은 선이 없는 박스가 대부분이라 dagre가 전부 한 줄로 늘어놓는다 —
    // 줄바꿈 배치로 접는다(c4layout.layoutFlow의 주석 참고).
    const placed = level === 'code' ? layoutFlow(shown) : layoutElements(shown, relations);
    const boxes = new Map(placed.map((b) => [b.alias, b]));
    const nodeList: Node[] = shown.map((element) => {
      const box = boxes.get(element.alias);
      return {
        id: element.alias,
        type: element.base === 'boundary' ? 'c4group' : 'c4node',
        position: { x: box?.x ?? 0, y: box?.y ?? 0 },
        // style이 아니라 노드의 width·height로 준다 — 미니맵은 style을 읽지 않아서
        // style로만 크기를 주면 미니맵에 노드가 하나도 그려지지 않는다.
        width: box?.w,
        height: box?.h,
        data: { element },
        draggable: false,
        selectable: element.base !== 'boundary',
        ...(element.parent && aliases.has(element.parent)
          ? { parentId: element.parent, extent: 'parent' as const }
          : {}),
      };
    });
    return {
      // 경계(부모)가 자식보다 먼저 와야 React Flow가 감싸 그린다.
      nodes: nodeList.sort((a, b) => Number(!!a.parentId) - Number(!!b.parentId)),
      edges: relations.map((r, i): Edge => ({
        id: `rel-${i}`,
        source: r.source,
        target: r.target,
        className: 'c4-edge',
        label: [r.label, r.technology && `[${r.technology}]`].filter(Boolean).join(' '),
        markerEnd: { type: MarkerType.ArrowClosed },
        markerStart: r.bidirectional ? { type: MarkerType.ArrowClosed } : undefined,
      })),
    };
  }, [data, level]);

  if (nodes.length === 0) {
    return <p className="mutedtext" style={{ fontSize: 12 }}>이 레벨에 그릴 요소가 없습니다.</p>;
  }
  return (
    <div className="c4-wrap">
      <ReactFlow
        // 레벨이 바뀌면 다시 마운트해 화면에 맞춰 다시 잡는다(fitView).
        key={level}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        colorMode="dark"
        fitView
        minZoom={0.2}
        maxZoom={2}
        nodesConnectable={false}
      >
        <Background gap={20} />
        {/* 노드 색은 CSS(.c4-node)에 있어 미니맵이 읽지 못한다 — 여기서 직접 준다. */}
        <MiniMap pannable zoomable nodeColor="rgba(74, 108, 247, 0.55)" nodeStrokeWidth={0} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

// --- 노드 ---

type C4NodeData = { element: C4Element };

function C4NodeView({ data }: NodeProps) {
  const { element } = data as C4NodeData;
  return (
    <div
      className={[
        'c4-node', `c4-${element.base}`,
        element.external ? 'c4-ext' : '',
      ].filter(Boolean).join(' ')}
    >
      <Handle type="target" position={Position.Top} className="c4-handle" />
      {/* 멤버 목록은 박스에 몇 개만 들어간다("+N") — 전체는 hover로 본다. */}
      <div className="c4-node-kind" title={`${element.label}
${element.description}`}>
        <span className="mono">{element.kind}</span>
      </div>
      <div className="c4-node-label">{element.label}</div>
      {element.technology && <div className="c4-node-tech">[{element.technology}]</div>}
      {element.description && <div className="c4-node-desc">{element.description}</div>}
      <Handle type="source" position={Position.Bottom} className="c4-handle" />
    </div>
  );
}

function C4GroupView({ data }: NodeProps) {
  const { element } = data as C4NodeData;
  return (
    <div className="c4-group">
      <Handle type="target" position={Position.Top} className="c4-handle" />
      <div className="c4-group-label" title={element.label}>
        {element.label}
        <span className="mutedtext"> · {element.kind}</span>
      </div>
      <Handle type="source" position={Position.Bottom} className="c4-handle" />
    </div>
  );
}

// React Flow는 이 맵이 렌더마다 새로 만들어지면 경고하고 노드를 다시 만든다 — 모듈 수준에 둔다.
const NODE_TYPES = { c4node: C4NodeView, c4group: C4GroupView };

// --- code 레벨 — 문서가 아니라 리포가 원천이다(services/codemap의 정적 파싱) ---

function CodeLevel({ projectId }: { projectId: number }) {
  const codemap = useApi(() => api.projectCodemap(projectId), [projectId]);
  return (
    <Async state={codemap}>
      {(data) => {
        // 컴포넌트별로 나누지 않고 **리포 전체**를 그린다. 나누면 $link이 적힌 컴포넌트만
        // 볼 수 있어서, 문서에 경로를 안 적었다는 이유로 코드가 안 보였다.
        const files = data.files;
        if (files.length === 0) {
          return (
            <p className="mutedtext" style={{ fontSize: 12 }}>
              구조를 파싱할 코드 파일이 없습니다 (Python·JS/TS 대상).
            </p>
          );
        }
        return (
          <>
            <div className="mutedtext" style={{ fontSize: 12, marginBottom: 6 }}>
              리포 전체 · 파일 {files.length}개 · 선언{' '}
              {files.reduce((n, f) => n + f.children.length, 0)}개 — 파일이 경계,
              클래스·함수가 노드, 선은 상속입니다
            </div>
            {/* 다른 레벨과 **같은 캔버스**를 쓴다 — 레벨마다 다른 엔진을 두면 확대·축소
                동작이 레벨별로 미묘하게 달라진다. */}
            <LevelCanvas level="code" data={codeLevel(files, '리포 전체')} />
            {/* 그림은 무엇이 있고 무엇을 상속하는지를 말한다. 메서드 시그니처와 설명은
                박스에 담을 수 없으니 접어서 함께 둔다 — 정보를 잃지 않는다. */}
            <details style={{ marginTop: 8 }}>
              <summary style={{ fontSize: 12, cursor: 'pointer' }}>
                파일별 상세 (시그니처·설명)
              </summary>
              <CodeStructure files={files} />
            </details>
          </>
        );
      }}
    </Async>
  );
}
