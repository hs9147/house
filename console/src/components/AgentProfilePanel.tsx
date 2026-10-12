import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import type { AgentProfileState } from '../lib/types';
import Async from './Async';

/**
 * 에이전트 용도 — 스마트워크가 이 앱을 띄울지 고르는 근거(services/agentreview).
 *
 * 이름만 보고 고르게 두었더니 이름이 스치는 요청마다 같은 앱이 열렸다. LLM이 **배포된 커밋**의
 * README·문서·진입 파일을 읽고 목적·하는 일·하지 않는 일을 남긴다. 무엇을 읽었는지(근거
 * 파일)를 함께 보여 준다 — 요약이 빗나갔으면 대개 README가 비었거나 낡은 것이다.
 */
export default function AgentProfilePanel({ projectId }: { projectId: number }) {
  const state = useApi(() => api.agentProfile(projectId), [projectId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const review = async () => {
    setBusy(true);
    setError('');
    try {
      await api.reviewAgentProfile(projectId);
      state.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>에이전트 용도</h2>
        <span className="spacer" />
        {state.data?.release_sha && (
          <button className="small secondary" disabled={busy} onClick={() => void review()}>
            {busy ? '검토 중...' : state.data.profile ? '다시 검토' : '지금 검토'}
          </button>
        )}
      </div>
      <Async state={state}>
        {(s: AgentProfileState) => <Body s={s} />}
      </Async>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Body({ s }: { s: AgentProfileState }) {
  if (!s.release_sha) {
    return (
      <p className="mutedtext">
        release로 떠 있지 않아 에이전트가 아닙니다 — 운영(release)으로 배포하면 스마트워크가 이 앱을
        띄울 수 있고, 그때 용도를 검토합니다.
      </p>
    );
  }
  const p = s.profile;
  if (!p) {
    return (
      <p className="mutedtext">
        아직 검토하지 않았습니다 — 스마트워크가 에이전트 목록을 만들 때 뒤에서 검토하고, 그 전까지는
        이 앱을 이름만으로 고르지 않습니다.
      </p>
    );
  }
  return (
    <>
      {p.error && (
        <p className="error">
          검토 실패: {p.error}{p.purpose ? ' — 아래는 이전 검토 결과입니다.' : ''}
        </p>
      )}
      {p.purpose && (
        <>
          <p style={{ marginTop: 0 }}>{p.purpose}</p>
          <List label="하는 일" items={p.does} />
          <List label="하지 않는 일" items={p.does_not} />
          <List label="사람이 넣는 것" items={p.inputs} />
        </>
      )}
      <p className="mutedtext" style={{ fontSize: 12, marginBottom: 0 }}>
        커밋 <span className="mono">{p.git_sha.slice(0, 12)}</span> · {new Date(p.reviewed_at).toLocaleString()}
        {p.provider && ` · ${p.provider} / ${p.model}`}
        {p.sources.length > 0 && <> · 읽은 파일: <span className="mono">{p.sources.join(', ')}</span></>}
        {s.stale && ' · 배포가 바뀌어 다시 검토할 차례입니다'}
      </p>
    </>
  );
}

function List({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div style={{ fontSize: 13, marginBottom: 6 }}>
      <span className="mutedtext">{label}: </span>
      {items.join(' · ')}
    </div>
  );
}
