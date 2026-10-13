import { useState } from 'react';
import type { ProjectOut } from '../lib/types';
import McpAccessModal from './McpAccessModal';

/**
 * "에이전트 접속" — 과제 행에서 여는 MCP 접속(McpAccessModal).
 *
 * 토큰은 과제가 아니라 **사람에게** 속한다(서버는 이메일로만 묶는다 — api/mcp_tokens.py). 여기서
 * 여는 것은 같은 창이고, 이 과제의 기획 MCP를 미리 골라 둘 뿐이다. 사이드바의 「MCP 접속」과 같다.
 */
export default function McpAccessButton({ project }: { project: ProjectOut }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="small secondary"
        title="내 토큰으로 이 과제의 기획 MCP와 플랫폼 MCP 서버를 등록할 설정을 만듭니다"
        // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(삭제 버튼과 동일).
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}
      >
        에이전트 접속
      </button>
      {open && (
        <div onClick={(e) => e.stopPropagation()}>
          <McpAccessModal initialProject={project.name} onClose={() => setOpen(false)} />
        </div>
      )}
    </>
  );
}
