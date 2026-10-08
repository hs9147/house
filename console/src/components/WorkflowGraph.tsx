import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Background, Handle, MarkerType, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { NODE_H, NODE_W, layoutWrapped } from '../lib/graphlayout';
import type { WorkflowSpec } from '../lib/types';
import { VIZ } from '../lib/viz';

/**
 * 워크플로 그림 — **스펙의 파생물**이다. 원천은 JSON 스펙이고 여기서는 배치해 보여 준다
 * (C4는 반대로 문서가 원천이다 — 여기는 실행에 필요한 정보가 스펙에만 있다).
 *
 * 배치는 계층(dagre LR)이다. 워크플로는 흐름이고, 흐름은 위치로 읽혀야 한다 — 물리
 * 시뮬레이션은 볼 때마다 모양이 달라져 같은 흐름을 두 번 비교할 수 없다. 끌어 옮기기·확대는
 * React Flow가 한다.
 *
 * 종류는 **색과 글자 둘 다**로 말한다(색만으로 뜻을 나르지 않는다). 사람 단계는 상태색
 * (warning)으로 따로 세운다 — 실행이 거기서 멈추므로, 그림에서 가장 먼저 찾을 것이 그것이다.
 */
const COLOR: Record<string, string> = {
  'storage.list': VIZ.ordinal[2],
  'docs.search': VIZ.ordinal[2],
  'doc.read': VIZ.ordinal[2],
  'storage.write': VIZ.ordinal[4],
  llm: VIZ.series,
  'mcp.tool': VIZ.ordinal[3],
  branch: VIZ.ordinal[1],
  human: VIZ.status.warning,
};

const LABEL: Record<string, string> = {
  'storage.list': '파일 목록',
  'docs.search': '문서 검색',
  'doc.read': '문서 읽기',
  'storage.write': '파일 저장',
  llm: 'LLM',
  'mcp.tool': '모듈 도구',
  branch: '분기',
  human: '사람 작업',
};

/** 노드가 실제로 무엇을 하는지 — 종류마다 중요한 항목 하나를 집어 보여 준다. */
function detailOf(node: Record<string, unknown>): string {
  const type = String(node.type);
  if (type === 'branch') {
    const when = (node.when ?? {}) as { kind?: string; text?: string; question?: string };
    return when.kind === 'contains' ? `"${when.text}" 포함?`
      : when.kind === 'llm' ? String(when.question ?? '')
        : when.kind === 'empty' ? '입력이 비었나?' : '';
  }
  if (type === 'human') return String(node.role ? `${node.title} · ${node.role}` : node.title ?? '');
  if (type === 'llm') return String(node.prompt ?? '');
  if (type === 'mcp.tool') return `${node.module ?? ''}.${node.tool ?? ''}`;
  if (type === 'docs.search') return `${node.store ?? ''}: ${node.query ?? ''}`;
  if (type === 'storage.write') return `${node.store ?? ''}:${node.path ?? ''}`;
  return String(node.store ?? node.path ?? '');
}

interface Props {
  spec: WorkflowSpec;
  height?: number;
  /** 실행 중이라면 단계별 상태 — 그림이 진행을 보여 준다(id → 상태). */
  status?: Record<string, string>;
  selected?: string;
  onSelect?: (id: string) => void;
}

