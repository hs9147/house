import { useState } from 'react';
import Modal from './Modal';
import { api } from '../lib/api';
import { copyText } from '../lib/clipboard';
import type { ProjectOut } from '../lib/types';

/**
 * "에이전트 접속" — 외주 개발 에이전트가 이 프로젝트에 붙을 자격과 주소를 만들어 준다.
 *
 * 쓰이는 곳은 둘이다: MCP(무엇을 만들지 읽는다)와 게이트웨이(/proxy·/a2a로 LLM·모듈·A2A에
 * 닿는다). 주소를 손으로 짜맞추다 막히는 일이 있어서(직접 인증 불가로 보고됨) 둘을 함께 준다.
 *
 * 외주 에이전트에게는 API 키가 없다. 관리자가 키를 나눠 주는 것도 답이 아니라(누구에게
 * 나갔는지·언제 회수하는지가 남지 않는다), **로그인한 사람이 자기 몫을 직접 발급한다.**
 * 접근 권한은 그 사람의 조직으로 판정되므로(서버의 require_project_mcp_access) 이 버튼이
 * 성공했다는 것 자체가 "이 사람은 이 프로젝트를 쓸 수 있다"는 뜻이다.
 *
 * 원문은 **한 번만** 나온다(서버가 해시만 저장한다). 그래서 발급 즉시 붙여 넣을 수 있는
 * 설정 전체를 보여 준다 — 주소와 토큰을 따로 찾아 조립하게 두면 거기서 틀린다.
 */
export default function McpAccessButton({ project }: { project: ProjectOut }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [issued, setIssued] = useState<
    { token: string; url: string; gateway_base_url: string; ttl_days: number } | null>(null);
  const [copied, setCopied] = useState(false);

  const issue = async () => {
    setBusy(true);
    setError('');
    try {
      setIssued(await api.createMcpToken(`${project.name} (콘솔에서 발급)`, project.name));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const config = issued
    ? JSON.stringify({
      mcpServers: {
        [`paas-${project.name}`]: {
          type: 'http',
          url: issued.url,
          headers: { Authorization: `Bearer ${issued.token}` },
        },
      },
    }, null, 2)
    : '';

  // 게이트웨이는 같은 토큰을 x-api-key(또는 Bearer)로 받는다 — 검증 스크립트가 쓰는 모양.
  const gatewayEnv = issued
    ? `PAAS_GATEWAY_BASE_URL=${issued.gateway_base_url}
PAAS_API_KEY=${issued.token}`
    : '';

  // 평문 http에서는 navigator.clipboard가 없다 — 되는 길로 복사하고, **실제 결과**를
  // 알린다(예전에는 실패해도 "복사됨"이라고 말했다). 팝업으로 세우는 이유: 버튼 글자만
  // 바뀌면 눌렀는지 복사됐는지 구분되지 않는다.
  const copyAll = async () => {
    const text = gatewayEnv ? `${config}

${gatewayEnv}
` : config;
    if (await copyText(text)) {
      setCopied(true);
      window.alert('설정을 복사했습니다. 개발 도구의 MCP 설정에 붙여 넣으세요.');
    } else {
      window.alert('복사하지 못했습니다 — 위 내용을 직접 선택해 복사하세요.');
    }
  };

  const close = () => {
    setOpen(false);
    setIssued(null);
    setError('');
    setCopied(false);
  };

  return (
    <>
      <button
        className="small secondary"
        title="외주 개발 에이전트가 쓸 MCP·게이트웨이 접속 설정을 발급합니다"
        // 프로젝트 목록은 행 클릭이 상세 이동이라 전파를 막아야 한다(삭제 버튼과 동일).
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}
      >
        에이전트 접속
      </button>
      {open && (
        <div onClick={(e) => e.stopPropagation()}>
          <Modal title={`에이전트 접속 정보 — ${project.name}`} onClose={close}>
            {!issued ? (
              <>
                <p style={{ fontSize: 13 }}>
                  외주 개발 에이전트가 이 프로젝트의 작업 지시·산출물·코드를 읽고
                  게이트웨이로 LLM·모듈을 부를 수 있도록 <b>내 이름으로</b> 토큰을
                  발급합니다. 내가 접근할 수 있는 프로젝트만 열립니다.
                </p>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  이 토큰은 MCP와 게이트웨이에서만 통합니다 — 배포·터미널·계정 관리에는
                  쓸 수 없습니다. 원문은 발급 직후 <b>한 번만</b> 보이므로 그 자리에서
                  복사하세요.
                </p>
                {error && <p className="error">{error}</p>}
                <div className="row">
                  <button onClick={issue} disabled={busy}>
                    {busy ? '발급 중...' : '토큰 발급'}
                  </button>
                  <button className="secondary" onClick={close}>취소</button>
                </div>
              </>
            ) : (
              <>
                <p style={{ fontSize: 13, margin: 0 }}>
                  아래 설정을 개발 도구의 MCP 설정에 넣으세요 ({issued.ttl_days}일 후 만료).
                </p>
                <p className="mutedtext" style={{ fontSize: 12 }}>
                  ⚠️ 이 화면을 닫으면 토큰을 다시 볼 수 없습니다 — 잃으면 새로 발급하고 쓰던
                  것을 폐기하세요.
                </p>
                <pre
                  className="mono"
                  style={{
                    background: '#090d16', padding: 12, borderRadius: 8, fontSize: 12,
                    maxHeight: 220, overflow: 'auto', whiteSpace: 'pre',
                  }}
                >
                  {config}
                </pre>
                {/* 같은 토큰으로 게이트웨이(/proxy·/a2a)도 부른다 — 에이전트는 MCP로 무엇을
                    만들지 읽고 게이트웨이로 자원에 닿는다. 주소를 손으로 짜맞추다 막히는
                    일이 있어서(직접 인증 불가) 환경변수 형태로 함께 준다. */}
                {issued.gateway_base_url && (
                  <>
                    <p style={{ fontSize: 13, margin: '10px 0 4px' }}>
                      게이트웨이(LLM·모듈·A2A)를 부를 때는 이 환경변수를 씁니다.
                    </p>
                    <pre
                      className="mono"
                      style={{
                        background: '#090d16', padding: 12, borderRadius: 8, fontSize: 12,
                        overflow: 'auto', whiteSpace: 'pre',
                      }}
                    >
                      {gatewayEnv}
                    </pre>
                  </>
                )}
                {!issued.url && (
                  <p className="error" style={{ fontSize: 12 }}>
                    서버의 공개 주소가 설정돼 있지 않아 MCP 주소를 만들 수 없었습니다
                    (PAAS_PLATFORM_PUBLIC_URL).
                  </p>
                )}
                <div className="row">
                  <button onClick={copyAll}>
                    {copied ? '복사됨' : '설정 복사'}
                  </button>
                  <button className="secondary" onClick={close}>닫기</button>
                </div>
              </>
            )}
          </Modal>
        </div>
      )}
    </>
  );
}
