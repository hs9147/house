import { useCallback, useMemo, useState } from 'react';
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import Async from './Async';
import CodeStructure from './CodeStructure';
import { api } from '../lib/api';
import { focusElements, layoutElements } from '../lib/c4layout';
import { useApi } from '../lib/hooks';
import type { C4Element, C4Level } from '../lib/types';

// C4 모델 시각화 — 확정 산출물에 실린 mermaid C4 블록이 원천이다(app/services/c4.py).
// 지도처럼 확대·축소한다: 노드를 클릭하면 한 단계 안으로(context → container →
// component → code), '상위 레벨'로 한 단계 밖으로 나간다. 뷰포트 자체의 확대·축소는
// React Flow의 Controls(＋/－/맞춤)와 휠·드래그가 처리한다.

type DocLevel = 'context' | 'container' | 'component';
type Level = DocLevel | 'code';

const LEVELS: { key: Level; label: string; locked: string }[] = [
  // locked 문구는 "확정을 안 했다"와 "확정했지만 문서에 블록이 없다"를 함께 덮는다 —
  // 두 경우를 가려내려면 단계별 확정 상태를 또 내려받아야 하는데, 사용자가 할 일은
  // 어느 쪽이든 같다(그 단계 산출물에 해당 블록을 실어 확정한다).
  {
    key: 'context',
    label: 'System Context',
    locked: '① 기획서 확정 산출물의 C4Context 블록에서 사용자·외부 환경을 그립니다 — 아직 그 블록이 없습니다.',
  },
  {
    key: 'container',
    label: 'Container',
    locked: '② 아키텍처 설계 확정 산출물의 C4Container 블록에서 그립니다 (③ 솔루션 구성에서 구체화) — 아직 그 블록이 없습니다.',
  },
  {
    key: 'component',
    label: 'Component',
    locked: '② 아키텍처 설계 확정 산출물의 C4Component 블록에서 그립니다 (③ 솔루션 구성에서 구체화) — 아직 그 블록이 없습니다.',
  },
  {
    key: 'code',
    label: 'Code',
    locked: 'Component 레벨에서 컴포넌트를 클릭하면 그 코드 구조가 열립니다.',
  },
];

interface Props {
  sessionId: number;
  projectId: number;
  // 확정 상태가 바뀌면 다시 불러온다(확정 커밋 sha를 이어 붙인 값).
  reloadKey: string;
  // stage → 기획 화면과 같은 단계 표기('① 기획서'). 그림이 어느 단계에서 왔는지 밝힌다.
  stageLabels: Record<string, string>;
}

export default function C4Diagram({ sessionId, projectId, reloadKey, stageLabels }: Props) {
  const model = useApi(() => api.planC4(sessionId), [sessionId, reloadKey]);
  const [level, setLevel] = useState<Level>('context');
  // 특정 컨테이너 안으로 들어간 상태 — 그 경계에 속한 요소만 그린다.
  const [focus, setFocus] = useState<string | null>(null);
  const [codeTarget, setCodeTarget] = useState<C4Element | null>(null);

  const levels = model.data?.levels ?? {};
  const current: C4Level | undefined = level === 'code' ? undefined : levels[level];

  const zoomOut = () => {
    const back: Record<Level, Level> = {
      code: 'component', component: 'container', container: 'context', context: 'context',
    };
    setLevel(back[level]);
    setFocus(null);
    setCodeTarget(null);
  };

  const selectLevel = (next: Level) => {
    setLevel(next);
    setFocus(null);
    if (next !== 'code') setCodeTarget(null);
  };

  // 클릭해서 한 단계 안으로 들어갈 수 있는 노드 — 판정과 실제 이동(onDrill)을 한 자리에
  // 둔다. 들어갈 곳이 없으면(다음 레벨 그림이 없거나 코드가 없으면) 클릭도 막는다.
  const drillable = useCallback((element: C4Element): boolean => {
    if (level === 'context') return element.base === 'system' && !element.external && !!levels.container;
    if (level === 'container') return element.base === 'container' && !!levels.component;
    if (level === 'component') return element.base === 'component' && element.paths.length > 0;
    return false;
  }, [level, levels]);

  return (
    <>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {LEVELS.map((l) => {
          const available = l.key === 'code' ? codeTarget !== null : !!levels[l.key as DocLevel];
          return (
            <button
              key={l.key}
              className={level === l.key ? 'primary small' : 'secondary small'}
              style={{ opacity: available ? 1 : 0.45 }}
              onClick={() => selectLevel(l.key)}
              title={available ? '' : l.locked}
            >
              {available ? '' : '🔒 '}{l.label}
            </button>
          );
        })}
        <div className="spacer" />
        {(level !== 'context' || focus) && (
          <button className="secondary small" onClick={zoomOut}>⤴ 상위 레벨</button>
        )}
        <button className="secondary small" onClick={() => model.reload()}>새로고침</button>
      </div>

      <p className="mutedtext" style={{ fontSize: 12, marginTop: 6 }}>
        {current ? (
          <>
            {stageLabels[current.stage] ?? current.stage} 확정 산출물의 C4 블록
            {current.title && <> · {current.title}</>}
            {focus && <> · <span className="mono">{focus}</span> 안쪽만</>}
            {' '}· 노드를 클릭하면 한 단계 안으로 들어갑니다
          </>
        ) : codeTarget ? (
          <>리포 코드 구조 — <b>{codeTarget.label}</b> 컴포넌트로 구현된 파일</>
        ) : (
          LEVELS.find((l) => l.key === level)?.locked
        )}
      </p>

      <Async state={model}>
        {() => (level === 'code'
          ? <CodeLevel projectId={projectId} component={codeTarget} />
          : current && (
            <LevelCanvas
              level={level}
              data={current}
              focus={focus}
              drillable={drillable}
              onDrill={(element) => {
                if (level === 'context' && levels.container) {
                  setLevel('container');
                  setFocus(null);
                } else if (level === 'container' && levels.component) {
                  setLevel('component');
                  setFocus(element.alias); // 같은 alias의 Container_Boundary로 좁힌다
                } else if (level === 'component') {
                  setLevel('code');
                  setCodeTarget(element);
                }
              }}
            />
          ))}
      </Async>
    </>
  );
}