export default function WorkflowGraph({ spec, height = 520, status, selected, onSelect }: Props) {
  // 배치에 쓸 폭은 **실제 패널 폭**이다 — 짐작해 두면 넓은 화면에서 가운데가 비고 좁은
  // 화면에서는 넘친다. 창 크기가 바뀌면 다시 접는다.
  const box = useRef<HTMLDivElement>(null);
  const [room, setRoom] = useState(0);
  useEffect(() => {
    const target = box.current;
    if (!target) return;
    // ResizeObserver를 쓰는 이유: 창 크기만 보면 **대화창을 접었다 펼 때** 다시 재지 않는다
    // (그때 본문 여백이 바뀌어 그림이 쓸 수 있는 폭이 달라진다). 자기 폭을 직접 본다.
    const observer = new ResizeObserver(() => setRoom(target.clientWidth));
    observer.observe(target);
    setRoom(target.clientWidth);
    return () => observer.disconnect();
  }, []);

  const laid = useMemo(() => {
    const list = (spec.nodes ?? []).map((n) => ({ id: String(n.id) }));
    // 글처럼 왼쪽에서 오른쪽으로 놓고 폭을 넘으면 접는다 — 가로를 다 쓰고, 길어지는 쪽은
    // 아래다(세로 한 줄은 폭의 6분의 1만 쓰고, 좌→우 한 줄은 6,000px이 된다).
    return layoutWrapped(list, (spec.edges ?? []).map(
      (e) => ({ source: String(e.from), target: String(e.to) })),
      Math.max(room - 24, NODE_W));
  }, [spec, room]);

  const nodes: Node[] = useMemo(() => {
    const placed = laid.positions;
    return (spec.nodes ?? []).map((n) => ({
      id: String(n.id),
      type: 'wf',
      position: placed.get(String(n.id)) ?? { x: 0, y: 0 },
      data: {
        ...n,
        detail: detailOf(n as unknown as Record<string, unknown>),
        runStatus: status?.[String(n.id)] ?? '',
        isSelected: selected === String(n.id),
      } as unknown as Record<string, unknown>,
    }));
  }, [spec, laid, status, selected]);

  // 캔버스를 **내용 크기만큼** 잡는다 — 그러면 브라우저가 진짜 스크롤바를 준다.
  // fitView로 화면에 맞추면 단계가 늘어날수록 글자가 작아지고, 확대·끌기를 아는 사람만
  // 읽을 수 있다. 세로로 길어지는 것은 스크롤이 해결할 문제다.
  const canvas = { width: laid.width + 24, height: laid.height + 24 };

  const edges: Edge[] = useMemo(() => (spec.edges ?? []).map((e, i) => ({
    id: `${e.from}->${e.to}-${i}`,
    source: String(e.from),
    target: String(e.to),
    label: e.case ?? '',
    labelStyle: { fill: e.case === '거짓' ? VIZ.muted : VIZ.ink, fontSize: 11 },
    labelBgStyle: { fill: VIZ.surface },
    markerEnd: { type: MarkerType.ArrowClosed, color: VIZ.axis },
    style: { stroke: VIZ.axis, strokeWidth: 2 },
  })), [spec]);

  if ((spec.nodes ?? []).length === 0) {
    return (
      <p className="mutedtext" style={{ fontSize: 12 }}>
        아직 단계가 없습니다 — 왼쪽에서 하고 싶은 일을 말로 적으면 LLM이 초안을 만듭니다.
      </p>
    );
  }
  return (
    <div
      ref={box}
      style={{
        maxHeight: height, overflow: 'auto',
        border: '1px solid var(--border-soft)', borderRadius: 8,
      }}
    >
      <div style={{ width: canvas.width, height: canvas.height }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodeClick={(_, node) => onSelect?.(node.id)}
          // 휠은 **스크롤**이다(확대가 아니다). preventScrolling=false로 휠 이벤트를
          // 컨테이너에 흘려 보내고, 끌기도 캔버스가 먹지 않게 한다 — 노드는 그대로 끌 수 있다.
          zoomOnScroll={false}
          zoomOnDoubleClick={false}
          panOnScroll={false}
          panOnDrag={false}
          preventScrolling={false}
          nodesConnectable={false}
          proOptions={{ hideAttribution: true }}
        >
          <Background color={VIZ.grid} gap={18} />
        </ReactFlow>
      </div>
    </div>
  );
}

const RUN_MARK: Record<string, string> = {
  ok: '✓', failed: '✕', waiting: '⏸', skipped: '—', rejected: '✕',
};

function WorkflowNodeBox({ data }: NodeProps) {
  const node = data as unknown as {
    id: string; type: string; detail: string; runStatus: string; isSelected: boolean;
  };
  const color = COLOR[node.type] ?? VIZ.series;
  const mark = RUN_MARK[node.runStatus] ?? '';
  return (
    <div
      style={{
        width: NODE_W, height: NODE_H, boxSizing: 'border-box',
        borderRadius: 6, padding: '6px 8px', background: VIZ.surface, overflow: 'hidden',
        border: `2px solid ${node.isSelected ? VIZ.ink : color}`,
        opacity: node.runStatus === 'skipped' ? 0.5 : 1,
        display: 'flex', flexDirection: 'column', justifyContent: 'center',
      }}
      title={`${LABEL[node.type] ?? node.type}: ${node.detail}`}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontSize: 11, lineHeight: 1.3, color }}>
        {LABEL[node.type] ?? node.type}
        {mark && <span style={{ marginLeft: 6, color: VIZ.ink }}>{mark}</span>}
      </div>
      <div style={{
        fontSize: 12, lineHeight: 1.3, color: VIZ.ink, fontWeight: 600,
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {node.id}
      </div>
      <div style={{
        // 설명은 두 줄까지 — 한 줄로 자르면 "무엇을 하는 단계"가 거의 안 보인다.
        fontSize: 11, lineHeight: 1.3, color: VIZ.muted,
        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
        overflow: 'hidden', overflowWrap: 'anywhere',
      }}>
        {node.detail}
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const NODE_TYPES = { wf: WorkflowNodeBox };
