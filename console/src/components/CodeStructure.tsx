import type { CodeMapFile, CodeMapNode } from '../lib/types';

// 정적 파싱으로 만든 파일→클래스/함수 계층 트리를 확대/축소로 탐색(요청 1).
// 네이티브 <details>/<summary>로 확장 상태를 관리해 별도 상태 코드가 필요 없다.

const KIND_LABEL: Record<CodeMapNode['kind'], string> = {
  class: 'class',
  function: 'fn',
  method: 'fn',
};

function NodeItem({ node }: { node: CodeMapNode }) {
  const label = (
    <>
      <span className="status dim" style={{ fontSize: 10 }}>{KIND_LABEL[node.kind]}</span>{' '}
      <span className="mono">{node.signature}</span>
      {node.doc && <span className="mutedtext" style={{ fontSize: 12 }}> — {node.doc}</span>}
    </>
  );
  if (node.children.length === 0) {
    return <div className="codemap-leaf">{label}</div>;
  }
  return (
    <details className="codemap-node">
      <summary>{label}</summary>
      <div className="codemap-children">
        {node.children.map((c) => (
          <NodeItem key={`${c.name}-${c.lineno}`} node={c} />
        ))}
      </div>
    </details>
  );
}

export default function CodeStructure({ files }: { files: CodeMapFile[] }) {
  if (files.length === 0) {
    return <p className="mutedtext">파싱 가능한 코드 파일이 없습니다 (Python·JS/TS 대상).</p>;
  }
  return (
    <div className="codemap">
      {files.map((f) => (
        <details key={f.path} className="codemap-file">
          <summary>
            <span className="mono">{f.path}</span>
            <span className="status info" style={{ fontSize: 10, marginLeft: 8 }}>{f.lang}</span>
            {f.summary && (
              <span className="mutedtext" style={{ fontSize: 12 }}> — {f.summary}</span>
            )}
          </summary>
          <div className="codemap-children">
            {f.children.length === 0 ? (
              <div className="mutedtext" style={{ fontSize: 12 }}>최상위 클래스·함수 없음</div>
            ) : (
              f.children.map((c) => <NodeItem key={`${c.name}-${c.lineno}`} node={c} />)
            )}
          </div>
        </details>
      ))}
    </div>
  );
}