// --- 레벨 하나의 캔버스 ---

interface CanvasProps {
  level: DocLevel;
  data: C4Level;
  focus: string | null;
  drillable: (element: C4Element) => boolean;
  onDrill: (element: C4Element) => void;
}

function LevelCanvas({ level, data, focus, drillable, onDrill }: CanvasProps) {
  const { nodes, edges, elements } = useMemo(() => {
    // 좁혀 본 결과가 비면(그 경계가 이 레벨에 없으면) 전체를 그린다.
    const narrowed = focus ? focusElements(data.elements, focus) : data.elements;
    const shown = narrowed.length > 0 ? narrowed : data.elements;
    const aliases = new Set(shown.map((e) => e.alias));
    const relations = data.relations.filter(
      (r) => aliases.has(r.source) && aliases.has(r.target),
    );
    const boxes = new Map(layoutElements(shown, relations).map((b) => [b.alias, b]));
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
        data: { element, drillable: drillable(element) },
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
      elements: new Map(shown.map((e) => [e.alias, e])),
    };
  }, [level, data, focus, drillable]);

  if (nodes.length === 0) {
    return <p className="mutedtext" style={{ fontSize: 12 }}>이 레벨에 그릴 요소가 없습니다.</p>;
  }
  return (
    <div className="c4-wrap">
      <ReactFlow
        // 레벨·좁힘이 바뀌면 다시 마운트해 화면에 맞춰 다시 잡는다(fitView).
        key={`${level}:${focus ?? ''}`}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        colorMode="dark"
        fitView
        minZoom={0.2}
        maxZoom={2}
        nodesConnectable={false}
        onNodeClick={(_event, node) => {
          const element = elements.get(node.id);
          if (element && (node.data as C4NodeData).drillable) onDrill(element);
        }}
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

type C4NodeData = { element: C4Element; drillable: boolean };

function C4NodeView({ data }: NodeProps) {
  const { element, drillable } = data as C4NodeData;
  return (
    <div
      className={[
        'c4-node', `c4-${element.base}`,
        element.external ? 'c4-ext' : '',
        drillable ? 'c4-drillable' : '',
      ].filter(Boolean).join(' ')}
    >
      <Handle type="target" position={Position.Top} className="c4-handle" />
      <div className="c4-node-kind">
        <span className="mono">{element.kind}</span>
        {drillable && <span className="c4-drill-mark">확대 ▸</span>}
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
      <div className="c4-group-label">
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

function CodeLevel({ projectId, component }: { projectId: number; component: C4Element | null }) {
  const codemap = useApi(() => api.projectCodemap(projectId), [projectId]);
  if (component === null) {
    return (
      <p className="mutedtext" style={{ fontSize: 12 }}>
        Component 레벨에서 컴포넌트를 클릭하면 그 코드 구조가 열립니다.
      </p>
    );
  }
  return (
    <Async state={codemap}>
      {(data) => {
        const paths = new Set(component.paths);
        const files = data.files.filter((f) => paths.has(f.path));
        return (
          <>
            <div style={{ fontSize: 12, marginBottom: 6 }}>
              <b>{component.label}</b>
              {component.technology && <span className="mutedtext"> · {component.technology}</span>}
              {' '}— 구현 파일 {component.paths.length}개
              {component.link && (
                <span className="mutedtext"> · $link <span className="mono">{component.link}</span></span>
              )}
            </div>
            {files.length === 0 ? (
              <p className="mutedtext" style={{ fontSize: 12 }}>
                구조를 파싱할 코드 파일이 없습니다 (Python·JS/TS 대상).
                {' '}대상 경로: <span className="mono">{component.paths.join(', ') || '—'}</span>
              </p>
            ) : (
              <CodeStructure files={files} />
            )}
          </>
        );
      }}
    </Async>
  );
}
